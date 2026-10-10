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


def test_migration_v5() -> None:
    """v5 = 「洛谷 0 AC + QOJ 全是别人」这两个 bug 的**数据侧**修复。

    光改代码不够：游标早就推进了，下次同步只拉新的，库里那 700 多行
    错数据会永远错下去。所以老行要在库里一次性翻新。
    """
    print("\n[5] 历史迁移：库里已经存坏的行怎么翻新")

    tmp = tempfile.mkdtemp(prefix="xcpc_db5_")
    conn = dbm.connect_sync(os.path.join(tmp, "xcpc.db"))
    dbm.migrate_sync(conn)

    def sub(uid, pf, sid, verdict, epoch=1000):
        conn.execute(
            "INSERT INTO submissions (user_id, platform, submission_id, "
            "problem_key, verdict, epoch) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, pf, sid, "%s:%s" % (pf.upper(), sid), verdict, epoch))

    # ---- 脏数据的形态**抄自用户服务器上的真实库** ----
    sub("u1", "luogu", "1", "12")     # 333 条真 AC，全被存成数字
    sub("u1", "luogu", "2", "14")     # 396 条
    sub("u1", "luogu", "3", "2")      # 3 条
    sub("u1", "luogu", "4", "99")     # 认不出的码：别乱改，让它在统计里露出来
    sub("u1", "codeforces", "c1", "OK")     # 本来就对
    sub("u1", "atcoder", "a1", "AC")        # 本来就对
    sub("u1", "qoj", "q1", "lrmlrm")        # ★ 别人的数据，verdict 是人名
    sub("u1", "qoj", "q2", "C++26")         # ★ verdict 是语言
    for pf, ep in (("qoj", 1791418428), ("luogu", 1788592435),
                   ("codeforces", 1790442284)):
        conn.execute("INSERT INTO sync_state (user_id, platform, last_epoch, "
                     "last_ok_at) VALUES ('u1', ?, ?, '2026-10-08 07:00:00')",
                     (pf, ep))
    conn.commit()

    # ---- 装回 v4：这就是"用户服务器升级前"的那个库 ----
    conn.execute("PRAGMA user_version=4")
    conn.commit()

    v = dbm.migrate_sync(conn)
    # ★ 别把版本号写死在这里 —— 加一次迁移就要改一遍测试，
    # 而这条测试真正要验的是"坏行被翻新了"，不是"版本号是几"。
    check("迁到最新版本", v == dbm.SCHEMA_VERSION and v >= 5, "实际 %d" % v)

    def one(sql, *a):
        return conn.execute(sql, a).fetchone()[0]

    check("★ 洛谷 '12' → 'AC'（「0 AC」的直接修复）",
          one("SELECT verdict FROM submissions WHERE submission_id='1'") == "AC",
          repr(one("SELECT verdict FROM submissions WHERE submission_id='1'")))
    check("洛谷 '14' → 'WA'",
          one("SELECT verdict FROM submissions WHERE submission_id='2'") == "WA",
          repr(one("SELECT verdict FROM submissions WHERE submission_id='2'")))
    check("洛谷 '2' → 'CE'",
          one("SELECT verdict FROM submissions WHERE submission_id='3'") == "CE",
          repr(one("SELECT verdict FROM submissions WHERE submission_id='3'")))
    check("认不出的码不被乱改（还是 '99'）",
          one("SELECT verdict FROM submissions WHERE submission_id='4'") == "99")
    check("CF 的 'OK' 没被动（迁移不该误伤对的平台）",
          one("SELECT verdict FROM submissions WHERE submission_id='c1'") == "OK")
    check("AtCoder 的 'AC' 没被动",
          one("SELECT verdict FROM submissions WHERE submission_id='a1'") == "AC")
    check("★ 修完之后洛谷有 1 条被 _is_ac 认的（迁移之前是 0）",
          one("SELECT COUNT(*) FROM submissions WHERE platform='luogu' "
              "AND UPPER(verdict) IN ('OK','AC','ACCEPTED')") == 1)

    check("★ QOJ 的脏行全删了（那些记录根本不属于他，翻新没意义）",
          one("SELECT COUNT(*) FROM submissions WHERE platform='qoj'") == 0)
    check("★ QOJ 的游标也清了（不清的话重拉时会被旧游标整片挡住）",
          one("SELECT last_epoch FROM sync_state WHERE platform='qoj'") is None)
    check("QOJ 的 last_ok_at 也清了",
          one("SELECT last_ok_at FROM sync_state WHERE platform='qoj'") is None)
    check("洛谷的游标没被动（他自己的记录是真的）",
          one("SELECT last_epoch FROM sync_state WHERE platform='luogu'")
          == 1788592435)
    check("CF 的游标没被动",
          one("SELECT last_epoch FROM sync_state WHERE platform='codeforces'")
          == 1790442284)

    # ---- 幂等：再跑一次什么都不该变 ----
    conn.execute("PRAGMA user_version=4")
    conn.commit()
    dbm.migrate_sync(conn)
    check("重复迁移是幂等的（版本还是最新）",
          dbm.schema_version_sync(conn) == dbm.SCHEMA_VERSION)
    check("重复迁移后洛谷仍是 4 行",
          one("SELECT COUNT(*) FROM submissions WHERE platform='luogu'") == 4)
    check("重复迁移后 QOJ 仍是 0 行",
          one("SELECT COUNT(*) FROM submissions WHERE platform='qoj'") == 0)
    conn.close()


def main() -> int:
    print("=" * 60)
    print("core/db.py 自测")
    print("=" * 60)
    test_sync_core()
    test_async_wrapper()
    test_concurrency()
    test_corrupt()
    test_migration_v5()
    print("\n" + "=" * 60)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
