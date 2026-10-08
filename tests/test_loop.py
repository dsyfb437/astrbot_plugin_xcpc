#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/loop.py 的自测 —— 五环调度。

重点：
  1. **LLM 失败时不降级**：报错 + 取出上一版方案（并说明这是旧的）
  2. 候选池的难度区间由**数据**推出（不是模型拍脑袋）
  3. 有回避方向时，那个方向被"规则注入"到候选池
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

    return db, store


PLAN_JSON = json.dumps({
    "assessment": "你 math 做了 30 题，但 dp 一道没碰。",
    "tasks": [{"kind": "practice", "title": "做一道 dp", "problem": "CF:DP0",
               "minutes": 45, "why": "补回避的方向"}],
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
        # dp 那道题在候选池里（因为回避方向被注入）
        check("候选池里有 dp 方向的题（规则注入生效）",
              "CF:DP0" in r.summary_text, "")
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
              r2.plan is not None and r2.plan.tasks[0].problem == "CF:DP0")

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
# 4. 难度区间由数据推出
# ---------------------------------------------------------------------------

def test_target_band():
    print("\n[4] 难度区间由数据推出")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)
        prep = await lp.prepare("u1", auto_sync=False)
        info = prep["summary"]

        check("识别出主来源是 cf_rating", "cf_rating" in info.by_source)
        med = info.by_source["cf_rating"]["median"]
        check("中位难度是 1500", med == 1500.0, repr(med))

        # 候选池 = 回避方向（放宽难度，排前面）+ 常规（目标区间）
        cands = prep["candidates"]
        check("候选池非空", len(cands) > 0, "%d" % len(cands))
        check("排除了已经做过的题",
              all(c["problem_key"] not in {"CF:MA%d" % i for i in range(30)}
                  for c in cands))

        # 分开检查：**回避方向的题允许超出目标区间**（这是故意的），
        # 常规候选必须在区间内。不能笼统地断言"全部在区间内" ——
        # 那会把"为难的方向放宽难度"这个正确行为判成失败。
        avoided_tags = {a["tag"] for a in info.avoided}
        normal = [c for c in cands
                  if not (set(c["tags"] or []) & avoided_tags)]
        injected = [c for c in cands
                    if set(c["tags"] or []) & avoided_tags]
        check("常规候选落在目标区间内",
              all(1300 <= c["difficulty"] <= 1900 for c in normal),
              "范围 %s" % ([c["difficulty"] for c in normal][:8],))
        if injected:
            check("回避方向的题被放宽了难度（否则注入是空话）",
                  max(c["difficulty"] for c in injected) > 1900,
                  "最大 %s" % max(c["difficulty"] for c in injected))
            check("放宽也有上限（不是无限制）",
                  max(c["difficulty"] for c in injected) <= 2400,
                  "最大 %s" % max(c["difficulty"] for c in injected))

        text = info.to_text()
        check("汇总里说明了「为什么放宽」",
              "放宽" in text and "空话" in text, text[-600:])

        text = info.to_text()
        check("汇总里说明了区间依据", "中位难度" in text and "舒适区" in text,
              text[-500:])
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 5. 回避方向被注入候选池
# ---------------------------------------------------------------------------

def test_avoidance_injection():
    print("\n[5] 回避方向注入候选池")

    async def main():
        db, store = await setup()
        lp = loopm.Loop(db, store)
        prep = await lp.prepare("u1", auto_sync=False)
        info = prep["summary"]

        check("认出了 dp 是回避方向",
              any(a["tag"] == "dp" for a in info.avoided),
              repr([a["tag"] for a in info.avoided]))

        cands = prep["candidates"]
        dp_cands = [c for c in cands if "dp" in (c["tags"] or [])]
        check("候选池里有 dp 的题（被注入）", len(dp_cands) > 0,
              "%d 道" % len(dp_cands))
        # 注入的应该排在前面
        if dp_cands and cands:
            check("dp 的题排在候选池前面",
                  cands[0]["problem_key"].startswith("CF:DP"),
                  cands[0]["problem_key"])
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


def test_avoid_candidates_interleaved():
    """★ 回避方向的候选要**轮流交错**，不能一个方向刷满前排。

    真机翻车：`want = [前两个回避方向]` 之后**一次性**调 `pick_candidates`，
    标签命中多的排前面 → 前 12 条**全是树题** → 模型一口气排了三道树，
    而提示词里明明写着「一道就够」。
    **候选池的排列顺序本身就是给模型的暗示**，它比提示词里多写一句管用。
    """
    print("\n[10] 回避方向的候选要交错")

    async def main():
        db, store = await setup(with_bank=False)
        probs = []
        for i in range(200):
            probs.append(Problem("codeforces", "CF:MA%d" % i, "ma%d" % i,
                                 tags=["math"], difficulty=1400,
                                 difficulty_source="cf_rating"))
        # 两个"难且没碰过"的方向，难度都落在放宽后的区间里
        for i in range(60):
            probs.append(Problem("codeforces", "CF:TR%d" % i, "tr%d" % i,
                                 tags=["trees"], difficulty=2200,
                                 difficulty_source="cf_rating"))
        for i in range(60):
            probs.append(Problem("codeforces", "CF:GR%d" % i, "gr%d" % i,
                                 tags=["graphs"], difficulty=2100,
                                 difficulty_source="cf_rating"))
        await store.upsert_problems("codeforces", probs)
        await store.upsert_submissions(
            "u1", "codeforces",
            [sub(i, "CF:MA%d" % i, diff=1400) for i in range(30)])

        lp = loopm.Loop(db, store)
        prep = await lp.prepare("u1", auto_sync=False)
        order = [a["tag"] for a in prep["summary"].avoided]
        check("至少认出两个回避方向", len(order) >= 2, repr(order))
        check("★ 前两个正好是 trees / graphs",
              order[:2] == ["trees", "graphs"], repr(order))

        cands = prep["candidates"]
        check("候选池有货", len(cands) >= 4, str(len(cands)))
        head = [set(c.get("tags") or []) & {"trees", "graphs"} for c in cands[:6]]
        check("★ 前几条里两个方向都出现了",
              any("trees" in t for t in head) and any("graphs" in t for t in head),
              repr([sorted(t) for t in head]))
        check("★ 相邻两条不是同一个方向（trees, graphs, trees, graphs…）",
              all(head[i] != head[i + 1] for i in range(len(head) - 1)),
              repr([sorted(t) for t in head]))
        await db.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/loop.py 自测（假 provider）")
    print("=" * 62)
    test_happy()
    test_no_degradation()
    test_bad_output()
    test_target_band()
    test_avoidance_injection()
    test_load_bank_not_truncated()
    test_avoid_candidates_interleaved()
    test_feedback()
    test_empty_uid()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
