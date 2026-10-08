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
        # ⚠️ 真机上翻过的车：以前这里渲染成「最近 0 天：完成 0 天，做了一半 0 天，
        # 没做 0 天」，模型读成"上一版方案 0 天打卡 → 之前排的量没接住"。
        # **0/0 不是"执行率低"，是"没有数据"。**
        t0 = s0.to_text()
        check("一次卡都没打时明说没有数据", "一次卡都没打过" in t0, t0[:900])
        check("不许拿没打卡去推断执行力",
              "不要据此判断他的执行力" in t0, t0[:900])
        check("不许出现「最近 0 天：完成 0 天」这种读起来像失败的写法",
              "最近 0 天" not in t0, t0[:900])

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


# ---------------------------------------------------------------------------
# 9. 回避方向的排序：不能只看难度差（用户问「dp 的权重是不是该大一点」）
# ---------------------------------------------------------------------------

def test_avoidance_weight():
    """2026-10-08 真机反馈。

    用户的推送里，第一个回避方向是 `string suffix structures`（他只碰过 1 题），
    他看完直接问「dp 是目标权重是不是应该大一点」。他说得对：`gap` 只回答
    "这个方向比整体难多少"，完全没回答"这个方向有多常见"。一场区域赛里
    dp 几乎必然出现，后缀结构可能一整年碰不到一次 —— 先补哪个不用想。

    这一节要钉住的是：**即使 fft 的难度差更大，dp 也得排在它前面**。
    """
    print("\n[9] 回避方向的排序权重（难度差 × 有多常见）")

    async def main():
        db, store = await fresh()
        await store.ensure_user("u1")

        probs = []
        # dp：题库里量很大（500 题），难度 2400
        for i in range(500):
            probs.append(Problem("codeforces", "CF:DP%d" % i, "dp%d" % i,
                                 tags=["dp"], difficulty=2400,
                                 difficulty_source="cf_rating"))
        # fft：更偏更难（3000），但题库里只有 30 题
        for i in range(30):
            probs.append(Problem("codeforces", "CF:FF%d" % i, "ff%d" % i,
                                 tags=["fft"], difficulty=3000,
                                 difficulty_source="cf_rating"))
        # 垫底的简单题，占多数好把整体中位压到 1400
        for i in range(800):
            probs.append(Problem("codeforces", "CF:MA%d" % i, "ma%d" % i,
                                 tags=["math"], difficulty=1400,
                                 difficulty_source="cf_rating"))
        await store.upsert_problems("codeforces", probs)

        # 他只做 math，dp / fft 一个没碰
        subs = [sub(i, "CF:MA%d" % i, diff=1400) for i in range(100)]
        await store.upsert_submissions("u1", "codeforces", subs)

        bank = {"codeforces": {}}
        for p in probs:
            bank["codeforces"][p.problem_key] = {
                "tags": p.tags, "difficulty": p.difficulty,
                "difficulty_source": p.difficulty_source, "title": p.title}

        s = await summ.build(store, "u1", bank=bank)
        order = [a["tag"] for a in s.avoided]
        check("dp 和 fft 都被认成回避方向",
              "dp" in order and "fft" in order, repr(order))

        dp = [a for a in s.avoided if a["tag"] == "dp"]
        fft = [a for a in s.avoided if a["tag"] == "fft"]
        if dp and fft:
            check("前提：fft 的难度差**更大**（3000 vs 2400）",
                  fft[0]["gap"] > dp[0]["gap"],
                  "fft %.0f vs dp %.0f" % (fft[0]["gap"], dp[0]["gap"]))
            check("★ 但 dp 排在 fft 前面（核心方向 + 题库里常见）",
                  order.index("dp") < order.index("fft"), repr(order))
            check("★ 只按 gap 排的话 fft 才是第一 —— 那正是用户看到的那一幕",
                  fft[0]["gap"] == max(a["gap"] for a in s.avoided), repr(order))
            check("dp 被标成绕不过去的方向",
                  dp[0].get("core") is True, repr(dp[0].get("core")))
            check("fft 没被标", fft[0].get("core") is False,
                  repr(fft[0].get("core")))
            check("每条都留了可比的 score（不是黑箱排序）",
                  all("score" in a for a in s.avoided), "")
            check("dp 的 score 确实比 fft 高",
                  dp[0]["score"] > fft[0]["score"],
                  "dp %.0f vs fft %.0f" % (dp[0]["score"], fft[0]["score"]))

        text = s.to_text()
        check("★ 汇总里说清楚排序不是只按难度差",
              "有多常见" in text, "")
        check("核心方向在正文里带标记", "绕不过去" in text, "")

        await db.close()

    asyncio.run(main())


def test_band_by_source():
    """2026-10-08 真机反馈：`_band` 拿 CF 的尺子去量洛谷。

    真机上 LLM 收到的那一行是：

        【luogu_level，洛谷 1-7 档，中位难度 2.0】 0-199：308 题

    洛谷的 difficulty 是 1..7，用 CF 那把 200 一档的尺子去分，
    七个档全落进 `0-199`。而 `SYSTEM_PROMPT` 硬规则第 3 条明令禁止
    跨平台比较难度 —— 规则写了，结果是我们在渲染层先破的戒。
    """
    print("\n[10] 分档要按难度来源换尺子")

    check("CF 还是 200 一档", summ._band(1100) == "1000-1199", summ._band(1100))
    check("显式传 cf_rating 也一样",
          summ._band(2400, "cf_rating") == "2400-2599", summ._band(2400, "cf_rating"))
    check("★ 洛谷直接用官方 1-7 档的名字，不折算成 rating",
          summ._band(3, "luogu_level") == "3 档（普及/提高-）",
          summ._band(3, "luogu_level"))
    check("洛谷 1 档（入门）",
          summ._band(1, "luogu_level") == "1 档（入门）", summ._band(1, "luogu_level"))
    check("洛谷 7 档（NOI/NOI+/CTSC）",
          summ._band(7, "luogu_level") == "7 档（NOI/NOI+/CTSC）",
          summ._band(7, "luogu_level"))
    check("★ 洛谷档名里带减号，`_band_key` 不能因此变成 0",
          summ._band_key("3 档（普及/提高-）") == 3,
          str(summ._band_key("3 档（普及/提高-）")))
    check("`_band_key` 认 5 档（提高+/省选-）",
          summ._band_key("5 档（提高+/省选-）") == 5,
          str(summ._band_key("5 档（提高+/省选-）")))
    check("`_band_key` 认 CF 的区间名",
          summ._band_key("1000-1199") == 1000, str(summ._band_key("1000-1199")))
    check("AtCoder IRT 可以是负数（档宽 400）",
          summ._band(-50, "atcoder_irt") == "-400~-1",
          summ._band(-50, "atcoder_irt"))
    check("AtCoder IRT 正数", summ._band(800, "atcoder_irt") == "800~1199",
          summ._band(800, "atcoder_irt"))


def test_luogu_bands_in_text():
    """七个档要逐条列出来，不能再并成一个 `0-199`。"""
    print("\n[11] 洛谷的难度分布按 1-7 档列出")

    async def main():
        db, store = await fresh()
        await store.ensure_user("u1")
        await store.upsert_submissions("u1", "luogu", [
            Submission(platform="luogu", submission_id="L%d" % i,
                       problem_key="LG:P%d" % i, verdict="AC",
                       epoch=1000 + i, difficulty=(i % 7) + 1,
                       difficulty_source="luogu_level") for i in range(140)])

        s = await summ.build(store, "u1", bank={"codeforces": {}})
        text = s.to_text()

        check("★ 不再出现 `0-199` 这一档", "0-199" not in text, "")
        check("★ 1 档（入门）在", "1 档（入门）" in text, "")
        check("3 档（普及/提高-）在 —— 档名里的减号没把它吃掉",
              "3 档（普及/提高-）" in text, "")
        check("7 档（NOI/NOI+/CTSC）在", "7 档（NOI/NOI+/CTSC）" in text, "")
        check("七个档都单独成行",
              all(("%d 档（" % k) in text for k in range(1, 8)), "")
        check("来源标签还是「洛谷 1-7 档」", "洛谷 1-7 档" in text, "")
        bands = (s.by_source.get("luogu_level") or {}).get("bands") or {}
        check("分档结果真的有 7 个桶（不是 1 个）", len(bands) == 7, repr(sorted(bands)))
        check("难度中位数是 2 这种小数字，不是 rating",
              (s.by_source.get("luogu_level") or {}).get("median") == 4.0,
              str((s.by_source.get("luogu_level") or {}).get("median")))
        await db.close()

    asyncio.run(main())


async def _tagged_fixture():
    """造一份"题库里只有 CF、洛谷题全没标签"的数据。

    这正是真机上的样子：他 AC 了 444 题，但题库只有 CF 的 ——
    洛谷那 308 道题不在题库里，压根没有标签。
    """
    db, store = await fresh()
    await store.ensure_user("u1")
    probs = []
    for i in range(400):
        probs.append(Problem("codeforces", "CF:TR%d" % i, "tr%d" % i,
                             tags=["trees"], difficulty=2400,
                             difficulty_source="cf_rating"))
    for i in range(600):
        probs.append(Problem("codeforces", "CF:MA%d" % i, "ma%d" % i,
                             tags=["math"], difficulty=1400,
                             difficulty_source="cf_rating"))
    await store.upsert_problems("codeforces", probs)
    # CF 上 AC 了 10 道 math（有标签）
    await store.upsert_submissions("u1", "codeforces",
                                   [sub(i, "CF:MA%d" % i, diff=1400)
                                    for i in range(10)])
    # 洛谷上又 AC 了 90 道（题库里没有洛谷题 → 没有标签）
    await store.upsert_submissions("u1", "luogu", [
        Submission(platform="luogu", submission_id="L%d" % i,
                   problem_key="LG:P%d" % i, verdict="AC",
                   epoch=2000 + i, difficulty=2,
                   difficulty_source="luogu_level") for i in range(90)])
    bank = {"codeforces": {}}
    for p in probs:
        bank["codeforces"][p.problem_key] = {
            "tags": p.tags, "difficulty": p.difficulty,
            "difficulty_source": p.difficulty_source, "title": p.title}
    return db, store, bank


def test_tagged_denominator():
    """回避判定的分母只能是「**有标签的** AC 题」。

    真机翻车：分母写成了"全部 AC 题"（444），而 `solved_by_tag` 只能数
    有标签的题 —— 分母被稀释 5 倍，模型还会把它读成「444 题里 0 题」，
    输出「trees 和 graphs 各 0 题」（用户一看就知道不对，他做过的题里
    明明有树）。
    """
    print("\n[12] 回避判定的分母是「有标签的 AC 题」")

    async def main():
        db, store, bank = await _tagged_fixture()
        s = await summ.build(store, "u1", bank=bank)
        text = s.to_text()
        tr = [a for a in s.avoided if a["tag"] == "trees"]
        check("trees 被认成回避方向", bool(tr),
              repr([a["tag"] for a in s.avoided]))
        if tr:
            check("★ 分母是 10（有标签的），不是 100（全部 AC）",
                  tr[0]["share_base"] == 10, str(tr[0]["share_base"]))
        check("★ 正文里也写的是 10", "分母是 10 道" in text, "")
        check("不会写成 100 道", "分母是 100 道" not in text, "")
        check("口径里说清了统计覆盖范围（题库只有 CF 的题有标签）",
              any("标签" in n for n in s.notes), repr(s.notes))
        await db.close()

    asyncio.run(main())


def test_candidate_avoid_marker():
    """候选池里要**当场标出**哪些题属于回避方向。

    真机翻车：候选池前 12 条全是树题（标签命中多的排前面），
    模型就一口气排了三道树 —— 而提示词里明明写着「一道就够」。
    光在提示词里加一句"不要贪多"没用，**把判据摊在它眼前**才有用。
    """
    print("\n[13] 候选池要标出回避方向")

    async def main():
        db, store, bank = await _tagged_fixture()
        subs = await store.list_submissions("u1", limit=10000)
        # 区间要罩得住 trees（2400）—— 罩不住的话候选池会退回 math，
        # 那就测不到标记了。
        cands = summ.pick_candidates(subs, bank, want_tags=["trees"], limit=6,
                                     min_difficulty=2000, max_difficulty=2800)
        s = await summ.build(store, "u1", bank=bank, candidates=cands)
        text = s.to_text()
        check("候选池挑出了题", bool(cands), "")
        check("挑的确实都是 trees 方向",
              all("trees" in (c.get("tags") or []) for c in cands), "")
        check("★ 正文里出现「← 回避方向：」标记", "← 回避方向：" in text, "")
        check("★ 并提醒最多挑一道", "最多挑一道" in text, "")
        await db.close()

    asyncio.run(main())


async def _alias_fixture():
    """造一份"题库里**同时有** CF 和洛谷"的数据。

    这是 v0.5.19 之后真机上的样子（洛谷题库拉起来了）。
    CF 的标签是英文粗标签（`trees`），洛谷的是中文细标签（`树形数据结构`）——
    **两套词表说的是同一件事**。不归一的话：
      * `solved_by_tag` 把同一个方向拆成两行，两边题量都偏低
      * `tagged_solved`（分母）把洛谷题算进去，而分子只认 CF 名字
    """
    db, store = await fresh()
    await store.ensure_user("u1")
    probs = []
    for i in range(400):
        probs.append(Problem("codeforces", "CF:TR%d" % i, "tr%d" % i,
                             tags=["trees"], difficulty=2400,
                             difficulty_source="cf_rating"))
    for i in range(600):
        probs.append(Problem("codeforces", "CF:MA%d" % i, "ma%d" % i,
                             tags=["math"], difficulty=1400,
                             difficulty_source="cf_rating"))
    for i in range(30):
        probs.append(Problem("codeforces", "CF:FF%d" % i, "ff%d" % i,
                             tags=["fft"], difficulty=2600,
                             difficulty_source="cf_rating"))
    lg = [Problem("luogu", "LG:P%d" % i, "p%d" % i,
                  tags=["树形数据结构"], difficulty=6,
                  difficulty_source="luogu_level") for i in range(300)]
    probs.extend(lg)

    await store.upsert_problems("codeforces", [p for p in probs
                                               if p.platform == "codeforces"])
    await store.upsert_problems("luogu", lg)

    # CF：AC 10 道 math、1 道 trees；洛谷：AC 20 道（标签是「树形数据结构」）
    await store.upsert_submissions("u1", "codeforces",
                                   [sub(i, "CF:MA%d" % i, diff=1400)
                                    for i in range(10)]
                                   + [sub(900, "CF:TR0", diff=2400)])
    await store.upsert_submissions("u1", "luogu", [
        Submission(platform="luogu", submission_id="L%d" % i,
                   problem_key="LG:P%d" % i, verdict="AC",
                   epoch=2000 + i, difficulty=6,
                   difficulty_source="luogu_level") for i in range(20)])

    bank = {"codeforces": {}, "luogu": {}}
    for p in probs:
        bank[p.platform][p.problem_key] = {
            "tags": p.tags, "difficulty": p.difficulty,
            "difficulty_source": p.difficulty_source, "title": p.title,
        }
    return db, store, bank


def test_tag_alias():
    """各平台的标签名要归一到**一套**词表（`TAG_ALIAS` / `_canon_tags`）。

    真机背景：他 444 题 AC 里"有标签的只有 76 题"，那 76 全是 CF。
    洛谷那 333 道 AC 因为词表不通（CF 说 `trees`，洛谷说 `树形数据结构`），
    在标签分析里**根本不存在** —— 这是整个诊断里最大的一处失真。
    """
    print("\n[14] 标签归一（CF 的 trees = 洛谷的「树形数据结构」）")

    # ---- 纯函数 ----
    check("★ None 原样透传（「给不出标签」和「标签是空列表」是两回事）",
          summ._canon_tags(None) is None, repr(summ._canon_tags(None)))
    check("空列表还是空列表", summ._canon_tags([]) == [], repr(summ._canon_tags([])))
    check("★ 两个中文标签都映到 dp 时**去重**（不然 solved_by_tag 会灌水）",
          summ._canon_tags(["动态规划 DP", "线性 DP"]) == ["dp"],
          repr(summ._canon_tags(["动态规划 DP", "线性 DP"])))
    check("CF 的标签本身就是规范名，原样通过",
          summ._canon_tags(["trees", "dp"]) == ["trees", "dp"],
          repr(summ._canon_tags(["trees", "dp"])))
    check("★ 一道题同时带两套词表的同一个方向时也去重",
          summ._canon_tags(["trees", "树形数据结构"]) == ["trees"],
          repr(summ._canon_tags(["trees", "树形数据结构"])))
    check("★ 映射表里没有的标签**原样保留**（不硬塞一个不准确的 CF 名）",
          summ._canon_tags(["Lyndon 分解"]) == ["Lyndon 分解"],
          repr(summ._canon_tags(["Lyndon 分解"])))
    check("并查集 → dsu", summ.TAG_ALIAS.get("并查集") == "dsu",
          repr(summ.TAG_ALIAS.get("并查集")))
    check("图论 → graphs", summ.TAG_ALIAS.get("图论") == "graphs",
          repr(summ.TAG_ALIAS.get("图论")))
    check("最短路 → shortest paths",
          summ.TAG_ALIAS.get("最短路") == "shortest paths",
          repr(summ.TAG_ALIAS.get("最短路")))

    async def main():
        db, store, bank = await _alias_fixture()
        s = await summ.build(store, "u1", bank=bank)
        text = s.to_text()

        # 分母 = 10（CF math）+ 1（CF trees）+ 20（洛谷）= 31
        ff = [a for a in s.avoided if a["tag"] == "fft"]
        check("fft 被认成回避方向（题库里 30 道、他一道没做）", bool(ff),
              repr([a["tag"] for a in s.avoided]))
        if ff:
            check("★ 分母把洛谷那 20 道算进来了（10+1+20 = 31，不是 11）",
                  ff[0]["share_base"] == 31, str(ff[0]["share_base"]))
        check("★ 正文里写的也是 31", "分母是 31 道" in text, "")

        # ★ 关键：洛谷的「树形数据结构」算进了 CF 的 trees
        check("★ 归一之后 trees **不再**是回避方向（他其实做过 21 道树题）",
              not [a for a in s.avoided if a["tag"] == "trees"],
              repr([a["tag"] for a in s.avoided]))

        stats = {t.tag: t for t in (s.weak_tags + s.strong_tags)}
        tr = stats.get("trees")
        check("★ 标签统计里有 trees 这一行", tr is not None, repr(sorted(stats)))
        if tr:
            check("★ trees 的题数是 1+20 = 21（洛谷那 20 道算进来了）",
                  tr.solved == 21, str(tr.solved))
        check("★ 不会另起一行叫「树形数据结构」（那说明根本没归一）",
              "树形数据结构" not in stats, repr(sorted(stats)))

        await db.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/summary.py 自测")
    print("=" * 62)
    test_shrink()
    test_no_cross_source()
    test_avoidance()
    test_avoidance_weight()
    test_band_by_source()
    test_luogu_bands_in_text()
    test_tagged_denominator()
    test_candidate_avoid_marker()
    test_tag_alias()
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
