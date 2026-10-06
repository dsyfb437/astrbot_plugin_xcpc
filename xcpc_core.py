#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""astrbot_plugin_xcpc 的纯逻辑层。

设计要点
--------
这个文件**不 import astrbot**。所有跟 AstrBot 框架打交道的代码都在 ``main.py``，
这里只有"读写 XCPC 工作区"和"解析复盘文本"两件事。好处是：

* 可以脱离 AstrBot 直接跑 ``python selftest.py`` 验证，改解析规则时不用起机器人；
* AstrBot 版本升级导致 API 变动时，需要改的只有 ``main.py``。

写入格式与 ``xcpc/02-tools/serve.py`` 的 ``save_review()`` **逐字节对齐**，
因为 ``02-tools/train_stats.py`` 会反过来解析这些文件来算个人 KPI。
改这里的格式之前，先看 train_stats.py 的 TITLE_RE / INFO_ROW_RE / PROB_ROW_RE。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: 北京时间相对 UTC 的偏移秒数。AstrBot 常跑在 Docker 里（TZ=UTC），
#: 直接 datetime.now() 会在晚上 8 点后算成第二天，所以统一换算。
CN_OFFSET = 8 * 3600

VERDICT_OK = "OK"

KIND_CHOICES = ["VP", "CF", "组队 VP", "ICPC", "CCPC", "省赛", "其他"]
PROBLEM_KINDS = ["思路", "实现", "调试", "知识", "读题", "—"]
CONTRIB_CHOICES = ["—", "独立想出", "参与讨论", "没参与"]
UPSOLVE_CHOICES = ["—", "已 AC", "P0", "P1", "P2", "P3"]

CONTRIB_INDEP = "独立想出"
UPSOLVE_DONE = "已 AC"
UPSOLVE_TODO = ("P0", "P1")

# 与 build_dashboard.py 保持一致的解析规则
CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.+?)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
MD_CODE_RE = re.compile(r"`([^`]+)`")
MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")

# 与 train_stats.py 保持一致的复盘文件解析规则
TITLE_RE = re.compile(r"^#\s*复盘[：:]\s*(.+?)\s*[—\-–]\s*(\d{4}-\d{2}-\d{2})\s*$")
INFO_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*$")
PROB_ROW_RE = re.compile(
    r"^\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|"
    r"\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$"
)


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
def now_cn() -> datetime:
    """当前北京时间（不依赖服务器 TZ）。"""
    return datetime.utcfromtimestamp(time.time() + CN_OFFSET)


def today_cn() -> str:
    return now_cn().strftime("%Y-%m-%d")


def from_cf_ts(ts: int) -> str:
    """Codeforces 时间戳（UTC 秒）→ 北京时间的 YYYY-MM-DD。"""
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts) + CN_OFFSET))


# --------------------------------------------------------------------------
# Markdown 小工具（复制自 build_dashboard.py，行为保持一致）
# --------------------------------------------------------------------------
def strip_md(text: str) -> str:
    """去掉行内 Markdown 标记（**粗体** / `代码` / [链接](url) / *斜体*）。"""
    text = MD_LINK_RE.sub(r"\1", text)
    text = MD_BOLD_RE.sub(r"\1", text)
    text = MD_CODE_RE.sub(r"\1", text)
    text = MD_ITALIC_RE.sub(r"\1", text)
    return text.strip()


def parse_checklist(path: str, fallback_title: str = "任务"):
    """把 Markdown 里的 ``- [ ] xxx`` 按最近的标题分组。

    返回 ``[{"title": str, "items": [{"text": str, "done": bool}]}]``，
    与 ``build_dashboard.parse_checklist`` 同构（这里不需要 stable_id，
    因为机器人端不回写勾选状态 —— 勾选仍然在手机上做）。
    """
    if not os.path.exists(path):
        return []
    groups = []
    current = {"title": fallback_title, "items": []}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            heading = HEADING_RE.match(line)
            if heading:
                title = heading.group(2).strip()
                if current["items"]:
                    groups.append(current)
                current = {"title": strip_md(title), "items": []}
                continue
            item = CHECKBOX_RE.match(line)
            if item:
                current["items"].append({
                    "text": strip_md(item.group(2).strip()),
                    "done": item.group(1).lower() == "x",
                })
    if current["items"]:
        groups.append(current)
    return [g for g in groups if g["items"]]


# --------------------------------------------------------------------------
# 指标计算（复制自 cf_analyze.activity_streak，避免依赖工作区的 Python 包）
# --------------------------------------------------------------------------
def activity_streak(submissions) -> dict:
    """按北京时间的自然日算连续提交天数。"""
    days = sorted({
        from_cf_ts(sub["creationTimeSeconds"])
        for sub in submissions if sub.get("creationTimeSeconds")
    })
    empty = {
        "days_active": 0, "max_streak": 0, "current_streak": 0,
        "last_day": None, "days_since_last": None, "recent30": 0,
    }
    if not days:
        return empty

    def to_date(text):
        return datetime.strptime(text, "%Y-%m-%d").date()

    best = run = 1
    for i in range(1, len(days)):
        if (to_date(days[i]) - to_date(days[i - 1])).days == 1:
            run += 1
            best = max(best, run)
        else:
            run = 1
    best = max(best, run)

    current = 1
    for i in range(len(days) - 1, 0, -1):
        if (to_date(days[i]) - to_date(days[i - 1])).days == 1:
            current += 1
        else:
            break

    today = datetime.strptime(today_cn(), "%Y-%m-%d").date()
    return {
        "days_active": len(days),
        "max_streak": best,
        "current_streak": current,
        "last_day": days[-1],
        "days_since_last": (today - to_date(days[-1])).days,
        "recent30": sum(1 for x in days if (today - to_date(x)).days < 30),
    }


def count_solved(submissions) -> int:
    """去重后的 AC 题数（(contestId, index) 唯一）。"""
    seen = set()
    for sub in submissions:
        if sub.get("verdict") != VERDICT_OK:
            continue
        prob = sub.get("problem") or {}
        cid, idx = prob.get("contestId"), prob.get("index")
        if cid is None or idx is None:
            continue
        seen.add((cid, idx))
    return len(seen)


def reviewed_today(contests, day: str | None = None) -> bool:
    """``contests`` 里有没有 ``day``（默认今天）那一条。

    输入是 ``build_dashboard.parse_contests()`` 的输出形状 —— 也就是
    ``03-log/contests.csv`` 的每一行，字段 ``date`` / ``kind`` / ``contest`` ...。

    为什么需要它：``backend=http`` 时插件拿不到工作区的文件系统，没法去 glob
    ``04-review/``。但 ``serve.py::save_review()`` 每写一份复盘都会往
    ``03-log/contests.csv`` 追加一行、``date`` 列就是复盘日期，而 ``/api/data``
    的返回里带着解析好的 ``contests`` 数组 —— 所以这里能等价地回答
    "今天到底记过没有"，不用给 serve.py 加新端点。

    宽容处理：非 dict、缺字段、日期带时间后缀（``2026-10-06 21:03``）都不崩。
    """
    want = day or today_cn()
    for row in (contests or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("date") or "").strip()[:10] == want:
            return True
    return False


# --------------------------------------------------------------------------
# 复盘解析
# --------------------------------------------------------------------------
@dataclass
class Problem:
    label: str = ""
    rating: str = ""
    kind: str = "—"
    contrib: str = "—"
    upsolve: str = "—"
    note: str = ""


@dataclass
class ReviewDraft:
    """一份待写入的复盘。字段名与 serve.py 的 /api/review 载荷一致。"""

    name: str = ""
    date: str = ""
    kind: str = "VP"
    solved: str = ""
    penalty: str = ""
    rank: str = ""
    order: str = ""
    mine: str = ""
    wrong: str = ""
    pattern: str = ""
    tricks: str = ""
    upsolve: str = ""
    problems: list = field(default_factory=list)
    #: 完全没解析出结构化字段时，原文放这里，保证不丢东西
    raw: str = ""

    @property
    def independent_count(self) -> int:
        return sum(1 for p in self.problems if p.contrib == CONTRIB_INDEP)

    def is_empty(self) -> bool:
        return not any([
            self.name, self.wrong, self.tricks, self.mine,
            self.solved, self.problems, self.raw,
        ])


# 中英文键名 → ReviewDraft 字段
_KEY_ALIASES = {
    "name": "name", "比赛": "name", "比赛名": "name", "比赛名称": "name",
    "名称": "name", "题目": "name", "场次": "name",
    "date": "date", "日期": "date",
    "kind": "kind", "类型": "kind",
    "solved": "solved", "过题": "solved", "过题数": "solved", "ac": "solved",
    "ac数": "solved", "题数": "solved",
    "penalty": "penalty", "罚时": "penalty",
    "rank": "rank", "排名": "rank", "名次": "rank",
    "order": "order", "ac顺序": "order", "顺序": "order", "开题顺序": "order",
    "mine": "mine", "我的贡献": "mine", "贡献": "mine", "我做了啥": "mine",
    "wrong": "wrong", "想歪": "wrong", "想歪的地方": "wrong", "复盘": "wrong",
    "问题": "wrong", "卡点": "wrong",
    "pattern": "pattern", "模式": "pattern", "偏航的共同模式": "pattern",
    "共同模式": "pattern",
    "tricks": "tricks", "套路": "tricks", "可复用套路": "tricks",
    "upsolve": "upsolve", "补题": "upsolve", "补题情况": "upsolve",
}

# 题号行：`A 思路 看到 n=2e5 就反射性想线段树`
# 也接受 `A题`、`A.`、`1.`、`A：`、`A -` 等写法
_PROB_LINE_RE = re.compile(
    r"^\s*(?:第\s*)?(?P<label>[A-Za-z]\d?|\d{1,2})\s*(?:题)?\s*"
    r"(?:[.、,:：\-—|]\s*)?"
    r"(?P<kind>思路|实现|调试|知识|读题|没做出|不会|没想出来)?\s*"
    r"(?:[.、,:：\-—|]\s*)?"
    r"(?P<note>.*\S)\s*$"
)

# 只在整行只有题号时才认，避免把 `2026-10-06 打了场 VP` 误判成题号 20
_PROB_LABEL_ONLY_RE = re.compile(r"^\s*(?:第\s*)?([A-Za-z]\d?)\s*(?:题)?\s*$")


def _norm_key(text: str) -> str:
    return re.sub(r"[\s　]+", "", text).strip().lower()


def _extract_int(text: str):
    m = re.search(r"\d+", text or "")
    return m.group(0) if m else ""


def _guess_contrib(note: str) -> str:
    if "独立" in note or "自己想出" in note or "自己做" in note:
        return CONTRIB_INDEP
    if "讨论" in note or "队友" in note:
        return "参与讨论"
    if "没参与" in note or "没看" in note:
        return "没参与"
    return "—"


def _guess_upsolve(note: str) -> str:
    for tag in ("P0", "P1", "P2", "P3"):
        if tag in note:
            return tag
    flat = note.replace(" ", "")
    if "已AC" in flat or "补完" in flat or "补了" in flat or "过了" in flat:
        return UPSOLVE_DONE
    return "—"


def parse_review_text(text: str, default_date: str | None = None) -> ReviewDraft:
    """把用户发来的一段话解析成 ReviewDraft。

    支持两种写法混用：

    1. 键值行 —— ``比赛: CF Round 1024`` / ``过题：3`` / ``罚时=145``
       （``:`` ``：`` ``=`` 都认，也认 ``比赛 CF Round 1024`` 这种空格分隔）
    2. 逐题行 —— ``A 思路 看到区间就上线段树`` / ``E 实现 独立想出 P1 边界挂了``

    认不出来的句子会被收进 ``wrong``（"想歪的地方"），**一句话都不会丢**。
    """
    draft = ReviewDraft(date=default_date or today_cn())
    if not text:
        return draft

    leftovers = []
    pending_problem = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 去掉常见的 Markdown 列表符号和"复盘"这种标题前缀
        line = re.sub(r"^[-*+]\s+", "", line).strip()
        if not line:
            continue
        if line in ("复盘", "记录复盘", "vp复盘", "VP复盘"):
            continue

        # ---- 1) 键值行 ----
        pair = re.split(r"\s*[:：=]\s*", line, maxsplit=1)
        if len(pair) == 2:
            key = _KEY_ALIASES.get(_norm_key(pair[0]))
            if key:
                value = pair[1].strip()
                if key in ("solved", "penalty", "rank"):
                    value = _extract_int(value) or value
                if key == "problems":  # 不会出现，占位防止未来加键
                    continue
                if getattr(draft, key, None) in ("", None):
                    setattr(draft, key, value)
                else:  # 同一个键出现两次 → 追加，别覆盖
                    setattr(draft, key, "%s；%s" % (getattr(draft, key), value))
                pending_problem = None
                continue

        # ---- 2) 纯题号行，作为下一条逐题记录的题号 ----
        only = _PROB_LABEL_ONLY_RE.match(line)
        if only:
            pending_problem = Problem(label=only.group(1).upper())
            draft.problems.append(pending_problem)
            continue

        # ---- 3) 逐题行 ----
        prob = _PROB_LINE_RE.match(line)
        if prob and prob.group("note") and not prob.group("note").startswith("http"):
            label = prob.group("label").upper()
            # 数字题号要求行首就是数字，且后面跟着已知卡点类型或明显是记录
            looks_like_problem = bool(re.match(r"^\s*[A-Za-z]", line)) or \
                prob.group("kind") is not None
            if looks_like_problem:
                item = Problem(
                    label=label,
                    kind=prob.group("kind") or "—",
                    note=prob.group("note").strip(),
                )
                item.contrib = _guess_contrib(item.note)
                item.upsolve = _guess_upsolve(item.note)
                draft.problems.append(item)
                pending_problem = item
                continue

        leftovers.append(line)

    # ---- 4) 没说清楚的都进"想歪的地方" ----
    if leftovers:
        extra = "\n".join("- %s" % x for x in leftovers)
        draft.wrong = (draft.wrong + "\n" + extra).strip() if draft.wrong else extra

    # ---- 5) 比赛名兜底 ----
    if not draft.name:
        if draft.problems:
            draft.name = "未命名复盘"
        else:
            draft.name = "随手复盘"
        draft.raw = text.strip()

    if draft.kind not in KIND_CHOICES:
        draft.kind = "VP"

    return draft


# --------------------------------------------------------------------------
# 粘贴解析：榜单 / 提交记录
# --------------------------------------------------------------------------
# 为什么有这一节
# --------------
# 用户原话：「我要对着 QOJ VP 的榜单和提交记录才能想起来今天整个过程到底是咋样的」。
# 而 QOJ **抓不到**（实测 403 + robots.txt 禁止 /submission/），所以唯一的路子是
# 「人工粘贴、程序解析」—— Web 复盘台上已经做好了（serve.py 的 /api/standings），
# 这里把同一条路铺到 QQ 上，因为他打完 VP 人本来就在 QQ 里。
#
# 解析规则**不在这里**：`02-tools/standings.py` 已经写好、有 50+ 项自测、还被
# serve.py 和复盘页共用。抄一份过来 = 以后两边规则一定分叉（罚时、判题码……），
# 所以这里只做三件事：
#   1. 把它借过来 —— file 后端直接 import 那个文件；http 后端走 /api/standings；
#   2. 把两个后端的返回值**统一成一种结构**，让上面的指令代码不用管后端；
#   3. 把结果**只补空地**合进 ReviewDraft（手打的字段优先，绝不覆盖）。

#: 罚时规则**因平台而异**：ICPC / QOJ / 大多数区域赛是 20 分钟/次，
#: Codeforces 是 10 分钟/次。默认 20 —— 用户的主力场景是 QOJ 上的组队 VP。
#: 这个数字**错了会污染 train_stats.py 算出来的 KPI**，所以它出现在每一句
#: 回报里（让人能当场核对），也允许在插件配置里改（``penalty_per_fail``）。
DEFAULT_PER_FAIL = 20
CF_PER_FAIL = 10

#: serve.py /api/standings 返回的 ``kind`` → 人话
CONTEST_KIND_LABELS = {
    "standings": "榜单",
    "submissions": "提交记录",
    "both": "榜单 + 提交记录",
    "none": "（没认出来）",
}

#: 我们自己那句"有 N 行没能理解"的统计头（serve.py 和本地解析都会加）。
#: 回报里会自己写小标题，留着它就重复了 —— 统一滤掉，只留**原始未识别行**。
_SELF_WARNING_RE = re.compile(r"^有\s*\d+\s*行没能理解")

#: 正文第一行的平台提示词：`cf` → 10 分钟/次，`qoj`/`icpc` → 20。
_PER_FAIL_DIRECTIVE_RE = re.compile(r"^(cf|codeforces|qoj|icpc)$", re.I)

#: 「机器格子」的一个 token：题号 / 标记(+2 -3 ?) / 时间(1:45) / 判题码 / 纯数字 / 分隔符。
#: 为什么要认它：粘贴进来的内容也会被 ``parse_review_text`` 逐行扫一遍，于是一行
#: ``A\t+2\t1:45`` 会变成一条"逐题记录"、说明栏写着 ``+2 1:45``；一行
#: ``0:45 B Wrong Answer`` 会掉进「我想歪的地方」。**那些格子不是用户想说的话**，
#: 它们已经被结构化到别的字段里了，不该在文件里再出现一遍。
_MACHINE_TOKEN_RE = re.compile(
    r"^(?:[+\-?]\d*"                                   # + +2 -3 ?
    r"|\d{1,3}:\d{2}(?::\d{2})?"                       # 1:45 / 0:45 / 14:23:05
    r"|\d+"                                            # 排名 / 分数 / 罚时
    r"|[A-Za-z]\d?"                                    # 题号 A / B1
    r"|ac|wa|tle|mle|re|ce|pe|ole|uke|ok"              # 短判题码
    r"|accepted|wrong|answer|time|limit|memory|runtime"
    r"|compilation|presentation|output|unknown|exceeded|error"
    r"|正确|通过|答案错|超时|时间超限|超内存|内存超限|运行错|编译错"
    r"|[#|,;:、，（）()\[\]{}<>/\\*·.\-—+=_]+)$", re.I)

#: 题号 token（用于判断"像不像粘贴"）
_PASTE_LABEL_RE = re.compile(r"^[A-Za-z]\d?$")
#: 判题结果 token（同上）
_PASTE_VERDICT_RE = re.compile(
    r"^(?:ac|wa|tle|mle|re|ce|pe|ole|uke|ok|accepted|wrong|answer|time|limit"
    r"|memory|runtime|compilation|exceeded|error|正确|通过|超时|超限)$", re.I)


def _as_int(value):
    """宽松取整数：int / float / 字符串 / None 都要能过。

    http 后端那边是**另一个进程**，"数字"在 JSON 里可能是字符串；
    宽容一点，总比整个 /xcpc 解析 崩掉强。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        found = re.search(r"-?\d+", value)
        if found:
            return int(found.group(0))
    return None


def empty_contest_result(error: str = "", backend: str = "",
                         per_fail: int = DEFAULT_PER_FAIL) -> dict:
    """解析失败时的统一返回 —— 上层永远拿到同样形状的 dict，不用判空。"""
    return {
        "ok": False, "error": error, "backend": backend, "per_fail": per_fail,
        "kind": "none", "rows": [], "timeline": [], "submission_count": 0,
        "summary": "", "ac_order": "", "penalty": None,
        "penalty_breakdown": "", "warnings": [],
    }


def normalize_contest_result(data, backend: str = "", per_fail=None) -> dict:
    """把「解析结果」压成统一结构（两个后端唯一的对外形状）。

    输入既可以是 serve.py ``/api/standings`` 的返回值，也可以是 file 后端本地
    解析出来的同形状 dict —— **故意让两边长得一样**，这样指令代码只认这一种。

    宽容优先：缺字段、类型不对都不崩。那边是另一个进程 / 另一份仓库，
    它改了字段我们也不能跟着挂。
    """
    pf = _as_int(per_fail) or DEFAULT_PER_FAIL
    if not isinstance(data, dict):
        return empty_contest_result(
            "解析返回值不是对象（%s）" % type(data).__name__, backend, pf)
    if data.get("error"):
        return empty_contest_result(str(data["error"]), backend,
                                    _as_int(data.get("per_fail")) or pf)

    out = empty_contest_result("", backend, _as_int(data.get("per_fail")) or pf)
    out["ok"] = True
    out["kind"] = str(data.get("kind") or "none")
    out["rows"] = [r for r in (data.get("rows") or []) if isinstance(r, dict)]
    out["timeline"] = [t for t in (data.get("timeline") or [])
                       if isinstance(t, dict)]
    out["submission_count"] = _as_int(data.get("submission_count")) or 0
    out["summary"] = str(data.get("summary") or "")
    out["ac_order"] = str(data.get("ac_order") or "")
    out["penalty"] = _as_int(data.get("penalty"))
    out["penalty_breakdown"] = str(data.get("penalty_breakdown") or "")
    out["warnings"] = [str(w).strip() for w in (data.get("warnings") or [])
                       if str(w).strip()
                       and not _SELF_WARNING_RE.match(str(w).strip())]
    return out


def contest_recognized(parsed) -> bool:
    """这段文本"认出东西"了没有。

    判定：榜单认出 ≥1 道题，**或**提交记录 ≥2 条。

    为什么提交记录要 2 条起：单条提交几乎必然是误认（任何一行同时出现题号和
    "AC" 的文字都会被认出来），拿一条去补全复盘等于瞎猜 —— 宁可什么都不填。
    """
    if not parsed or not parsed.get("ok"):
        return False
    if parsed.get("rows"):
        return True
    return int(parsed.get("submission_count") or 0) >= 2


def contest_labels(parsed) -> list:
    """这次粘贴里出现了哪些题号（去重，保持出现顺序）。

    提交记录和时间线都收：榜单里可能有"没提交"的题（``D`` 那种），
    那也是这场比赛的一部分，用户要看到它。
    """
    out, seen = [], set()
    for source in (parsed.get("timeline") or [], parsed.get("rows") or []):
        for item in source:
            label = str(item.get("label") or "").strip().upper()
            if label and label not in seen:
                seen.add(label)
                out.append(label)
    return out


def _ac_labels(parsed) -> list:
    """过了哪些题。有提交记录就用它（带时间，比榜单准）。"""
    timeline = parsed.get("timeline") or []
    if timeline:
        return [str(t.get("label") or "").strip().upper()
                for t in timeline if t.get("verdict") == "AC"]
    return [str(r.get("label") or "").strip().upper()
            for r in (parsed.get("rows") or []) if r.get("result") == "AC"]


def _is_machine_line(line: str) -> bool:
    """整行都是机器格子吗（→ 不是人说的话）。

    故意保守：只要有一个 token 不是格子就返回 False（宁可留下噪音，
    也不要把用户写的话删掉）。超长行也不当格子 —— 那更像是一句话。
    """
    tokens = [t for t in re.split(r"\s+", (line or "").strip()) if t]
    if not tokens or len(tokens) > 40:
        return False
    return all(_MACHINE_TOKEN_RE.match(t) for t in tokens)


def _is_placeholder(problem) -> bool:
    """这条逐题记录是不是"空壳"——只有题号，说明栏要么空着、要么是粘贴出来的格子。

    空壳会被**就地换掉**（题号留下、说明清空），两个理由：

    * 说明是格子的（``A\\t+2\\t1:45``）—— 那不是他写的话；
    * 只有题号的（``D``）—— 本来就是空行，换掉只是让顺序回到榜单顺序。

    写了卡点类型（``A 思路 ...``）或写了说明的都算他自己的话，一律不动。
    """
    if getattr(problem, "kind", "—") not in ("", "—"):
        return False
    note = (getattr(problem, "note", "") or "").strip()
    return (not note) or _is_machine_line(note)


def looks_like_paste(text: str) -> bool:
    """这段文本像不像"从榜单/提交记录页面复制出来的"（≥2 行机器格子）。

    只用在"解析没成"时决定**要不要多嘴提醒一句** —— 正常聊天不会连着两行
    都是格子，所以这个启发式够用，也不会给普通复盘刷上错误提示。
    """
    hits = 0
    for line in (text or "").splitlines():
        if not line.strip() or not _is_machine_line(line):
            continue
        tokens = [t for t in re.split(r"\s+", line.strip()) if t]
        if any(_PASTE_LABEL_RE.match(t) for t in tokens) or \
                any(_PASTE_VERDICT_RE.match(t) for t in tokens):
            hits += 1
    return hits >= 2


def split_per_fail_directive(text: str, default=None):
    """认正文第一行的平台提示词，返回 ``(per_fail 或 None, 去掉那行后的正文)``。

    为什么要有：罚时规则因平台而异（ICPC/QOJ=20，CF=10），而罚时填错会直接
    污染 KPI。让他在正文最前面多打一个词，比让他去 WebUI 改配置快得多 ——
    他是**在手机上**发的这条消息。

    只认"整行就是一个提示词"，所以 ``比赛: CF Round 1024`` 不会被误吃。
    """
    lines = (text or "").splitlines()
    for i, raw in enumerate(lines):
        head = raw.strip()
        if not head:
            continue                      # 前面允许有空行
        m = _PER_FAIL_DIRECTIVE_RE.match(head)
        if not m:
            break                         # 第一行有内容且不是提示词 → 正文不动
        word = m.group(1).lower()
        rest = "\n".join(lines[:i] + lines[i + 1:])
        return (CF_PER_FAIL if word in ("cf", "codeforces") else DEFAULT_PER_FAIL), \
            rest.strip()
    return default, text or ""


def merge_contest_into_draft(draft: ReviewDraft, parsed) -> dict:
    """把粘贴解析的结果**补**进 draft（只补空的），返回一份"补了什么"的说明。

    三条不能破的规矩：

    1. **手打的字段优先** —— 他写「过题: 3」是对着屏幕数的，粘贴内容不能顶掉；
    2. **主观信息一律不猜** —— 逐题记录只补题号，「我的贡献」「补题级别」留空
       （那是他的判断，猜错了比留空更糟）；
    3. **认不出就什么都不做** —— 退回 ``parse_review_text`` 原来的行为，不报错。
    """
    report = {
        "applied": False, "kind": "none", "kind_label": "", "auto": [],
        "kept_manual": [], "labels": [], "added_labels": [],
        "problems_added": 0, "problems_total": 0,
        "dropped_lines": 0, "summary": "", "penalty": None,
        "penalty_breakdown": "", "penalty_skipped": False,
        "per_fail": DEFAULT_PER_FAIL, "submission_count": 0, "row_count": 0,
        "ac_count": 0, "warnings": [],
    }
    if not contest_recognized(parsed):
        return report                  # 规矩 3：不动 draft，一个字段都不改

    rows = parsed.get("rows") or []
    timeline = parsed.get("timeline") or []
    labels = contest_labels(parsed)
    ac_labels = _ac_labels(parsed)
    per_fail = _as_int(parsed.get("per_fail")) or DEFAULT_PER_FAIL
    order = (parsed.get("ac_order") or "").strip()
    penalty = parsed.get("penalty")

    report.update({
        "applied": True, "kind": parsed.get("kind") or "none",
        "kind_label": CONTEST_KIND_LABELS.get(parsed.get("kind") or "none", "内容"),
        "labels": labels, "summary": parsed.get("summary") or "",
        "penalty": penalty, "penalty_breakdown": parsed.get("penalty_breakdown") or "",
        "per_fail": per_fail,
        "submission_count": int(parsed.get("submission_count") or 0),
        "row_count": len(rows), "ac_count": len(ac_labels),
        "warnings": list(parsed.get("warnings") or []),
    })

    # ---- 1) 结果字段：只补空的（规矩 1）----
    if rows or timeline:
        if not draft.solved:
            draft.solved = str(len(ac_labels))
            report["auto"].append(("过题", draft.solved))
        elif draft.solved != str(len(ac_labels)):
            report["kept_manual"].append(("过题", draft.solved))
    if penalty is not None:
        if not draft.penalty:
            draft.penalty = str(penalty)
            report["auto"].append(("罚时", draft.penalty))
        elif str(penalty) != str(draft.penalty):
            report["kept_manual"].append(("罚时", draft.penalty))
    elif not draft.penalty:
        # penalty 是 None = 时间信息不足。**不瞎填**，但要告诉用户为什么空着。
        report["penalty_skipped"] = True
    if order:
        if not draft.order:
            draft.order = order
            report["auto"].append(("AC 顺序", order))
        elif draft.order != order:
            report["kept_manual"].append(("AC 顺序", draft.order))

    # ---- 2) 逐题记录：题号来自粘贴，主观字段留空（规矩 2）----
    shells = [p for p in draft.problems if _is_placeholder(p)]
    kept = [p for p in draft.problems if not _is_placeholder(p)]
    seen = {p.label.upper() for p in kept if p.label}
    added = []
    for label in list(labels) + [p.label.upper() for p in shells if p.label]:
        if label and label not in seen:
            seen.add(label)
            # kind / contrib / upsolve 全保持 "—"：这些得他自己说
            added.append(Problem(label=label))
    if shells or added:
        # 空壳被换成了干净的题号行 —— 题号一个都不会丢（shells 的题号一定
        # 会进 added，因为它们的 label 不在 kept 里）
        draft.problems = kept + added
    report["problems_added"] = len(added)
    report["added_labels"] = [p.label for p in added]
    report["problems_total"] = len(draft.problems)

    # ---- 3) 「想歪的地方」里别再重复一遍机器格子 ----
    # 粘贴的内容也会走 parse_review_text，那些行会掉进 leftovers。它们现在
    # 已经是结构化字段了，重复记一遍只会把复盘文件刷成流水账。
    if draft.wrong:
        kept_lines, dropped = [], 0
        for line in draft.wrong.splitlines():
            body = re.sub(r"^[-*+]\s+", "", line).strip()
            if body and _is_machine_line(body):
                dropped += 1
                continue
            kept_lines.append(line)
        report["dropped_lines"] = dropped
        draft.wrong = "\n".join(kept_lines).strip()

    return report


# --------------------------------------------------------------------------
# 粘贴解析结果的排版
# --------------------------------------------------------------------------
# 为什么排版函数放在这个文件（而不是像 format_status 那样放 main.py）：
# 这几个数字**必须交代来源**（罚时是估的、按什么规则估的、哪个字段被手打的
# 顶住了），那是这个功能的契约，放在这里才能被 selftest.py 守住 ——
# main.py 依赖 astrbot，自测里 import 不了。

def fmt_clock(seconds) -> str:
    """秒 → ``12:34`` / ``1:02:33``。提交记录的 ``at`` 就是秒。"""
    total = _as_int(seconds)
    if total is None:
        return "—"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def fmt_minutes(minutes) -> str:
    """分钟 → ``12:34``。**榜单**的 ``time`` 是分钟（standings.parse 的约定）。

    和 ``fmt_clock`` 差 60 倍 —— 所以这两个函数故意不合并：
    混用会让每道题的用时静默错 60 倍。
    """
    total = _as_int(minutes)
    if total is None:
        return "—"
    return "%d:%02d" % (total // 60, total % 60)


def _fmt_row(row) -> str:
    """榜单一行 → ``A 过 1:45（3 次）``。"""
    label = str(row.get("label") or "?")
    result = str(row.get("result") or "NONE").upper()
    attempts = _as_int(row.get("attempts")) or 0
    if result == "AC":
        tail = fmt_minutes(row.get("time"))
        if attempts > 1:
            tail += "（%d 次）" % attempts
        return "%s 过 %s" % (label, tail)
    if result == "FAIL":
        return "%s 没过（交了 %d 次）" % (label, attempts or 1)
    return "%s 没提交" % label


def _fmt_timeline(rows, limit: int = 14) -> list:
    """折叠后的提交时间线 → ``0:12 A AC``、``0:45 B WA×2`` 这样的短句。"""
    out = []
    for item in rows[:limit]:
        piece = "%s %s %s" % (fmt_clock(item.get("at")),
                              str(item.get("label") or "?"),
                              str(item.get("verdict") or "?"))
        fails = _as_int(item.get("fails")) or 0
        if fails > 1:
            piece += "×%d" % fails
        out.append(piece)
    if len(rows) > limit:
        out.append("…还有 %d 条" % (len(rows) - limit))
    return out


def format_contest_report(report) -> list:
    """``/xcpc 复盘`` 回报里那几行"顺手解析了你粘的内容"。

    为什么非要写这么细：罚时是**估算**的，而且规则因平台而异。不把算式和
    规则摆出来，用户就不知道这个数字能不能信，回头 KPI 算错也找不到原因。
    """
    if not report or not report.get("applied"):
        return []
    lines = []
    head = "· 解析了你粘的%s" % (report.get("kind_label") or "内容")
    if report.get("submission_count"):
        head += "（%d 条提交）" % report["submission_count"]
    if report.get("row_count"):
        head += "（榜单 %d 题）" % report["row_count"]
    lines.append(head)

    auto = report.get("auto") or []
    if auto:
        lines.append("· 自动补上：%s"
                     % "、".join("%s %s" % (k, v) for k, v in auto))
    if report.get("problems_added"):
        lines.append("· 逐题记录补了 %d 条题号（%s）—— "
                     "贡献/补题留空了，那是你说了算的"
                     % (report["problems_added"],
                        "/".join((report.get("added_labels")
                                  or report.get("labels") or [])[:12])))
    for field, value in (report.get("kept_manual") or []):
        lines.append("· 手打的「%s %s」优先，没被粘贴里的估算顶掉"
                     % (field, value))
    if report.get("penalty_skipped"):
        lines.append("· 罚时没自动填：粘的内容里缺时间信息 —— "
                     "这种情况不瞎填，你手写一个")
    if report.get("penalty_breakdown"):
        lines.append("· 罚时算式：%s" % report["penalty_breakdown"])
        lines.append("· ⚠️ 罚时是按 %d 分钟/次**估**的（ICPC/QOJ 规则）；"
                     "Codeforces 是 10 分钟/次，别的平台自己核对"
                     % (report.get("per_fail") or DEFAULT_PER_FAIL))
    if report.get("summary"):
        lines.append("· 比赛的形状：%s" % report["summary"])
    if report.get("warnings"):
        lines.append("· 有 %d 行没看懂 —— 发 /xcpc 解析 能看到原样列表"
                     % len(report["warnings"]))
    if report.get("dropped_lines"):
        lines.append("· 粘贴里的 %d 行机器格子已经变成上面的字段了，"
                     "就没重复记进「想歪的地方」" % report["dropped_lines"])
    return lines


def format_parse_reply(parsed) -> str:
    """``/xcpc 解析`` 的回报（只读不落盘，所以要把话说全）。

    内容：识别类型 / 题数 / 过题 / AC 顺序 / 估算罚时+算式 / 比赛的形状 /
    时间线 / 逐题 / **没能理解的行（原样列出）**。
    """
    if not parsed or not parsed.get("ok"):
        err = (parsed or {}).get("error") or "没认出这段内容"
        return ("解析不了：%s\n\n"
                "能认两种东西：\n"
                "① 榜单 —— CF 那种格子，例如 A +2 1:45 ｜ B -3\n"
                "② 提交记录 —— 每行带题号和判题结果，"
                "例如 0:45 B Wrong Answer\n"
                "（在那两个页面上全选复制，直接粘过来就行）" % err)

    kind = parsed.get("kind") or "none"
    rows = parsed.get("rows") or []
    timeline = parsed.get("timeline") or []
    n_subs = int(parsed.get("submission_count") or 0)
    per_fail = _as_int(parsed.get("per_fail")) or DEFAULT_PER_FAIL
    labels = contest_labels(parsed)
    ac_labels = _ac_labels(parsed)
    order = (parsed.get("ac_order") or "").strip()
    penalty = parsed.get("penalty")
    warnings = parsed.get("warnings") or []

    out = ["🔍 解析结果（没有写任何文件）", ""]
    out.append("· 类型：%s" % CONTEST_KIND_LABELS.get(kind, "没认出来"))
    if n_subs:
        out.append("· 提交记录 %d 条" % n_subs)
    if rows:
        n_ac = sum(1 for r in rows if r.get("result") == "AC")
        n_fail = sum(1 for r in rows if r.get("result") == "FAIL")
        out.append("· 榜单 %d 题：%d 题过、%d 题没过、%d 题没提交"
                   % (len(rows), n_ac, n_fail, len(rows) - n_ac - n_fail))
    if labels:
        shown = "/".join(labels[:20])
        if len(labels) > 20:
            shown += "（还有 %d 道）" % (len(labels) - 20)
        out.append("· 题号：%s" % shown)
        out.append("· 过题：%d 道" % len(ac_labels))
    if order:
        out.append("· AC 顺序：%s" % order)

    if rows or timeline:
        if penalty is not None:
            out.append("· 估算罚时：%s 分钟" % penalty)
            if parsed.get("penalty_breakdown"):
                out.append("  算式：%s" % parsed["penalty_breakdown"])
            out.append("  ⚠️ 按 %d 分钟/次算（ICPC/QOJ）；Codeforces 是 10 —— "
                       "正文第一行单独写个 cf 就按 10 算" % per_fail)
        else:
            out.append("· 估算罚时：算不出来（缺时间信息）"
                       "—— 这种情况不瞎填，你手填")

    if parsed.get("summary"):
        out.append("· 比赛的形状：%s" % parsed["summary"])
    if timeline:
        out.append("· 时间线：%s" % " → ".join(_fmt_timeline(timeline)))
    if rows:
        out.append("· 逐题：%s" % " ｜ ".join(_fmt_row(r) for r in rows[:20]))

    if warnings:
        out.append("")
        out.append("· 没能理解的行（原样列出，你自己判断要不要手填）：")
        for item in warnings[:14]:
            out.append("  " + str(item).strip()[:120])
        if len(warnings) > 14:
            out.append("  …还有 %d 行" % (len(warnings) - 14))

    if kind == "none" or not (rows or timeline):
        out.append("")
        out.append("没认出榜单或提交记录 —— 上面那些没理解的行就是全部内容。")
        out.append("能认的两种：① CF 式榜单格子（A +2 1:45 ｜ B -3）；"
                   "② 提交记录（0:45 B Wrong Answer，题号和判题结果在同一行）。")

    out.append("")
    out.append("确认没问题就把同样的内容发 /xcpc 复盘 —— 手打的字段优先，不会被覆盖。")
    return "\n".join(out)


# --------------------------------------------------------------------------
# 借用 02-tools/standings.py（file 后端专用）
# --------------------------------------------------------------------------
#: 路径 → 已经加载好的 standings 模块。AstrBot 是常驻进程，/xcpc 复盘 可能被连着发
#: 好几次，每次重新 exec 一遍那个文件没必要。
_STANDINGS_CACHE: dict = {}


def _import_standings_file(path: str):
    """按**文件路径**加载一个模块，不进 ``sys.modules``。

    什么时候需要它：``sys.modules`` 里的 ``standings`` 可能来自**另一个**
    工作区（用户换了 ``workspace_root``，或者同一进程先跑过别的目录的自测）。
    那样就会拿旧目录的规则去解析，罚时/判题码**静默算错** —— 这类错最难查。
    """
    import importlib.util

    name = "_xcpc_standings_" + "".join(
        ch if ch.isalnum() else "_" for ch in os.path.normcase(path))[-60:]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - 解析器加载失败不能让接口崩
        return None
    return module


def _load_standings(root: str):
    """从 ``<root>/02-tools/standings.py`` 借解析器，返回 ``(模块 或 None, 错误说明)``。

    为什么这么绕：workspace 可能是个**老版本**（没有 02-tools），或者这份插件
    被拷到了 AstrBot 的 data/plugins 下、旁边根本没有工作区。这几种情况都只该
    "粘贴解析用不了"，**不该让整个 /xcpc 复盘 挂掉**，所以这里从不抛异常。
    """
    root = os.path.abspath(os.path.expanduser(root or ""))
    tools = os.path.join(root, "02-tools")
    path = os.path.join(tools, "standings.py")
    if not os.path.isfile(path):
        return None, ("workspace 里找不到 02-tools/standings.py"
                      "（老版本工作区？粘贴解析先跳过了）")
    key = os.path.normcase(path)
    cached = _STANDINGS_CACHE.get(key)
    if cached is not None:
        return cached, ""

    if tools not in sys.path:
        # 按约定"把 02-tools 加进 sys.path 再 import standings"。
        # 副作用是这一条会一直留在 sys.path 里（AstrBot 进程里 `import serve`
        # 之类会优先命中工作区脚本）—— 可接受：那些文件本来就在这台机器上，
        # 而留着它才能保证 standings.py 以后要是 import 同目录的兄弟模块也不会断。
        sys.path.insert(0, tools)
    try:
        import standings as module            # noqa: PLC0415 - 故意延迟导入
    except Exception as exc:  # noqa: BLE001
        return None, "加载 02-tools/standings.py 失败：%s" % exc

    if os.path.normcase(os.path.abspath(getattr(module, "__file__", "") or "")) != key:
        module = _import_standings_file(path)   # 同名模块来自别处 → 按路径重来
        if module is None:
            return None, "加载 02-tools/standings.py 失败（同名模块冲突）"
    _STANDINGS_CACHE[key] = module
    return module, ""


# --------------------------------------------------------------------------
# 复盘渲染（格式必须与 serve.py::save_review 一致！）
# --------------------------------------------------------------------------
def render_review_md(p: ReviewDraft) -> str:
    """把 ReviewDraft 渲染成 04-review/*.md 的内容。"""

    def cell(value):
        return str(value if value not in (None, "") else "-")

    lines = []
    lines.append("# 复盘：%s — %s" % (p.name or "未命名", p.date))
    lines.append("")
    lines.append("> 由 QQ 机器人提交（`astrbot_plugin_xcpc`）。回来可以让我整理成正式复盘。")
    lines.append("")
    lines.append("## 0. 基本信息")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append("| 类型 | %s |" % cell(p.kind))
    lines.append("| 结果 | 排名 %s ｜ 过题 %s ｜ 罚时 %s |"
                 % (cell(p.rank), cell(p.solved), cell(p.penalty)))
    lines.append("| AC 顺序 | %s |" % cell(p.order))
    lines.append("| 我的贡献 | %s |" % cell(p.mine))
    lines.append("| 独立想出的题数 | %d |" % p.independent_count)
    lines.append("")

    lines.append("## 1. 逐题记录")
    lines.append("")
    if p.problems:
        lines.append("| 题 | 难度 | 卡点类型 | 我的贡献 | 补题 | 说明 |")
        lines.append("|---|---|---|---|---|---|")
        for row in p.problems:
            note = (row.note or "").replace("\n", "<br>").replace("|", "\\|")
            lines.append("| %s | %s | %s | %s | %s | %s |"
                         % (cell(row.label), cell(row.rating), cell(row.kind),
                            row.contrib or "—", row.upsolve or "—", note or "-"))
    else:
        lines.append("_（没填）_")
    lines.append("")

    lines.append("## 2. 我想歪的地方")
    lines.append("")
    lines.append(p.wrong or "_（没填）_")
    lines.append("")
    if p.pattern:
        lines.append("**偏航的共同模式**：%s" % p.pattern)
        lines.append("")

    lines.append("## 3. 可复用套路")
    lines.append("")
    lines.append(p.tricks or "_（没填）_")
    lines.append("")

    lines.append("## 4. 补题")
    lines.append("")
    lines.append(p.upsolve or "_（没填）_")
    lines.append("")
    return "\n".join(lines)


def safe_filename(name: str) -> str:
    """与 serve.py 一致的文件名净化规则。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "-", name or "未命名")[:40]


# --------------------------------------------------------------------------
# 复盘目录 → 个人 KPI（train_stats.summarize 的轻量版）
# --------------------------------------------------------------------------
def parse_review_file(path: str):
    """解析一份复盘 Markdown；不是复盘就返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return None

    first_line = content.split("\n", 1)[0].strip()
    m = TITLE_RE.match(first_line)
    if not m:
        return None

    rec = {
        "file": os.path.basename(path), "name": m.group(1).strip(),
        "date": m.group(2), "kind": "", "solved": None, "penalty": None,
        "rank": None, "mine": "", "problems": [],
    }
    in_table = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("| 题 ") or stripped.startswith("|题|"):
            in_table = True
            continue
        if in_table and set(stripped) <= set("|-: "):
            continue
        if in_table:
            row = PROB_ROW_RE.match(stripped)
            if row and row.group(1) not in ("题",):
                rating_raw = row.group(2).strip()
                rec["problems"].append({
                    "label": row.group(1).strip(),
                    "rating": int(rating_raw) if rating_raw.isdigit() else None,
                    "kind": row.group(3).strip(),
                    "contrib": row.group(4).strip(),
                    "upsolve": row.group(5).strip(),
                    "note": row.group(6).strip(),
                })
                continue
            in_table = False
        info = INFO_ROW_RE.match(stripped)
        if not info:
            continue
        key, value = info.group(1).strip(), info.group(2).strip()
        if key == "类型":
            rec["kind"] = value
        elif key == "我的贡献":
            rec["mine"] = "" if value == "-" else value
        elif key == "结果":
            for label, fld in (("过题", "solved"), ("罚时", "penalty"), ("排名", "rank")):
                found = re.search(label + r"\s*([0-9]+)", value)
                if found:
                    rec[fld] = int(found.group(1))

    rec["independent"] = sum(1 for x in rec["problems"] if x["contrib"] == CONTRIB_INDEP)
    rec["upsolve_done"] = [x for x in rec["problems"] if x["upsolve"] == UPSOLVE_DONE]
    rec["upsolve_p0p1"] = [x for x in rec["problems"] if x["upsolve"] in UPSOLVE_TODO]
    indep_ratings = [x["rating"] for x in rec["problems"]
                     if x["contrib"] == CONTRIB_INDEP and x["rating"]]
    rec["max_independent_rating"] = max(indep_ratings) if indep_ratings else None
    return rec


def review_kpis(review_dir: str) -> dict:
    """扫 04-review/ 算出个人 KPI。目录不存在就返回 sessions=0。"""
    if not os.path.isdir(review_dir):
        return {"sessions": 0}
    records = []
    for name in sorted(os.listdir(review_dir)):
        if not name.endswith(".md"):
            continue
        rec = parse_review_file(os.path.join(review_dir, name))
        if rec:
            records.append(rec)
    if not records:
        return {"sessions": 0}

    total = len(records)
    independent = [r["independent"] for r in records]
    done, todo = 0, 0
    for r in records:
        done += len(r["upsolve_done"])
        todo += len(r["upsolve_done"]) + len(r["upsolve_p0p1"])
    ceilings = [r["max_independent_rating"] for r in records
                if r["max_independent_rating"]]
    return {
        "sessions": total,
        "avg_independent": sum(independent) / total,
        "max_independent": max(independent) if independent else 0,
        "difficulty_ceiling": max(ceilings) if ceilings else None,
        "p0p1_total": todo,
        "p0p1_rate": (done / todo) if todo else 0.0,
        "with_contribution_recorded": sum(
            1 for r in records if r["independent"] or r["mine"]),
    }


# --------------------------------------------------------------------------
# 后端 1：直接读写工作区文件
# --------------------------------------------------------------------------
class WorkspaceFS:
    """AstrBot 与 XCPC 工作区在**同一台机器**时用这个后端。

    读写的就是 ``xcpc/`` 目录本身，不需要 serve.py 在跑。
    """

    backend_name = "file"

    def __init__(self, root: str, handle: str = ""):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.handle = handle

    # ---- 路径 ----
    @property
    def review_dir(self):
        return os.path.join(self.root, "04-review")

    @property
    def inbox_path(self):
        return os.path.join(self.root, "03-log", "inbox.md")

    @property
    def csv_path(self):
        return os.path.join(self.root, "03-log", "contests.csv")

    @property
    def sprint_path(self):
        return os.path.join(self.root, "00-plan", "sprint.md")

    @property
    def lists_dir(self):
        return os.path.join(self.root, "00-plan", "lists")

    def _json(self, *parts):
        path = os.path.join(self.root, *parts)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    # ---- 只读 ----
    def status(self) -> dict:
        info = self._json("data", "%s_info.json" % self.handle) or {}
        subs = self._json("data", "%s_status.json" % self.handle) or []
        profile = self._json("config", "cf_profile.json") or {}
        schedule = self._json("config", "schedule.json") or {}

        solved_api = count_solved(subs) if isinstance(subs, list) else 0
        streak = activity_streak(subs) if isinstance(subs, list) else activity_streak([])
        solved_profile = (profile.get("solved") or {})

        # 下一场比赛倒计时
        today = datetime.strptime(today_cn(), "%Y-%m-%d").date()
        next_contest = None
        for c in (schedule.get("contests") or []):
            raw = c.get("date") or c.get("start_date")
            if not raw:
                continue
            try:
                d = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            delta = (d - today).days
            if delta < 0:
                continue
            if next_contest is None or delta < next_contest["days"]:
                next_contest = {
                    "name": c.get("name") or "比赛",
                    "date": str(raw)[:10],
                    "days": delta,
                }

        return {
            "handle": info.get("handle") or self.handle,
            "rating": info.get("rating"),
            "max_rating": info.get("maxRating"),
            "rank": info.get("rank"),
            "solved_api": solved_api,
            "submissions": len(subs) if isinstance(subs, list) else 0,
            "solved_all_time": solved_profile.get("all_time"),
            "streak": streak,
            "next_contest": next_contest,
            "kpi": review_kpis(self.review_dir),
            "generated_at": now_cn().strftime("%Y-%m-%d %H:%M"),
            "root": self.root,
        }

    def today_tasks(self, day: str | None = None) -> dict:
        """返回今天那组任务。匹配规则与看板一致：标题里含 ``MM/DD``。"""
        day = day or today_cn()
        want = day[5:].replace("-", "/")           # 2026-10-06 -> 10/06
        norm = lambda s: re.sub(r"[\s*`（）()｜|]", "", str(s))  # noqa: E731
        groups = parse_checklist(self.sprint_path, "冲刺打卡")
        group = next((g for g in groups if norm(want) in norm(g["title"])), None)
        if group is None:
            group = next((g for g in groups
                          if any(not i["done"] for i in g["items"])), None)
        if group is None:
            return {"date": day, "title": "（没找到今天的安排）", "items": []}
        return {
            "date": day,
            "title": group["title"],
            "items": group["items"],
            "done": sum(1 for i in group["items"] if i["done"]),
            "total": len(group["items"]),
        }

    def problem_lists(self) -> list:
        """读 00-plan/lists/*.md，返回每份题单的标题和前若干道题。"""
        if not os.path.isdir(self.lists_dir):
            return []
        out = []
        for name in sorted(os.listdir(self.lists_dir)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(self.lists_dir, name)
            items = []
            title = name[:-3]
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        h = HEADING_RE.match(line)
                        if h and h.group(1) == "#" and len(h.group(1)) == 1:
                            title = strip_md(h.group(2))
                            continue
                        m = re.match(
                            r"^\|\s*\[([ xX])\]\s*\|\s*\*\*(\d+)\*\*\s*\|\s*"
                            r"\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]*?)\s*\|\s*"
                            r"(\d+)\s*\|\s*([^|]*?)\s*\|", line)
                        if m:
                            items.append({
                                "done": m.group(1).lower() == "x",
                                "rating": m.group(6),
                                "label": strip_md(m.group(3)),
                                "url": m.group(4),
                                "name": strip_md(m.group(5)),
                            })
            except OSError:
                continue
            if items:
                out.append({"title": title, "items": items})
        return out

    def read_inbox(self, limit: int = 8) -> list:
        if not os.path.exists(self.inbox_path):
            return []
        try:
            with open(self.inbox_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return []
        items = []
        for block in re.split(r"^##\s+", content, flags=re.M)[1:]:
            parts = block.split("\n", 1)
            stamp = parts[0].strip()
            body = (parts[1] if len(parts) > 1 else "").strip()
            if body:
                items.append({"time": stamp, "text": body})
        return items[-limit:]

    def today_reviewed(self, day: str | None = None) -> bool:
        """今天往 ``04-review/`` 里写过复盘没有（推送里那句催促要用）。"""
        want = day or today_cn()
        try:
            return bool(glob.glob(os.path.join(self.review_dir, "%s-*.md" % want)))
        except Exception:
            return False

    def parse_contest_text(self, text: str, per_fail: int | None = None) -> dict:
        """解析粘贴进来的**榜单 / 提交记录**（复用 ``02-tools/standings.py``）。

        为什么不内置一份解析器：那份已经写好、有 50+ 项自测、还被 serve.py 的
        ``/api/standings`` 和手机复盘页共用。抄一份过来 = 以后一定分叉
        （罚时规则、判题码、新平台格式……），而分叉的代价是他自己都说不清
        哪边的数字才是对的。

        下面的编排（谁优先、警告怎么拼）**与 serve.py 那个端点逐行对齐**，
        这样 file 和 http 两个后端给出的是同一个东西。那份代码写在 handler
        里面、import 不了，所以只能照抄一遍 ——
        **改 serve.py 的 ``/api/standings`` 时，这里要跟着改。**

        失败（老工作区没有 02-tools、文件坏了）只返回 ``ok: False``，
        **从不抛异常** —— 粘贴解析是加分项，不该让 /xcpc 复盘 记不下来。
        """
        pf = _as_int(per_fail) or DEFAULT_PER_FAIL
        module, err = _load_standings(self.root)
        if module is None:
            return empty_contest_result(err, self.backend_name, pf)
        try:
            rows, warn1 = module.parse(text)
            raw_items, warn2 = module.parse_submissions(text)
            timeline = module.build_timeline(raw_items) if raw_items else []
            summary = module.timeline_summary(raw_items) if raw_items else ""
            # AC 顺序和罚时都**优先从提交记录推**：榜单只给每题的最终状态，
            # 提交记录带时间，能算出"在 B 上挣扎了 25 分钟"这种形状。
            if raw_items:
                ac_order = module.ac_order_from_submissions(raw_items)
                penalty, _ = module.estimate_penalty(raw_items, per_fail=pf)
                breakdown = module.penalty_breakdown(raw_items, per_fail=pf)
            else:
                ac_order = module.to_ac_order(rows) if rows else ""
                penalty, breakdown = None, ""
        except Exception as exc:  # noqa: BLE001 - 解析器再烂也不能把指令搞崩
            return empty_contest_result("解析失败：%s" % exc, self.backend_name, pf)

        if rows and raw_items:
            kind = "both"
        elif raw_items:
            kind = "submissions"
        elif rows:
            kind = "standings"
        else:
            kind = "none"

        warnings = []
        if kind in ("standings", "both") and warn1:
            warnings += [w for w in warn1 if "（" not in w[:1]]
        if kind in ("submissions", "both") and warn2:
            warnings.append("有 %d 行没能理解（原样列出）：" % len(warn2))
            warnings += ["  " + s.strip()[:120] for s in warn2[:10] if s.strip()]
        if kind == "none":
            warnings = (warn1 or []) + ["没能认出榜单或提交记录。"]

        return normalize_contest_result({
            "kind": kind, "rows": rows, "timeline": timeline, "summary": summary,
            "warnings": warnings, "ac_order": ac_order, "penalty": penalty,
            "penalty_breakdown": breakdown, "per_fail": pf,
            "submission_count": len(raw_items),
        }, self.backend_name, pf)

    # ---- 只写 ----
    def append_inbox(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise ValueError("内容为空")
        stamp = now_cn().strftime("%Y-%m-%d %H:%M")
        os.makedirs(os.path.dirname(self.inbox_path), exist_ok=True)
        with open(self.inbox_path, "a", encoding="utf-8") as fh:
            fh.write("\n## %s\n\n%s\n" % (stamp, text))
        return stamp

    def save_review(self, draft: ReviewDraft) -> str:
        """写 04-review/<date>-<name>.md，并往 03-log/contests.csv 追加一行。"""
        if not draft.date:
            draft.date = today_cn()
        if not draft.name:
            draft.name = "未命名复盘"

        os.makedirs(self.review_dir, exist_ok=True)
        filename = "%s-%s.md" % (draft.date, safe_filename(draft.name))
        path = os.path.join(self.review_dir, filename)
        if os.path.exists(path):   # 同一天同名不覆盖
            filename = "%s-%s-%s.md" % (
                draft.date, safe_filename(draft.name), now_cn().strftime("%H%M%S"))
            path = os.path.join(self.review_dir, filename)

        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render_review_md(draft))

        # 追加比赛总表（列顺序见 03-log/contests.csv 表头）
        def cell(value):
            return str(value or "").replace(",", "，").replace("\n", " ")

        os.makedirs(os.path.dirname(self.csv_path), exist_ok=True)
        new_file = not os.path.exists(self.csv_path)
        with open(self.csv_path, "a", encoding="utf-8") as fh:
            if new_file:
                fh.write("date,kind,contest,team_or_solo,rank,solved,"
                         "total_problems,penalty,ac_order,my_contribution,notes\n")
            fh.write("%s,%s,%s,,%s,%s,,%s,%s,%s,来自 QQ 机器人\n" % (
                draft.date, cell(draft.kind), cell(draft.name), cell(draft.rank),
                cell(draft.solved), cell(draft.penalty), cell(draft.order),
                cell(draft.mine)))
        return filename


# --------------------------------------------------------------------------
# 后端 2：调 serve.py 的 HTTP 接口
# --------------------------------------------------------------------------
class XcpcHttp:
    """AstrBot 和 XCPC 工作区**不在同一台机器**时用这个后端。

    需要工作区那边开着 ``02-tools/serve.py``，并且 AstrBot 能访问到它。
    好处是工作区不用暴露文件系统；代价是必须有个常驻服务 + 那张机器得开机。

    只用到 serve.py 已有的接口，没有新增端点：
      GET  /api/data?k=<token>
      POST /api/review?k=<token>
      POST /api/note?k=<token>
      GET  /api/inbox?k=<token>
      POST /api/standings?k=<token>   解析粘贴的榜单/提交记录
    """

    backend_name = "http"

    def __init__(self, base: str, token: str = "", timeout: float = 20.0):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return "%s%s%s" % (self.base, path,
                           ("%sk=%s" % (sep, self.token)) if self.token else "")

    async def _request(self, method: str, path: str, payload=None):
        # 延迟导入：只有真的选 http 后端时才需要 aiohttp
        import aiohttp

        url = self._url(path)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            kwargs = {}
            if payload is not None:
                kwargs["json"] = payload
            async with sess.request(method, url, **kwargs) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError("HTTP %s: %s" % (resp.status, body[:200]))
                try:
                    return json.loads(body)
                except ValueError:
                    raise RuntimeError("返回值不是 JSON：%s" % body[:200])

    async def status(self) -> dict:
        data = await self._request("GET", "/api/data")
        return normalize_dashboard(data)

    async def today_tasks(self, day: str | None = None) -> dict:
        data = await self._request("GET", "/api/data")
        return normalize_today(data, day)

    async def problem_lists(self) -> list:
        data = await self._request("GET", "/api/data")
        return normalize_lists(data)

    async def read_inbox(self, limit: int = 8) -> list:
        data = await self._request("GET", "/api/inbox")
        return (data.get("items") or [])[-limit:]

    async def today_reviewed(self, day: str | None = None) -> bool:
        """跨机器时怎么知道"今天记过复盘"。

        ``/api/data`` 里带着 ``contests``（serve.py 那边的 ``03-log/contests.csv``
        解析结果），而写复盘一定会往那张表追加一行 —— 所以直接看表就行，
        **不需要新端点**。

        之前这里是缺口：``main.py`` 只会 glob 本地 ``04-review/``，
        而 http 后端没有 ``root``，于是**每晚都误报"今天还没记复盘"**。
        """
        data = await self._request("GET", "/api/data")
        return reviewed_today(data.get("contests"), day)

    async def parse_contest_text(self, text: str, per_fail: int | None = None) -> dict:
        """解析粘贴的榜单/提交记录 —— 走工作区那边**已经存在的**
        ``POST /api/standings``（没有为了这个功能给 serve.py 加任何代码）。

        跨机器时这份解析只能在那边跑（解析器和他工作区在一起），所以这里
        天然是"我发文本、它回结构"。

        和 file 后端一样**不抛异常**：那边没开机、token 过期、旧版本 serve.py
        没有这个端点，都只是"这次解析没成"，不该让 /xcpc 复盘 记不下来。
        """
        pf = _as_int(per_fail) or DEFAULT_PER_FAIL
        try:
            data = await self._request("POST", "/api/standings",
                                       {"text": text, "per_fail": pf})
        except Exception as exc:  # noqa: BLE001
            return empty_contest_result(
                "调复盘台的 /api/standings 失败：%s" % exc, self.backend_name, pf)
        return normalize_contest_result(data, self.backend_name, pf)

    async def append_inbox(self, text: str) -> str:
        await self._request("POST", "/api/note", {"text": text})
        return now_cn().strftime("%Y-%m-%d %H:%M")

    async def save_review(self, draft: ReviewDraft) -> str:
        payload = {
            "name": draft.name or "未命名复盘",
            "date": draft.date or today_cn(),
            "kind": draft.kind,
            "solved": draft.solved,
            "penalty": draft.penalty,
            "rank": draft.rank,
            "order": draft.order,
            "mine": draft.mine,
            "wrong": draft.wrong,
            "pattern": draft.pattern,
            "tricks": draft.tricks,
            "upsolve": draft.upsolve,
            "problems": [
                {"label": p.label, "rating": p.rating, "kind": p.kind,
                 "contrib": p.contrib, "upsolve": p.upsolve, "note": p.note}
                for p in draft.problems
            ],
        }
        data = await self._request("POST", "/api/review", payload)
        return data.get("file") or "(未知文件名)"


# --------------------------------------------------------------------------
# 把 /api/data 的原始结构压成和 WorkspaceFS.status() 一样的形状
# --------------------------------------------------------------------------
def normalize_dashboard(data: dict) -> dict:
    prof = data.get("profile") or {}
    totals = data.get("totals") or {}
    schedule = data.get("schedule") or {}
    cfp = data.get("cf_profile") or {}
    per = data.get("personal") or {}
    today = datetime.strptime(today_cn(), "%Y-%m-%d").date()

    next_contest = None
    for c in (schedule.get("contests") or []):
        raw = c.get("date") or c.get("start_date")
        if not raw:
            continue
        try:
            d = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        delta = (d - today).days
        if delta < 0:
            continue
        if next_contest is None or delta < next_contest["days"]:
            next_contest = {"name": c.get("name") or "比赛",
                            "date": str(raw)[:10], "days": delta}

    return {
        "handle": data.get("handle"),
        "rating": prof.get("rating"),
        "max_rating": prof.get("max_rating"),
        "rank": prof.get("rank"),
        "solved_api": totals.get("solved"),
        "submissions": totals.get("submissions"),
        "solved_all_time": (cfp.get("solved") or {}).get("all_time"),
        "streak": data.get("streak") or {},
        "next_contest": next_contest,
        "kpi": per,
        "generated_at": data.get("generated_at"),
        "root": None,
    }


def normalize_today(data: dict, day: str | None = None) -> dict:
    day = day or today_cn()
    want = day[5:].replace("-", "/")
    norm = lambda s: re.sub(r"[\s*`（）()｜|]", "", str(s))  # noqa: E731
    groups = (data.get("sprint") or []) + (data.get("protocol") or [])
    group = next((g for g in groups if norm(want) in norm(g.get("title"))), None)
    if group is None:
        group = next((g for g in groups
                      if any(not i.get("done") for i in g.get("items", []))), None)
    if group is None:
        return {"date": day, "title": "（没找到今天的安排）", "items": []}
    items = group.get("items") or []
    return {"date": day, "title": group.get("title") or "",
            "items": items, "done": sum(1 for i in items if i.get("done")),
            "total": len(items)}


def normalize_lists(data: dict) -> list:
    out = []
    for lst in (data.get("problem_lists") or []):
        out.append({"title": lst.get("title") or "题单",
                    "items": lst.get("items") or []})
    return out
