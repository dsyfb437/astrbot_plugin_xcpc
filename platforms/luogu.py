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

# 「Welcome - Luogu Spilopelia」曾经被我当成"第二层挑战页"的特征。
#
# ⚠️ **那是误判（2026-10-08 实测）**：这个标题是洛谷 SPA 外壳对
# `record.list` 这类模板渲染的**默认标题**，正常的记录页也长这样。
#     正常记录页：17097 字节，带 `lentille-context`，`data.records.count = 733`
#     真风控页：  ~3027 字节，**没有** `lentille-context`
# 光看标题会把整页好数据丢掉 —— 洛谷同步因此一条都拉不到，
# 还报成"被第二层挑战页挡住"，看着像风控，其实是自己的判据错了。
_WELCOME_RE = re.compile(r"(?i)<title>\s*Welcome\s*-\s*Luogu", re.I)


def _context_payload(html: str) -> dict | None:
    """把 `<script id="lentille-context">` 里的 JSON 解出来。

    洛谷所有 `_contentOnly` 响应都把数据塞在这个容器里：
    题面是 `data.problem`，记录页是 `data.records`。
    """
    m = _CONTEXT_RE.search(html or "")
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _blocked(html: str) -> bool:
    """是不是被洛谷的风控页挡了。

    **有 `lentille-context` 就是真页面**，标题不算数（理由见 `_WELCOME_RE`）。
    """
    if _CONTEXT_RE.search(html or ""):
        return False
    return bool(_WELCOME_RE.search((html or "")[:4000]))


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
        payload = _context_payload(html)
        if payload is None:
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
        if _blocked(resp.text):
            return Fetched(ok=False, error_kind="挑战未过",
                           detail="被风控页挡住（C3VK 已过但还不够）")
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

        现状：`POST /auth/login` 拿回来的不是登录表单，而是一个
        `Welcome - Luogu Spilopelia` 外壳页 —— 说明还有一层我没看懂的
        门（可能是另一个 cookie、或者需要先访问预热页）。

        ⚠️ 别把这个标题当成风控特征：它同时是**正常页面**的默认标题
        （见 `_WELCOME_RE` 上方那段），所以"看到 Welcome 就是被挡了"
        这个判据是错的，只在**没有 `lentille-context`** 时才成立。

        所以现阶段洛谷走**手动导入 Cookie**（`/credentials` 接口）：
        用户在浏览器里登录后复制 Cookie 贴进来。
        **这是设计内的路径，不是降级** —— 它同样能拿到完整数据。
        """
        return Fetched(
            ok=False, error_kind="挑战未过",
            detail=("洛谷自动登录没打通（POST /auth/login 返回的不是登录表单）。"
                    "请用「手动导入 Cookie」：浏览器登录洛谷后，"
                    "从开发者工具里复制 Cookie 贴到插件页面。"))

    # 洛谷记录页一页固定 20 条（`perPage` 不可调）。第一次同步要能拉全
    # 历史，所以页数给足；之后带了 `since_epoch` 就会在第一页提前收工。
    _MAX_PAGES = 40

    async def fetch_submissions(self, uid: str, since_epoch: int | None = None,
                                client=None) -> Fetched:
        """拉提交记录。

        实测（2026-10-08，带真 Cookie 从服务器发起）：
            `GET /record/list?user=<uid>&page=N&_contentOnly=1`
            → 200 / 17097 字节，数据在 `lentille-context` 里：
                {"records": {"perPage": 20, "count": 733, "result": [...]}}
            列表**从新到旧**排；`count=733` + `perPage=20` → 共 37 页。

        以前这里有三个 bug 叠在一起（详见 `_WELCOME_RE` 上方的注释）：
        `_WELCOME_RE` 把正常页误判成风控页、`self._extract_json` 这个方法
        压根不存在、JSON 路径写成了 `currentData.records.result`。后两个
        被第一个挡住了，所以一直没暴露 —— 表现是"一条都同步不到"。
        """
        if not client or not client.cookies:
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="洛谷需要登录（或导入 Cookie）才能看提交记录")
        if not uid:
            return Fetched(ok=False, error_kind="凭据失效", detail="没填洛谷 uid")

        out: list[Submission] = []
        truncated = False

        for page in range(1, self._MAX_PAGES + 1):
            resp = await client.get(
                "%s/record/list?user=%s&page=%d&_contentOnly=1"
                % (ORIGIN, uid, page))
            if resp.status == 401:
                return Fetched(ok=False, error_kind="凭据失效",
                               detail="Cookie 已失效（HTTP 401），请重新导入")
            if _blocked(resp.text):
                return Fetched(ok=False, error_kind="挑战未过",
                               detail="被风控页挡住")
            if not resp:
                return Fetched(ok=False, error_kind="网络不可达",
                               detail="HTTP %d" % resp.status)

            records = ((_context_payload(resp.text) or {}).get("data")
                       or {}).get("records")
            if not isinstance(records, dict):
                return Fetched(ok=False, error_kind="页面结构变化",
                               detail="记录页里找不到 data.records（洛谷改版了？）")
            rows = records.get("result")
            if not isinstance(rows, list):
                return Fetched(ok=False, error_kind="页面结构变化",
                               detail="JSON 结构变了（找不到 records.result）")
            if not rows:
                break

            reached_old = False
            for r in rows:
                prob = r.get("problem") or {}
                pid = str(prob.get("pid") or "")
                if not pid:
                    continue
                epoch = int(r.get("submitTime") or 0)
                if since_epoch and epoch and epoch <= since_epoch:
                    # 从新到旧排的，碰到旧于游标的就可以收工了
                    reached_old = True
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
                    verdict=str(r.get("status")
                                if r.get("status") is not None else ""),
                    epoch=epoch,
                    language="",
                    difficulty=diff,
                    difficulty_source="luogu_level" if diff is not None
                    else "unknown",
                ))
            if reached_old:
                break

            total = records.get("count")
            per_page = records.get("perPage") or 20
            if isinstance(total, int) and page * per_page >= total:
                break                       # 已经是最后一页了
        else:
            truncated = True                # for 跑完没 break = 撞到页数上限

        return Fetched(items=out, ok=True, truncated=truncated)

    async def fetch_contests(self, uid: str, client=None) -> Fetched:
        return Fetched(ok=False, error_kind="页面结构变化",
                       detail="洛谷的比赛记录还没做（暂不支持）")
