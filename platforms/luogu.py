#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""洛谷适配器。

实测结论（2026-10）
------------------
* 直接请求 → 345 字节的 JS 挑战页：
      window.document.cookie="C3VK=c82bf1; path=/; max-age=300;"
  **cookie 值明文写在里面** —— 不需要浏览器。
  `core/http.py` 会自动解出并重试，本模块不用管。
* 过了 C3VK 之后：
  * `GET /problem/P1001?_contentOnly=1` → **200 / 42 KB，能拿到 difficulty 和 tags**
  * `GET /record/list` → **401**（要登录）
  * `GET /auth/login` → 200 但只有 3027 字节、标题 `Welcome - Luogu Spilopelia`
    —— **这是第二层门，我还没破**（见下面 `login()` 的说明）

难度尺度
--------
洛谷是 **1–7 档**（入门/普及-/普及/普及+/提高+/省选/NOI），
和 CF rating、AtCoder IRT **完全是三套尺子**。所以标 `luogu_level`，
跨平台比较时用"档位"而不是数值。
"""

from __future__ import annotations

import json
import re

from .base import Fetched, Problem, Submission

ORIGIN = "https://www.luogu.com.cn"

# 洛谷 1–7 档的名字（`difficulty` 字段就是这个数字）
LEVEL_NAMES = {
    0: "暂无评定", 1: "入门", 2: "普及-", 3: "普及/提高-",
    4: "普及+/提高", 5: "提高+/省选-", 6: "省选/NOI-", 7: "NOI/NOI+/CTSC",
}

# 数据容器。**实测（2026-10）**：
#     <script id="lentille-context" type="application/json">{"data":{"problem":{...}}}</script>
# 34 KB 干净 JSON。
#
# ⚠️ **不要用"正则去 HTML 里捞 difficulty"这种做法**。
# 同一页的 `recommendations` 数组里装着**别的题**的 difficulty，
# 乱捞必然捞到推荐题的难度 —— 而且值看起来完全合理（都是 1-7 的档位），
# 属于最难发现的那类错误。必须认准容器。
_CONTEXT_RE = re.compile(
    r'<script[^>]*id="lentille-context"[^>]*>(.*?)</script>', re.S)

# 标签表（ID → 名字）。`window.__luoguTagRequest = '/_lfe/tags'`
_TAG_URL = ORIGIN + "/_lfe/tags"

# 「Welcome - Luogu Spilopelia」= 第二层挑战页，不是登录表单
_WELCOME_RE = re.compile(r"(?i)<title>\s*Welcome\s*-\s*Luogu", re.I)


class Luogu:
    name = "luogu"
    supports_submissions = True
    supports_contests = False       # 洛谷的比赛记录没找到可用入口
    supports_problems = True
    min_interval = 1.5              # 有 CDN 反爬，慢一点

    @staticmethod
    def make_client(recorder=None, rate_scale: float = 1.0):
        """造一个带洛谷专用头的 client。

        ⚠️ **`?_contentOnly=1` 光有查询参数不够** —— 实测直接请求会返回
        完整的 HTML（42687 字节，`<!DOCTYPE html>` 开头），
        真正的 JSON 接口需要**同时带上 `x-luogu-type: content-only` 请求头**。
        这是我踩过的坑：只加参数的话，正则去 HTML 里捞 JSON 会捞到
        "推荐题目"里的别的题的 difficulty，**静默给出错误的难度**。
        """
        from ..core import http as httpm
        return httpm.HttpClient(
            platform="luogu", recorder=recorder, min_interval=Luogu.min_interval,
            rate_scale=rate_scale,
            extra_headers={"x-luogu-type": "content-only"},
        )

    @staticmethod
    def problem_key(pid: str) -> str:
        pid = (pid or "").strip().upper()
        return "LG:%s" % pid

    @staticmethod
    def parse_problem_page(html: str):
        """从题面页里解出 `{pid, name, difficulty, tags}`。

        分开成静态方法是为了**能离线测** —— 不用联网就能验证解析对不对。
        返回 `None` 表示认不出结构（页面改版或不是题面页）。
        """
        m = _CONTEXT_RE.search(html or "")
        if not m:
            return None
        try:
            payload = json.loads(m.group(1))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        problem = ((payload.get("data") or {}).get("problem")) or {}
        if not isinstance(problem, dict) or not problem:
            return None

        diff = problem.get("difficulty")
        try:
            diff = int(diff) if diff is not None else None
        except (TypeError, ValueError):
            diff = None

        raw_tags = problem.get("tags")
        # ⚠️ 洛谷的 tags 是**数字 ID**（比如 `[1]`），不是名字。
        # 直接塞给上层的话，用户会看到"标签 1"，毫无意义。
        tag_ids = [t for t in raw_tags if isinstance(t, int)] if isinstance(raw_tags, list) else []

        return {
            "pid": str(problem.get("pid") or ""),
            "name": str(problem.get("name") or ""),
            "difficulty": diff,
            "tag_ids": tag_ids,
            "tags": None,          # 等拿到标签表再填
        }

    async def fetch_tag_map(self, client) -> dict:
        """拉标签表：`{id: 名字}`。

        **实测结构（2026-10）**：
            {"tags":[{"id":1,"name":"模拟","type":2,"parent":110}, ...],
             "types":[...], "_locale":"zh-CN", "_version":...}

        ⚠️ 外面**包了一层 `tags` 键**。我第一版只处理"裸列表"和
        "`{id: name}` 字典"两种形式，结果 32 KB 的响应一条都没解析出来，
        `tags` 全是 None —— 看起来像"洛谷不给标签"，其实是结构没认对。

        拿不到就返回空 dict —— 上层会把 tags 留成 None（"给不出"），
        **而不是编一个名字**。
        """
        try:
            resp = await client.get(_TAG_URL)
            data = resp.json()
        except Exception:
            return {}

        # 把几种可能的形式都摊平成一个 list of {id, name}
        items = []
        if isinstance(data, dict):
            inner = data.get("tags")
            if isinstance(inner, list):
                items = inner
            elif isinstance(inner, dict):
                for k, v in inner.items():
                    try:
                        items.append({"id": int(k),
                                      "name": v.get("name") if isinstance(v, dict) else str(v)})
                    except (TypeError, ValueError):
                        continue
            else:
                # 退一步：顶层就是 {id: ...}
                for k, v in data.items():
                    try:
                        items.append({"id": int(k),
                                      "name": v.get("name") if isinstance(v, dict) else str(v)})
                    except (TypeError, ValueError):
                        continue
        elif isinstance(data, list):
            items = data

        out = {}
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                continue
            try:
                tid = int(item["id"])
            except (TypeError, ValueError):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                out[tid] = name
        return out

    async def fetch_problem(self, pid: str, client,
                            tag_map: dict | None = None) -> Fetched:
        """拿单题的难度和标签。

        实测（2026-10）：过了 C3VK 之后
            `GET /problem/P1001?_contentOnly=1` → 200 / 42 KB，
            数据在 `<script id="lentille-context" type="application/json">` 里。
        """
        if not pid:
            return Fetched(ok=False, error_kind="内部错误", detail="没给题号")
        resp = await client.get("%s/problem/%s?_contentOnly=1" % (ORIGIN, pid))
        if _WELCOME_RE.search(resp.text[:1000]):
            return Fetched(ok=False, error_kind="挑战未过",
                           detail="被第二层挑战页挡住（C3VK 已过但还不够）")
        if resp.status == 401:
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="需要登录（HTTP 401）")
        if not resp:
            return Fetched(ok=False, error_kind="网络不可达",
                           detail="HTTP %d" % resp.status)

        parsed = self.parse_problem_page(resp.text)
        if parsed is None:
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="题面里找不到 lentille-context 数据块"
                                  "（洛谷可能改版了）")

        tags = None
        if parsed["tag_ids"]:
            tmap = tag_map if tag_map is not None else await self.fetch_tag_map(client)
            names = [tmap.get(i) for i in parsed["tag_ids"]]
            names = [n for n in names if n]
            if names:
                tags = names
            # 拿不到名字就留 None（"给不出"），**不编**

        return Fetched(items=[Problem(
            platform=self.name,
            problem_key=self.problem_key(parsed["pid"] or pid),
            title=parsed["name"][:200],
            tags=tags,
            difficulty=parsed["difficulty"],
            difficulty_source="luogu_level" if parsed["difficulty"] is not None
            else "unknown",
        )], ok=True)

    # ---- 登录（第二层门，未破）------------------------------------------
    async def login(self, username: str, password: str, client) -> Fetched:
        """洛谷的登录**还没打通**，这里如实说明，不假装成功。

        现状：过了 C3VK 之后 `/auth/login` 返回 3027 字节的
        `Welcome - Luogu Spilopelia`，不是登录表单 —— 说明还有一层
        我没识别出来的门（可能是另一个 cookie、或者需要先访问预热页）。

        所以现阶段洛谷走**手动导入 Cookie**（`/credentials` 接口）：
        用户在浏览器里登录后复制 Cookie 贴进来。
        **这是设计内的路径，不是降级** —— 它同样能拿到完整数据。
        """
        return Fetched(
            ok=False, error_kind="挑战未过",
            detail=("洛谷自动登录还没打通（第二层挑战页未识别）。"
                    "请用「手动导入 Cookie」：浏览器登录洛谷后，"
                    "从开发者工具里复制 Cookie 贴到插件页面。"))

    # ---- 提交（需登录）--------------------------------------------------
    async def fetch_submissions(self, uid: str, since_epoch: int | None = None,
                                client=None) -> Fetched:
        if not client or not client.cookies:
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="洛谷需要登录（或导入 Cookie）才能看提交记录")
        if not uid:
            return Fetched(ok=False, error_kind="凭据失效", detail="没填洛谷 uid")

        resp = await client.get("%s/record/list?user=%s&_contentOnly=1" % (ORIGIN, uid))
        if resp.status == 401:
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="Cookie 已失效（HTTP 401），请重新导入")
        if _WELCOME_RE.search(resp.text[:1000]):
            return Fetched(ok=False, error_kind="挑战未过",
                           detail="被第二层挑战页挡住")
        if not resp:
            return Fetched(ok=False, error_kind="网络不可达",
                           detail="HTTP %d" % resp.status)

        data = self._extract_json(resp.text)
        if not isinstance(data, dict):
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="记录页里找不到内嵌 JSON")

        records = ((data.get("currentData") or {}).get("records")) or {}
        rows = records.get("result") if isinstance(records, dict) else None
        if not isinstance(rows, list):
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="JSON 结构变了（找不到 records.result）")

        out = []
        for r in rows:
            prob = r.get("problem") or {}
            pid = str(prob.get("pid") or "")
            if not pid:
                continue
            diff = prob.get("difficulty")
            try:
                diff = int(diff) if diff is not None else None
            except (TypeError, ValueError):
                diff = None
            out.append(Submission(
                platform=self.name,
                submission_id=str(r.get("id") or ""),
                problem_key=self.problem_key(pid),
                verdict=str(r.get("status") if r.get("status") is not None else ""),
                epoch=int(r.get("submitTime") or 0),
                language="",
                difficulty=diff,
                difficulty_source="luogu_level" if diff is not None else "unknown",
            ))
        return Fetched(items=out, ok=True)

    async def fetch_contests(self, uid: str, client=None) -> Fetched:
        return Fetched(ok=False, error_kind="页面结构变化",
                       detail="洛谷的比赛记录还没做（暂不支持）")
