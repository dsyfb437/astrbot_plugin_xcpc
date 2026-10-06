#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/sync.py 的自测。

重点：
  1. **失败绝不推进断点**（推进了下次会静默漏数据）
  2. **各平台互相独立**（一个挂了不能拖垮别的）
  3. **"没有"和"拿不到"要区分**（洛谷 Cookie 过期不能显示成"零提交"）
  4. 用**假适配器 + 本地 stub**，完全不碰外网

跑法：python tests/test_sync.py
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
from core import sync as syncm    # noqa: E402
from platforms.base import Fetched, Submission  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  \u2713 %s" % label)
    else:
        FAIL += 1
        print("  \u2717 %s%s" % (label, ("  -> " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# 假适配器 —— 可以精确控制每一次的返回
# ---------------------------------------------------------------------------

class FakeAdapter:
    """按脚本返回结果。`script` 是一串 Fetched（或异常）。"""
    supports_contests = False
    supports_problems = False

    def __init__(self, submissions_script=None, contests_script=None):
        self.subs = list(submissions_script or [])
        self.cons = list(contests_script or [])
        self.calls = []

    async def fetch_submissions(self, handle, since_epoch=None, client=None):
        self.calls.append(("subs", handle, since_epoch))
        if not self.subs:
            return Fetched(items=[], ok=True, cursor=0)
        nxt = self.subs.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def fetch_contests(self, handle, client=None):
        if not self.cons:
            return Fetched(items=[], ok=True)
        nxt = self.cons.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def mk_sub(sid, key="CF:1A", epoch=100):
    return Submission(platform="codeforces", submission_id=str(sid),
                      problem_key=key, verdict="OK", epoch=epoch)


async def fresh(adapter_map=None, handles=None):
    """造一套干净的 db + store + syncer。

    **用依赖注入而不是 monkeypatch**：`Syncer(adapter_factory=...)`。
    之前我用 `syncm._adapter = fake` 打模块级补丁，多个测试之间会互相污染
    （前一个测试的补丁留在原地，后一个测试在不知情的情况下用了假适配器）。
    """
    tmp = tempfile.mkdtemp(prefix="xcpc_sync_")
    db = dbm.Database(os.path.join(tmp, "t.db"))
    ok, detail = await db.open()
    assert ok, detail
    store = stm.Store(db)
    amap = adapter_map or {}

    def factory(platform):
        if platform in amap:
            return amap[platform]
        return syncm._adapter(platform)

    s = syncm.Syncer(db, store, adapter_factory=factory)
    for uid, plats in (handles or {}).items():
        for p, h in plats.items():
            await store.set_handle(uid, p, h)
    return db, store, s, factory


# ---------------------------------------------------------------------------
# 1. 失败不推进断点
# ---------------------------------------------------------------------------

def test_failure_keeps_cursor():
    print("\n[1] 失败不推进断点")

    async def main():
        ad = FakeAdapter(submissions_script=[
            Fetched(items=[mk_sub(1), mk_sub(2)], ok=True, cursor=500),
            Fetched(ok=False, error_kind="限流", detail="被 429 了"),
            Fetched(items=[mk_sub(3)], ok=True, cursor=900),
        ])
        db, store, s, _ = await fresh({"codeforces": ad},
                                      {"u1": {"codeforces": "someone"}})

        r1 = await s.sync_one("u1", "codeforces")
        check("第一次成功", r1.ok and r1.submissions == 2, r1.line())
        st = await store.get_sync_state("u1", "codeforces")
        check("断点推进到 500", st["last_epoch"] == 500, repr(st["last_epoch"]))

        r2 = await s.sync_one("u1", "codeforces")
        check("第二次失败", not r2.ok and r2.error_kind == "限流", r2.line())
        st = await store.get_sync_state("u1", "codeforces")
        check("失败后断点仍是 500（没被推进）", st["last_epoch"] == 500,
              repr(st["last_epoch"]))
        check("失败被记进 sync_state", st["error_kind"] == "限流")

        # 第三次的 since 应该是 500 —— 而不是被失败的 0 或更大值污染
        await s.sync_one("u1", "codeforces")
        since_used = [c for c in ad.calls if c[0] == "subs"][-1][2]
        check("恢复后从 500 续拉（没跳段）", since_used == 500, repr(since_used))

        # 失败时不该写进任何提交
        check("失败的两次没写脏数据", await store.count_submissions("u1") == 3,
              "实际 %d" % await store.count_submissions("u1"))

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 2. 平台互相独立
# ---------------------------------------------------------------------------

def test_platform_independence():
    print("\n[2] 平台互相独立")

    async def main():
        good = FakeAdapter(submissions_script=[
            Fetched(items=[mk_sub(1)], ok=True, cursor=100)])
        bad = FakeAdapter(submissions_script=[
            Fetched(ok=False, error_kind="网络不可达", detail="连不上")])
        db, store, s, _ = await fresh(
            {"codeforces": good, "atcoder": bad},
            {"u1": {"codeforces": "a", "atcoder": "b"}})

        report = await s.sync("u1")
        check("整体报告不为空", len(report.results) == 2)
        check("CF 成功", [r for r in report.results if r.platform == "codeforces"][0].ok)
        check("AtCoder 失败", not [r for r in report.results
                                   if r.platform == "atcoder"][0].ok)
        check("整体 ok=False（有失败）", report.ok is False)
        check("一个挂了不影响另一个已写入",
              await store.count_submissions("u1", "codeforces") == 1)
        check("失败的平台没写数据",
              await store.count_submissions("u1", "atcoder") == 0)

        text = report.text()
        check("回报里两个平台都出现", "CF" in text and "AtCoder" in text, text[:200])
        check("回报里带失败分类", "网络不可达" in text, text[:200])

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. 适配器抛异常也不能拖垮整次同步
# ---------------------------------------------------------------------------

def test_adapter_crash():
    print("\n[3] 适配器抛异常")

    async def main():
        boom = FakeAdapter(submissions_script=[RuntimeError("适配器写崩了")])
        ok = FakeAdapter(submissions_script=[
            Fetched(items=[mk_sub(1)], ok=True, cursor=1)])
        db, store, s, _ = await fresh(
            {"codeforces": boom, "atcoder": ok},
            {"u1": {"codeforces": "a", "atcoder": "b"}})

        report = await s.sync("u1")
        check("异常被兜住，没往上抛", len(report.results) == 2)
        r = [x for x in report.results if x.platform == "codeforces"][0]
        check("崩掉的平台被标为失败", not r.ok)
        check("失败分类是内部的", r.error_kind in ("内部错误", "网络不可达"),
              r.error_kind)
        check("另一个平台照常成功",
              [x for x in report.results if x.platform == "atcoder"][0].ok)

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. "没有"和"拿不到"必须区分
# ---------------------------------------------------------------------------

def test_missing_vs_unavailable():
    print("\n[4] 「没有」和「拿不到」要区分")

    async def main():
        ad = FakeAdapter(submissions_script=[Fetched(items=[], ok=True, cursor=0)])
        db, store, s, _ = await fresh({"qoj": ad}, {"u1": {"qoj": "someone"}})

        # 需要登录但没凭据：应该报「凭据失效」，**不能**返回成功 + 零条
        r = await s.sync_one("u1", "qoj")
        check("没登录时报「凭据失效」而不是成功", not r.ok, r.line())
        check("失败分类是凭据失效", r.error_kind == "凭据失效", r.error_kind)
        check("说明里提到登录", "登录" in r.detail, r.detail)
        check("没有把 0 条当成结果写进去",
              await store.count_submissions("u1", "qoj") == 0)
        check("适配器根本没被调用（没发注定失败的请求）", len(ad.calls) == 0,
              repr(ad.calls))

        # 没绑 handle：也要明确报
        db2, store2, s2, _ = await fresh({}, {"u2": {}})
        r2 = await s2.sync_one("u2", "codeforces")
        check("没绑 handle 时报凭据失效", not r2.ok and r2.error_kind == "凭据失效",
              r2.line())
        check("说明里让人去绑定", "绑定" in r2.detail, r2.detail)
        await db2.close()

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 5. 幂等：重复同步不重复计数
# ---------------------------------------------------------------------------

def test_idempotent_sync():
    print("\n[5] 重复同步")

    async def main():
        items = [mk_sub(1), mk_sub(2), mk_sub(3)]
        ad = FakeAdapter(submissions_script=[
            Fetched(items=items, ok=True, cursor=300),
            Fetched(items=items, ok=True, cursor=300),
        ])
        db, store, s, _ = await fresh({"codeforces": ad},
                                      {"u1": {"codeforces": "x"}})
        r1 = await s.sync_one("u1", "codeforces", with_contests=False)
        check("第一次 3 条", r1.submissions == 3, r1.line())
        r2 = await s.sync_one("u1", "codeforces", with_contests=False)
        check("第二次 0 条新增", r2.submissions == 0, r2.line())
        check("总数仍是 3", await store.count_submissions("u1") == 3)

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 6. 多用户：同一平台两个人各自同步
# ---------------------------------------------------------------------------

def test_multi_user_sync():
    print("\n[6] 多用户同时同步")

    async def main():
        ad = FakeAdapter(submissions_script=[
            Fetched(items=[mk_sub(1, "CF:1A")], ok=True, cursor=100),
            Fetched(items=[mk_sub(9, "CF:9Z")], ok=True, cursor=200),
        ])
        db, store, s, _ = await fresh(
            {"codeforces": ad},
            {"qq1001": {"codeforces": "alice"}, "qq2002": {"codeforces": "bob"}})

        await s.sync_one("qq1001", "codeforces")
        await s.sync_one("qq2002", "codeforces")

        check("A 只看到自己的题",
              all(r["problem_key"] == "CF:1A"
                  for r in await store.list_submissions("qq1001")))
        check("B 只看到自己的题",
              all(r["problem_key"] == "CF:9Z"
                  for r in await store.list_submissions("qq2002")))

        # 断点也必须各管各的
        sa = await store.get_sync_state("qq1001", "codeforces")
        sb = await store.get_sync_state("qq2002", "codeforces")
        check("A 的断点是 100", sa["last_epoch"] == 100, repr(sa["last_epoch"]))
        check("B 的断点是 200", sb["last_epoch"] == 200, repr(sb["last_epoch"]))

        # 同一用户同一平台的锁
        lk1 = s._lock("qq1001", "codeforces")
        lk2 = s._lock("qq2002", "codeforces")
        check("不同用户的锁不同", lk1 is not lk2)
        check("同一用户同一平台锁相同",
              s._lock("qq1001", "codeforces") is lk1)

        await db.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/sync.py 自测")
    print("=" * 62)
    test_failure_keeps_cursor()
    test_platform_independence()
    test_adapter_crash()
    test_missing_vs_unavailable()
    test_idempotent_sync()
    test_multi_user_sync()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
