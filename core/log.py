#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构化日志 —— 用户要的 debug 入口。

两条设计约束
------------
1. **必须能定位问题**：每条日志带事件类型、平台、user_id、耗时、结果、失败分类。
   失败分类是**固定枚举**（不是自由文本），这样才能按类统计"到底哪类错最多"。

2. **绝不能泄漏凭据**：日志会被贴出来求助、会被翻。所以 Cookie / Set-Cookie /
   password / _token 这类字段**一律掩码**，`users` 的 cookie 更是连 repr 都不进。
   —— 参考插件（listen_music）把 cookie 字段设成 `repr=False`，就是同一个考虑。

时间统一走固定 UTC+8，不依赖宿主时区（VPS 是 UTC 时日期会差一天）。
"""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# 中国标准时间，恒定 UTC+8（无夏令时）。
# 不用 zoneinfo：Windows 上要额外装 tzdata，而这里只需要一个固定偏移。
CN_TZ = timezone(timedelta(hours=8), "CST")

# ---------------------------------------------------------------------------
# 掩码
# ---------------------------------------------------------------------------

MASK = "***"

# 这些 header / 字段名**整个值都要盖掉**
SECRET_KEYS = frozenset({
    "cookie", "set-cookie", "cookies", "cookie_blob",
    "password", "passwd", "pwd",
    "_token", "token", "csrf", "csrf_token",
    "authorization", "auth", "apikey", "api_key", "secret",
    "sessdata", "bili_jct", "dedeuserid", "c3vk",
})

# 需要掩码的失败分类（固定枚举 —— 上层只能从这里取，避免各写各的）
ERROR_KINDS = (
    "凭据失效",      # cookie 过期 / 被踢
    "限流",          # 429 / 风控
    "挑战未过",      # Cloudflare / C3VK 过不去
    "页面结构变化",  # 解析不到预期元素 → 对面改版
    "网络不可达",    # 超时 / DNS / 连接被拒
    "解析失败",      # 拿到了响应但解析不出来
    "数据库错误",
    "内部错误",
)

# 掩码的两条正则。**分 `:` 和 `=` 两种分隔符处理，这是踩过坑的**：
#
#   最初只写了一条 `key[:=]value`（value 到空白为止），结果
#       `Authorization: Bearer abc123deadbeef`
#   只盖住了 `Bearer`，**真正的 token 原样留在日志里**（自测抓出来的）。
#
# 正确规则：
#   * `key: value`（HTTP 头风格）→ **盖到行尾**。因为值里可能含空格
#     （`Bearer xxx`、`Digest xxx`、`SESSDATA=a; Path=/`），到空白就停必然漏。
#   * `key=value`（查询串 / 表单风格）→ 盖到 `&`、`;`、`,`、空白为止，
#     否则会把后面无关的参数也吃掉。
#
# 原则：**宁可多盖**。盖多了只是不好看，盖少了是安全事故。
_KEY_ALT = "|".join(re.escape(k) for k in sorted(SECRET_KEYS, key=len, reverse=True))
_HEADER_RE = re.compile(r"(?i)\b(" + _KEY_ALT + r")\b(\s*:\s*)([^\r\n]{1,500})")
_INLINE_RE = re.compile(r"(?i)\b(" + _KEY_ALT + r")\b(\s*=\s*)([^\s&;,\"']{1,300})")
# JS 里的 `window.document.cookie="C3VK=9713b1; path=/"`
_JS_COOKIE_RE = re.compile(r"(?i)document\.cookie\s*=\s*([\"'])([^\"']{1,300})\1")


def mask_text(text: Any) -> str:
    """把一段文本里的凭据盖掉。**只输出，不解析语义** —— 宁可多盖。"""
    if text is None:
        return ""
    s = str(text)
    # 1. 先处理 JS 里写 cookie 的形式（洛谷的 C3VK 挑战就是这种）
    s = _JS_COOKIE_RE.sub(lambda m: 'document.cookie="%s"' % MASK, s)
    # 2. `key: value` —— 盖到行尾（Authorization: Bearer xxx 靠这条）
    s = _HEADER_RE.sub(lambda m: "%s%s%s" % (m.group(1), m.group(2), MASK), s)
    # 3. `key=value` —— 盖到 token 边界
    s = _INLINE_RE.sub(lambda m: "%s%s%s" % (m.group(1), m.group(2), MASK), s)
    return s


def mask_mapping(data: Any) -> Any:
    """递归地把 dict / list 里的敏感字段盖掉。用于结构化日志的字段。"""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if str(k).lower() in SECRET_KEYS:
                out[k] = MASK if v else ""
            else:
                out[k] = mask_mapping(v)
        return out
    if isinstance(data, (list, tuple)):
        return [mask_mapping(x) for x in data]
    if isinstance(data, str):
        return mask_text(data)
    return data


def headers_for_log(headers: Any) -> dict:
    """把 headers 变成一个**可以安全写进日志**的 dict。"""
    if not headers:
        return {}
    try:
        items = headers.items() if hasattr(headers, "items") else dict(headers).items()
    except Exception:
        return {"_": MASK}
    return {k: (MASK if str(k).lower() in SECRET_KEYS else str(v)[:120])
            for k, v in items}


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def stamp() -> str:
    return now_cn().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 日志器
# ---------------------------------------------------------------------------

class Recorder:
    """结构化日志：写文件 + 走 AstrBot logger + 在内存里留一份环形缓冲。

    环形缓冲是给 `/日志 [n]` 用的 —— 用户不该为了看一眼日志去开网页。
    """

    def __init__(self, log_path: str, level: str = "info",
                 buffer_size: int = 500, to_astrbot: bool = True) -> None:
        self.log_path = log_path
        self.level = (level or "info").lower()
        self._buf: deque[str] = deque(maxlen=max(50, int(buffer_size)))
        self._lock = threading.Lock()
        self._to_astrbot = to_astrbot
        self._logger: logging.Logger | None = None
        self._fh: logging.Handler | None = None

    # ---- 生命周期 ------------------------------------------------------
    def open(self) -> tuple[bool, str]:
        """建目录 + 挂轮转文件 handler。失败不抛，返回 (ok, 说明)。"""
        try:
            parent = os.path.dirname(os.path.abspath(self.log_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            logger = logging.getLogger("astrbot_plugin_xcpc")
            logger.setLevel(logging.DEBUG if self.level == "debug" else logging.INFO)
            logger.propagate = False
            # 幂等：重复 open 不重复挂 handler
            if self._fh is not None:
                try:
                    logger.removeHandler(self._fh)
                except Exception:
                    pass
            fh = logging.handlers.RotatingFileHandler(
                self.log_path, maxBytes=2 * 1024 * 1024, backupCount=3,
                encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(fh)
            self._logger = logger
            self._fh = fh
            return True, "ok"
        except OSError as exc:
            return False, "日志文件打不开：%s" % exc

    def close(self) -> None:
        if self._logger is not None and self._fh is not None:
            try:
                self._logger.removeHandler(self._fh)
                self._fh.close()
            except Exception:
                pass
        self._fh = None

    # ---- 写入 ----------------------------------------------------------
    def _emit(self, line: str, level: str) -> None:
        with self._lock:
            self._buf.append(line)
        if self._logger is not None:
            self._logger.log(
                logging.DEBUG if level == "debug" else logging.INFO, line)
        if self._to_astrbot:
            try:
                from astrbot.api import logger as ab_logger  # 延迟导入：脱离 AstrBot 也能测
                (ab_logger.debug if level == "debug" else ab_logger.info)(
                    "[xcpc] %s" % line)
            except Exception:
                pass
        if self.level == "debug" and self._logger is None:
            # 兜底输出：文件 handler 没挂上、又是 debug 级别时，
            # 至少让消息出现在控制台上，别静默丢掉。
            #
            # ⚠️ **兜底本身也可能崩**：中文 Windows 控制台是 GBK，
            # 而日志行里有中文（失败分类就是中文的，比如「凭据失效」）。
            # 所以这里显式转码，编不出来就用 ? 代替 ——
            # 一条带 ? 的日志远好过整个脚本崩掉。
            try:
                safe = line.encode(sys.stderr.encoding or "utf-8",
                                   errors="replace").decode(
                    sys.stderr.encoding or "utf-8", errors="replace")
            except (LookupError, UnicodeError):
                safe = line.encode("ascii", errors="replace").decode("ascii")
            try:
                print(safe, file=sys.stderr)
            except Exception:
                pass

    def event(self, name: str, *, user_id: str = "", platform: str = "",
              level: str = "info", duration_ms: int | None = None,
              ok: bool = True, error_kind: str = "", detail: str = "",
              **fields: Any) -> str:
        """记一条结构化事件。

        `error_kind` 只能取 `ERROR_KINDS` 里的值 —— 自由文本没法统计。
        """
        parts = [stamp(), "%-14s" % name]
        if user_id:
            parts.append("user=%s" % user_id)
        if platform:
            parts.append("pf=%s" % platform)
        if duration_ms is not None:
            parts.append("%dms" % duration_ms)
        parts.append("ok" if ok else "FAIL")
        if not ok:
            if error_kind not in ERROR_KINDS:
                error_kind = "内部错误"
            parts.append("[%s]" % error_kind)
        if detail:
            parts.append(mask_text(detail)[:400])
        safe = mask_mapping(fields)
        if safe:
            try:
                parts.append(json.dumps(safe, ensure_ascii=False)[:400])
            except (TypeError, ValueError):
                parts.append(str(safe)[:400])
        line = " ".join(parts)
        self._emit(line, level)
        return line

    def http(self, method: str, url: str, *, status: int | None = None,
             duration_ms: int | None = None, resp_bytes: int | None = None,
             user_id: str = "", platform: str = "", note: str = "",
             error_kind: str = "", level: str = "debug") -> str:
        """记一条 HTTP 请求。**URL 会过掩码**（有些接口把 token 放 query 里）。"""
        bits = [stamp(), "http.request  ", "%-4s" % method, mask_text(url)[:220]]
        if status is not None:
            bits.append("-> %s" % status)
        if duration_ms is not None:
            bits.append("%dms" % duration_ms)
        if resp_bytes is not None:
            bits.append("%dB" % resp_bytes)
        if user_id:
            bits.append("user=%s" % user_id)
        if platform:
            bits.append("pf=%s" % platform)
        if note:
            bits.append("(%s)" % note)
        if error_kind:
            bits.append("[%s]" % (error_kind if error_kind in ERROR_KINDS else "内部错误"))
        line = " ".join(bits)
        self._emit(line, level)
        return line

    # ---- 读取（给 /日志 命令）------------------------------------------
    def tail(self, n: int = 20, user_id: str = "") -> list[str]:
        """最近 n 条。传 user_id 时只返回与该用户相关或全局的行。

        **普通用户看不到别人的日志** —— 日志里带 user= 前缀，这里据此过滤。
        """
        with self._lock:
            lines = list(self._buf)
        if user_id:
            keep = []
            for ln in lines:
                if ("user=%s " % user_id) in ln + " " or "user=" not in ln:
                    keep.append(ln)
            lines = keep
        return lines[-max(1, min(int(n), 200)):]
