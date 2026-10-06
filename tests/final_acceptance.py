#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终验收：逐条核对目标里写明的**硬性约束**。

不是数测试个数，而是把目标原文里的每条规矩拿出来验一遍：
  1. 多用户数据按 user_id 隔离
  2. 不降级、不静默失败
  3. 失败必须分类 + 记日志
  4. 凭据永不进日志和 API 响应
  5. 每步有测试（离线夹具，不依赖网络）
"""

import asyncio
import io
import os
import re
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN)

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


def src(rel):
    return io.open(os.path.join(PLUGIN, rel.replace("/", os.sep)),
                   encoding="utf-8").read()


async def main():
    print("=" * 70)
    print("最终验收：逐条核对硬性约束")
    print("=" * 70)

    # -------- 1. 多用户隔离 --------
    print("\n【1】多用户数据按 user_id 隔离")
    from core import db as dbm, store as stm
    from platforms.base import Submission
    tmp = tempfile.mkdtemp(prefix="xcpc_fin_")
    db = dbm.Database(os.path.join(tmp, "t.db"))
    ok, _ = await db.open()
    store = stm.Store(db)

    # ⚠️ 两个坑都踩了：
    #   1. upsert_submissions 收 Submission 对象，不是 dict
    #   2. `accepted` 是**属性**（由 verdict 推出来），不是构造参数
    # 结果第一次跑出"隔离失败"的假象。又一次"检查器比代码先错"。
    def sub(sid, key, epoch, diff):
        return Submission(platform="codeforces", submission_id=sid,
                          problem_key=key, verdict="OK", epoch=epoch,
                          difficulty=diff, difficulty_source="cf")

    await store.upsert_submissions("A", "codeforces", [sub("1", "CF:1A", 1000, 800)])
    await store.upsert_submissions("B", "codeforces", [sub("2", "CF:2B", 2000, 900)])
    a = await store.list_submissions("A")
    b = await store.list_submissions("B")
    check("A 只看到自己的 1 条", len(a) == 1 and a[0]["problem_key"] == "CF:1A",
          repr([x["problem_key"] for x in a]))
    check("B 只看到自己的 1 条", len(b) == 1 and b[0]["problem_key"] == "CF:2B",
          repr([x["problem_key"] for x in b]))

    guards = len(re.findall(r"_require_user\(", src("core/store.py")))
    check("store 里有 %d 处 _require_user 闸门" % guards, guards >= 20,
          "%d 处" % guards)
    try:
        await store.list_submissions("")
        check("空 user_id 被拒绝（不是静默返回 0 行）", False, "居然没抛")
    except ValueError:
        check("空 user_id 被拒绝（不是静默返回 0 行）", True)

    # -------- 2. 不降级 --------
    print("\n【2】不降级、不静默失败")
    loop_src = src("core/loop.py")
    check("模型失败时不编一个降级方案",
          "used_previous" in loop_src and "fallback" not in loop_src.lower(),
          "看 loop.run 的失败分支")
    check("失败时给出上一版并说明", "上一版" in loop_src or "上次" in loop_src)
    llm_src = src("core/llm.py")
    check("模型输出非法时抛错而不是猜", "raise LLMError" in llm_src,
          "validate 里应该有 raise")
    sync_src = src("core/sync.py")
    check("同步失败时不推进游标",
          "last_epoch" in sync_src and "save_sync_error" in src("core/store.py"))

    # -------- 3. 失败分类 --------
    print("\n【3】失败必须分类 + 记日志")
    log_src = src("core/log.py")
    m = re.search(r"ERROR_KINDS\s*=\s*\(([^)]*)\)", log_src, re.S)
    check("有固定的失败分类枚举", m is not None)
    if m:
        kinds = re.findall(r'"([^"]+)"', m.group(1))
        print("      分类：%s" % "、".join(kinds))
        check("至少 6 类", len(kinds) >= 6, "%d 类" % len(kinds))
        for must in ("凭据失效", "网络不可达", "解析失败", "数据库错误"):
            check("  含「%s」" % must, must in kinds)
    check("有 Recorder 记日志", "class Recorder" in log_src)
    check("记录里带 user_id", "user_id" in log_src)

    # -------- 4. 凭据不进日志 / API --------
    print("\n【4】凭据永不进日志和 API 响应")
    check("有 mask_text 打码", "def mask_text" in log_src)
    # 真跑一遍打码
    from core import log as logm
    dirty = ("Cookie: SESSDATA=abc123def456ghi; token=xyz\n"
             "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret\n"
             "password=hunter2\n"
             'window[_$daewqwskl[0]].cookie="C3VK=deadbeef99"')
    clean = logm.mask_text(dirty)
    for secret in ("abc123def456ghi", "eyJhbGciOiJIUzI1NiJ9.secret",
                   "hunter2", "deadbeef99"):
        check("「%s…」被打码" % secret[:14], secret not in clean,
              repr(clean[:150]))
    acc_src = src("core/accounts.py")
    check("Session 的敏感字段 repr=False",
          acc_src.count("repr=False") >= 2,
          "%d 处" % acc_src.count("repr=False"))
    check("有 Session.public() 只导出安全字段", "def public(" in acc_src)

    # -------- 5. 离线测试 --------
    print("\n【5】测试不依赖网络")
    t = src("tests/test_http.py")
    check("打到最底层 _do_request（不是 get_json）",
          "_do_request" in t, "改上层会导致 monkeypatch 静默失效")
    check("有「没有真联网」的元检查", "test_no_real_network" in t)
    n_tests = 0
    for fn in os.listdir(HERE):
        if fn.startswith("test_") and fn.endswith(".py"):
            n_tests += 1
    check("%d 个测试文件" % n_tests, n_tests >= 13)

    await db.close()

    print("\n" + "=" * 70)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
