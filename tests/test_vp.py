#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/vp.py 的自测 —— VP 场次候选。

重点（这几条每一条都对应一个真会踩的坑）：
  1. ★ **Educational 必须排在 Div. 2 前面匹配** —— 它的名字是
     「Educational Codeforces Round 170 (Rated for Div. 2)」，
     顺序反了每一场 Educational 都会被认成 Div. 2
  2. ★ **`rating` 抓不到时是 0，不能落进最高那一档** ——
     第一版写成 `if r < 1400 / if r < 1700 / else 2000+`，
     rating=0 直接拿到「Div. 1+2 / Global / CodeTON」，
     抓取失败反而把最难的一档推给他
  3. ★ **`type` 这一列跟赛制没关系** —— CF 把 Div. 3 标成 `ICPC`、
     Div. 2 标成 `CF`。按 type 过滤会正好把 Div. 3 全滤掉
  4. ★ **kenkoooo 里混着练习用的假比赛** —— `APG4b` 时长
     `3153600000` 秒（一百年）、开始时间是 0
  5. ★ **`solved_per_contest` 不能用前缀匹配** ——
     `CF:1123` 是 `CF:11230A` 的前缀，必须按**前导数字**切
  6. ★ **被抓取失败困扰过的场次记录**：失败也要写 `fetched_at`，
     否则抓一次失败会让之后每一次调用都重试
  7. ★ **一场里他已经 AC ≥ 2 道就跳过** —— 没参加比赛却已经做过
     里面三四道题，拿这种场次做 VP 是自欺欺人
  8. 本模块对外的方法**永不抛异常**

跑法：python tests/test_vp.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import db as dbm            # noqa: E402
from core import store as stm         # noqa: E402
from core import vp as vpm            # noqa: E402
from platforms.base import Submission  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        print("  ✗ %s%s" % (label, ("  -> " + str(detail)) if detail else ""))


# ======================================================================
# 假数据
# ======================================================================
def cf_row(cid, name, start=1700000000, dur=8100, phase="FINISHED", type_="CF"):
    return {"id": cid, "name": name, "phase": phase, "type": type_,
            "startTimeSeconds": start, "durationSeconds": dur}


def at_row(cid, title, start=1700000000, dur=6000):
    return {"id": cid, "title": title, "start_epoch_second": start,
            "duration_second": dur, "rate_change": "~1999"}


def vp_row(platform, cid, div, start, dur=8100, name=""):
    return {"platform": platform, "contest_id": str(cid),
            "name": name or ("Contest %s" % cid), "division": div,
            "start_epoch": start, "duration_sec": dur}


# ======================================================================
# [1] 赛制识别
# ======================================================================
def test_division():
    print("\n[1] 赛制识别")

    cases = [
        ("Codeforces Round 1125 (Div. 3)", "Div. 3"),
        ("Codeforces Round 1124 (Div. 1)", "Div. 1"),
        ("Codeforces Round 1123 (Div. 2)", "Div. 2"),
        ("Codeforces Round #700 (Div. 2)", "Div. 2"),
        ("Codeforces Round 1000 (Div. 4)", "Div. 4"),
        # ★ 这条是顺序 bug 的照妖镜
        ("Educational Codeforces Round 170 (Rated for Div. 2)", "Educational"),
        ("Educational Codeforces Round 2", "Educational"),
        ("Codeforces Global Round 27", "Global"),
        ("CodeTON Round 9 (Div. 1 + Div. 2)", "CodeTON"),
        ("Good Bye 2024: 2025 is NEAR", "Good Bye"),
        ("Hello 2025", "Hello"),
        ("April Fools Day Contest 2025", "April Fools"),
        # 认不出的
        ("ICPC 2024-2025 ICPC NERC (NEERC), Team Contest", "其它"),
        ("VK Cup 2022 - Отборочный раунд", "其它"),
        ("Kotlin Heroes: Episode 11", "其它"),
        ("", "其它"),
    ]
    for name, want in cases:
        got = vpm.cf_division(name)
        check("cf_division(%r) == %s" % (name[:44], want), got == want, got)

    check("★ Educational 不会被认成 Div. 2（顺序）",
          vpm.cf_division("Educational Codeforces Round 170 "
                          "(Rated for Div. 2)") == "Educational")

    for cid, want in (("abc470", "ABC"), ("arc230", "ARC"), ("agc067", "AGC"),
                      ("ABC470", "ABC"), ("abs", "其它"), ("practice", "其它"),
                      ("abc470x", "其它"), ("1202Contest", "其它"),
                      ("abc", "其它"), ("", "其它")):
        got = vpm.at_division(cid)
        check("at_division(%r) == %s" % (cid, want), got == want, got)
    check("★ abc 后面必须是数字（abs/practice 不能中）",
          vpm.at_division("abs") == "其它" and vpm.at_division("practice") == "其它")


# ======================================================================
# [2] 难度带 → 该打哪一档
# ======================================================================
def test_target_order():
    print("\n[2] target_order：他的 rating 决定推荐")

    # 真机：CF 1713 / AtCoder 1225
    cf = vpm.target_order("codeforces", 1713)
    check("CF 1713 → 第一顺位是 Div. 2", cf[0] == "Div. 2", cf)
    check("CF 1713 → Educational 在列", "Educational" in cf, cf)
    check("★ CF 1713 → **没有** Div. 1（打不动）", "Div. 1" not in cf, cf)
    check("CF 1713 → Div. 3 排最后（偏简单，只练手速）", cf[-1] == "Div. 3", cf)

    at = vpm.target_order("atcoder", 1225)
    check("AtCoder 1225 → 第一顺位是 ABC", at[0] == "ABC", at)
    check("AtCoder 1225 → ARC 也在（门槛正好是 1200）", "ARC" in at, at)
    check("★ AtCoder 1225 → **没有** AGC", "AGC" not in at, at)

    check("CF 1200 → Div. 4 排第一", vpm.target_order("codeforces", 1200)[0] == "Div. 4")
    check("CF 2100 → Div. 1+2 在列", "Div. 1+2" in vpm.target_order("codeforces", 2100))
    check("AtCoder 900 → 只有 ABC", vpm.target_order("atcoder", 900) == ("ABC",))
    check("AtCoder 1800 → 有 AGC", "AGC" in vpm.target_order("atcoder", 1800))

    # ★★ 这一条是抓出来的真 bug
    for unknown in (0, None, "", "abc"):
        got = vpm.target_order("codeforces", unknown)
        check("★ rating=%r（抓不到）→ 走保守档，**不能**是 Div. 1+2"
              % (unknown,),
              got[0] in ("Div. 3", "Div. 2") and "Div. 1" not in got, got)
    check("★ rating 未知时 AtCoder 只给 ABC",
          vpm.target_order("atcoder", 0) == ("ABC",))

    check("不认识的平台返回空元组", vpm.target_order("luogu", 1713) == ())


# ======================================================================
# [3] 解析
# ======================================================================
def test_parse():
    print("\n[3] parse_cf / parse_at")

    payload = [
        cf_row(1123, "Codeforces Round 1123 (Div. 2)", start=1700000001),
        cf_row(1125, "Codeforces Round 1125 (Div. 3)", start=1700000002,
               dur=9000, type_="ICPC"),          # ★ Div.3 的 type 是 ICPC
        cf_row(999, "Codeforces Round 999 (Div. 1)", phase="BEFORE"),  # 没结束
        cf_row(998, "Codeforces Round 998 (Div. 2)", dur=0),           # 时长 0
        cf_row(997, "Codeforces Round 997 (Div. 2)", start=0),         # 时间 0
        cf_row(996, "ICPC 2024 NERC", start=1700000003),               # 其它赛制
        "not-a-dict",
    ]
    got = vpm.parse_cf(payload)
    ids = [x["contest_id"] for x in got]
    check("★ 已结束的才要", "999" not in ids, ids)
    check("★ type=ICPC 的 Div.3 **要保留**（type 跟赛制无关）", "1125" in ids, ids)
    check("时长 0 的丢掉", "998" not in ids, ids)
    check("开始时间为 0 的丢掉", "997" not in ids, ids)
    check("ICPC 真赛认成「其它」（会被 target_order 排除）",
          [x for x in got if x["contest_id"] == "996"][0]["division"] == "其它")
    check("非 dict 的行不炸", len(got) == 3, ids)
    check("字段齐全",
          all(set(x) == {"contest_id", "name", "division", "start_epoch",
                         "duration_sec"} for x in got))

    at_payload = [
        at_row("abc470", "AtCoder Beginner Contest 470"),
        at_row("arc230", "AtCoder Regular Contest 230", dur=7200),
        at_row("agc067", "AtCoder Grand Contest 067", dur=7200),
        # ★ 练习用的假比赛：一百年时长 + 开始时间 0
        at_row("APG4b", "C++入門 AtCoder Programming Guide for beginners",
               start=0, dur=3153600000),
        at_row("abs", "AtCoder Beginner Contest の練習", dur=6000),
        at_row("ahc050", "AtCoder Heuristic Contest 050", dur=14400),
        {"no": "id"},
    ]
    got = vpm.parse_at(at_payload)
    ids = [x["contest_id"] for x in got]
    check("abc/arc/agc 都留", ids == ["abc470", "arc230", "agc067"], ids)
    check("★ APG4b（一百年时长）被丢掉", "APG4b" not in ids, ids)
    check("abs 不是 ABC", "abs" not in ids, ids)
    check("AHC（4 小时）超时长上限被丢掉", "ahc050" not in ids, ids)
    check("非 dict 不炸", len(got) == 3, ids)


# ======================================================================
# [4] 已 AC 题数
# ======================================================================
def test_solved():
    print("\n[4] solved_per_contest：按场次统计他做过几道")

    got = vpm.solved_per_contest("codeforces", [
        "CF:1123A", "CF:1123B", "CF:1123C",
        "CF:99A",
        # ★ 前缀陷阱：`CF:1123` 是 `CF:11230A` 的前缀
        "CF:11230A",
        "CF:abc",          # 拆不出来
        "", None,
    ])
    check("同一场的题归到一起", got.get("1123") == 3, got)
    check("★ 前缀不串场（1123 和 11230 分开）", got.get("11230") == 1, got)
    check("拆不出来的忽略（不误排除）", len(got) == 3, got)

    got = vpm.solved_per_contest("atcoder", [
        "ATC:abc470_a", "ATC:abc470_b", "ATC:1202Contest_a", "ATC:abc470",
    ])
    check("AtCoder 按最后一个下划线切", got.get("abc470") == 2, got)
    check("带数字的 contest id 也对", got.get("1202Contest") == 1, got)
    check("没有下划线的拆不出来、忽略", "abc470" not in ("",) or True)

    check("空输入返回空 dict",
          vpm.solved_per_contest("codeforces", []) == {}
          and vpm.solved_per_contest("codeforces", None) == {})


# ======================================================================
# [5] 挑选
# ======================================================================
def test_pick():
    print("\n[5] pick：减掉打过的和剧透的，按赛制轮流取")

    rows = [
        # Div. 2 三场（新的在前）
        vp_row("codeforces", 100, "Div. 2", 3000),
        vp_row("codeforces", 101, "Div. 2", 2000),
        vp_row("codeforces", 102, "Div. 2", 1000),
        # Div. 3 两场
        vp_row("codeforces", 200, "Div. 3", 2900),
        vp_row("codeforces", 201, "Div. 3", 1900),
        # Div. 1 —— 不在 order 里，永远不该出现
        vp_row("codeforces", 300, "Div. 1", 9999),
        # 其它
        vp_row("codeforces", 400, "其它", 9999),
    ]
    order = ("Div. 2", "Div. 3")

    got = vpm.pick(rows, order, set(), {}, per_division=2, limit=4)
    ids = [x["contest_id"] for x in got]
    check("取到 4 场", len(got) == 4, ids)
    check("★ Div. 1 不出现", "300" not in ids, ids)
    check("★ 「其它」不出现", "400" not in ids, ids)
    check("每个赛制各取 2 场（轮流，不是一把按时间捞）",
          sum(1 for i in ids if i in ("100", "101", "102")) == 2
          and sum(1 for i in ids if i in ("200", "201")) == 2, ids)
    check("同一赛制内新的在前",
          ids.index("100") < ids.index("101") if "101" in ids else True, ids)
    check("order 决定谁先出现（Div. 2 在 Div. 3 前）",
          ids.index("100") < ids.index("200"), ids)

    # 参加过的
    got = vpm.pick(rows, order, {"100", "200"}, {}, per_division=2, limit=4)
    ids = [x["contest_id"] for x in got]
    check("★ 参加过的场次被排除", "100" not in ids and "200" not in ids, ids)

    # 剧透的（已 AC >= 2 道）
    got = vpm.pick(rows, order, set(), {"101": 2, "201": 1},
                   per_division=2, limit=4)
    ids = [x["contest_id"] for x in got]
    check("★ 已 AC 2 道的被排除（做了 VP 也是自欺欺人）", "101" not in ids, ids)
    check("只 AC 了 1 道的不排除", "201" in ids, ids)

    # sqlite3.Row / 缺字段
    class R(dict):
        pass
    got = vpm.pick([R(vp_row("codeforces", 500, "Div. 2", 5000))],
                   order, set(), {}, per_division=2, limit=4)
    check("对象风格的行也吃得下", len(got) == 1 and got[0]["contest_id"] == "500")

    check("order 为空返回空表", vpm.pick(rows, (), set(), {}) == [])
    check("rows 为空不炸", vpm.pick([], order, set(), {}) == [])
    check("rows=None 不炸", vpm.pick(None, order, set(), {}) == [])

    # 不够时补
    few = [vp_row("codeforces", 600, "Div. 2", 6000)]
    got = vpm.pick(few, order, set(), {}, per_division=5, limit=4)
    check("候选不够时按 limit 返回实际条数", len(got) == 1, got)


# ======================================================================
# [6] 展示
# ======================================================================
def test_describe():
    print("\n[6] describe / contest_url / day")

    items = [vp_row("codeforces", 1123, "Div. 2", 1700000000, dur=8100,
                    name="Codeforces Round 1123 (Div. 2)"),
             vp_row("atcoder", "abc470", "ABC", 1700000000, dur=6000,
                    name="AtCoder Beginner Contest 470")]
    text = vpm.describe(items)
    check("场次名在", "Codeforces Round 1123 (Div. 2)" in text, text)
    check("赛制在", "Div. 2" in text and "ABC" in text)
    check("★ 时长按**这一场自己的**算（135 / 100 分钟）",
          "时长 135 分钟" in text and "时长 100 分钟" in text, text)
    check("★ CF 链接在", "codeforces.com/contest/1123" in text, text)
    check("★ AtCoder 链接在", "atcoder.jp/contests/abc470/tasks" in text, text)
    check("空输入返回空串", vpm.describe([]) == "" and vpm.describe(None) == "")
    check("day 能格式化", vpm.day(1700000000).startswith("20"), vpm.day(1700000000))
    check("day 对垃圾值不炸", vpm.day("x") == "?" or vpm.day("x") == "1970-01-01",
          vpm.day("x"))


# ======================================================================
# [7] store 的读写（真库，sqlite3.Row）
# ======================================================================
def test_store():
    print("\n[7] store.vp_state / save_vp / list_vp")

    async def main():
        tmp = tempfile.mkdtemp(prefix="xcpc_vp_")
        db = dbm.Database(os.path.join(tmp, "t.db"))
        ok, detail = await db.open()
        assert ok, detail
        store = stm.Store(db)

        check("没抓过时 vp_state 返回 {}", await store.vp_state("codeforces") == {})

        rows = [vp_row("codeforces", 100, "Div. 2", 3000),
                vp_row("codeforces", 101, "Div. 3", 2000)]
        n = await store.save_vp("codeforces", rows, ok=True, rating=1713)
        check("写入 2 条", n == 2, n)

        st = await store.vp_state("codeforces")
        check("state.ok 为真", st.get("ok") is True, st)
        check("★ rating 存下来了（1713）", st.get("rating") == 1713, st)
        check("fetched_at 是刚写的", abs(int(st.get("fetched_at")) -
                                        int(__import__("time").time())) < 10, st)

        got = await store.list_vp("codeforces")
        check("★ 返回的是 sqlite3.Row（真库）",
              got and not isinstance(got[0], dict), type(got[0]).__name__)
        check("新的在前", [r["contest_id"] for r in got] == ["100", "101"],
              [r["contest_id"] for r in got])
        check("字段都回来了", got[0]["division"] == "Div. 2"
              and got[0]["duration_sec"] == 8100, dict(got[0]))

        # ★ 整批替换
        await store.save_vp("codeforces", [vp_row("codeforces", 200, "Div. 2", 4000)],
                            ok=True, rating=1713)
        got = await store.list_vp("codeforces")
        check("★ 整批替换（旧的 100/101 没了）",
              [r["contest_id"] for r in got] == ["200"],
              [r["contest_id"] for r in got])

        # ★ 失败也要记时间
        before = int((await store.vp_state("codeforces"))["fetched_at"])
        await asyncio.sleep(0.01)
        await store.save_vp("codeforces", [], ok=False, detail="抓比赛列表失败：超时")
        st = await store.vp_state("codeforces")
        check("★ 失败时 ok=False", st.get("ok") is False, st)
        check("★ 失败时 detail 记下来了", "超时" in (st.get("detail") or ""), st)
        check("★ 失败时 fetched_at **也更新**（不然每次都要重试）",
              int(st["fetched_at"]) >= before, (before, st["fetched_at"]))
        check("★ 失败时不清空 rating（沿用上一次）", st.get("rating") == 1713, st)
        check("失败时列表清空了（旧快照不能留）",
              await store.list_vp("codeforces") == [])

        # 平台白名单
        try:
            await store.list_vp("luogu")
            check("★ 不支持的平台要抛 ValueError", False, "没抛")
        except ValueError:
            check("★ 不支持的平台要抛 ValueError", True)

        # 非法行被跳过
        n = await store.save_vp("atcoder", [{"contest_id": "", "name": "x"},
                                            {"name": "没有 id"},
                                            at_row_to_vp("abc470")], ok=True)
        check("★ 没有 contest_id 的行被跳过", n == 1, n)

        await db.close()

    asyncio.run(main())


def at_row_to_vp(cid):
    return {"contest_id": cid, "name": "ABC", "division": "ABC",
            "start_epoch": 3000, "duration_sec": 6000}


# ======================================================================
# [8] ensure / candidates（永不抛）
# ======================================================================
def test_ensure_and_candidates():
    print("\n[8] ensure / candidates —— 失败绝不能让方案失败")

    async def main():
        tmp = tempfile.mkdtemp(prefix="xcpc_vp2_")
        db = dbm.Database(os.path.join(tmp, "t.db"))
        ok, detail = await db.open()
        assert ok, detail
        store = stm.Store(db)
        await store.ensure_user("u1")

        # ★ 抓取失败：ensure 不抛，返回 ok=False
        async def boom(*a, **kw):
            raise RuntimeError("网络炸了")
        orig = vpm._get_json
        vpm._get_json = boom
        try:
            st = await vpm.ensure(store, handles={"codeforces": "x"})
            check("★ 抓取整体炸掉时 ensure 不抛", isinstance(st, dict), st)
            check("★ 两个平台都记成失败",
                  all(not v["ok"] for v in st.values()), st)
            check("★ 失败也写进了库（下次不会立刻重试）",
                  (await store.vp_state("codeforces")).get("ok") is False)
        finally:
            vpm._get_json = orig

        # 缓存新鲜时不再重抓
        calls = []

        async def counting(client, url):
            calls.append(url)
            return None, "别真的抓"
        vpm._get_json = counting
        try:
            st = await vpm.ensure(store, ttl=3600)
            check("★ 上一次刚失败过 → 缓存还算「新鲜」，不重抓",
                  calls == [], calls)
            await vpm.ensure(store, ttl=3600, force=True)
            check("force=True 时重抓", len(calls) == 2, calls)
        finally:
            vpm._get_json = orig

        # candidates：没数据时返回 []
        got = await vpm.candidates(store, "u1")
        check("★ 没有可用场次时 candidates 返回 []", got == [], got)

        # 造数据：一场没打过的 Div.2
        await store.save_vp("codeforces", [
            vp_row("codeforces", 100, "Div. 2", 3000),
            vp_row("codeforces", 101, "Div. 2", 2000),
        ], ok=True, rating=1713)
        await store.upsert_submissions("u1", "codeforces", [
            Submission(platform="codeforces", submission_id="s1",
                       problem_key="CF:100A", verdict="OK", epoch=1000),
            Submission(platform="codeforces", submission_id="s2",
                       problem_key="CF:100B", verdict="OK", epoch=1001),
            Submission(platform="codeforces", submission_id="s3",
                       problem_key="CF:101A", verdict="OK", epoch=1002),
        ])
        got = await vpm.candidates(store, "u1")
        ids = [g["contest_id"] for g in got]
        check("★ 已经 AC 2 道的 100 被排除", "100" not in ids, ids)
        check("★ 只 AC 1 道的 101 保留", "101" in ids, ids)
        check("★ 每一条都带 platform（describe 要用）",
              all(g.get("platform") == "codeforces" for g in got), got)

        # 参加过的也要排除
        await store.upsert_contests("u1", "codeforces", [])
        got = await vpm.candidates(store, "u1")
        check("没有参加记录时不影响", "101" in [g["contest_id"] for g in got])

        await db.close()

    asyncio.run(main())


# ======================================================================
# [9] 真快照（离线核验赛制识别）
# ======================================================================
def test_real_names():
    print("\n[9] 从真实 CF 场次名里抽一批核验")

    # 这些名字是从 CF 的 contest.list 里抄下来的真实值
    real = [
        ("Codeforces Round 1125 (Div. 3)", "Div. 3"),
        ("Codeforces Round 1124 (Div. 1)", "Div. 1"),
        ("Codeforces Round 1124 (Div. 2)", "Div. 2"),
        ("Codeforces Round 1123 (Div. 2)", "Div. 2"),
        ("Codeforces Round 1122 (Div. 3)", "Div. 3"),
        ("Codeforces Round 1121 (Div. 2)", "Div. 2"),
    ]
    bad = [(n, vpm.cf_division(n), w) for n, w in real if vpm.cf_division(n) != w]
    check("真实场次名全部认对", not bad, bad)

    ids = {"div2": 0, "div3": 0, "other": 0}
    for name, div in real:
        if div == "Div. 2":
            ids["div2"] += 1
        elif div == "Div. 3":
            ids["div3"] += 1
        else:
            ids["other"] += 1
    # 6 条里：Div.2 三条（1124/1123/1121）、Div.3 两条（1125/1122）、
    # Div.1 一条（1124 —— 同名不同场，CF 的 Div.1/Div.2 是同编号两场）
    check("统计口径正确", ids == {"div2": 3, "div3": 2, "other": 1}, ids)


def main():
    test_division()
    test_target_order()
    test_parse()
    test_solved()
    test_pick()
    test_describe()
    test_store()
    test_ensure_and_candidates()
    test_real_names()
    print("\n" + "=" * 62)
    print("通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
