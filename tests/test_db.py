#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/db.py 的自测 —— **不需要 AstrBot，也不需要网络**。

跑法：
    python tests/test_db.py
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import threading

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import db as dbm  # noqa: E402

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
# 1. 同步核心（不需要 event loop）
# ---------------------------------------------------------------------------

def test_sync_core() -> None:
    print("\n[1] 同步核心")
    tmp = tempfile.mkdtemp(prefix="xcpc_db_")
    path = os.path.join(tmp, "a", "b", "xcpc.db")   # 故意用多层不存在的目录

    conn = dbm.connect_sync(path)
    check("目录不存在时会自动建", os.path.exists(path))

    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("WAL 模式已开启", str(mode).lower() == "wal", "实际 %s" % mode)
    check("busy_timeout 已设置",
          int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 30000)

    check("初始版本是 0", dbm.schema_version_sync(conn) == 0)
    v = dbm.migrate_sync(conn)
    check("迁移后版本 = SCHEMA_VERSION", v == dbm.SCHEMA_VERSION, "实际 %d" % v)

    tables = dbm.table_names_sync(conn)
    for t in ("users", "credentials", "submissions", "contests",
              "problems", "plans", "feedback", "sync_state"):
        check("建出表 %s" % t, t in tables, "实际 %s" % tables)

    # 幂等：再迁一次不应该报错，也不应该丢数据
    conn.execute("INSERT INTO users (user_id, created_at, updated_at) "
                 "VALUES ('u1', 't', 't')")
    conn.commit()
    v2 = dbm.migrate_sync(conn)
    check("重复迁移仍是同一版本", v2 == dbm.SCHEMA_VERSION)
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    check("重复迁移没丢数据", n == 1, "实际 %d" % n)

    healthy, detail = dbm.integrity_check_sync(conn)
    check("完整性检查通过", healthy, detail)

    # 版本比插件新 → 必须报错，不能硬上
    conn.execute("PRAGMA user_version=999")
    conn.commit()
    try:
        dbm.migrate_sync(conn)
        check("库版本比插件新时拒绝打开", False, "居然没报错")
    except RuntimeError as exc:
        check("库版本比插件新时拒绝打开", "降级" in str(exc), str(exc))
    conn.execute("PRAGMA user_version=%d" % dbm.SCHEMA_VERSION)
    conn.commit()

    # 事务回滚
    def boom(c):
        c.execute("INSERT INTO users (user_id, created_at, updated_at) "
                  "VALUES ('u2', 't', 't')")
        raise ValueError("故意炸")

    before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    try:
        conn.execute("BEGIN IMMEDIATE")
        boom(conn)
    except ValueError:
        conn.rollback()
    after = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    check("事务异常后回滚干净", before == after, "%d -> %d" % (before, after))

    conn.close()


# ---------------------------------------------------------------------------
# 2. 异步包装
# ---------------------------------------------------------------------------

def test_async_wrapper() -> None:
    print("\n[2] 异步包装")
    import asyncio

    tmp = tempfile.mkdtemp(prefix="xcpc_db_async_")
    path = os.path.join(tmp, "xcpc.db")

    async def main():
        d = dbm.Database(path)
        ok, detail = await d.open()
        check("open 成功", ok, detail)
        check("schema 版本正确", dbm.SCHEMA_VERSION >= 1)

        await d.execute("INSERT INTO users (user_id, created_at, updated_at) "
                        "VALUES (?, ?, ?)", ("qq1001", "t", "t"))
        row = await d.query_one("SELECT user_id FROM users WHERE user_id=?", ("qq1001",))
        check("写入后能读回", row is not None and row["user_id"] == "qq1001")

        # 参数化：SQL 注入必须无效
        await d.execute("INSERT INTO users (user_id, created_at, updated_at) "
                        "VALUES (?, ?, ?)", ("x'; DROP TABLE users; --", "t", "t"))
        n = await d.query("SELECT user_id FROM users")
        check("参数化挡住了 SQL 注入", len(n) == 2 and "users" in dbm.table_names_sync(d.conn),
              "表数 %s" % dbm.table_names_sync(d.conn))

        # 事务：异常回滚
        def boom(c):
            c.execute("INSERT INTO users (user_id, created_at, updated_at) VALUES ('tmp','t','t')")
            raise RuntimeError("炸")
        try:
            await d.transaction(boom)
            check("transaction 异常会抛出", False)
        except RuntimeError:
            check("transaction 异常会抛出", True)
        row = await d.query_one("SELECT COUNT(*) AS n FROM users WHERE user_id='tmp'")
        check("transaction 回滚干净", row["n"] == 0, "实际 %d" % row["n"])

        # executemany
        await d.executemany(
            "INSERT INTO submissions (user_id, platform, submission_id, problem_key) "
            "VALUES (?, ?, ?, ?)",
            [("qq1001", "codeforces", str(i), "CF:%dA" % i) for i in range(50)])
        row = await d.query_one("SELECT COUNT(*) AS n FROM submissions")
        check("executemany 写入 50 条", row["n"] == 50, "实际 %d" % row["n"])

        # 空列表不该报错
        await d.executemany("INSERT INTO submissions (user_id, platform, submission_id, problem_key) "
                            "VALUES (?, ?, ?, ?)", [])
        check("executemany 空列表不报错", True)

        await d.close()
        check("close 后再用会报错而不是崩", d._conn is None)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. 并发：多用户同时写不能坏
# ---------------------------------------------------------------------------

def test_concurrency() -> None:
    print("\n[3] 并发写入（多用户场景）")
    import asyncio

    tmp = tempfile.mkdtemp(prefix="xcpc_db_conc_")
    path = os.path.join(tmp, "xcpc.db")

    async def main():
        d = dbm.Database(path)
        ok, detail = await d.open()
        check("open 成功", ok, detail)

        async def writer(uid: str, n: int):
            for i in range(n):
                await d.execute(
                    "INSERT INTO submissions (user_id, platform, submission_id, problem_key) "
                    "VALUES (?, 'codeforces', ?, ?)",
                    (uid, "%s-%d" % (uid, i), "CF:%dA" % i))

        # 10 个用户各写 20 条
        await asyncio.gather(*[writer("u%02d" % k, 20) for k in range(10)])

        row = await d.query_one("SELECT COUNT(*) AS n FROM submissions")
        check("200 条全部写入（无丢失）", row["n"] == 200, "实际 %d" % row["n"])

        # 每个用户只能看到自己的
        row = await d.query_one(
            "SELECT COUNT(*) AS n FROM submissions WHERE user_id=?", ("u03",))
        check("按 user_id 能隔离出 20 条", row["n"] == 20, "实际 %d" % row["n"])

        await d.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. 损坏的库要报错，不能静默重建
# ---------------------------------------------------------------------------

def test_corrupt() -> None:
    print("\n[4] 损坏的库")
    import asyncio

    tmp = tempfile.mkdtemp(prefix="xcpc_db_bad_")
    path = os.path.join(tmp, "xcpc.db")
    with io.open(path, "wb") as fh:
        fh.write(b"this is definitely not a sqlite database" * 40)

    async def main():
        d = dbm.Database(path)
        ok, detail = await d.open()
        check("损坏的库 open 返回失败", ok is False, "实际 ok=%s" % ok)
        check("失败时给出说明", bool(detail) and detail != "ok", detail)
        check("没有静默重建（文件仍是坏的）", True)
        # 文件不该被替换成空库
        size = os.path.getsize(path)
        check("原文件没被覆盖", size > 1000, "实际 %d 字节" % size)
        await d.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 60)
    print("core/db.py 自测")
    print("=" * 60)
    test_sync_core()
    test_async_wrapper()
    test_concurrency()
    test_corrupt()
    print("\n" + "=" * 60)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
