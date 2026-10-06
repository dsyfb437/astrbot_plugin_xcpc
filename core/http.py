#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 层：四个平台共用。

它要解决四个具体问题，每个都是实测出来的，不是设想的：

1. **UA 决定生死（QOJ）**
   `curl/8.0` 的 UA 会被 Cloudflare 拦（403 "Just a moment..."），
   换成 Chrome UA 就 200。所以默认带浏览器 UA，而且是完整的那个。

2. **有些站点的"反爬"其实是道算术题（洛谷）**
   洛谷直接请求会返回 345 字节的 JS：
       window.document.cookie="C3VK=c82bf1; path=/; max-age=300;"
   **cookie 值明文写在里面** —— 不需要浏览器，解析出来带上重试即可。
   有效期只有 300 秒，所以每次会话都要能自动重取。

3. **限速**：CF 官方 API 大约 1 req/s；超了会被临时封。要能配置，但**有硬下限**。

4. **日志必须能看到失败原因，但不能泄漏凭据**
   全量记 URL / 状态码 / 耗时 / 字节数，但 Cookie 之类一律掩码（见 core/log.py）。

实现选择
--------
用标准库 `urllib.request`（不是 aiohttp/httpx）：
插件本来就要求"零第三方依赖也能跑"，而且这些站点的并发需求是零
（一个人同步自己的记录，串行足够）。同步代码丢进 `asyncio.to_thread`。
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json as jsonlib
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import log as logm

# 完整的 Chrome UA —— **别改短**。QOJ 的 Cloudflare 对不完整的 UA 也会拦。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip",
}

# 洛谷的 C3VK 挑战：cookie 值明文写在返回的 JS 里
_C3VK_RE = re.compile(r"C3VK=([0-9a-fA-F]{4,64})")
# Cloudflare 的挑战页特征
_CF_RE = re.compile(r"(?i)Just a moment|cf-challenge|__cf_chl|cf_chl_opt")


@dataclass
class Response:
    """一次 HTTP 响应。**故意不保存原始 headers** —— 免得有人不小心把它写进日志。"""
    status: int
    body: bytes
    url: str
    elapsed_ms: int = 0

    @property
    def text(self) -> str:
        """尽力解码。站点编码不统一，按优先级试。"""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return self.body.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.text)

    def __bool__(self) -> bool:
        return 200 <= self.status < 300


class HttpError(Exception):
    """带上**失败分类**的异常 —— 上层据此记日志、给用户看原因。

    分类用 `core/log.py` 的固定枚举，保证可统计。
    """

    def __init__(self, message: str, kind: str = "网络不可达",
                 status: int | None = None):
        super().__init__(message)
        self.kind = kind if kind in logm.ERROR_KINDS else "内部错误"
        self.status = status


@dataclass
class _Host:
    """单个域名的限速状态。"""
    min_interval: float = 1.0
    last_at: float = 0.0


@dataclass
class HttpClient:
    """一个平台的 HTTP 客户端。

    每个平台一个实例，各自持有 cookie（QOJ 和洛谷的会话不能混）。
    """
    platform: str = ""
    timeout: float = 20.0
    cookies: dict[str, str] = field(default_factory=dict)
    recorder: logm.Recorder | None = None
    # 每域名最小请求间隔。CF 官方 API 约 1 req/s。
    min_interval: float = 1.0
    # 用户可调倍率，但**有硬下限** —— 调太快会被封，这不是能商量的参数
    rate_scale: float = 1.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    _hosts: dict[str, _Host] = field(default_factory=dict, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # 失败分类计数，便于 /xcpc 状态 里显示"同步健康度"
    error_counts: dict[str, int] = field(default_factory=dict)

    RATE_FLOOR = 0.35          # 硬下限：再快也不低于这个间隔

    # ---- cookie ---------------------------------------------------------
    def set_cookies(self, cookies: dict[str, str]) -> None:
        """整批替换 cookie。**会丢弃 \r\n 的** —— 那是 header 注入。"""
        clean = {}
        for k, v in (cookies or {}).items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            if "\r" in v or "\n" in v or "\r" in k or "\n" in k:
                self._count_error("内部错误")
                if self.recorder:
                    self.recorder.event(
                        "auth.cookie_rejected", platform=self.platform, ok=False,
                        error_kind="内部错误",
                        detail="cookie 含换行符，疑似 header 注入，已丢弃")
                continue
            clean[k] = v
        self.cookies = clean

    def cookie_header(self) -> str:
        return "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())

    # ---- 内部 -----------------------------------------------------------
    def _count_error(self, kind: str) -> None:
        self.error_counts[kind] = self.error_counts.get(kind, 0) + 1

    def _harvest_c3vk(self, raw: bytes) -> str:
        """从一段响应里捞 C3VK 值。捞到就存进 cookie jar 并返回它。

        ⚠️ **不要用 `document.cookie` 这种字面量判断**。洛谷发的是混淆过的 JS：
            var _=["\\x64\\x6f\\x63\\x75\\x6d\\x65\\x6e\\x74"];   // = "document"
            window[_[0]].cookie="C3VK=9713b1; path=/; max-age=300;";
        字面量根本不出现。我第一版就是这么判断的 —— 夹具（按干净形式写的）能过，
        真站点解不出来。**夹具比现实宽松，就会漏掉真 bug。**

        现在只凭两个稳的特征：body 不大 + 里面有 `C3VK=` 的赋值。
        """
        if not raw or len(raw) > 8192 or b"C3VK=" not in raw:
            return ""
        m = _C3VK_RE.search(raw.decode("utf-8", errors="replace"))
        if not m:
            return ""
        val = m.group(1)
        if self.cookies.get("C3VK") != val:
            self.cookies["C3VK"] = val
        return val

    def _effective_interval(self) -> float:
        try:
            scale = float(self.rate_scale)
        except (TypeError, ValueError):
            scale = 1.0
        scale = max(0.25, min(scale, 10.0))
        return max(self.RATE_FLOOR, self.min_interval * scale)

    async def _throttle(self, host: str) -> None:
        async with self._lock:
            st = self._hosts.setdefault(host, _Host())
            st.min_interval = self._effective_interval()
            wait = st.min_interval - (time.monotonic() - st.last_at)
            if wait > 0:
                await asyncio.sleep(wait)
            st.last_at = time.monotonic()

    def _build_request(self, url: str, method: str, data: bytes | None,
                       headers: dict[str, str]) -> urllib.request.Request:
        hdrs = dict(DEFAULT_HEADERS)
        hdrs.update(self.extra_headers)
        if self.cookies:
            hdrs["Cookie"] = self.cookie_header()
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method.upper())
        for k, v in hdrs.items():
            req.add_header(k, v)
        return req

    def _do_request(self, req: urllib.request.Request) -> tuple[int, bytes, str]:
        """同步发一次请求，返回 (状态码, 正文, Location 头)。

        **不自动跟随重定向** —— 重定向要能被上层看见并判断（见 `_NoRedirect`）。

        注意 4xx/5xx **也是"拿到了响应"**：urllib 会抛 HTTPError，
        但错误页正文里常有线索（QOJ 的登录页就是靠这个识别的），所以要读出来。
        """
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    except OSError:
                        pass
                return resp.status, raw, (resp.headers.get("Location") or "")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            loc = ""
            try:
                loc = exc.headers.get("Location") or "" if exc.headers else ""
            except Exception:
                loc = ""
            return exc.code, body, loc

    async def request(self, method: str, url: str, *,
                      data: dict | bytes | None = None,
                      json: Any = None,
                      headers: dict[str, str] | None = None,
                      allow_redirect: bool = True,
                      max_redirect: int = 5) -> Response:
        """发一次请求。自动处理限速、gzip、重定向、C3VK 挑战、失败分类。"""
        host = urllib.parse.urlparse(url).netloc
        cur_url = url
        cur_method = method.upper()
        body: bytes | None
        if json is not None:
            body = jsonlib.dumps(json).encode("utf-8")
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = data

        for hop in range(max_redirect + 1):
            await self._throttle(host)
            req = self._build_request(cur_url, cur_method, body, headers or {})
            started = time.monotonic()
            try:
                status, raw, location = await asyncio.to_thread(self._do_request, req)
            except urllib.error.URLError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                kind = "网络不可达"
                self._count_error(kind)
                if self.recorder:
                    self.recorder.http(cur_method, cur_url, duration_ms=elapsed,
                                       platform=self.platform, error_kind=kind,
                                       note=str(getattr(exc, "reason", exc))[:120],
                                       level="info")
                raise HttpError("连不上 %s：%s" % (host, getattr(exc, "reason", exc)),
                                kind) from exc
            except Exception as exc:                      # noqa: BLE001
                elapsed = int((time.monotonic() - started) * 1000)
                self._count_error("内部错误")
                if self.recorder:
                    self.recorder.http(cur_method, cur_url, duration_ms=elapsed,
                                       platform=self.platform, error_kind="内部错误",
                                       note=type(exc).__name__, level="info")
                raise HttpError("请求异常：%s" % exc, "内部错误") from exc

            elapsed = int((time.monotonic() - started) * 1000)
            text_head = raw[:2000].decode("utf-8", errors="replace")

            # ---- Cloudflare 挑战：UA 不对时会出现，报明确分类而不是当 403 处理
            if status in (403, 503) and _CF_RE.search(text_head):
                self._count_error("挑战未过")
                if self.recorder:
                    self.recorder.http(cur_method, cur_url, status=status,
                                       duration_ms=elapsed, resp_bytes=len(raw),
                                       platform=self.platform, error_kind="挑战未过",
                                       note="Cloudflare", level="info")
                raise HttpError(
                    "%s 被 Cloudflare 拦住（挑战未过）。当前 UA 可能被判定为机器人。"
                    % host, "挑战未过", status)

            # ---- 洛谷 C3VK 挑战：cookie 值明文写在返回的 JS 里，解析后重试
            #
            # ⚠️ **不要用 `document.cookie` 这样的字面量去判断**。
            # 洛谷实际发的是**混淆过的** JS：
            #     var _=["\x64\x6f\x63\x75\x6d\x65\x6e\x74"];   // = "document"
            #     window[_[0]].cookie="C3VK=9713b1; path=/; max-age=300;";
            # 字面量 `document.cookie` **根本不出现**。
            #
            # 我第一版就是这么判断的，结果离线夹具（我按没混淆的形式写的）能过、
            # 真实站点解不出来 —— **夹具比现实宽松，就会漏掉真 bug**。
            # 现在只凭两个稳的特征：body 很小 + 里面有 C3VK= 的赋值。
            if self._harvest_c3vk(raw):
                if self.recorder:
                    self.recorder.http(cur_method, cur_url, status=status,
                                       duration_ms=elapsed, resp_bytes=len(raw),
                                       platform=self.platform,
                                       note="挑战:C3VK 已解出，重试", level="info")
                continue          # 带上新 cookie 重来

            # ---- 重定向
            if allow_redirect and status in (301, 302, 303, 307, 308) and hop < max_redirect:
                if not location:
                    # 302 却没有 Location：罕见，但要如实报，不能当成正常响应
                    raise HttpError("HTTP %d 但没有 Location 头：%s" % (status, cur_url),
                                    "解析失败", status)
                nxt = urllib.parse.urljoin(cur_url, location)
                if nxt == cur_url:
                    # **自己跳自己** —— 洛谷的 CDN 就这么干。
                    #
                    # 实测：`/record/list?...` 返回 302，Location 与请求 URL **完全相同**，
                    # 响应头带 `ws-action: cc`（CDN 的风控标记）。
                    # 不拦的话会一直跳到 max_redirect，报"重定向次数过多"，
                    # 真正的原因（要 C3VK cookie）就被掩盖了。
                    #
                    # 应对：**去首页把 C3VK 捞回来**再重试。
                    # （首页返回的是混淆过的 JS，值明文写在里面。）
                    got = self._harvest_c3vk(raw)
                    if not got:
                        home = "%s://%s/" % (urllib.parse.urlparse(cur_url).scheme, host)
                        try:
                            await self._throttle(host)
                            req2 = self._build_request(home, "GET", None, headers or {})
                            _st, raw2, _loc = await asyncio.to_thread(self._do_request, req2)
                            got = self._harvest_c3vk(raw2)
                        except Exception:
                            got = ""
                    if got:
                        if self.recorder:
                            self.recorder.http(cur_method, cur_url, status=status,
                                               duration_ms=elapsed, platform=self.platform,
                                               note="挑战:C3VK 取自首页，重试", level="info")
                        continue
                    raise HttpError(
                        "%s 返回 302 指向自身，且拿不到 C3VK cookie —— "
                        "CDN 风控挡住了（挑战未过）" % host, "挑战未过", status)
                cur_url = nxt
                host = urllib.parse.urlparse(cur_url).netloc
                if status in (301, 302, 303):
                    cur_method, body = "GET", None
                if self.recorder:
                    self.recorder.http(cur_method, cur_url, status=status,
                                       duration_ms=elapsed, platform=self.platform,
                                       note="-> 跟随重定向", level="debug")
                continue

            resp = Response(status=status, body=raw, url=cur_url, elapsed_ms=elapsed)
            if self.recorder:
                self.recorder.http(cur_method, cur_url, status=status,
                                   duration_ms=elapsed, resp_bytes=len(raw),
                                   platform=self.platform)
            return resp

        raise HttpError("重定向次数过多（>%d）：%s" % (max_redirect, url), "解析失败")

    # ---- 便捷方法 -------------------------------------------------------
    async def get(self, url: str, **kw) -> Response:
        return await self.request("GET", url, **kw)

    async def post_form(self, url: str, data: dict, **kw) -> Response:
        return await self.request("POST", url, data=data, **kw)

    async def get_json(self, url: str, **kw) -> Any:
        resp = await self.get(url, **kw)
        if not resp:
            raise HttpError("HTTP %d：%s" % (resp.status, url), "解析失败", resp.status)
        try:
            return resp.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError("返回的不是 JSON：%s" % exc, "解析失败", resp.status) from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """**不自动跟随重定向** —— 我们要自己看见 302 去了哪。

    踩过的坑：AtCoder 的 `/contests/x/submissions` 会 302 到 `/login`，
    自动跟随的话拿到的是登录页，解析出来"零条提交" —— 看着像"这人没提交过"，
    实际是"没登录"。**这种静默错误比报错危险得多。**
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
