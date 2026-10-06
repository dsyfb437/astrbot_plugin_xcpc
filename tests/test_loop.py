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

    def get_current_chat_provider_id(self, umo=""):
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


def main() -> int:
    print("=" * 62)
    print("core/loop.py 自测（假 provider）")
    print("=" * 62)
    test_happy()
    test_no_degradation()
    test_bad_output()
    test_target_band()
    test_avoidance_injection()
    test_feedback()
    test_empty_uid()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
