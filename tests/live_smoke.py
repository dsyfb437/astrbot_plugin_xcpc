#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对**真实站点**的冒烟测试 —— 验证适配器不是只在夹具上能跑。

这个**不进 CI**（网络抖动/对面改版会让它红），只用来人工确认。
跑法：python tests/live_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import http as httpm          # noqa: E402
from core import log as logm            # noqa: E402
from platforms import atcoder as atc    # noqa: E402
from platforms import codeforces as cf  # noqa: E402
from platforms import qoj as qojm       # noqa: E402
from platforms import luogu as lgm      # noqa: E402

CF_HANDLE = os.environ.get("XCPC_CF_HANDLE", "")
ATC_HANDLE = os.environ.get("XCPC_ATC_HANDLE", "")


async def cf_live():
    print("\n--- Codeforces（官方 API，不需要登录）---")
    c = httpm.HttpClient(platform="codeforces", min_interval=1.0)
    a = cf.Codeforces()

    got = await a.fetch_submissions(CF_HANDLE, client=c)
    if got.ok:
        ac = sum(1 for s in got.items if s.accepted)
        print("  提交 %d 条（AC %d），游标 %s" % (len(got.items), ac, got.cursor))
        if got.items:
            s = got.items[0]
            print("  最近一条：%s  %s  难度=%s(%s)"
                  % (s.problem_key, s.verdict, s.difficulty, s.difficulty_source))
    else:
        print("  失败 [%s] %s" % (got.error_kind, got.detail))

    got = await a.fetch_contests(CF_HANDLE, client=c)
    print("  比赛 %d 场%s" % (len(got.items),
                             "" if got.ok else "  失败 [%s] %s" % (got.error_kind, got.detail)))
    if got.items:
        r = got.items[-1]
        print("  最近一场：%s  rank=%s  Δrating=%s" % (r.name, r.rank, r.rating_delta))


async def atc_live():
    print("\n--- AtCoder（AtCoder Problems 社区 API，免登录）---")
    c = httpm.HttpClient(platform="atcoder", min_interval=1.0)
    a = atc.AtCoder()

    got = await a.fetch_submissions(ATC_HANDLE, client=c)
    if got.ok:
        print("  提交 %d 条%s" % (len(got.items), "（被截断）" if got.truncated else ""))
        if got.items:
            print("  最近一条：%s  %s" % (got.items[-1].problem_key, got.items[-1].verdict))
    else:
        print("  失败 [%s] %s" % (got.error_kind, got.detail))

    got = await a.fetch_contests(ATC_HANDLE, client=c)
    print("  比赛 %d 场（从提交反推）" % len(got.items))
    if got.items:
        r = got.items[-1]
        print("  最近一场：%s  过题 %s" % (r.contest_id, r.solved))

    got = await a.fetch_problems(client=c)
    if got.ok:
        withdiff = sum(1 for p in got.items if p.difficulty is not None)
        print("  题库 %d 题（%d 有难度）" % (len(got.items), withdiff))
        if got.items:
            p = got.items[0]
            print("  样例：%s  %s  难度=%s(%s)  tags=%r"
                  % (p.problem_key, p.title[:40], p.difficulty, p.difficulty_source, p.tags))
    else:
        print("  题库失败 [%s] %s" % (got.error_kind, got.detail))


async def qoj_live():
    print("\n--- QOJ（/problems 免登录可拿；提交要登录）---")
    c = httpm.HttpClient(platform="qoj", min_interval=1.0)
    a = qojm.Qoj()
    got = await a.fetch_problems(c)
    if got.ok:
        print("  题目 %d 条" % len(got.items))
        if got.items:
            print("  样例：%r" % (got.items[0],))
    else:
        print("  失败 [%s] %s" % (got.error_kind, got.detail))

    got = await a.fetch_submissions("")
    print("  提交记录：ok=%s  说明=%s" % (got.ok, got.detail[:80]))


async def luogu_live():
    print("\n--- 洛谷（C3VK 挑战 + 题库难度/标签）---")
    c = httpm.HttpClient(platform="luogu", min_interval=1.0)
    a = lgm.Luogu()
    got = await a.fetch_problem("P1001", c)
    if got.ok and got.items:
        p = got.items[0]
        print("  %s  %s  难度=%s(%s)" % (p.problem_key, p.title[:30], p.difficulty,
                                         p.difficulty_source))
    else:
        print("  失败 [%s] %s" % (got.error_kind, got.detail))
    print("  C3VK 是否已解出：%s" % ("是" if c.cookies.get("C3VK") else "否"))


async def main():
    rec = logm.Recorder("/tmp/xcpc_live.log", level="debug", to_astrbot=False)
    rec.open()
    print("=" * 62)
    print("真实站点冒烟测试（可能会慢，CF 限速 1 req/s）")
    print("=" * 62)
    # 把 recorder 挂上去，方便看每次请求
    for fn in (cf_live, atc_live, qoj_live, luogu_live):
        try:
            await fn()
        except Exception as exc:
            print("  ** 异常：%s: %s" % (type(exc).__name__, exc))
    rec.close()
    print("\n请求日志：/tmp/xcpc_live.log")


if __name__ == "__main__":
    asyncio.run(main())
