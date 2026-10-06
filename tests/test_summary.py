#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/summary.py 的自测。

重点：
  1. **不跨难度来源合并**（CF rating / AtCoder IRT 是三套尺子）
  2. 收缩（样本少不许下结论）
  3. 难度回避的判定
  4. **"没有数据"不等于"水平是零"**
  5. 候选池只出没做过的、且不能编造

跑法：python tests/test_summary.py
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

from core import db as dbm        # noqa: E402
from core import store as stm     # noqa: E402
from core import summary as summ  # noqa: E402
from platforms.base import ContestRecord, Problem, Submission  # noqa: E402

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


def sub(sid, key, verdict="OK", epoch=1000, diff=None, src="cf_rating"):
    return Submission(platform="codeforces", submission_id=str(sid),
                      problem_key=key, verdict=verdict, epoch=epoch,
                      difficulty=diff, difficulty_source=src)


async def fresh():
    tmp = tempfile.mkdtemp(prefix="xcpc_summ_")
    db = dbm.Database(os.path.join(tmp, "t.db"))
    ok, detail = await db.open()
    assert ok, detail
    return db, stm.Store(db)


# ---------------------------------------------------------------------------
# 1. 收缩
# ---------------------------------------------------------------------------

def test_shrink():
    print("\n[1] 经验贝叶斯收缩")
    # 2 题对 2 题，不该被当成 100%
    a = summ.shrunk_rate(2, 2, prior_rate=0.5, prior_weight=10)
    check("2/2 被拉向先验（不是 100%%）", a < 0.7, "%.3f" % a)
    # 200 题对 170 题，应该接近 85%
    b = summ.shrunk_rate(170, 200, prior_rate=0.5, prior_weight=10)
    check("170/200 接近 85%%", 0.82 < b < 0.87, "%.3f" % b)
    check("样本多的更接近真实值", b > a)
    check("零样本返回先验", abs(summ.shrunk_rate(0, 0, 0.5) - 0.5) < 1e-9)
    # 边界：ok 比 total 大（不该发生，但要兜住）
    check("ok>total 被夹住", summ.shrunk_rate(10, 5, 0.5) <= 1.0)
    check("负数被夹住", summ.shrunk_rate(-5, 10, 0.5) >= 0.0)


# ---------------------------------------------------------------------------
# 2. 不跨来源合并难度
# ---------------------------------------------------------------------------

def test_no_cross_source():
    print("\n[2] 不跨难度来源合并")

    async def main():
        db, store = await fresh()
        await store.ensure_user("u1")
        # CF rating 1500，AtCoder IRT -200（IRT 可以是负数）
        await store.upsert_submissions("u1", "codeforces", [
            sub(1, "CF:1A", diff=1500), sub(2, "CF:2B", diff=1700)])
        await store.upsert_submissions("u1", "atcoder", [
            sub(3, "ATC:x", diff=-200, src="atcoder_irt"),
            sub(4, "ATC:y", diff=800, src="atcoder_irt")])

        s = await summ.build(store, "u1")
        check("两个来源分开统计", set(s.by_source) == {"cf_rating", "atcoder_irt"},
              repr(list(s.by_source)))
        check("CF 有自己的中位难度",
              s.by_source["cf_rating"]["median"] == 1600.0,
              repr(s.by_source["cf_rating"]["median"]))
        check("AtCoder 有自己的中位难度",
              s.by_source["atcoder_irt"]["median"] == 300.0,
              repr(s.by_source["atcoder_irt"]["median"]))

        text = s.to_text()
        check("汇总里两个来源都出现", "cf_rating" in text and "atcoder_irt" in text)
        check("明确提示两者不可比",
              "不能和 CF rating 直接比大小" in text or "不是一套尺子" in text,
              text[:300])

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. 难度回避
# ---------------------------------------------------------------------------

def test_avoidance():
    print("\n[3] 难度回避判定")

    async def main():
        db, store = await fresh()
        await store.ensure_user("u1")

        # 题库：dp/graphs 很难（2400），greedy/math 相对简单（1500）
        probs = []
        for i in range(60):
            probs.append(Problem("codeforces", "CF:DP%d" % i, "dp%d" % i,
                                 tags=["dp"], difficulty=2400,
                                 difficulty_source="cf_rating"))
        for i in range(60):
            probs.append(Problem("codeforces", "CF:GR%d" % i, "gr%d" % i,
                                 tags=["graphs"], difficulty=2300,
                                 difficulty_source="cf_rating"))
        for i in range(200):
            probs.append(Problem("codeforces", "CF:MA%d" % i, "ma%d" % i,
                                 tags=["math"], difficulty=1400,
                                 difficulty_source="cf_rating"))
        await store.upsert_problems("codeforces", probs)

        # 他做了 100 道 math，1 道 dp，0 道 graphs
        subs = [sub(i, "CF:MA%d" % i, diff=1400) for i in range(100)]
        subs.append(sub(999, "CF:DP0", diff=2400))
        await store.upsert_submissions("u1", "codeforces", subs)

        bank = {"codeforces": {}}
        for p in probs:
            bank["codeforces"][p.problem_key] = {
                "tags": p.tags, "difficulty": p.difficulty,
                "difficulty_source": p.difficulty_source, "title": p.title}

        s = await summ.build(store, "u1", bank=bank)
        tags = {a["tag"] for a in s.avoided}
        check("认出 dp 是回避方向", "dp" in tags, repr(tags))
        check("认出 graphs 是回避方向", "graphs" in tags, repr(tags))
        check("不把练得多的 math 当成回避", "math" not in tags, repr(tags))

        dp = [a for a in s.avoided if a["tag"] == "dp"]
        if dp:
            check("给出了题库中位难度作为依据", dp[0]["bank_median"] > 2000,
                  repr(dp[0]["bank_median"]))
            check("给出了占比", 0 <= dp[0]["share"] < 0.04, repr(dp[0]["share"]))

        text = s.to_text()
        check("汇总里有「疑似难度回避」一节", "疑似难度回避" in text)
        check("如实说明这只是假设，不是结论",
              "假设" in text and "不是结论" in text, "")

        # 真实数据下这里会列出 20 个方向（91 道 AC 的样本下大部分 tag 都像回避），
        # 一次给 20 条等于没给 —— 必须收敛成"最突出的几条 + 其余只报个数"
        check("回避方向列表被收敛（不超过 6 条明细）",
              text.count("只做过") <= 6, "明细 %d 条" % text.count("只做过"))

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. 没有数据 ≠ 水平是零
# ---------------------------------------------------------------------------

def test_empty_data():
    print("\n[4] 「没有数据」不等于「水平是零」")

    async def main():
        db, store = await fresh()
        await store.ensure_user("u1")
        s = await summ.build(store, "u1")
        check("空数据不崩", s.total_submissions == 0)
        text = s.to_text()
        check("明确说这是「还没拉到」而不是「零」",
              "没有数据" in text and "不等于" in text, text[:200])
        check("空数据时 by_source 是空的（不编造统计）", not s.by_source)
        check("空数据时没有薄弱项（而不是「全部薄弱」）", not s.weak_tags)
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 5. 候选池
# ---------------------------------------------------------------------------

def test_candidates():
    print("\n[5] 候选池")
    bank = {"codeforces": {}}
    for i in range(20):
        d = 1000 + i * 100
        bank["codeforces"]["CF:P%d" % i] = {
            "tags": ["dp"] if i % 2 else ["math"], "difficulty": d,
            "difficulty_source": "cf_rating", "title": "P%d" % i}

    subs = [sub(1, "CF:P0"), sub(2, "CF:P1")]      # 已 AC 两道
    out = summ.pick_candidates(subs, bank, limit=50)
    keys = {c["problem_key"] for c in out}
    check("排除已 AC 的题", "CF:P0" not in keys and "CF:P1" not in keys,
          repr(sorted(keys)[:5]))
    check("返回了其他题", len(out) == 18, "拿到 %d" % len(out))

    # 按 tag 偏好
    out2 = summ.pick_candidates(subs, bank, want_tags=["dp"], limit=5)
    check("优先返回指定 tag", all("dp" in c["tags"] for c in out2),
          repr([c["tags"] for c in out2]))

    # 难度区间
    out3 = summ.pick_candidates(subs, bank, min_difficulty=1500,
                                max_difficulty=1700, limit=50)
    check("难度区间生效",
          all(1500 <= c["difficulty"] <= 1700 for c in out3),
          repr([c["difficulty"] for c in out3]))
    check("区间外的被排除", len(out3) < len(out), "%d vs %d" % (len(out3), len(out)))

    # 只出指定来源
    bank["codeforces"]["ATC:zzz"] = {"tags": ["dp"], "difficulty": 1500,
                                     "difficulty_source": "atcoder_irt",
                                     "title": "z"}
    out4 = summ.pick_candidates([], bank, source="cf_rating", limit=99)
    check("不混入别的难度来源",
          all(c["difficulty_source"] == "cf_rating" for c in out4),
          repr({c["difficulty_source"] for c in out4}))


# ---------------------------------------------------------------------------
# 6. 标签缺失要说清
# ---------------------------------------------------------------------------

def test_missing_bank():
    print("\n[6] 题库标注缺失")

    async def main():
        db, store = await fresh()
        await store.ensure_user("u1")
        await store.upsert_submissions("u1", "codeforces",
                                       [sub(i, "CF:%dA" % i) for i in range(20)])
        s = await summ.build(store, "u1", bank={})
        text = s.to_text()
        check("说明「没法按标签分析」而不是「没有薄弱项」",
              "没法按算法标签分析" in text, text[:400])
        check("没有编造薄弱项", not s.weak_tags)
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 7. 文本长度受控
# ---------------------------------------------------------------------------

def test_text_length():
    print("\n[7] 汇总长度受控")

    async def main():
        db, store = await fresh()
        await store.ensure_user("u1")
        await store.upsert_submissions("u1", "codeforces",
                                       [sub(i, "CF:%dA" % i) for i in range(50)])
        # 造一堆候选
        cands = [{"problem_key": "CF:X%d" % i, "title": "title %d" % i,
                  "tags": ["dp"], "difficulty": 1500,
                  "difficulty_source": "cf_rating"} for i in range(500)]
        s = await summ.build(store, "u1", candidates=cands)
        text = s.to_text(max_chars=3000)
        check("超长时被截断", len(text) <= 3400, "实际 %d" % len(text))
        check("截断的是候选池，诊断部分保留",
              "总量" in text and "候选" in text, "")
        await db.close()

    asyncio.run(main())


def test_execution():
    print("\n[8] 执行情况进汇总（循环闭环）")

    async def main():
        db, store = await fresh()
        await store.ensure_user("u1")
        await store.upsert_submissions("u1", "codeforces",
                                       [sub(i, "CF:%dA" % i) for i in range(20)])

        # 没打卡时不该编出一段"执行率 0%"
        s0 = await summ.build(store, "u1")
        check("没打卡时 execution 为空（不编 0%）",
              not s0.execution.get("rate") and "0%" not in s0.to_text(),
              repr(s0.execution.get("rate")))

        # 打卡之后
        for d, st in (("2026-10-01", "done"), ("2026-10-02", "done"),
                      ("2026-10-03", "partial"), ("2026-10-04", "skipped")):
            await store.log_task("u1", st, date=d, note="测试")
        s = await summ.build(store, "u1")
        text = s.to_text()
        check("汇总里有执行情况一节", "上一版方案的执行情况" in text)
        check("说明了这是他自己打的卡（不是猜的）",
              "不是猜的" in text, text[:600])
        check("天数对", s.execution["days"] == 4, repr(s.execution["days"]))
        check("执行率把「一半」算半次（2 + 0.5）/4 = 62.5%",
              abs(s.execution["rate"] - 0.625) < 1e-9, repr(s.execution["rate"]))
        check("明确要求模型先看这段", "先看这个" in text, text[:700])

        # 执行率低时给"改计划"的提示，而不是评判人
        db2, store2 = await fresh()
        await store2.ensure_user("u2")
        for i in range(6):
            await store2.log_task("u2", "skipped", date="2026-09-%02d" % (i + 1))
        s2 = await summ.build(store2, "u2")
        t2 = s2.to_text()
        check("执行率低时提示「量排多了」而不是「他不努力」",
              "量排多了" in t2, t2[:800])
        check("说清「连着做不完的计划等于没有计划」",
              "没有计划" in t2, t2[:900])

        await db.close()
        await db2.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/summary.py 自测")
    print("=" * 62)
    test_shrink()
    test_no_cross_source()
    test_avoidance()
    test_empty_data()
    test_candidates()
    test_missing_bank()
    test_text_length()
    test_execution()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
