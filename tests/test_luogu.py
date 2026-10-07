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
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from platforms.luogu import Luogu, LEVEL_NAMES, _blocked  # noqa: E402
from platforms.base import Fetched  # noqa: E402

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
TAGS_JSON = {
    "tags": [
        {"id": -2, "name": "语言入门", "type": 2, "parent": None},
        {"id": 1, "name": "模拟", "type": 2, "parent": 110},
        {"id": 7, "name": "递归", "type": 2, "parent": None},
        {"id": 8, "name": "快速傅里叶变换 FFT", "type": 2, "parent": None},
        {"id": 9, "name": "快速数论变换 NTT", "type": 2, "parent": None},
        {"id": 10, "name": "", "type": 2, "parent": None},      # 空名字，应被丢
    ],
    "types": [{"id": 2, "type": "Algorithm", "name": "算法"}],
    "_locale": "zh-CN",
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
        check("verdict 是字符串（不是 None）",
              got.ok and got.items[0].verdict == "12",
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
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
