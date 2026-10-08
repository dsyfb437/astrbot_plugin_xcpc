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

# 洛谷标签表里只有 `type=2`（Algorithm / 算法）是"算法方向"。
# 六种 type 的分布见 `fetch_tag_map()` 的 docstring —— 505 个标签里只有 262 个是。
_ALGORITHM_TAG_TYPE = 2

# 题库列表。**实测（2026-10-08）**：
#     GET /problem/list?page=N&_contentOnly=1  →  200 / 61 KB（**HTML**，不是 JSON）
#     数据同样在 `lentille-context` 里：
#         data.problems.count    = 17686     题库总题数
#         data.problems.perPage  = 50        每页固定 50
#         data.problems.result   = [{pid, name, difficulty, tags:[id...]}]
#
# ⚠️ **`perPage` 传什么都是 50**（实测 `perPage=1000` / `perPage=200` 返回的
# 仍是 `perPage: 50` 和 50 条）。所以全量拉一次是 17686/50 ≈ 354 个请求。
# 这也是它**不进自动同步**的原因（见 `core/sync.py` 的说明）。
_PROBLEM_LIST_URL = ORIGIN + "/problem/list?page=%d&_contentOnly=1"
_PROBLEM_PER_PAGE = 50
_MAX_PROBLEM_PAGES = 400        # 354 够用，留点余量以防题数涨了

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


# 洛谷的评测结果是**数字**，记录页只给数字：`{"status": 12, "score": 100}`。
#
# ⚠️ **不翻译就会静默算错，而且错得很像"他没做出来"。**
# 全插件判断 AC 的地方是 `core/summary.py:_is_ac()`，它只认
# OK / AC / ACCEPTED（CF 发的是 "OK"，AtCoder 发的是 "AC"）。
# 旧代码直接 `str(r["status"])` 存进去，于是库里躺着的是 `"12"` ——
# **333 条真 AC 全部算成"一次都没过"**。用户 2026-10-08 看到的
# 「洛谷 733 提交 0 AC」就是这个，不是他的问题，是我的。
#
# 下面的码是**用真数据交叉验证过的**，不是抄的：
#   `GET /record/296686294?_contentOnly=1` 的
#   `detail.judgeResult.subtasks[].testCases[].status` 就是 12，同一层还带
#   `"description": "ok accepted"` —— 12 = Accepted，板上钉钉。
#   而 `status=14` 那条（记录级 score=40）的测试点里混着 `status=5`
#   （`time: 1200`，超出该题限时）→ 5 = TLE，14 = Unaccepted（洛谷把
#   **所有非满分**笼统归到这一类，0 分到 90 分都见过）。
#   全量 733 条的实测分布：12 → 333 条（score 全是 100 或缺失）、
#   14 → 397 条（score 0~100 都有）、2 → 3 条（score 缺失，编译失败）。
#
# **认不出的码返回 `LG<n>`**：它不会被 `_is_ac()` 认成 AC（安全方向），
# 又能在统计里露出来 —— 比悄悄当成 WA 强，也比抛异常强。
_LG_STATUS = {
    0: "Waiting",
    1: "Judging",
    2: "CE",          # 实测：score 缺失，失败发生在 compileResult
    3: "OLE",
    4: "MLE",
    5: "TLE",         # 实测：出现在 testCases 里，time=1200（该题限时 1000）
    6: "WA",
    7: "RE",
    12: "AC",         # 实测：测试点 description = "ok accepted"
    14: "WA",         # 洛谷叫 "Unaccepted"：只要不是满分就是它
}


def _lg_verdict(status) -> str:
    """把洛谷的数字状态码翻成全插件统一的词表（见 `_LG_STATUS`）。"""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return ""
    return _LG_STATUS.get(code, "LG%d" % code)


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

    @staticmethod
    def parse_problem_list(html: str):
        """从**题库列表页**里解出这一页。

        实测（2026-10-08），数据同样在 `lentille-context` 里，但形状和
        题面页不同 —— 是 `data.problems.{count, perPage, result}`：

            {"data": {"problems": {"count": 17686, "perPage": 50,
                                   "result": [{"pid": "P1000",
                                               "name": "超级玛丽游戏",
                                               "difficulty": 1,
                                               "tags": [2, 108]}, ...]}}}

        返回 `{"count": int, "problems": [{"pid","name","difficulty","tag_ids"}]}`，
        每条的形状**和 `parse_problem_page` 保持一致**（上层不用分两种情况）。
        返回 `None` 表示认不出结构。

        ⚠️ 列表页的每条**没有 `tags` 名字，只有数字 ID**，和题面页一样 ——
        名字要去 `/_lfe/tags` 换（`fetch_tag_map`）。
        这里**不编名字**：拿不到 tag_map 就留 None（"给不出"）。
        """
        payload = _context_payload(html)
        if payload is None:
            return None
        problems = ((payload.get("data") or {}).get("problems")) or {}
        if not isinstance(problems, dict):
            return None
        rows = problems.get("result")
        if not isinstance(rows, list):
            return None

        try:
            count = int(problems.get("count") or 0)
        except (TypeError, ValueError):
            count = 0

        out = []
        for p in rows:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("pid") or "")
            if not pid:
                continue
            diff = p.get("difficulty")
            try:
                diff = int(diff) if diff is not None else None
            except (TypeError, ValueError):
                diff = None
            raw_tags = p.get("tags")
            tag_ids = ([t for t in raw_tags if isinstance(t, int)]
                       if isinstance(raw_tags, list) else [])
            out.append({
                "pid": pid,
                "name": str(p.get("name") or ""),
                "difficulty": diff,
                "tag_ids": tag_ids,
            })
        return {"count": count, "problems": out}

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

        ★ **只保留算法标签（`type == 2`）**（v0.5.20）。
        洛谷的标签表有 **505 个标签、六种 type**：

            type=2 Algorithm 算法        262 个  ← 只有这类是"算法方向"
            type=3 Origin    来源         82 个  USACO / NOI / 各省省选 / 洛谷原创
            type=6 Others    其他         63 个  算法 / 数据结构 / 来源 / 时间（分类节点）
            type=1 Region    区域         56 个  重庆 / 四川 / 浙江 / 北京 …
            type=4 Time      时间         37 个  1997 / 1998 / … / 2015
            type=5 SpecialProblem 特殊题目  5 个  交互题 / 提交答案 / Special Judge / O2优化

        不滤的话，"相对薄弱"里会冒出「O2优化：AC 38 题 / 提交 215，
        通过率 27%」「梦熊比赛」「天津」「2007」这种 —— **它们不是算法方向，
        拿来做训练诊断没有意义**，而且样本量还特别大，会把真正的薄弱项挤下去。
        这就是 v0.5.19 把洛谷题库拉起来之后真机上立刻看到的样子。

        ⚠️ 判据是**「这个标签自己带没带 type」**，不是"响应里有没有 types 数组"：
        认不出的结构里标签**原样保留** —— 结构变了就少给几个标签，
        比静默地把整张表丢空要好（后者看起来像"洛谷不给标签"）。
        """
        try:
            resp = await client.get(_TAG_URL)
            data = resp.json()
        except Exception:
            return {}

        # 把几种可能的形式都摊平成一个 list of {id, name, type}
        items = []
        if isinstance(data, dict):
            inner = data.get("tags")
            if isinstance(inner, list):
                items = inner
            elif isinstance(inner, dict):
                for k, v in inner.items():
                    try:
                        items.append({"id": int(k),
                                      "name": v.get("name") if isinstance(v, dict) else str(v),
                                      "type": v.get("type") if isinstance(v, dict) else None})
                    except (TypeError, ValueError):
                        continue
            else:
                # 退一步：顶层就是 {id: ...}
                for k, v in data.items():
                    try:
                        items.append({"id": int(k),
                                      "name": v.get("name") if isinstance(v, dict) else str(v),
                                      "type": v.get("type") if isinstance(v, dict) else None})
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
            # ★ 带 type 且不是算法类的，直接不要（见 docstring 里的 505/262）
            ttype = item.get("type")
            if ttype is not None:
                try:
                    if int(ttype) != _ALGORITHM_TAG_TYPE:
                        continue
                except (TypeError, ValueError):
                    pass          # type 是个认不出的东西 —— 当成"不知道"，保留
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

    # ---- 题库（全量）----------------------------------------------------
    #
    # 为什么要有这个
    # --------------
    # `supports_problems = True` 曾经是**假的**：这个类有 `fetch_problem`
    # （单题）却没有 `fetch_problems`（全量），所以哪天谁调一次
    # `ensure_problem_bank("luogu")`，就是 `AttributeError` —— 而接口声明的
    # 意思恰恰是"这个平台能提供题库"。**声明和实现不一致，比不会更坏**：
    # 上层会放心地调，错在运行时才炸。
    #
    # 为什么值得实现
    # --------------
    # 洛谷的题**有标签也有难度**（1-7 档），是四个平台里除了 CF 之外
    # 唯一两样都给得出来的。用户的洛谷 AC 有 333 道 —— 题库里没有它们的话，
    # "按标签分析"那条线的分母就只有 CF 的 76 道（见 `core/summary.py` 里
    # `tagged_solved` 那段注释）。**这是整个诊断里最大的一处失真。**
    #
    # 代价：17686 题 / 每页 50 → 354 个请求，按 `min_interval=1.5s`
    # 要十几分钟。所以它**不进自动同步**，只在 `force=True` 时拉。
    async def fetch_problems(self, client=None) -> Fetched:
        """拉全量题库标注（题号 + 标题 + 难度 + 标签）。

        `/_lfe/tags` 是**公开**的，`/problem/list` 也是 —— 不需要登录，
        也不需要用户的 Cookie。所以这个可以全局拉一次给所有人用。
        """
        tag_map = await self.fetch_tag_map(client)
        # 标签表拿不到不算失败：难度照样有用，标签留 None（"给不出"），**不编**。

        out: list[Problem] = []
        seen: set[str] = set()
        total = 0
        for page in range(1, _MAX_PROBLEM_PAGES + 1):
            resp = await client.get(_PROBLEM_LIST_URL % page)

            if page == 1:
                # 第一页就出问题 —— 直接如实报错，别返回一个空题库
                # （空题库会让上层以为"洛谷没题"，而不是"没拉到"）。
                if _blocked(resp.text):
                    return Fetched(ok=False, error_kind="挑战未过",
                                   detail="被风控页挡住（题库列表页）")
                if not resp:
                    return Fetched(ok=False, error_kind="网络不可达",
                                   detail="HTTP %d" % resp.status)

            parsed = self.parse_problem_list(resp.text)
            if parsed is None:
                if page == 1:
                    return Fetched(ok=False, error_kind="页面结构变化",
                                   detail="题库列表页里找不到 lentille-context 数据块"
                                          "（洛谷可能改版了）")
                # 中途某一页结构变了：**返回已经拿到的**，不要因为最后一页
                # 把前面十几分钟全丢掉。
                break

            rows = parsed["problems"]
            if not rows:
                break

            if not total:
                total = parsed["count"]

            for p in rows:
                key = self.problem_key(p["pid"])
                if key in seen:
                    continue
                seen.add(key)
                names = None
                if p["tag_ids"] and tag_map:
                    got = [tag_map.get(i) for i in p["tag_ids"]]
                    got = [n for n in got if n]
                    if got:
                        names = got
                out.append(Problem(
                    platform=self.name,
                    problem_key=key,
                    title=p["name"][:200],
                    tags=names,
                    difficulty=p["difficulty"],
                    difficulty_source=("luogu_level"
                                       if p["difficulty"] is not None else "unknown"),
                ))

            if total and page * _PROBLEM_PER_PAGE >= total:
                break

        return Fetched(items=out, ok=True)

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
        # 游标 = 见过的最新一条的时间。**每一行都要更新，不能只更新入队的那
        # 些** —— 增量同步时进来的行全被 `since_epoch` 挡掉，如果只在入队时
        # 更新，游标就会退化成 0，下一次又变成全量 37 页（实测 72 秒）。
        newest = 0

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
                newest = max(newest, epoch)
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
                    verdict=_lg_verdict(r.get("status")),
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

        # `max(newest, since_epoch)` 而不是光 `newest`：这一页可能整页都比
        # 游标旧（账号最近没交题），那时 `newest < since_epoch`，直接拿
        # `newest` 当游标会让游标**倒退**，下次白拉一遍。
        return Fetched(items=out, ok=True, cursor=max(newest, since_epoch or 0),
                       truncated=truncated)

    async def fetch_contests(self, uid: str, client=None) -> Fetched:
        return Fetched(ok=False, error_kind="页面结构变化",
                       detail="洛谷的比赛记录还没做（暂不支持）")
