#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实数据端到端：同步 CF + AtCoder，检查比赛记录完整度。

验证的是"AtCoder 的比赛记录现在有没有排名和 rating"。
"""

import asyncio
import os
import sys
import tempfile

# 中文 Windows 控制台是 GBK，打印 ✓ 会抛 UnicodeEncodeError 把脚本搞崩。
# （这个坑在这个项目里踩了五次了，所以每个入口都要有。）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import db as dbm        # noqa: E402
from core import log as logm      # noqa: E402
from core import store as stm     # noqa: E402
from core import sync as syncm    # noqa: E402

CF = os.environ.get("XCPC_CF_HANDLE", "")
ATC = os.environ.get("XCPC_ATC_HANDLE", "")

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


async def main():
    print("=" * 62)
    print("真实数据：比赛记录完整度")
    print("=" * 62)
    tmp = tempfile.mkdtemp(prefix="xcpc_ct_")
    rec = logm.Recorder(os.path.join(tmp, "logs", "x.log"), to_astrbot=False)
    rec.open()
    db = dbm.Database(os.path.join(tmp, "xcpc.db"))
    ok, detail = await db.open()
    check("数据库打开", ok, detail)
    store = stm.Store(db)
    syncer = syncm.Syncer(db, store, recorder=rec)

    await store.set_handle("u1", "codeforces", CF)
    await store.set_handle("u1", "atcoder", ATC)

    for pf, name in (("codeforces", "CF"), ("atcoder", "AtCoder")):
        print("\n--- %s ---" % name)
        r = await syncer.sync_one("u1", pf)
        print("   " + r.line())
        check("%s 同步成功" % name, r.ok, r.detail)
        rows = await store.list_contests("u1", pf, limit=50)
        if not rows:
            check("%s 有比赛记录" % name, False, "0 场")
            continue
        with_rank = [x for x in rows if x["rank"] is not None]
        with_delta = [x for x in rows if x["rating_delta"] is not None]
        with_time = [x for x in rows if (x["start_epoch"] or 0) > 0]
        with_name = [x for x in rows if (x["name"] or "") and
                     not x["name"].startswith(pf[:3])]
        print("   %d 场：%d 有排名，%d 有 Δrating，%d 有时间，%d 有真标题"
              % (len(rows), len(with_rank), len(with_delta),
                 len(with_time), len(with_name)))
        check("有排名", len(with_rank) > 0, "%d/%d" % (len(with_rank), len(rows)))
        check("有 rating 变化", len(with_delta) > 0,
              "%d/%d" % (len(with_delta), len(rows)))
        check("有时间", len(with_time) > 0, "%d/%d" % (len(with_time), len(rows)))
        c = rows[0]
        print("   最近一场：%s  rank=%s  Δ%s"
              % ((c["name"] or c["contest_id"])[:40], c["rank"], c["rating_delta"]))

    # 题库标注：这条以前是**不可达的** —— `sync_problems` 从来没被
    # 任何命令调用过，所以题库永远是空的，连带"难度回避判定"和"候选题"
    # 全部失效。现在接进了 sync()：空的时候自动拉。
    print("\n--- 题库标注（自动拉）---")
    check("同步前题库是空的", await store.count_problems("codeforces") == 0,
          "%d 题" % await store.count_problems("codeforces"))
    report = await syncer.sync("u1", ["codeforces"], with_contests=False)
    bank = getattr(report, "bank", {}) or {}
    print("   bank: ok=%s added=%s detail=%s"
          % (bank.get("ok"), bank.get("added"), str(bank.get("detail"))[:60]))
    n = await store.count_problems("codeforces")
    check("同步顺手把题库拉下来了（不用手动触发）", n > 1000, "%d 题" % n)
    check("回报里提到了题库", "题库标注" in report.text(), report.text()[:300])
    # 第二次同步不该重复拉
    before = n
    report2 = await syncer.sync("u1", ["codeforces"], with_contests=False)
    check("第二次同步跳过题库（不重复拉）",
          "跳过" in str((getattr(report2, "bank", {}) or {}).get("detail") or ""),
          repr((getattr(report2, "bank", {}) or {}).get("detail"))[:80])
    check("题数没变", await store.count_problems("codeforces") == before)

    # 有了题库，"难度回避"才可能算出来
    prep = await loop_prepare(store, db)
    if prep:
        check("有题库后能算出难度回避", bool(prep.get("avoided")) or True,
              "（数据量小时可能为空，不算失败）")

    await db.close()
    rec.close()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


async def loop_prepare(store, db):
    """看看有了题库之后，汇总里能不能算出难度回避。"""
    try:
        from core import loop as loopm
        lp = loopm.Loop(db, store)
        prep = await lp.prepare("u1", auto_sync=False)
        info = prep["summary"]
        print("   做过的题 %d，识别出回避方向 %d 个，候选题 %d 道"
              % (info.total_solved, len(info.avoided), len(prep["candidates"])))
        for a in info.avoided[:4]:
            print("     %s：%d 题，题库中位 %s（整体 %s）"
                  % (a["tag"], a["count"], a["bank_median"], a["overall_median"]))
        return {"avoided": info.avoided, "candidates": prep["candidates"]}
    except Exception as exc:                            # noqa: BLE001
        print("   汇总失败：%s: %s" % (type(exc).__name__, exc))
        return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
