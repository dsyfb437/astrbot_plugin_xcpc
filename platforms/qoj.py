#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QOJ 适配器。

实测结论（2026-10）
------------------
* `GET /` 和 `GET /problems` → **200**（用浏览器 UA；`curl/x.y` 的 UA 会被
  Cloudflare 拦成 403 "Just a moment..."）
* `GET /submissions`、`/contest/N/standings`、`/contest/N/submissions`
  → 全部返回**同一份 12224 字节的登录页**（`<title>Login - QOJ.ac</title>`，0 个表格行）
  → 也就是说：**提交和榜单必须登录**。
* `robots.txt` 另写着 `Disallow: /submission/` 和 `Disallow: /hack/`

登录流程（已完全摸清）
---------------------
1. `GET /login` → 200，页面里有 `<form id="form-login" method="post">`
2. 页面内联 JS 里带 CSRF：`$.post('/login', { _token: "Q4Aw…", ... })`
3. `POST /login` 带 `{_token, username, password}`
4. 若服务端返回 `msg == '2fa'`，需要再走 `/login/2fa`

本模块的立场
------------
**不绕过登录、不伪装、不猜。** 拿不到就明确报"需要登录"，
让上层去要凭据 —— 而不是返回空列表让人以为"这人没提交过"。
"""

from __future__ import annotations

import calendar
import re
from urllib.parse import quote

from .base import Fetched, Problem, Submission

ORIGIN = "https://qoj.ac"

# 会话 cookie 名。**不是 `__client_id`** —— 那是洛谷的，QOJ 上没有这个东西。
# 上游 `vfleaking/uoj` 的 `web/app/models/Session.php` 里是
# `session_name('UOJSESSID')`；qoj.ac 部署时加了 `__Host-` 前缀，实发头就是
# `set-cookie: __Host-UOJSESSID=…; path=/; secure; HttpOnly; SameSite=Lax`。
# 两个名字都认，别人自建的 UOJ 可能没前缀。


def _has_session_cookie(jar: dict) -> bool:
    for key in jar or {}:
        if key in ("__Host-UOJSESSID", "UOJSESSID") or str(key).endswith("UOJSESSID"):
            return True
    return False


# 登录页里的 CSRF token
_TOKEN_RE = re.compile(r"""_token\s*:\s*["']([^"']{8,200})["']""")
# 登录页判定（所有数据页未登录时返回的都是它）
_LOGIN_TITLE_RE = re.compile(r"(?i)<title>\s*Login\s*-\s*QOJ", re.I)
# /problems 页里的题目行
_PROBLEM_ROW_RE = re.compile(r'href="/problem/(\d+)"[^>]*>([^<]{0,200})<')


# ---- 提交表格的解析零件 ------------------------------------------------
#
# 列名明写在 `<th>` 里。实测（2026-10-08）：
#     ID | Problem | Submitter | Result | Time | Memory | Language |
#     File size | Submit time
# **按名字查列号，不按次序猜** —— QOJ 哪天调换列序，我们不该跟着开始存错数据。
_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# 真正的评测结果：`<a class="uoj-score" data-full="100.0" data-score="100.0">AC ✓</a>`
# 这是 QOJ 自己给的语义标记，比"第几列"可靠得多。
_SCORE_RE = re.compile(
    r'<a(?P<attrs>[^>]*\bclass="uoj-score"[^>]*)>(?P<text>.*?)</a>', re.S)
_SCORE_ATTR_RE = re.compile(r'data-(score|full)="([^"]*)"')
# 提交时间：`<time class="uoj-time" datetime="2026-10-08T08:13:48+08:00">`
_TIME_RE = re.compile(r'class="uoj-time"[^>]*datetime="([^"]+)"')
# 题目链接：`/problem/12371`，以及比赛里的 `/contest/3504/problem/16831`
_PROBLEM_LINK_RE = re.compile(r'href="(?:/contest/\d+)?/problem/(\d+)"')
# 分页：页面自己给出的 `?page=N` 链接才是"还有下一页"的权威信号 ——
# 比"一页固定 20 条"这种假设可靠（实测那页只有 10 行）。
_NEXT_PAGE_RE = "[?&]page=%d\\b"
# `2026-10-08T08:13:48+08:00` / `2026-10-08 08:13:48`
_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:?\d{2})?")


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html or "")).strip()


def _cells(tr: str) -> list[str]:
    return [_strip_tags(c) for c in _TD_RE.findall(tr or "")]


def _header_columns(html: str) -> dict[str, int]:
    """表头 `<th>` 的列名 → 列号（小写）。没有表头就返回 `{}`。"""
    names = [_strip_tags(th).lower() for th in _TH_RE.findall(html or "")]
    return {n: i for i, n in enumerate(names) if n}


def _has_next_page(html: str, page: int) -> bool:
    return bool(re.search(_NEXT_PAGE_RE % (page + 1), html or ""))


def _parse_iso(s: str) -> int:
    """`2026-10-08T08:13:48+08:00` → epoch 秒。认不出返回 0。

    ⚠️ **必须减掉时区偏移**。QOJ 发的是 `+08:00`，直接 `timegm` 会
    把时间整体推后 8 小时 —— 跨天的提交会落到错误的那一天，
    而"今天做了几道题"这类统计正好按天切。
    """
    m = _ISO_RE.search(s or "")
    if not m:
        return 0
    y, mo, d, h, mi, sec, tz = m.groups()
    try:
        base = calendar.timegm((int(y), int(mo), int(d), int(h),
                                int(mi), int(sec), 0, 0, 0))
    except (TypeError, ValueError):
        return 0
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        base -= sign * (int(tz[1:3]) * 3600 + int(tz[-2:]) * 60)
    return base


def _qoj_verdict(text: str, score: str = "", full: str = "") -> str:
    """结果单元格 → 统一词表（`AC` / `WA` / `TL` / `RE` / `CE`…）。

    UOJ 的结果格是短词（`AC ✓` 带一个装饰性对勾）。计分题可能只给分数
    不给词 —— 那种情况才退回用 `data-score`/`data-full` 判满分。
    **绝不去猜"哪个单元格像结果"**：那正是旧版把提交者用户名
    当成评测结果的原因。
    """
    t = _strip_tags(text).replace("✓", "").strip()
    if t:
        up = t.upper()
        return "AC" if up.startswith("AC") else up[:20]
    try:
        s, f = float(score or 0), float(full or 0)
    except (TypeError, ValueError):
        return ""
    return "AC" if (f > 0 and s >= f) else ""


class Qoj:
    name = "qoj"
    supports_submissions = True     # 登录之后可以
    supports_contests = True
    supports_problems = True
    min_interval = 1.0

    # ---- 判断"这是不是登录页" -------------------------------------------
    @staticmethod
    def looks_like_login(resp) -> bool:
        """所有数据页在未登录时返回同一份登录页 —— 必须识别出来。

        **这是最关键的一处**：如果不识别，抓提交会得到 0 条，
        上层会显示"你还没提交过"，而真相是"没登录"。
        """
        try:
            text = resp.body[:4000].decode("utf-8", errors="replace")
        except Exception:
            return False
        return bool(_LOGIN_TITLE_RE.search(text))

    # ---- 登录 -----------------------------------------------------------
    async def fetch_login_token(self, client) -> str:
        """拿登录页里的 CSRF token。"""
        resp = await client.get(ORIGIN + "/login")
        text = resp.text
        m = _TOKEN_RE.search(text)
        return m.group(1) if m else ""

    async def login(self, username: str, password: str, client,
                    token2fa: str = "") -> Fetched:
        """走登录流程。

        返回 `ok=True` 表示拿到了会话 cookie。
        若需要两步验证，返回 `ok=False, error_kind='凭据失效'` 且
        `detail` 里带 `need_2fa` 标记，让上层去弹输入框。
        """
        if not username or not password:
            return Fetched(ok=False, error_kind="凭据失效", detail="用户名或密码为空")

        token = await self.fetch_login_token(client)
        if not token:
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="登录页里找不到 _token（QOJ 可能改版了）")

        payload = {"_token": token, "username": username, "password": password}
        if token2fa:
            payload["token"] = token2fa
        resp = await client.post_form(ORIGIN + "/login", payload)

        # 成功与否看**有没有拿到会话 cookie**，而不是看状态码 ——
        # QOJ 登录失败也回 200，只在 JSON 里给 msg。
        if _has_session_cookie(client.cookies):
            return Fetched(ok=True, detail="登录成功")

        body = resp.text[:500]
        if "2fa" in body.lower():
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="need_2fa：这个账号开了两步验证，需要验证码")
        if "failed" in body.lower() or "invalid" in body.lower():
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="need_credentials：用户名或密码不对")
        return Fetched(ok=False, error_kind="凭据失效",
                       detail="登录没有拿到会话 cookie（原因不明，请看日志）")

    # ---- 提交 -----------------------------------------------------------
    #
    # ⚠️ 这一节 2026-10-08 **整个重写过**。旧版存进去的每一条都是错的，
    # 而且错得看不出来 —— 数据进库了、条数也在涨，只是**全是别人的**：
    #
    # 1. **漏了 `?submitter=`。** `GET /submissions` 是 QOJ 的**全站最近提交**，
    #    不是"我的"。实测那一页 10 行来自 10 个不同的提交者
    #    （lrmlrm、pino、Crazyouth、Tbat…），**一个都不是用户**。
    #    "只看我的"链接是 QOJ 自己在分页栏里给的
    #    `/submissions?submitter=<用户名>` —— 官方口径，不是我猜的。
    # 2. **verdict 从错的列拿。** 旧代码是"在 `<td>` 里找第一个像英文单词的
    #    单元格"。表格列是
    #    `ID | Problem | Submitter | Result | Time | Memory | Language | File size | Submit time`，
    #    前两个以 `#` 开头不匹配，于是**第三列 `Submitter` 永远先命中** ——
    #    库里那 31 行的"评测结果"是 `ppip`、`ZhaoZiLong` 这些**人名**；
    #    提交者那格为空时才往后掉到 `Language`（`C++26`）。
    #    真正的结果是 `<a class="uoj-score" data-score="100.000000">AC ✓</a>`。
    # 3. **`epoch=0`。** 旧注释写"列表页不给绝对时间"——**给**：
    #    `<time class="uoj-time" datetime="2026-10-08T08:13:48+08:00">`。
    #    时间全 0 等于把所有 QOJ 提交堆在 1970 年。
    # 4. 题目链接有两种：`/problem/12371` 和 `/contest/3504/problem/16831`
    #    （比赛里的题），旧正则只认前一种。
    # 5. "**不分页**"也是错的：页面上有 `?page=2` … `?page=19`。
    #
    # 前三条都属于"看起来一切正常，其实数据是假的"，所以现在**按表头定位列**
    # （`<th>` 里明写着列名），加 QOJ 自己的 `uoj-score` / `uoj-time` 语义标记，
    # 不再靠"猜哪个单元格像结果"。
    _MAX_PAGES = 30

    async def fetch_submissions(self, handle: str, since_epoch: int | None = None,
                                client=None) -> Fetched:
        """抓**自己的**提交。必须已登录，否则明确报错。"""
        if client is None:
            return Fetched(ok=False, error_kind="内部错误", detail="没有传入 client")
        if not client.cookies:
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="QOJ 需要登录才能看提交记录，请先在插件页面登录")
        handle = str(handle or "").strip()
        if not handle:
            # 没用户名就没法过滤，而**不过滤拿到的是全站数据**。
            # 宁可报错，也不能把别人的记录当成他的 —— 这正是旧版的病。
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="没填 QOJ 用户名：不加 ?submitter= 抓回来的是"
                                  "全站提交，不是你的。去绑定页把用户名填上。")

        out: list[Submission] = []
        truncated = False
        # 游标取"见过的"最新时间，**每一行都要更新**（包括被 since 挡掉的），
        # 否则增量同步时游标会退化成 0，下次又全量重拉。
        newest = 0

        for page in range(1, self._MAX_PAGES + 1):
            resp = await client.get(
                "%s/submissions?submitter=%s&page=%d"
                % (ORIGIN, quote(handle, safe=""), page))
            if self.looks_like_login(resp):
                return Fetched(ok=False, error_kind="凭据失效",
                               detail="会话已失效（被重定向到登录页），请重新登录")
            if not resp:
                return Fetched(ok=False, error_kind="网络不可达",
                               detail="HTTP %d" % resp.status)

            rows = self._parse_submission_rows(resp.text)
            if rows is None:
                # 表头都没有 = 页面结构变了。**这和"零提交"必须分开**，
                # 否则改版会被误报成"你没交过题"。
                return Fetched(ok=False, error_kind="页面结构变化",
                               detail="提交页里找不到表头（QOJ 可能改版了）")
            if not rows:
                break                     # 表头在、这页空 = 真的到底了

            for s in rows:
                newest = max(newest, s.epoch or 0)
                if since_epoch and s.epoch and s.epoch <= since_epoch:
                    continue
                out.append(s)

            # 增量：这一页整页都比游标旧（列表从新到旧排）就可以收工
            if since_epoch and all((s.epoch or 0) <= since_epoch for s in rows):
                break
            # 有没有下一页，看页面自己有没有给 `page=N+1` 的链接 ——
            # 不靠"一页固定 20 条"这种假设。
            if not _has_next_page(resp.text, page):
                break
        else:
            truncated = True              # for 跑完没 break = 撞到页数上限

        return Fetched(items=out, ok=True,
                       cursor=max(newest, since_epoch or 0), truncated=truncated)

    @staticmethod
    def _parse_submission_rows(html: str) -> list[Submission] | None:
        """解析提交表格。

        返回 `None` = **连表头都没找到**（页面结构变了）；
        返回 `[]`   = 表头在，只是这页没有数据行。
        上层靠这个区分"改版了"和"他真的没交过" —— 旧版把两者
        混成一句"解析不出任何行"，改版会被说成"零提交"。

        列的位置**按表头名字查**，不按次序猜：QOJ 调换列序不该让
        我们开始存错数据。
        """
        cols = _header_columns(html)
        if not cols:
            return None

        out: list[Submission] = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            sub_m = re.search(r'href="/submission/(\d+)"', tr)
            prob_m = _PROBLEM_LINK_RE.search(tr)
            if not sub_m or not prob_m:
                continue

            cells = _cells(tr)
            # 结果优先认 QOJ 自己的 `class="uoj-score"` —— 这是唯一
            # 无歧义的标记；万一哪天没了，再退回"表头说是 Result 的那一列"。
            score = full = ""
            sm = _SCORE_RE.search(tr)
            if sm:
                attrs = dict(_SCORE_ATTR_RE.findall(sm.group("attrs")))
                score, full = attrs.get("score", ""), attrs.get("full", "")
                raw = sm.group("text") or ""
            else:
                idx = cols.get("result")
                raw = cells[idx] if idx is not None and idx < len(cells) else ""

            # 时间：`<time class="uoj-time" datetime="2026-10-08T08:13:48+08:00">`
            epoch = 0
            tm = _TIME_RE.search(tr)
            if tm:
                epoch = _parse_iso(tm.group(1))
            if not epoch:
                idx = cols.get("submit time")
                if idx is not None and idx < len(cells):
                    epoch = _parse_iso(cells[idx])

            out.append(Submission(
                platform="qoj",
                submission_id=sub_m.group(1),
                problem_key="QOJ:%s" % prob_m.group(1),
                verdict=_qoj_verdict(raw, score, full),
                epoch=epoch,
                language=(cells[cols["language"]]
                          if cols.get("language") is not None
                          and cols["language"] < len(cells) else ""),
                difficulty=None,
                difficulty_source="qoj_none",   # QOJ 不公开难度
            ))
        return out

    # ---- 比赛记录 -------------------------------------------------------
    async def fetch_contests(self, handle: str, client=None) -> Fetched:
        if client is None or not client.cookies:
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="QOJ 需要登录才能看比赛记录")
        return Fetched(ok=False, error_kind="页面结构变化",
                       detail="比赛记录解析还没实现（第 6 期）")

    # ---- 题库 -----------------------------------------------------------
    async def fetch_problems(self, client=None) -> Fetched:
        """`/problems` **免登录可拿**（实测 200 / 50 KB）。

        只解析得出题号和标题 —— QOJ 不公开难度和标签，所以
        `difficulty=None, tags=None`（**不编**）。
        """
        resp = await client.get(ORIGIN + "/problems")
        if not resp:
            return Fetched(ok=False, error_kind="网络不可达",
                           detail="HTTP %d" % resp.status)
        if self.looks_like_login(resp):
            return Fetched(ok=False, error_kind="挑战未过",
                           detail="被要求登录（意料之外，/problems 本应公开）")

        seen: dict[str, Problem] = {}
        for pid, title in _PROBLEM_ROW_RE.findall(resp.text):
            key = "QOJ:%s" % pid
            if key in seen:
                continue
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                continue
            seen[key] = Problem(
                platform="qoj", problem_key=key, title=title[:200],
                tags=None, difficulty=None, difficulty_source="qoj_none")
        if not seen:
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="/problems 页里解析不出题目链接")
        return Fetched(items=list(seen.values()), ok=True)
