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

import re

from .base import Fetched, Problem, Submission

ORIGIN = "https://qoj.ac"

# 登录页里的 CSRF token
_TOKEN_RE = re.compile(r"""_token\s*:\s*["']([^"']{8,200})["']""")
# 登录页判定（所有数据页未登录时返回的都是它）
_LOGIN_TITLE_RE = re.compile(r"(?i)<title>\s*Login\s*-\s*QOJ", re.I)
# /problems 页里的题目行
_PROBLEM_ROW_RE = re.compile(r'href="/problem/(\d+)"[^>]*>([^<]{0,200})<')


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
        if client.cookies.get("__client_id") or client.cookies.get("uoj_username"):
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
    async def fetch_submissions(self, handle: str, since_epoch: int | None = None,
                                client=None) -> Fetched:
        """抓自己的提交。**必须已登录**，否则明确报错。"""
        if client is None:
            return Fetched(ok=False, error_kind="内部错误", detail="没有传入 client")
        if not client.cookies:
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="QOJ 需要登录才能看提交记录，请先在插件页面登录")

        resp = await client.get(ORIGIN + "/submissions")
        if self.looks_like_login(resp):
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="会话已失效（被重定向到登录页），请重新登录")

        rows = self._parse_submission_rows(resp.text)
        if not rows:
            # 登录了但一行都解析不出来 → 多半是改版了，**不能当成"零提交"**
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="登录成功但提交页解析不出任何行（QOJ 可能改版）")
        return Fetched(items=rows, ok=True)

    @staticmethod
    def _parse_submission_rows(html: str) -> list[Submission]:
        """解析提交表格。

        这里刻意写得**保守**：只认明确能对上的结构，认不出就返回空，
        让上层报"页面结构变化"。宁可报错，也不要猜出一堆错的记录。
        """
        out: list[Submission] = []
        # QOJ 的提交表格：<tr> 里有 <a href="/submission/N"> 和 <a href="/problem/M">
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            sub_m = re.search(r'href="/submission/(\d+)"', tr)
            prob_m = re.search(r'href="/problem/(\d+)"', tr)
            if not sub_m or not prob_m:
                continue
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            verdict = ""
            for c in cells:
                if c and re.fullmatch(r"[A-Za-z][A-Za-z0-9 _()+-]{1,30}", c):
                    verdict = c
                    break
            out.append(Submission(
                platform="qoj",
                submission_id=sub_m.group(1),
                problem_key="QOJ:%s" % prob_m.group(1),
                verdict=verdict,
                epoch=0,            # 列表页不给绝对时间，需要进详情页 —— 先留 0
                language="",
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
