#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端真实测试：真同步 → 真落库 → 真查询。

和 live_smoke.py 的区别：那个只验证适配器能取到数，这个验证
**取到数之后整条链路**（落库、去重、比赛流、断点、多用户隔离）都对。

会连外网。跑法：python tests/live_e2e.py
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
from core import log as logm      # noqa: E402
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


async def main():
    print("=" * 62)
    print("端到端真实测试（会连外网）")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="xcpc_e2e_")
    rec = logm.Recorder(os.path.join(tmp, "logs", "x.log"), to_astrbot=False)
    rec.open()
    db = dbm.Database(os.path.join(tmp, "xcpc.db"))
    ok, detail = await db.open()
    check("数据库打开", ok, detail)
    store = stm.Store(db)
    syncer = syncm.Syncer(db, store, recorder=rec)

    # 两个用户，只有 A 绑了 CF
    await store.set_handle("qq1001", "codeforces", CF)
    await store.set_handle("qq2002", "codeforces", "definitely_not_a_real_handle_xyz")

    print("\n--- 同步 A（真实 CF 账号）---")
    r = await syncer.sync_one("qq1001", "codeforces")
    print("   " + r.line())
    check("A 同步成功", r.ok, r.detail)
    check("拿到了提交", r.total_submissions > 0, "共 %d" % r.total_submissions)
    check("拿到了比赛记录", r.contests > 0, "共 %d" % r.contests)

    # 落库校验
    subs = await store.list_submissions("qq1001", limit=5)
    check("能查回提交", len(subs) > 0)
    if subs:
        s = subs[0]
        print("   最近：%s %s 难度=%s(%s)"
              % (s["problem_key"], s["verdict"], s["difficulty"], s["difficulty_source"]))
        check("难度带了来源标记", s["difficulty_source"] in
              ("cf_rating", "unknown"), s["difficulty_source"])

    contests = await store.list_contests("qq1001", limit=3)
    check("能查回比赛", len(contests) > 0)
    if contests:
        c = contests[0]
        print("   最近：%s rank=%s Δ%s" % ((c["name"] or "")[:30], c["rank"],
                                           c["rating_delta"]))
        check("比赛有 rank 或 rating 变化",
              c["rank"] is not None or c["rating_delta"] is not None)

    # 幂等
    print("\n--- 再同步一次（应无新增）---")
    r2 = await syncer.sync_one("qq1001", "codeforces")
    print("   " + r2.line())
    check("第二次不产生重复", r2.submissions == 0, "新增 %d" % r2.submissions)
    check("总数不变",
          await store.count_submissions("qq1001") == r.total_submissions)

    # 断点
    st = await store.get_sync_state("qq1001", "codeforces")
    check("断点已记录", bool(st and st["last_epoch"]), repr(st and st["last_epoch"]))
    check("没有残留错误", not (st and st["error_kind"]), repr(st and st["error_kind"]))

    # 隔离
    print("\n--- 多用户隔离 ---")
    check("B 没有同步过", await store.count_submissions("qq2002") == 0)
    check("B 查不到 A 的数据",
          all(r["user_id"] == "qq1001" for r in await store.list_submissions("qq1001")))
    try:
        await store.count_submissions("")
        check("空 user_id 被拒绝", False, "居然没报错")
    except ValueError:
        check("空 user_id 被拒绝", True)

    # 无效 handle 要明确失败
    print("\n--- 无效 handle ---")
    rb = await syncer.sync_one("qq2002", "codeforces")
    print("   " + rb.line())
    check("无效 handle 明确失败", not rb.ok)
    check("归到凭据失效", rb.error_kind == "凭据失效", rb.error_kind)
    check("失败没写脏数据", await store.count_submissions("qq2002") == 0)

    # 未登录平台
    print("\n--- 未登录的平台 ---")
    await store.set_handle("qq1001", "qoj", "someone")
    rq = await syncer.sync_one("qq1001", "qoj")
    print("   " + rq.line())
    check("QOJ 未登录时明确报凭据失效", not rq.ok and rq.error_kind == "凭据失效")

    await db.close()
    rec.close()

    # 日志里不能有凭据
    logtext = open(os.path.join(tmp, "logs", "x.log"), encoding="utf-8").read()
    import re as _re
    leaks = _re.findall(r"(?i)(cookie|password|_token)\s*[:=]\s*(?!\*\*\*)(\S{6,})", logtext)
    check("日志里没有凭据泄漏", not leaks, repr(leaks[:3]))
    print("   日志 %d 行" % logtext.count("\n"))

    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
