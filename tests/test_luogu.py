#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""洛谷适配器的离线自测。

**夹具必须和现实一样脏。**
这里的 HTML 是从真实响应里抄的结构（`lentille-context` 容器 + 推荐题数组），
不是我自己编的干净版本 —— 我在这上面吃过两次亏：
  1. C3VK 夹具用了没混淆的 `document.cookie`，代码照此判断，
     结果夹具过、真站点解不出来
  2. 标签表夹具想当然写成裸列表，真实结构是 `{"tags":[...]}`，
     结果 32 KB 响应一条都没解析出来

所以下面每个夹具都标注了它的来源和"哪里脏"。

跑法：python tests/test_luogu.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from platforms.luogu import Luogu, LEVEL_NAMES, _blocked, _lg_verdict  # noqa: E402
from platforms.base import Fetched  # noqa: E402
from core.summary import _is_ac  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  \u2713 %s" % label)
    else:
        FAIL += 1
        print("  \u2717 %s%s" % (label, ("  -> " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# 夹具：结构抄自真实响应
# ---------------------------------------------------------------------------

# ⚠️ 脏点 1：数据包在 <script id="lentille-context" type="application/json"> 里
# ⚠️ 脏点 2：**recommendations 里装着别的题的 difficulty** ——
#    用正则乱捞必然捞到推荐题的难度，而且值看起来完全合理（都是 1-7 的档位）。
#    这是本文件最重要的一个夹具。
P1001_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<script>window.__feConfigVersion = '74d532c6c7861bfe';</script>
<script id="lentille-context" type="application/json">%s</script>
<script type="application/ld+json">{"name":"P1001 A+B Problem"}</script>
</head><body></body></html>""" % json.dumps({
    "instance": "main", "template": "problem.show", "status": 200,
    "locale": "zh-CN",
    "data": {
        "problem": {
            "pid": "P1001", "type": "P", "name": "A+B Problem",
            "difficulty": 1, "fullScore": 100, "tags": [1],
            "totalSubmit": 2218654, "totalAccepted": 1283274,
        },
        # 这些是**别的题**，难度也都是 1 —— 乱捞就会捞到这里
        "recommendations": [
            {"pid": "P1421", "name": "小玉买文具", "difficulty": 1},
            {"pid": "P1000", "name": "超级玛丽游戏", "difficulty": 1},
        ],
    },
}, ensure_ascii=False)

# 难度 7 + 多个标签
P3803_HTML = """<!DOCTYPE html><html><head>
<script id="lentille-context" type="application/json">%s</script>
</head><body></body></html>""" % json.dumps({
    "data": {"problem": {
        "pid": "P3803", "name": "【模板】多项式乘法（FFT）",
        "difficulty": 7, "tags": [7, 8, 9],
    }},
}, ensure_ascii=False)

# ⚠️ 脏点 3：标签表**外面包了一层 `tags` 键**（真实结构就是这样）
#
# ⚠️ 脏点 4（v0.5.20 才发现）：**505 个标签里只有 262 个是算法方向**。
#     其余是区域 / 来源 / 时间 / 特殊题目 / 其他 —— 不滤的话「相对薄弱」
#     里会冒出「O2优化：AC 38 题 / 提交 215，通过率 27%」「梦熊比赛」
#     「天津」「2007」，而且样本量大得把真正的薄弱项挤下去。
TAGS_JSON = {
    "tags": [
        {"id": -2, "name": "语言入门", "type": 2, "parent": None},
        {"id": 1, "name": "模拟", "type": 2, "parent": 110},
        {"id": 7, "name": "递归", "type": 2, "parent": None},
        {"id": 8, "name": "快速傅里叶变换 FFT", "type": 2, "parent": None},
        {"id": 9, "name": "快速数论变换 NTT", "type": 2, "parent": None},
        {"id": 10, "name": "", "type": 2, "parent": None},      # 空名字，应被丢
        # ↓ 下面这些**不该出现在算法标签里**
        {"id": 100, "name": "重庆", "type": 1, "parent": None},        # Region
        {"id": 101, "name": "NOI", "type": 3, "parent": None},         # Origin
        {"id": 102, "name": "2007", "type": 4, "parent": None},        # Time
        {"id": 103, "name": "O2优化", "type": 5, "parent": None},      # SpecialProblem
        {"id": 104, "name": "Special Judge", "type": 5, "parent": None},
        {"id": 105, "name": "数据结构", "type": 6, "parent": None},    # Others（分类节点）
        {"id": 106, "name": "洛谷月赛", "type": 3, "parent": None},
    ],
    "types": [
        {"id": 1, "type": "Region", "name": "区域"},
        {"id": 2, "type": "Algorithm", "name": "算法"},
        {"id": 3, "type": "Origin", "name": "来源"},
        {"id": 4, "type": "Time", "name": "时间"},
        {"id": 5, "type": "SpecialProblem", "name": "特殊题目"},
        {"id": 6, "type": "Others", "name": "其他"},
    ],
    "_locale": "zh-CN",
}

# 没有 `type` 字段的结构（真机换版式时可能长这样）—— 这时**原样保留**，
# 因为"认不出"不等于"不是算法标签"
TAGS_NO_TYPE_JSON = {
    "tags": [{"id": 1, "name": "模拟"}, {"id": 100, "name": "重庆"}],
}

# ⚠️ 脏点 3（2026-10-08 才发现）：`Welcome - Luogu Spilopelia` **不是**风控页特征。
#    它是洛谷 SPA 外壳对 `record.list` 这类模板渲染的**默认标题**，正常记录页
#    也长这样 —— 实测正常记录页 17097 字节、带 lentille-context、733 条记录；
#    真风控页 ~3027 字节且**没有** lentille-context。
#    我原来只看标题就报"被第二层挑战页挡住"，结果洛谷同步一条都拉不到、
#    还甩锅给"风控"。下面 `records_html()` 造的就是这种**标题一样但正常**的页。
WELCOME_HTML = "<!DOCTYPE html><html><head><title>Welcome - Luogu Spilopelia</title>"


def records_html(rows, count=733, per_page=20, uid=1823658):
    """造一个**正常**的洛谷记录页 —— 结构照抄 2026-10-08 抓的真实响应。

    关键脏点：标题是 `Welcome`（和风控页一模一样），但**带 lentille-context**，
    数据在 `data.records.result`（不是 `currentData.records.result`）。
    """
    payload = {
        "instance": "main",
        "template": "record.list",
        "status": 200,
        "locale": "zh-CN",
        "data": {"records": {"perPage": per_page, "count": count,
                             "result": rows}},
        "user": {"uid": uid, "name": "dsyfb437"},
        "time": 1788592500,
    }
    return ('<!DOCTYPE html><html><head><title>Welcome - Luogu Spilopelia'
            '</title></head><body>'
            '<script id="lentille-context" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script></body></html>")


def row(sid, pid, epoch, status=12, diff=1):
    """一条提交记录（字段名照抄真实响应：submitTime / problem.pid）。"""
    return {"id": sid, "status": status, "submitTime": epoch,
            "problem": {"pid": pid, "difficulty": diff}}


def list_html(rows, count=17686, per_page=50):
    """造一个**题库列表页** —— 结构照抄 2026-10-08 抓的真实响应。

    实测：`GET /problem/list?page=N&_contentOnly=1` 返回的是 **HTML**
    （61 KB，不是 JSON），数据同样在 `lentille-context` 里，但形状和题面页
    不同 —— 是 `data.problems.{count, perPage, result}`，每条是
    `{pid, name, difficulty, tags:[数字ID]}`（**没有标签名字**）。
    """
    payload = {
        "instance": "main",
        "template": "problem.list",
        "status": 200,
        "locale": "zh-CN",
        "data": {"problems": {"perPage": per_page, "count": count,
                              "result": rows}},
        "user": None,
        "time": 1791424475,
    }
    return ('<!DOCTYPE html><html><head><title>题库 - 洛谷</title></head><body>'
            '<script id="lentille-context" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script></body></html>")


def lrow(pid, name, diff=1, tags=None):
    """题库列表里的一条（字段名照抄真实响应）。"""
    return {"pid": pid, "type": "P", "name": name, "difficulty": diff,
            "fullScore": 100, "tags": list(tags or []),
            "totalSubmit": 1878697, "totalAccepted": 718269, "flag": 5}


class Resp:
    def __init__(self, status=200, text="", body=None):
        self.status = status
        self.text = text
        self.body = (body if body is not None
                     else text.encode("utf-8", errors="replace"))
        self.url = ""

    def json(self):
        return json.loads(self.text)

    def __bool__(self):
        return 200 <= self.status < 300


class FakeClient:
    """按 URL 关键词返回夹具。**未覆盖的 URL 直接失败，绝不走真网络。**"""

    def __init__(self, routes):
        self.routes = routes
        self.cookies = {}

    def set_cookies(self, c):
        self.cookies = dict(c or {})

    async def get(self, url, **kw):
        for key in sorted(self.routes, key=len, reverse=True):
            if key in url:
                v = self.routes[key]
                return v() if callable(v) else v
        raise AssertionError("夹具没覆盖：%s（**不允许走真网络**）" % url)


# ---------------------------------------------------------------------------
# 1. 题面解析
# ---------------------------------------------------------------------------

def test_parse():
    print("\n[1] 题面解析")

    got = Luogu.parse_problem_page(P1001_HTML)
    check("能解析出来", got is not None, repr(got))
    if got:
        check("题号对", got["pid"] == "P1001", got["pid"])
        check("名字对", got["name"] == "A+B Problem", got["name"])
        check("难度是本题的 1（不是推荐题的）", got["difficulty"] == 1,
              repr(got["difficulty"]))
        check("标签 ID 取的是本题的 [1]（不是推荐题）",
              got["tag_ids"] == [1], repr(got["tag_ids"]))

    got2 = Luogu.parse_problem_page(P3803_HTML)
    check("难度 7 解析对", got2 and got2["difficulty"] == 7,
          repr(got2 and got2["difficulty"]))
    check("多个标签 ID 都对", got2 and got2["tag_ids"] == [7, 8, 9],
          repr(got2 and got2["tag_ids"]))

    check("认不出的页面返回 None", Luogu.parse_problem_page("<html>没有容器</html>") is None)
    check("空输入返回 None", Luogu.parse_problem_page("") is None)
    check("容器里不是 JSON 时返回 None",
          Luogu.parse_problem_page(
              '<script id="lentille-context">不是json</script>') is None)
    check("JSON 里没有 problem 时返回 None",
          Luogu.parse_problem_page(
              '<script id="lentille-context">{"data":{}}</script>') is None)

    # 最关键的一条：不能把推荐题的难度当成本题的
    # 构造一个"本题难度 5，推荐题全是 1"的页面
    tricky = ('<script id="lentille-context">%s</script>' % json.dumps({
        "data": {
            "problem": {"pid": "P9999", "name": "tricky",
                        "difficulty": 5, "tags": [3]},
            "recommendations": [{"pid": "P1", "difficulty": 1},
                                {"pid": "P2", "difficulty": 1}],
        },
    }))
    got3 = Luogu.parse_problem_page(tricky)
    check("有推荐题干扰时仍取本题难度",
          got3 and got3["difficulty"] == 5, repr(got3 and got3["difficulty"]))


# ---------------------------------------------------------------------------
# 2. 标签表
# ---------------------------------------------------------------------------

def test_tags():
    print("\n[2] 标签表")

    async def main():
        c = FakeClient({"/_lfe/tags": Resp(200, json.dumps(TAGS_JSON,
                                                           ensure_ascii=False))})
        tmap = await Luogu().fetch_tag_map(c)
        check("能从 `{tags:[...]}` 里解析出来（我第一版就是栽在这）",
              len(tmap) == 5, "拿到 %d 个：%r" % (len(tmap), tmap))
        check("ID 1 → 模拟", tmap.get(1) == "模拟", repr(tmap.get(1)))
        check("负数 ID 也能用", tmap.get(-2) == "语言入门", repr(tmap.get(-2)))
        check("空名字被丢掉", 10 not in tmap, repr(tmap))
        check("整体数字段没被当成标签",
              all(not isinstance(v, dict) for v in tmap.values()))

        # ★ v0.5.20：只留算法标签（type=2）
        bad = {i: tmap.get(i) for i in (100, 101, 102, 103, 104, 105, 106)}
        check("★ 区域（重庆）/ 来源（NOI、洛谷月赛）/ 时间（2007）/ "
              "特殊（O2优化、Special Judge）/ 其他（数据结构）全部丢掉",
              all(v is None for v in bad.values()), repr(bad))
        check("★ 「O2优化」「Special Judge」不进标签表（它们不是算法方向）",
              "O2优化" not in tmap.values()
              and "Special Judge" not in tmap.values(),
              repr(sorted(tmap.values())))
        check("算法标签照留（FFT / NTT / 递归都在）",
              tmap.get(8) == "快速傅里叶变换 FFT" and tmap.get(7) == "递归"
              and tmap.get(9) == "快速数论变换 NTT",
              repr(sorted(tmap.values())))

        # 结构里没有 type 字段时**原样保留**（"认不出"≠"不是算法标签"）
        c2 = FakeClient({"/_lfe/tags": Resp(200, json.dumps(
            TAGS_NO_TYPE_JSON, ensure_ascii=False))})
        tmap3 = await Luogu().fetch_tag_map(c2)
        check("★ 标签里没有 type 字段时不乱滤（原样保留）",
              tmap3.get(1) == "模拟" and tmap3.get(100) == "重庆",
              repr(tmap3))

        # 拿不到标签表时不能崩，也不能编
        class Boom(FakeClient):
            async def get(self, url, **kw):
                raise RuntimeError("网络炸了")
        tmap2 = await Luogu().fetch_tag_map(Boom({}))
        check("拿不到时返回空 dict（不崩）", tmap2 == {})

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. fetch_problem 全流程
# ---------------------------------------------------------------------------

def test_fetch_problem():
    print("\n[3] fetch_problem 全流程")

    async def main():
        routes = {
            "/problem/P1001": Resp(200, P1001_HTML),
            "/_lfe/tags": Resp(200, json.dumps(TAGS_JSON, ensure_ascii=False)),
        }
        got = await Luogu().fetch_problem("P1001", FakeClient(routes))
        check("成功", got.ok, got.detail)
        if got.ok:
            p = got.items[0]
            check("key 是 LG:P1001", p.problem_key == "LG:P1001", p.problem_key)
            check("标签解析成了名字（不是 ID）",
                  p.tags == ["模拟"], repr(p.tags))
            check("难度来源标 luogu_level", p.difficulty_source == "luogu_level",
                  p.difficulty_source)

        # 拿不到标签表时，tags 必须是 None（"给不出"），不是空列表
        routes2 = {"/problem/P1001": Resp(200, P1001_HTML),
                   "/_lfe/tags": Resp(500, "err")}
        got2 = await Luogu().fetch_problem("P1001", FakeClient(routes2))
        check("拿不到标签表时仍能返回难度", got2.ok and got2.items[0].difficulty == 1,
              repr(got2)[:120])
        check("这时 tags 是 None（不是空列表，也不是编的）",
              got2.ok and got2.items[0].tags is None,
              repr(got2.items[0].tags) if got2.ok else "")

        # 第二层挑战页
        got3 = await Luogu().fetch_problem(
            "P1001", FakeClient({"/problem/": Resp(200, WELCOME_HTML)}))
        check("认出第二层挑战页", not got3.ok and got3.error_kind == "挑战未过",
              "%s / %s" % (got3.error_kind, got3.detail[:60]))

        # 401
        got4 = await Luogu().fetch_problem(
            "P1001", FakeClient({"/problem/": Resp(401, "no")}))
        check("401 报凭据失效", not got4.ok and got4.error_kind == "凭据失效",
              got4.error_kind)

        # 改版
        got5 = await Luogu().fetch_problem(
            "P1001", FakeClient({"/problem/": Resp(200, "<html>改版了</html>")}))
        check("结构变了明确报错（不猜一个难度出来）",
              not got5.ok and got5.error_kind == "页面结构变化",
              "%s / %s" % (got5.error_kind, got5.detail[:60]))

        # 空题号
        got6 = await Luogu().fetch_problem("", FakeClient({}))
        check("空题号拒绝", not got6.ok)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. 难度档位名
# ---------------------------------------------------------------------------

def test_levels():
    print("\n[4] 难度档位")
    check("7 档都有名字", all(i in LEVEL_NAMES for i in range(8)))
    check("1 = 入门", LEVEL_NAMES[1] == "入门", LEVEL_NAMES[1])
    check("7 = NOI/NOI+/CTSC", "NOI" in LEVEL_NAMES[7], LEVEL_NAMES[7])
    check("档位是 1-7，和 CF rating 完全不是一套尺子",
          max(LEVEL_NAMES) == 7)


# ---------------------------------------------------------------------------
# 5. 自动登录仍未打通 —— 必须如实报
# ---------------------------------------------------------------------------

def test_login_honest():
    print("\n[5] 自动登录如实报未打通")

    async def main():
        class FakeSvc:
            async def make_client(self, uid, pf):
                return FakeClient({})
        got = await Luogu().login("u", "p", FakeClient({}))
        check("明确说明自动登录没打通", not got.ok, repr(got))
        check("分类是挑战未过", got.error_kind == "挑战未过", got.error_kind)
        check("指引到手动导入 Cookie（不是说'暂不支持'）",
              "手动导入" in got.detail or "Cookie" in got.detail, got.detail[:120])

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 6. 提交记录 —— 三个 bug 就藏在这儿，这节是它们以后翻不了身的保证
# ---------------------------------------------------------------------------

def test_fetch_submissions():
    print("\n[6] fetch_submissions（三个 bug 的回归）")

    async def main():
        lg = Luogu()
        CK = {"__client_id": "x", "_uid": "1823658"}

        # --- 6.1 正常页：标题是 Welcome，但带 lentille-context → 必须认得出来
        html = records_html([row(1, "P1001", 1788592435),
                             row(2, "P3803", 1788592127)], count=2)
        c = FakeClient({"/record/list": Resp(200, html)})
        c.set_cookies(CK)
        got = await lg.fetch_submissions("1823658", None, c)
        check("★ 标题 Welcome + 有数据容器 → **不**该报风控",
              got.ok, "%s / %s" % (got.error_kind, got.detail[:70]))
        check("解析出 2 条", got.ok and len(got.items) == 2,
              repr(len(got.items)) if got.ok else "")
        check("★ 走的是 data.records.result（原来写的是 currentData）",
              got.ok and got.items[0].problem_key == "LG:P1001",
              got.items[0].problem_key if got.ok else "")
        check("epoch 取自 submitTime",
              got.ok and got.items[0].epoch == 1788592435,
              repr(got.items[0].epoch) if got.ok else "")
        check("难度带得出来",
              got.ok and got.items[0].difficulty == 1,
              repr(got.items[0].difficulty) if got.ok else "")
        check("难度来源标 luogu_level",
              got.ok and got.items[0].difficulty_source == "luogu_level", "")
        # ⚠️ 这里原来断言的是 `verdict == "12"` —— **等于把 bug 写进了测试**。
        # 洛谷给的是数字码，而全插件判断 AC 的 `_is_ac()` 只认
        # OK / AC / ACCEPTED，于是 333 条真 AC 被算成"一次都没过"
        # （用户看到的「洛谷 733 提交 0 AC」）。
        # 测试照着错误实现写，就是给 bug 发了张通行证 —— 这已经是第二次了
        # （上一个是 `test_main_static` 抄了官方签名却不检查调用点）。
        check("★ verdict 翻成了 AC（不是数字 12）",
              got.ok and got.items[0].verdict == "AC",
              repr(got.items[0].verdict) if got.ok else "")

        # --- 6.1b ★ 游标：洛谷**从来不设 cursor**，于是 `save_sync_ok`
        # 存下的 `last_epoch` 永远是 None，每次同步都全量拉 37 页 / 72 秒。
        # 拉得越久越容易撞上某一页超时（2026-10-08 02:09 第 8 页 20.4 秒）。
        check("★ 返回 cursor（= 见过的最新时间）",
              got.ok and got.cursor == 1788592435,
              repr(got.cursor) if got.ok else "")
        c6 = FakeClient({"/record/list": Resp(
            200, records_html([row(9, "P1001", 700)], count=1))})
        c6.set_cookies(CK)
        got6 = await lg.fetch_submissions("1823658", 1788592435, c6)
        check("全都是旧记录时不入队", got6.ok and got6.items == [],
              repr(len(got6.items)) if got6.ok else "")
        check("★ 游标不倒退（还是 1788592435）",
              got6.ok and got6.cursor == 1788592435, repr(got6.cursor))
        c7 = FakeClient({"/record/list": Resp(200, records_html(
            [row(10, "P1002", 1788599999), row(11, "P1003", 700)], count=2))})
        c7.set_cookies(CK)
        got7 = await lg.fetch_submissions("1823658", 1788592435, c7)
        check("增量只收新的那一条", got7.ok and len(got7.items) == 1,
              repr(len(got7.items)) if got7.ok else "")
        check("游标前进到最新", got7.ok and got7.cursor == 1788599999,
              repr(got7.cursor))

        # --- 6.2 那个根本不存在的方法
        check("★ 不再调用不存在的 self._extract_json",
              not hasattr(Luogu, "_extract_json"))

        # --- 6.3 真风控页：标题一样，但**没有**数据容器
        check("_blocked：真风控页判 True", _blocked(WELCOME_HTML))
        check("_blocked：正常记录页判 False", not _blocked(html))
        c3 = FakeClient({"/record/list": Resp(200, WELCOME_HTML)})
        c3.set_cookies(CK)
        got3 = await lg.fetch_submissions("1823658", None, c3)
        check("没容器 + Welcome 标题 = 挑战未过",
              not got3.ok and got3.error_kind == "挑战未过",
              "%s / %s" % (got3.error_kind, got3.detail[:50]))

        # --- 6.4 有容器但结构变了 → 页面结构变化（不许猜，也不许当风控）
        c4 = FakeClient({"/record/list": Resp(
            200, records_html([]).replace('"records"', '"somethingElse"'))})
        c4.set_cookies(CK)
        got4 = await lg.fetch_submissions("1823658", None, c4)
        check("结构变了报页面结构变化",
              not got4.ok and got4.error_kind == "页面结构变化",
              "%s / %s" % (got4.error_kind, got4.detail[:50]))

        # --- 6.5 空结果 = 真的没有（ok=True、0 条），不是错误
        c5 = FakeClient({"/record/list": Resp(200, records_html([], count=0))})
        c5.set_cookies(CK)
        got5 = await lg.fetch_submissions("1823658", None, c5)
        check("空结果是 ok=True + 0 条（不是报错）",
              got5.ok and got5.items == [],
              "%s / %s" % (got5.error_kind, got5.detail[:50]))

        # --- 6.6 401
        c6 = FakeClient({"/record/list": Resp(401, "no")})
        c6.set_cookies(CK)
        got6 = await lg.fetch_submissions("1823658", None, c6)
        check("401 报凭据失效", not got6.ok and got6.error_kind == "凭据失效",
              got6.error_kind)

        # --- 6.7 没 Cookie / 没 uid：直接拒绝，一个请求都不发
        got7 = await lg.fetch_submissions("1823658", None, FakeClient({}))
        check("没 Cookie 直接拒绝（不发请求）",
              not got7.ok and got7.error_kind == "凭据失效", got7.error_kind)
        c8 = FakeClient({})
        c8.set_cookies(CK)
        got8 = await lg.fetch_submissions("", None, c8)
        check("没 uid 直接拒绝", not got8.ok and got8.error_kind == "凭据失效",
              got8.error_kind)

        # --- 6.8 ★ 分页：733 条要全拉回来，不能只拉第一页那 20 条
        seen = []

        def page_route(n, rows, count):
            def f():
                seen.append(n)
                return Resp(200, records_html(rows, count=count))
            return f

        p1 = [row(i, "P1001", 1788590000 + i) for i in range(20)]
        p2 = [row(100 + i, "P1001", 1788500000 + i) for i in range(20)]
        p3 = [row(200 + i, "P1001", 1788400000 + i) for i in range(5)]
        c9 = FakeClient({"page=1&": page_route(1, p1, 45),
                         "page=2&": page_route(2, p2, 45),
                         "page=3&": page_route(3, p3, 45)})
        c9.set_cookies(CK)
        got9 = await lg.fetch_submissions("1823658", None, c9)
        check("★ 分页把 3 页 45 条全拉回来",
              got9.ok and len(got9.items) == 45,
              repr(len(got9.items)) if got9.ok
              else "%s / %s" % (got9.error_kind, got9.detail[:50]))
        check("正好请求 3 页（count=45 / perPage=20 就收）",
              seen == [1, 2, 3], repr(seen))
        check("没撞上限就不算截断", got9.truncated is False, repr(got9.truncated))

        # --- 6.9 ★ 增量：带 since_epoch 时碰到旧的就收工，不白拉 37 页
        seen2 = []

        def page_route2():
            seen2.append(1)
            return Resp(200, records_html(
                [row(1, "P1001", 1000), row(2, "P1001", 900),
                 row(3, "P1001", 800), row(4, "P1001", 700)], count=733))

        c10 = FakeClient({"page=1&": page_route2, "page=2&": page_route2})
        c10.set_cookies(CK)
        got10 = await lg.fetch_submissions("1823658", 850, c10)
        check("★ 只取新于游标的（1000/900，丢掉 800/700）",
              got10.ok and [i.epoch for i in got10.items] == [1000, 900],
              repr([i.epoch for i in got10.items]) if got10.ok else "")
        check("★ 碰到旧的就停，没再去拉第 2 页",
              len(seen2) == 1, repr(len(seen2)))

        # --- 6.10 撞到页数上限要如实标 truncated（别假装拉全了）
        seen3 = []

        def always():
            seen3.append(1)
            return Resp(200, records_html(
                [row(1, "P1001", 1788592435) for _ in range(20)],
                count=999999))

        c11 = FakeClient({"/record/list": always})
        c11.set_cookies(CK)
        got11 = await lg.fetch_submissions("1823658", None, c11)
        check("撞到 _MAX_PAGES 上限时 truncated=True",
              got11.ok and got11.truncated is True, repr(got11.truncated))
        check("也确实是拉满了 _MAX_PAGES 页",
              len(seen3) == Luogu._MAX_PAGES, repr(len(seen3)))

    asyncio.run(main())


def test_lg_verdict():
    print("\n[7] 洛谷数字状态码 → 统一词表（「0 AC」的根因）")

    # ⚠️ 这些映射不是从文档抄的，是拿真记录交叉验证出来的
    # （证据写在 platforms/luogu.py 的 _LG_STATUS 上方）：
    #   /record/296686294 的 detail.judgeResult 里测试点 status=12，
    #   同一层带着 description="ok accepted"；
    #   status=14 那条记录总分 score=40，测试点里混着 status=5 且
    #   单点 time=1200ms（→ TLE）。
    check("12 → AC", _lg_verdict(12) == "AC", repr(_lg_verdict(12)))
    check("14 → WA（洛谷把它叫 Unaccepted，0~100 分都归这里）",
          _lg_verdict(14) == "WA", repr(_lg_verdict(14)))
    check("2 → CE（全站那 3 条 score 是空的）",
          _lg_verdict(2) == "CE", repr(_lg_verdict(2)))
    check("5 → TLE", _lg_verdict(5) == "TLE", repr(_lg_verdict(5)))
    check('字符串 "12" 也认（JSON 里可能是字符串）',
          _lg_verdict("12") == "AC", repr(_lg_verdict("12")))
    check("None → 空串", _lg_verdict(None) == "", repr(_lg_verdict(None)))
    check("空串 → 空串", _lg_verdict("") == "", repr(_lg_verdict("")))
    check("认不出的码 → LG<code>，不会冒充 AC",
          _lg_verdict(99) == "LG99", repr(_lg_verdict(99)))

    # ★ 下面三条才是「0 AC」的真正回归点：翻译出来的东西必须能被
    #   全插件判断 AC 的那个函数认出来 —— **两边分头改就会再次失配**，
    #   而这正是这次事故的形状（一个平台存数字码，另一个函数只认英文词）。
    check("★ 翻出来的 AC 确实被 _is_ac() 认（0 AC 的直接回归）",
          bool(_is_ac(_lg_verdict(12))), repr(_lg_verdict(12)))
    check("★ 而翻译之前的裸数字不被认 —— 这就是 bug 本身",
          not _is_ac("12"), repr("12"))
    check("★ 认不出的码也不会被当成 AC",
          not _is_ac(_lg_verdict(99)), repr(_lg_verdict(99)))


def test_fetch_problems():
    print("\n[8] 全量题库（fetch_problems）")

    # ---- 声明和实现必须一致 ----
    #
    # 真 bug（v0.5.19 修）：`Luogu.supports_problems = True` 挂了很久，
    # 但这个类**根本没有 `fetch_problems`** —— 有单题的 `fetch_problem`，
    # 没有全量的那个。谁要是调 `ensure_problem_bank("luogu")` 就是
    # `AttributeError`，被 `core/sync.py` 的 `except` 兜住，表现成
    # "题库拉取失败：[内部错误]"，看不出是接口没实现。
    # **声明的意思是"这个平台能提供题库"，假的比没有更坏。**
    check("★ supports_problems=True 就必须真的有 fetch_problems（这就是修掉的 bug）",
          bool(getattr(Luogu, "supports_problems", False))
          and callable(getattr(Luogu, "fetch_problems", None)))

    # ---- 解析器（离线）----
    got = Luogu.parse_problem_list(
        list_html([lrow("P1000", "超级玛丽游戏", 1, [1, 7])]))
    check("能从题库列表页解析出来", got is not None, repr(got))
    if got:
        check("总题数读到了", got["count"] == 17686, repr(got["count"]))
        check("解出 1 条", len(got["problems"]) == 1, repr(len(got["problems"])))
        p = got["problems"][0]
        check("题号对", p["pid"] == "P1000", p["pid"])
        check("名字对", p["name"] == "超级玛丽游戏", p["name"])
        check("难度对", p["difficulty"] == 1, repr(p["difficulty"]))
        check("标签是数字 ID（列表页只给 ID，没有名字）",
              p["tag_ids"] == [1, 7], repr(p["tag_ids"]))
        check("形状和 parse_problem_page 一致（上层不用分两种情况处理）",
              set(p.keys()) == {"pid", "name", "difficulty", "tag_ids"},
              repr(sorted(p.keys())))

    check("没有 lentille-context 时返回 None",
          Luogu.parse_problem_list("<html>没有容器</html>") is None)
    check("空输入返回 None", Luogu.parse_problem_list("") is None)
    check("没有 problems 键时返回 None",
          Luogu.parse_problem_list(
              '<script id="lentille-context" type="application/json">'
              '{"data":{"problem":{}}}</script>') is None)
    check("result 不是列表时返回 None",
          Luogu.parse_problem_list(
              '<script id="lentille-context" type="application/json">'
              '{"data":{"problems":{"count":1,"result":"x"}}}</script>') is None)
    check("缺 pid 的那条被丢掉（不塞一个空题号进题库）",
          Luogu.parse_problem_list(
              '<script id="lentille-context" type="application/json">'
              '{"data":{"problems":{"count":2,"result":'
              '[{"name":"没有题号"},{"pid":"P1","name":"有"}]}}}</script>'
          )["problems"] == [{"pid": "P1", "name": "有", "difficulty": None,
                             "tag_ids": []}])

    # ---- 全量拉取：分页 + 收工条件 ----
    class PageClient:
        """按 URL 里的 `page=` 分页返回题库列表。**不覆盖的 URL 直接失败。**"""

        def __init__(self, total, per=50):
            self.total = total
            self.per = per
            self.pages = []
            self.cookies = {}

        def set_cookies(self, c):
            self.cookies = dict(c or {})

        async def get(self, url, **kw):
            if "_lfe/tags" in url:
                return Resp(200, json.dumps(TAGS_JSON, ensure_ascii=False))
            m = re.search(r"page=(\d+)", url)
            if not m:
                raise AssertionError("夹具没覆盖：%s（**不允许走真网络**）" % url)
            n = int(m.group(1))
            self.pages.append(n)
            start = (n - 1) * self.per
            # 最后一页只给剩下的那些 —— 真实接口不会多吐行出来
            n_rows = max(0, min(self.per, self.total - start))
            rows = [lrow("P%05d" % (start + i), "题%d" % (start + i),
                         (i % 7) + 1, [1, 7]) for i in range(n_rows)]
            return Resp(200, list_html(rows, count=self.total, per_page=self.per))

    async def main():
        c = PageClient(total=120)           # 120 题 / 每页 50 → 恰好 3 页
        got = await Luogu().fetch_problems(c)
        check("拉成功了", got.ok, got.detail)
        check("题数对", len(got.items) == 120, repr(len(got.items)))
        check("★ 只请求需要的那几页（120 题 / 每页 50 → 3 页，不多拉）",
              c.pages == [1, 2, 3], repr(c.pages))

        if got.ok and got.items:
            first = got.items[0]
            check("key 带 LG: 前缀", first.problem_key == "LG:P00000",
                  first.problem_key)
            check("难度来源标 luogu_level",
                  first.difficulty_source == "luogu_level",
                  first.difficulty_source)
            check("★ 标签 ID 换成了名字", first.tags == ["模拟", "递归"],
                  repr(first.tags))
            check("平台标 luogu", first.platform == "luogu", first.platform)
            # 第 51 条应该来自第 2 页（分页没错位）
            check("第 51 条来自第 2 页（分页没错位）",
                  got.items[50].problem_key == "LG:P00050",
                  got.items[50].problem_key)
            check("难度按 i%7+1 走，第 7 条是 7 档",
                  got.items[6].difficulty == 7, repr(got.items[6].difficulty))

        # 拿不到标签表时**仍然返回难度**，标签留 None（"给不出"，不编）
        class NoTagClient(PageClient):
            async def get(self, url, **kw):
                if "_lfe/tags" in url:
                    return Resp(500, "boom")
                return await PageClient.get(self, url, **kw)

        got2 = await Luogu().fetch_problems(NoTagClient(total=50))
        check("拿不到标签表时仍能拉到题", got2.ok and len(got2.items) == 50,
              repr(len(got2.items) if got2.ok else got2.detail))
        check("这时 tags 是 None（不是空列表，也不是编的名字）",
              got2.ok and got2.items[0].tags is None,
              repr(got2.ok and got2.items[0].tags))

        # ★ v0.5.21：标签表**拿到了**，但这题一个算法标签都没有
        #   → tags 必须是 `[]`（"确实没有"），**不能是 None**（"给不出"）。
        #   真实翻车：老实现见名字为空就留 None，入库时
        #   `tags_json=COALESCE(excluded, 老值)` 见 None 就保留旧值 ——
        #   重拉 17686 道题花掉 13 分钟，`O2优化`/`天津`/`2007` 一个没冲掉。
        class OnlyBadTagsClient(PageClient):
            async def get(self, url, **kw):
                if "_lfe/tags" in url:
                    return Resp(200, json.dumps(TAGS_JSON, ensure_ascii=False))
                if "/problem/list" in url:
                    # 每道题只挂非算法标签（重庆 type1 / 2007 type4 / O2优化 type5）
                    start = 0
                    rows = [lrow("P%05d" % i, "题%d" % i, 2, [100, 102, 103])
                            for i in range(self.per)]
                    return Resp(200, list_html(rows, count=self.total,
                                               per_page=self.per))
                return await PageClient.get(self, url, **kw)

        got3 = await Luogu().fetch_problems(OnlyBadTagsClient(total=50))
        check("★ 只有非算法标签的题：tags 是 []（确实没有），不是 None（给不出）",
              got3.ok and got3.items and got3.items[0].tags == [],
              repr(got3.ok and got3.items and got3.items[0].tags))
        check("★ 这一条不等于「拿不到标签表」",
              got3.ok and got3.items and got3.items[0].tags is not None,
              repr(got3.ok and got3.items and got3.items[0].tags))

        # 第一页就被风控 → 如实报错，**不要返回空题库**
        # （空题库会让上层以为"洛谷没题"，而不是"没拉到"）
        #
        # ⚠️ 判据是「没有 lentille-context」而不是「标题是 Welcome」——
        # 见 `platforms/luogu.py` 里 `_blocked()` 那段注释：Welcome 是
        # 正常页面的默认标题，光看标题会把整页好数据丢掉。
        class BlockedClient(PageClient):
            async def get(self, url, **kw):
                if "_lfe/tags" in url:
                    return Resp(200, json.dumps(TAGS_JSON, ensure_ascii=False))
                return Resp(200, WELCOME_HTML)       # 没容器 = 风控页

        got3 = await Luogu().fetch_problems(BlockedClient(total=120))
        check("第一页没容器时报「挑战未过」（不返回空题库）",
              not got3.ok and got3.error_kind == "挑战未过",
              "%s / %s" % (got3.ok, got3.error_kind))
        check("★ 失败时 items 是空的（不是「0 题」的意思）",
              not got3.items, repr(len(got3.items)))

        # 有容器但形状变了（洛谷改版）→ 报「页面结构变化」，
        # 而不是猜一个难度出来
        class ShapeClient(PageClient):
            async def get(self, url, **kw):
                if "_lfe/tags" in url:
                    return Resp(200, json.dumps(TAGS_JSON, ensure_ascii=False))
                return Resp(200,
                            '<script id="lentille-context" type="application/json">'
                            '{"data":{"problem":{}}}</script>')

        got3b = await Luogu().fetch_problems(ShapeClient(total=120))
        check("第一页结构变了明确报「页面结构变化」",
              not got3b.ok and got3b.error_kind == "页面结构变化",
              "%s / %s" % (got3b.ok, got3b.error_kind))

        class DownClient(PageClient):
            async def get(self, url, **kw):
                if "_lfe/tags" in url:
                    return Resp(200, json.dumps(TAGS_JSON, ensure_ascii=False))
                return Resp(503, "unavailable")

        got4 = await Luogu().fetch_problems(DownClient(total=120))
        check("第一页 503 报网络不可达", not got4.ok
              and got4.error_kind == "网络不可达",
              "%s / %s" % (got4.ok, got4.error_kind))

        # ★ 中途某一页坏掉：**返回已经拿到的**，别把前面十几分钟全丢掉
        class MidFailClient(PageClient):
            async def get(self, url, **kw):
                if "_lfe/tags" in url:
                    return Resp(200, json.dumps(TAGS_JSON, ensure_ascii=False))
                m = re.search(r"page=(\d+)", url)
                if m and int(m.group(1)) >= 3:
                    return Resp(200, WELCOME_HTML)
                return await PageClient.get(self, url, **kw)

        got5 = await Luogu().fetch_problems(MidFailClient(total=1000))
        check("★ 中途一页坏掉时保留已拉到的（不是全丢）",
              got5.ok and len(got5.items) == 100, repr(len(got5.items)))

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 9. 比赛记录（data.elo 那条嵌套链）
# ---------------------------------------------------------------------------

def _elo_node(cid, name, start, rating, prev_diff=None, previous=None):
    node = {
        "rating": rating, "time": start,
        "contest": {"id": cid, "startTime": start, "endTime": start + 7200,
                    "name": name},
        "userCount": 10,
    }
    if prev_diff is not None:
        node["prevDiff"] = prev_diff
    if previous is not None:
        node["previous"] = previous
    return node


# 结构抄自真机响应（2026-10-08 实测 uid=1823658）。
# ⚠️ 关键点：**顶层只有 2 条，但链上还挂着更早的场次** ——
#    只读顶层会安静地少掉一半比赛，而且看起来完全正常。
ELO_USER_JSON = {
    "instance": "main", "template": "user.show", "status": 200,
    "data": {
        "user": {"uid": "1823658", "name": "dsyfb437",
                 "passedProblemCount": 303, "elo": None},
        "elo": [
            _elo_node(293373, "【LGR-274-Div.2】洛谷 3 月月赛 II", 1774072800, 1270,
                      prev_diff=17,
                      previous=_elo_node(282947, "【LGR-267-Div.2】洛谷 2 月月赛 II",
                                         1770789600, 1253, prev_diff=103,
                                         previous=_elo_node(236252, "【LGR-266-Div.2】",
                                                            1770530400, 1150,
                                                            prev_diff=72))),
            # 第二条链**汇进**第一条（共享 236252）—— 去重必须挡住
            _elo_node(232936, "【LGR-236-Div.2】洛谷 8 月月赛 II", 1754719200, 3,
                      previous=_elo_node(250409, "【LGR-238-Div.2】", 1755064800, 462,
                                         prev_diff=459,
                                         previous=_elo_node(236252, "【LGR-266-Div.2】",
                                                            1770530400, 1150,
                                                            prev_diff=72))),
        ],
        "gu": {"rating": 135},
    },
}

ELO_USER_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<script id="lentille-context" type="application/json">%s</script>
</head><body></body></html>""" % json.dumps(ELO_USER_JSON, ensure_ascii=False)


def test_fetch_contests():
    print("\n[9] 比赛记录（data.elo）")

    async def main():
        # 能力位和实现必须一致 —— v0.5.19 的教训：`supports_problems = True`
        # 曾经挂在一个**没有 fetch_problems 的类**上
        check("★ 声明了 supports_contests 就真的有 fetch_contests",
              Luogu.supports_contests is True
              and callable(getattr(Luogu, "fetch_contests", None)),
              repr(Luogu.supports_contests))

        c = FakeClient({"/user/": Resp(200, ELO_USER_HTML)})
        got = await Luogu().fetch_contests("1823658", c)
        check("拉成功了", got.ok, got.detail)
        check("★ 顺着 previous 链走平了（顶层 2 条 → 5 场）",
              got.ok and len(got.items) == 5,
              repr(len(got.items) if got.ok else got.detail))
        if got.ok and got.items:
            by_id = {x.contest_id: x for x in got.items}
            check("★ 链上的比赛没漏（236252 在内）", "236252" in by_id,
                  repr(sorted(by_id)))
            check("★ 共享的 id 只进一次（去重挡得住）",
                  len(got.items) == len(by_id), repr(len(got.items)))
            check("名字取的是 contest.name",
                  by_id["293373"].name.startswith("【LGR-274"),
                  repr(by_id["293373"].name))
            check("start_epoch 取的是 contest.startTime",
                  by_id["293373"].start_epoch == 1774072800,
                  repr(by_id["293373"].start_epoch))
            check("★ rating_delta 取的是 prevDiff（+17）",
                  by_id["293373"].rating_delta == 17,
                  repr(by_id["293373"].rating_delta))
            check("链尾那场没有 prevDiff → delta 是 None（不是 0）",
                  by_id["232936"].rating_delta is None,
                  repr(by_id["232936"].rating_delta))
            check("平台标 luogu",
                  all(x.platform == "luogu" for x in got.items))
            check("按时间升序排",
                  [x.start_epoch for x in got.items]
                  == sorted(x.start_epoch for x in got.items),
                  repr([x.start_epoch for x in got.items][:3]))
            check("kind 是 rated",
                  all(x.kind == "rated" for x in got.items))

        # ★ v0.5.21 的教训用在这里：`[]`（确实没打过）和 `None`（拿不到）
        #   必须分开 —— 合并的话一次改版会**安静地清空**他的比赛记录
        check("★ 真没打过 → []（不是 None）",
              Luogu.parse_contest_history({"data": {"elo": []}}) == [])
        check("★ 结构变了 → None（不是 []，不能当成「没打过」）",
              Luogu.parse_contest_history({"data": {"elo": "boom"}}) is None)
        check("完全没有 elo 字段 → None",
              Luogu.parse_contest_history({"data": {}}) is None)

        empty = await Luogu().fetch_contests(
            "1823658", FakeClient({"/user/": Resp(200, ELO_USER_HTML.replace(
                '"elo": [', '"elo": [], "elo_old": ['))}))
        check("elo 是空数组时 ok=True 且 0 场", empty.ok and empty.items == [],
              repr((empty.ok, empty.items)))

        bad = await Luogu().fetch_contests(
            "1823658", FakeClient({"/user/": Resp(
                200, ELO_USER_HTML.replace('"elo": [', '"elo": "x", "elo_old": ['))}))
        check("★ elo 不是数组时报「页面结构变化」，不报 0 场",
              (not bad.ok) and bad.error_kind == "页面结构变化",
              repr((bad.ok, bad.error_kind, bad.detail)))

        # uid 填错：页面结构对，但没有 user → 凭据失效（不是"他 0 场比赛"）
        _nouser_html = ELO_USER_HTML.replace('"user": {', '"user_x": {').replace(
            '"uid": "1823658"', '"uid": "999999"')
        nouser = await Luogu().fetch_contests(
            "999999", FakeClient({"/user/": Resp(200, _nouser_html)}))
        check("uid 填错报「凭据失效」，不报 0 场",
              (not nouser.ok) and nouser.error_kind == "凭据失效",
              repr((nouser.ok, nouser.error_kind, nouser.detail)))

        noctx = await Luogu().fetch_contests(
            "1823658", FakeClient({"/user/": Resp(200, "<html>nope</html>")}))
        check("没有 lentille-context 时报「页面结构变化」",
              (not noctx.ok) and noctx.error_kind == "页面结构变化",
              repr((noctx.ok, noctx.error_kind)))

        blocked = await Luogu().fetch_contests(
            "1823658", FakeClient({"/user/": Resp(200, WELCOME_HTML)}))
        check("风控页报「挑战未过」",
              (not blocked.ok) and blocked.error_kind == "挑战未过",
              repr((blocked.ok, blocked.error_kind)))

        bad500 = await Luogu().fetch_contests(
            "1823658", FakeClient({"/user/": Resp(503, "")}))
        check("非 2xx 报「网络不可达」",
              (not bad500.ok) and bad500.error_kind == "网络不可达",
              repr((bad500.ok, bad500.error_kind)))

        check("没填 uid 直接报「凭据失效」",
              (await Luogu().fetch_contests("", c)).error_kind == "凭据失效")

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("洛谷适配器离线自测")
    print("=" * 62)
    test_parse()
    test_tags()
    test_fetch_problem()
    test_levels()
    test_login_honest()
    test_fetch_submissions()
    test_lg_verdict()
    test_fetch_problems()
    test_fetch_contests()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
