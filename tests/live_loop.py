#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实数据全链路：真同步 CF → 真汇总 → 假模型出方案。

验证的是"喂给模型的东西对不对" —— 这是"模型胡说"最常见的真正原因。

跑法：python tests/live_loop.py
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
from core import log as logm      # noqa: E402
from core import loop as loopm    # noqa: E402
from core import store as stm     # noqa: E402
from core import sync as syncm    # noqa: E402

CF = os.environ.get("XCPC_CF_HANDLE", "")
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
    def __init__(self, t):
        self.completion_text = t


class FakeCtx:
    def __init__(self, t):
        self.t = t
        self.prompt = ""

    def get_current_chat_provider_id(self, umo=""):
        return "fake"

    async def llm_generate(self, **kw):
        self.prompt = kw.get("prompt", "")
        return FakeResp(self.t)


async def main():
    print("=" * 62)
    print("真实数据全链路")
    print("=" * 62)
    tmp = tempfile.mkdtemp(prefix="xcpc_live_loop_")
    rec = logm.Recorder(os.path.join(tmp, "logs", "x.log"), to_astrbot=False)
    rec.open()
    db = dbm.Database(os.path.join(tmp, "xcpc.db"))
    ok, detail = await db.open()
    check("数据库打开", ok, detail)
    store = stm.Store(db)
    syncer = syncm.Syncer(db, store, recorder=rec)
    lp = loopm.Loop(db, store, recorder=rec, syncer=syncer)

    await store.set_handle("qq1001", "codeforces", CF)

    print("\n--- 1. 真同步 CF ---")
    r = await syncer.sync_one("qq1001", "codeforces")
    print("   " + r.line())
    check("同步成功", r.ok, r.detail)

    print("\n--- 2. 拉题库标注（难度+标签的来源）---")
    okp, msg, added = await syncer.sync_problems("codeforces")
    check("题库拉取成功", okp, msg)
    total = await store.count_problems("codeforces")
    print("   题库 %d 题（新增 %d）" % (total, added))
    check("题库有内容", total > 1000, "%d" % total)

    print("\n--- 3. 汇总（模型实际会看到的东西）---")
    prep = await lp.prepare("qq1001", auto_sync=False)
    info = prep["summary"]
    text = info.to_text()
    print("   汇总 %d 字符" % len(text))
    check("汇总里有总量", "提交" in text and "AC" in text)
    check("按难度来源分开列", "【cf_rating" in text, text[:200])
    check("有难度分布", "难度分布" in text)
    cands = prep["candidates"]
    print("   候选池 %d 题" % len(cands))
    check("候选池非空", len(cands) > 0)
    if cands:
        ds = [c["difficulty"] for c in cands]
        print("   候选难度范围 %s ~ %s" % (min(ds), max(ds)))
        check("候选难度合理（不是全 800 也不是全 3500）",
              min(ds) >= 600 and max(ds) <= 3000, "%s~%s" % (min(ds), max(ds)))
        check("候选里没有他已经 AC 的题",
              not (set(c["problem_key"] for c in cands)
                   & await store.accepted_problems("qq1001")))

    # 有没有识别出回避
    print("\n--- 4. 难度回避 ---")
    if info.avoided:
        for a in info.avoided:
            print("   %s：做过 %d 题（%.1f%%），题库中位 %s（整体 %s）"
                  % (a["tag"], a["count"], a["share"] * 100,
                     a["bank_median"], a["overall_median"]))
        check("识别出回避方向", True)
    else:
        print("   （没识别出 —— 可能题目标签还没到，或数据量不够）")
        check("回避判定没崩", True)

    print("\n--- 5. 假模型出方案（验证提示词完整性）---")
    # 用真实的候选池里的题号，模拟"模型挑了真题"
    picked = cands[0]["problem_key"] if cands else ""
    plan_json = json.dumps({
        "assessment": "测试",
        "tasks": [{"kind": "practice", "title": "做题", "problem": picked,
                   "minutes": 45, "why": "测试"}],
    }, ensure_ascii=False)
    ctx = FakeCtx(plan_json)
    res = await lp.run(ctx, "qq1001", auto_sync=False, prep=prep)
    check("方案生成成功", res.ok, res.detail)
    if res.ok:
        check("从候选池挑的题号被保留", res.plan.tasks[0].problem == picked,
              "%r vs %r" % (res.plan.tasks[0].problem, picked))
    check("提示词里带了汇总", "训练数据汇总" in ctx.prompt)
    check("提示词里带了候选池", "候选题目" in ctx.prompt)
    check("提示词里带了「不能编题号」的要求",
          "不得编造" in ctx.prompt or "只能从这里挑" in ctx.prompt)

    print("\n--- 6. 编题号会被拦 ---")
    bad_json = json.dumps({
        "assessment": "测试",
        "tasks": [{"kind": "practice", "title": "做题", "problem": "CF:99999Z",
                   "minutes": 45, "why": "测试"}],
    }, ensure_ascii=False)
    res2 = await lp.run(FakeCtx(bad_json), "qq1001", auto_sync=False, prep=prep)
    check("编的题号被剔除",
          res2.ok and res2.plan.tasks[0].problem == "",
          repr(res2.plan.tasks[0].problem) if res2.ok else res2.detail)
    check("用户能看到拦截说明",
          res2.ok and any("CF:99999Z" in p for p in res2.plan.problems),
          repr(res2.plan.problems) if res2.ok else "")

    await db.close()
    rec.close()
    logtext = open(os.path.join(tmp, "logs", "x.log"), encoding="utf-8").read()
    check("日志里没凭据泄漏",
          not any(k in logtext.lower() for k in ("sessdata=", "password=")),
          "")
    print("   日志 %d 行" % logtext.count("\n"))

    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
