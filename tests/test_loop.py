#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/loop.py 的自测 —— 五环调度。

重点：
  1. **LLM 失败时不降级**：报错 + 取出上一版方案（并说明这是旧的）
  2. 候选池的难度区间由**数据**推出（不是模型拍脑袋）
  3. **训练块**（v0.6.0）：候选池以"当前子专题"为主，不再按回避方向每天换
  4. 反馈能存能取
  5. 全链路用假 provider，不调真模型

跑法：python tests/test_loop.py
"""

from __future__ import annotations

import asyncio
import json
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
from core import llm as llmm      # noqa: E402
from core import curriculum as cur  # noqa: E402
from core import loop as loopm    # noqa: E402
from core import store as stm     # noqa: E402
from platforms.base import Problem, Submission  # noqa: E402

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


class FakeResp:
    def __init__(self, text):
        self.completion_text = text


class FakeContext:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc
        self.calls = []

    # 官方是 async（astrbot/core/star/context.py:329），替身照签名写。
    async def get_current_chat_provider_id(self, umo=""):
        return "fake"

    async def llm_generate(self, **kw):
        self.calls.append(kw)
        if self._exc:
            raise self._exc
        return FakeResp(self._text)


def sub(sid, key, verdict="OK", diff=1500):
    return Submission(platform="codeforces", submission_id=str(sid),
                      problem_key=key, verdict=verdict, epoch=1700000000 + int(sid),
                      difficulty=diff, difficulty_source="cf_rating")


async def setup(*, with_bank=True, n_solved=30):
    """造一个有数据的库。"""
    tmp = tempfile.mkdtemp(prefix="xcpc_loop_")
    db = dbm.Database(os.path.join(tmp, "t.db"))
    ok, detail = await db.open()
    assert ok, detail
    store = stm.Store(db)
    await store.ensure_user("u1")

    # 他做过的题：math，难度 1500
    subs = [sub(i, "CF:MA%d" % i, diff=1500) for i in range(n_solved)]
    await store.upsert_submissions("u1", "codeforces", subs)

    if with_bank:
        probs = []
        # math 简单（题库中位 1400）
        for i in range(200):
            probs.append(Problem("codeforces", "CF:MA%d" % i, "ma%d" % i,
                                 tags=["math"], difficulty=1400,
                                 difficulty_source="cf_rating"))
        # dp 难（2400），他一道没做过 → 应该被判为回避
        for i in range(60):
            probs.append(Problem("codeforces", "CF:DP%d" % i, "dp%d" % i,
                                 tags=["dp"], difficulty=2400,
                                 difficulty_source="cf_rating"))
        # 没做过的 math 题（候选）
        for i in range(200, 400):
            probs.append(Problem("codeforces", "CF:MA%d" % i, "ma%d" % i,
                                 tags=["math"], difficulty=1500,
                                 difficulty_source="cf_rating"))
        await store.upsert_problems("codeforces", probs)
        # ★ 洛谷的「线性 DP」题 —— 自动开的第一个训练块（dp_linear）的候选池。
        #
        # 为什么必须放洛谷的：`dp_linear` 在 CF 上**表达不出来**
        # （CF 只有 38 个粗标签，没有「线性 DP」这个词，`dp` 一个标签
        # 盖住了 2538 道题）。所以 `curriculum` 里它的 `cf=()`，
        # 只能从洛谷挑 —— 见 core/curriculum.py 的匹配规则注释。
        # 难度带 1000-1400 翻成洛谷是 2-3 档。
        await store.upsert_problems("luogu", [
            Problem("luogu", "LG:P%d" % i, "p%d" % i, tags=["线性 DP"],
                    difficulty=3, difficulty_source="luogu_level")
            for i in range(40)])

    return db, store


PLAN_JSON = json.dumps({
    "assessment": "你 math 做了 30 题，但 dp 一道没碰。",
    "tasks": [{"kind": "practice", "title": "做一道线性 DP", "problem": "LG:P1",
               "minutes": 45, "why": "当前训练块就是这个方向"}],
    "watch": "别怕难题",
}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. 正常跑通
# ---------------------------------------------------------------------------

def test_happy():
    print("\n[1] 正常跑通")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)
        ctx = FakeContext(text=PLAN_JSON)
        r = await lp.run(ctx, "u1", auto_sync=False)
        check("ok=True", r.ok, r.detail)
        check("拿到方案", r.plan is not None and len(r.plan.tasks) == 1)
        check("没退回上一版", r.used_previous is False)
        check("汇总非空", len(r.summary_text) > 100)
        # 自动开出来的训练块应该是最下面那道"洛谷线性 DP"题
        check("★ 自动开了训练块（阶梯第一个子专题 dp_linear）",
              "当前训练块" in r.summary_text and "线性 DP" in r.summary_text, "")
        check("候选池里有训练块的题", "LG:P" in r.summary_text, "")
        # 提示词确实带上了汇总
        check("提示词里带了汇总", "训练数据汇总" in ctx.calls[0]["prompt"])
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 2. LLM 失败不降级
# ---------------------------------------------------------------------------

def test_no_degradation():
    print("\n[2] LLM 失败不降级")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)

        # 先成功一次，存下方案
        ok_ctx = FakeContext(text=PLAN_JSON)
        r1 = await lp.run(ok_ctx, "u1", auto_sync=False, date="2026-10-07")
        check("第一次成功", r1.ok)

        # 再失败一次
        bad_ctx = FakeContext(exc=RuntimeError("模型 500"))
        r2 = await lp.run(bad_ctx, "u1", auto_sync=False, date="2026-10-08")
        check("失败时 ok=False（没有返回凑合的方案）", not r2.ok, r2.detail)
        check("失败有分类", bool(r2.error_kind), r2.error_kind)
        check("失败说明里有原因", "500" in r2.detail, r2.detail)
        check("取出了上一版方案", r2.used_previous and r2.plan is not None)
        check("上一版是旧的那份",
              r2.plan is not None and r2.plan.tasks[0].problem == "LG:P1")

        text = r2.text()
        check("文本里明确说这是上一次的方案",
              "上一次" in text and "不是新生成" in text, text[:200])

        # 没有旧方案时不该假装有
        db2, store2 = await setup()
        lp2 = loopm.Loop(db2, store2)
        r3 = await lp2.run(FakeContext(exc=RuntimeError("x")), "u1", auto_sync=False)
        check("没有旧方案时不假装有", not r3.ok and r3.used_previous is False)
        check("这时只给错误信息", "内部错误" in r3.text(), r3.text()[:120])
        await db2.close()
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. 模型返回非法内容
# ---------------------------------------------------------------------------

def test_bad_output():
    print("\n[3] 模型返回非法内容")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)
        r = await lp.run(FakeContext(text="我觉得你应该多练 dp"),
                         "u1", auto_sync=False)
        check("非法输出时 ok=False", not r.ok)
        check("分类是解析失败", r.error_kind == "解析失败", r.error_kind)
        check("没有编出一个方案来糊弄", r.plan is None or r.used_previous)
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. 训练块：难度带按平台分别翻译
# ---------------------------------------------------------------------------

def test_block_band():
    print("\n[4] 训练块的难度带按平台分别翻译")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)
        prep = await lp.prepare("u1", auto_sync=False)
        info = prep["summary"]

        blk = dict(info.block or {})
        check("★ 自动开了训练块", blk.get("topic") == "dp_linear",
              repr(blk.get("topic")))
        check("块里存的是 CF 尺子的难度带",
              (blk.get("band_lo"), blk.get("band_hi")) == (1000, 1400),
              repr((blk.get("band_lo"), blk.get("band_hi"))))
        check("target 取自 curriculum", blk.get("target") == 15,
              repr(blk.get("target")))
        check("started_at 有值（进度从这里起算）", bool(blk.get("started_at")),
              repr(blk.get("started_at")))

        # ★ 同一条 lo/hi 在两个平台上要分别翻译 —— 洛谷是 1-7 档。
        # CF 1000-1400 翻成洛谷是 3-4 档（普及/提高- ~ 普及+/提高）。
        # ★ 这条断言的重点不是"正好是 3-4"，而是**它被翻译过** ——
        # 直接拿 1000/1400 去卡洛谷的 1-7 档会一道都挑不出来
        # （v0.5.14 真机：`0-199：308 题`）。
        check("★ 洛谷的带子被翻译成 1-7 档（不是照抄 CF 的 1000/1400）",
              cur.band_for(cur.BY_KEY["dp_linear"], "luogu") == (3, 4),
              repr(cur.band_for(cur.BY_KEY["dp_linear"], "luogu")))
        check("CF 的带子原样",
              cur.band_for(cur.BY_KEY["dp_linear"], "codeforces") == (1000, 1400),
              repr(cur.band_for(cur.BY_KEY["dp_linear"], "codeforces")))

        cands = prep["candidates"]
        check("候选池非空", len(cands) > 0, "%d" % len(cands))
        check("排除了已经做过的题",
              all(c["problem_key"] not in {"CF:MA%d" % i for i in range(30)}
                  for c in cands))
        lg = [c for c in cands if c["problem_key"].startswith("LG:")]
        check("★ 候选里有训练块方向的洛谷题", len(lg) > 0, "%d 道" % len(lg))
        check("★ 洛谷候选的难度落在 2-3 档（没拿 CF 的尺子去卡洛谷）",
              all(2 <= c["difficulty"] <= 3 for c in lg),
              repr(sorted({c["difficulty"] for c in lg})))
        check("洛谷候选的难度的确来自 luogu_level",
              all(c["difficulty_source"] == "luogu_level" for c in lg), "")

        text = info.to_text()
        check("★ 汇总里有「当前训练块」那一段", "当前训练块" in text, "")
        check("汇总里写了进度", "进度：" in text, "")
        check("汇总里说清了「今天的任务必须从这一块出」",
              "今天的任务必须从这一块的候选池里出" in text, "")
        check("汇总里说明了候选池的顺序", "条是**当前训练块**" in text, "")
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 5. 候选池以训练块为主
# ---------------------------------------------------------------------------

def test_block_candidates():
    print("\n[5] 候选池以训练块为主")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)
        prep = await lp.prepare("u1", auto_sync=False)
        info = prep["summary"]

        cands = prep["candidates"]
        lg = [c for c in cands if c["problem_key"].startswith("LG:")]
        check("候选池里有训练块的题", len(lg) > 0, "%d 道" % len(lg))
        check("★ 训练块的题排在最前面（顺序本身就是给模型的暗示）",
              cands[0]["problem_key"].startswith("LG:"), cands[0]["problem_key"])
        # 块外的兜底候选可以存在，但必须排在块的后面
        first_other = next((i for i, c in enumerate(cands)
                            if not c["problem_key"].startswith("LG:")), None)
        check("★ 兜底候选排在训练块候选之后",
              first_other is None or first_other >= len(lg),
              "第一个非块内候选在第 %s 位，块内共 %d 道" % (first_other, len(lg)))

        # 没有块时（新用户第一次），prepare 会**自己开一个** ——
        # 用户不该先学会一条命令才能拿到有针对性的方案。
        blk = await store.get_block("u1")
        check("★ prepare 顺手把块落库了（不用用户先发命令）",
              blk is not None and blk["topic"] == "dp_linear",
              repr(dict(blk) if blk else None))

        # 再跑一次不该换块，也不该把进度起点推后
        before = blk["started_at"]
        prep2 = await lp.prepare("u1", auto_sync=False)
        blk2 = await store.get_block("u1")
        check("第二次 prepare 不换块", blk2["topic"] == "dp_linear", blk2["topic"])
        check("★ 重复开块不会把 started_at 推后（否则进度永远停在 0）",
              blk2["started_at"] == before,
              "%s -> %s" % (before, blk2["started_at"]))
        check("第二次候选池还在", len(prep2["candidates"]) > 0, "")
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 6. 反馈
# ---------------------------------------------------------------------------

def test_feedback():
    print("\n[6] 反馈闭环")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)
        await lp.feedback("u1", "今天有点累，明天少安排点")
        rows = await store.list_feedback("u1")
        check("反馈存下来了", len(rows) == 1 and "累" in rows[0]["text"])

        # 下一轮的汇总里要带上
        prep = await lp.prepare("u1", auto_sync=False)
        check("反馈进了汇总（下一轮模型能看到）",
              "有点累" in prep["summary"].to_text(), "")

        # 反馈也要按用户隔离
        await store.ensure_user("u2")
        await lp.feedback("u2", "别人的反馈")
        rows2 = await store.list_feedback("u1")
        check("反馈按用户隔离",
              all("别人" not in r["text"] for r in rows2), repr(rows2))
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 7. 空 user_id
# ---------------------------------------------------------------------------

def test_empty_uid():
    print("\n[7] 空 user_id")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)
        for fn, args in (("prepare", ("",)), ("feedback", ("", "x"))):
            try:
                await getattr(lp, fn)(*args, auto_sync=False) if fn == "prepare" \
                    else await getattr(lp, fn)(*args)
                check("%s 拒绝空 user_id" % fn, False, "居然没报错")
            except ValueError:
                check("%s 拒绝空 user_id" % fn, True)
        await db.close()

    asyncio.run(main())


class RecordingDB:
    """把 `query` 的 SQL 记下来，其余全部转发给真库。"""

    def __init__(self, db):
        self._db = db
        self.sqls = []

    async def query(self, sql, params=()):
        self.sqls.append((sql, params))
        return await self._db.query(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


def test_load_bank_not_truncated():
    """★ 题库读取不能被 LIMIT 悄悄截断。

    真机翻车：`_load_bank` 默认 `limit_per_platform=4000`，而题库有
    **11425 道** CF 题 —— 而且 SQL 不带 `ORDER BY`，等于**任意切了 4000 道**。

    后果不是"少一点数据"，而是两份数据对不上：他 AC 的 90 道 CF 题里
    只有 **6 道**能在题库里找到标签 → 「疑似难度回避」是在 **6 道题**上
    算出来的 → `dp 做过 2 题 → share = 2/6 = 33%`，**高于 4% 的阈值，
    直接判定为"不算回避"**。用户问「dp 的权重是不是该大一点」，
    而 dp 那条**根本没能进列表**。
    """
    print("\n[9] 题库读取不能被 LIMIT 截断")

    async def main():
        db, store = await setup(with_bank=True)
        rec = RecordingDB(db)
        lp = loopm.Loop(rec, store)

        rec.sqls.clear()
        bank = await lp._load_bank()
        sqls = [s for s, _ in rec.sqls]
        check("默认调用（不传 limit）的 SQL 里没有 LIMIT",
              bool(sqls) and all("LIMIT" not in s.upper() for s in sqls),
              repr(sqls))

        row = await db.query_one(
            "SELECT COUNT(*) AS n FROM problems WHERE platform='codeforces'")
        n_db = int(row["n"]) if row is not None else 0
        check("这个测试有意义（题库不止几十道题）", n_db > 100, str(n_db))
        check("★ 读回来的题数 == 库里的题数（一道不少）",
              len(bank.get("codeforces") or {}) == n_db,
              "bank %d vs db %d" % (len(bank.get("codeforces") or {}), n_db))

        rec.sqls.clear()
        small = await lp._load_bank(5)
        sqls2 = [s for s, _ in rec.sqls]
        check("显式传 limit 时 SQL 里带 LIMIT",
              bool(sqls2) and all("LIMIT" in s.upper() for s in sqls2),
              repr(sqls2))
        check("显式 limit 真的生效（不是被忽略）",
              len(small.get("codeforces") or {}) == 5,
              str(len(small.get("codeforces") or {})))
        await db.close()

    asyncio.run(main())


def test_topic_platform_interleave():
    """★ 同一个子专题在两个平台上都有题时，候选要**两个平台轮流取**。

    为什么不能一把捞完洛谷再接 CF：**候选池的排列顺序本身就是给模型的暗示**。
    洛谷的题凑齐了排在前面，模型就只会报洛谷题号 —— 而他两个平台都在打。
    真机翻车见 v0.5.14（前 12 条全是树题 → 模型一口气排了三道树）。

    这里用 `dp_tree`：它在洛谷有「树形 DP」，在 CF 有 `dp`+`trees`，
    两边都有货，正好能验交错。
    """
    print("\n[10] 训练块的候选要跨平台交错")

    async def main():
        db, store = await setup(with_bank=False)
        # 洛谷：树形 DP，4 档（dp_tree 的洛谷带是 4-5）
        await store.upsert_problems("luogu", [
            Problem("luogu", "LG:T%d" % i, "lt%d" % i, tags=["树形 DP"],
                    difficulty=4, difficulty_source="luogu_level")
            for i in range(40)])
        # CF：dp + trees，1600（dp_tree 的 CF 带是 1400-1800）
        await store.upsert_problems("codeforces", [
            Problem("codeforces", "CF:T%d" % i, "ct%d" % i, tags=["dp", "trees"],
                    difficulty=1600, difficulty_source="cf_rating")
            for i in range(40)])
        await store.upsert_submissions(
            "u1", "codeforces",
            [sub(i, "CF:MA%d" % i, diff=1400) for i in range(30)])
        # 手动把块设成 dp_tree
        await store.set_block("u1", "dp", "dp_tree", target=20,
                              band_lo=1400, band_hi=1800, note="")

        lp = loopm.Loop(db, store)
        prep = await lp.prepare("u1", auto_sync=False)
        cands = prep["candidates"]
        check("候选池有货", len(cands) >= 6, str(len(cands)))
        head = [c["problem_key"].split(":")[0] for c in cands[:6]]
        check("★ 前几条里两个平台都出现了",
              "LG" in head and "CF" in head, repr(head))
        check("★ 相邻两条不是同一个平台（LG, CF, LG, CF…）",
              all(head[i] != head[i + 1] for i in range(len(head) - 1)),
              repr(head))
        # 块是 dp_tree，两边的题都必须真的是这个子专题
        check("洛谷候选的标签是「树形 DP」",
              all("树形 DP" in (c.get("tags") or []) for c in cands
                  if c["problem_key"].startswith("LG:")), "")
        check("CF 候选同时带 dp 和 trees",
              all({"dp", "trees"} <= set(c.get("tags") or []) for c in cands
                  if c["problem_key"].startswith("CF:")), "")
        await db.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/loop.py 自测（假 provider）")
    print("=" * 62)
    test_happy()
    test_no_degradation()
    test_bad_output()
    test_block_band()
    test_block_candidates()
    test_load_bank_not_truncated()
    test_topic_platform_interleave()
    test_feedback()
    test_empty_uid()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
