#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/store.py 的自测。

重点在两件事：
  1. **多用户隔离** —— A 的数据在 B 的查询里必须查不到
  2. **失败不能推进断点** —— 否则下次同步会从错的位置开始，静默漏数据

跑法：python tests/test_store.py
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

from core import db as dbm          # noqa: E402
from core import log as logm         # noqa: E402
from core import store as stm       # noqa: E402
from platforms.base import ContestRecord, Problem, Submission  # noqa: E402

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


def sub(sid, key="CF:1A", verdict="OK", epoch=100, diff=1500):
    return Submission(platform="codeforces", submission_id=str(sid),
                      problem_key=key, verdict=verdict, epoch=epoch,
                      difficulty=diff,
                      difficulty_source="cf_rating" if diff is not None else "unknown")


def new_store():
    tmp = tempfile.mkdtemp(prefix="xcpc_store_")
    db = dbm.Database(os.path.join(tmp, "t.db"))
    return db, stm.Store(db)


# ---------------------------------------------------------------------------
# 1. 多用户隔离 最重要
# ---------------------------------------------------------------------------

def test_isolation():
    print("\n[1] 多用户隔离")

    async def main():
        db, s = new_store()
        ok, detail = await db.open()
        check("库打开", ok, detail)

        await s.set_handle("qq1001", "codeforces", "alice")
        await s.set_handle("qq2002", "codeforces", "bob")
        check("A 的 handle 是 alice",
              await s.get_handle("qq1001", "codeforces") == "alice")
        check("B 的 handle 是 bob",
              await s.get_handle("qq2002", "codeforces") == "bob")
        check("改 A 不影响 B",
              await s.get_handle("qq2002", "codeforces") == "bob")

        await s.upsert_submissions("qq1001", "codeforces",
                                   [sub(1, "CF:1A"), sub(2, "CF:2B")])
        await s.upsert_submissions("qq2002", "codeforces", [sub(1, "CF:9Z")])

        check("A 有 2 条", await s.count_submissions("qq1001") == 2)
        check("B 有 1 条", await s.count_submissions("qq2002") == 1)

        a = await s.list_submissions("qq1001")
        b = await s.list_submissions("qq2002")
        check("A 的列表里没有 B 的题",
              all(r["problem_key"] != "CF:9Z" for r in a), repr([r["problem_key"] for r in a]))
        check("B 的列表里没有 A 的题",
              all(r["problem_key"] != "CF:1A" for r in b), repr([r["problem_key"] for r in b]))

        # 同一个 submission_id 在两个用户下必须互不干扰
        check("相同 submission_id 不串（A）",
              (await s.list_submissions("qq1001"))[0]["problem_key"] in ("CF:1A", "CF:2B"))
        check("相同 submission_id 不串（B）",
              (await s.list_submissions("qq2002"))[0]["problem_key"] == "CF:9Z")

        # AC 集合也要隔离
        await s.upsert_submissions("qq2002", "codeforces", [sub(2, "CF:3C", "OK")])
        check("A 的 AC 集合不含 B 的题",
              "CF:9Z" not in await s.accepted_problems("qq1001"))

        # 凭据隔离
        await s.set_credentials("qq1001", "qoj", {"__client_id": "secretA"})
        await s.set_credentials("qq2002", "qoj", {"__client_id": "secretB"})
        check("A 拿到自己的凭据",
              (await s.get_credentials("qq1001", "qoj")).get("__client_id") == "secretA")
        check("B 拿到自己的凭据",
              (await s.get_credentials("qq2002", "qoj")).get("__client_id") == "secretB")

        # 脱敏状态里绝不能出现 blob
        st = await s.credential_status("qq1001")
        check("状态接口不泄漏 cookie", "secretA" not in repr(st), repr(st))
        check("状态接口有 status 字段", st.get("qoj", {}).get("status") == "valid")

        # 比赛记录隔离
        await s.upsert_contests("qq1001", "codeforces", [
            ContestRecord(platform="codeforces", contest_id="100", name="A 的比赛")])
        await s.upsert_contests("qq2002", "codeforces", [
            ContestRecord(platform="codeforces", contest_id="200", name="B 的比赛")])
        ca = await s.list_contests("qq1001")
        check("A 只看到自己的比赛",
              len(ca) == 1 and ca[0]["contest_id"] == "100",
              repr([r["contest_id"] for r in ca]))

        # 方案与反馈隔离
        await s.save_plan("qq1001", "2026-10-07", {"for": "A"})
        await s.save_plan("qq2002", "2026-10-07", {"for": "B"})
        pa = await s.latest_plan("qq1001")
        check("方案隔离", '"A"' in pa["payload_json"], pa["payload_json"])

        await s.add_feedback("qq1001", "2026-10-07", "A 的反馈")
        fb = await s.list_feedback("qq1001")
        check("反馈隔离", all("A 的反馈" in r["text"] for r in fb))

        # 同步断点隔离
        await s.save_sync_ok("qq1001", "codeforces", last_epoch=111)
        await s.save_sync_ok("qq2002", "codeforces", last_epoch=222)
        check("断点隔离",
              (await s.get_sync_state("qq1001", "codeforces"))["last_epoch"] == 111)

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 2. 幂等
# ---------------------------------------------------------------------------

def test_idempotent():
    print("\n[2] 幂等（重复同步不产生重复数据）")

    async def main():
        db, s = new_store()
        await db.open()
        items = [sub(1, "CF:1A"), sub(2, "CF:2B")]

        n1 = await s.upsert_submissions("u1", "codeforces", items)
        check("第一次新增 2 条", n1 == 2, "实际 %d" % n1)

        n2 = await s.upsert_submissions("u1", "codeforces", items)
        check("第二次新增 0 条", n2 == 0, "实际 %d" % n2)
        check("总数仍是 2", await s.count_submissions("u1") == 2)

        # 同 id 但状态变了：应该更新而不是新增
        n3 = await s.upsert_submissions("u1", "codeforces", [sub(1, "CF:1A", "WRONG_ANSWER")])
        check("同 id 更新不算新增", n3 == 0, "实际 %d" % n3)
        check("总数仍是 2", await s.count_submissions("u1") == 2)
        row = [r for r in await s.list_submissions("u1") if r["submission_id"] == "1"][0]
        check("状态被更新", row["verdict"] == "WRONG_ANSWER", row["verdict"])

        # 没有 submission_id 的应被丢弃（没法去重）
        n4 = await s.upsert_submissions("u1", "codeforces",
                                        [Submission(platform="codeforces", submission_id="",
                                                    problem_key="CF:9Z")])
        check("没有 id 的被丢弃", n4 == 0)

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. 同步失败不能推进断点
# ---------------------------------------------------------------------------

def test_failure_keeps_cursor():
    print("\n[3] 同步失败不能推进断点")

    async def main():
        db, s = new_store()
        await db.open()

        await s.save_sync_ok("u1", "codeforces", last_epoch=1000)
        check("成功时断点推进",
              (await s.get_sync_state("u1", "codeforces"))["last_epoch"] == 1000)

        await s.save_sync_error("u1", "codeforces", "限流", "被 429 了")
        st = await s.get_sync_state("u1", "codeforces")
        check("失败后断点**没有**被推进", st["last_epoch"] == 1000,
              "实际 %s" % st["last_epoch"])
        check("失败被记下来", st["error_kind"] == "限流", repr(st["error_kind"]))
        check("失败详情被记下来", "429" in (st["error_detail"] or ""))

        await s.save_sync_ok("u1", "codeforces", last_epoch=2000)
        st = await s.get_sync_state("u1", "codeforces")
        check("恢复成功后错误被清空", st["error_kind"] == "" and st["last_epoch"] == 2000)

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. 难度与标签的语义
# ---------------------------------------------------------------------------

def test_difficulty_semantics():
    print("\n[4] 难度与标签")

    async def main():
        db, s = new_store()
        await db.open()

        # 重复同步时，已有的难度不能被 NULL 覆盖
        await s.upsert_submissions("u1", "atcoder", [
            Submission(platform="atcoder", submission_id="1", problem_key="ATC:x",
                       difficulty=None, difficulty_source="atcoder_irt")])
        await s.upsert_submissions("u1", "atcoder", [
            Submission(platform="atcoder", submission_id="1", problem_key="ATC:x",
                       difficulty=800, difficulty_source="atcoder_irt")])
        row = (await s.list_submissions("u1"))[0]
        check("难度能被补上", row["difficulty"] == 800, repr(row["difficulty"]))

        await s.upsert_submissions("u1", "atcoder", [
            Submission(platform="atcoder", submission_id="1", problem_key="ATC:x",
                       difficulty=None, difficulty_source="atcoder_irt")])
        row = (await s.list_submissions("u1"))[0]
        check("已抓到的难度不被 NULL 抹掉", row["difficulty"] == 800, repr(row["difficulty"]))

        # tags: None（给不出）和 []（真的没有）必须区分
        await s.upsert_problems("atcoder", [
            Problem(platform="atcoder", problem_key="ATC:a", title="A", tags=None,
                    difficulty=100, difficulty_source="atcoder_irt")])
        row = await s.get_problem("atcoder", "ATC:a")
        check("tags=None 存成 NULL", row["tags_json"] is None, repr(row["tags_json"]))

        await s.upsert_problems("codeforces", [
            Problem(platform="codeforces", problem_key="CF:1A", title="A", tags=[],
                    difficulty=800, difficulty_source="cf_rating")])
        row = await s.get_problem("codeforces", "CF:1A")
        check("tags=[] 存成 '[]'（和 None 不同）", row["tags_json"] == "[]",
              repr(row["tags_json"]))

        # ★ v0.5.21：**重新入库**时的语义 —— 这是 v0.5.20 修完过滤规则
        #   却"重拉一次题库什么也没变"的根因。
        #   老实现把"只有非算法标签的题"也算成 tags=None，
        #   而 COALESCE 见 None 就保留旧值 → 脏标签永远冲不掉。
        await s.upsert_problems("luogu", [
            Problem(platform="luogu", problem_key="LG:P1000", title="脏",
                    tags=["O2优化", "天津"], difficulty=2,
                    difficulty_source="luogu_level")])
        await s.upsert_problems("luogu", [
            Problem(platform="luogu", problem_key="LG:P1000", title="净",
                    tags=[], difficulty=2, difficulty_source="luogu_level")])
        row = await s.get_problem("luogu", "LG:P1000")
        check("★ tags=[] 会**冲掉**旧的脏标签（不是保留）",
              row["tags_json"] == "[]", repr(row["tags_json"]))
        check("标题也被更新了", row["title"] == "净", repr(row["title"]))

        # 反过来：None 表示"这次给不出"，必须保留旧值
        await s.upsert_problems("luogu", [
            Problem(platform="luogu", problem_key="LG:P1000", title="净",
                    tags=None, difficulty=2, difficulty_source="luogu_level")])
        row = await s.get_problem("luogu", "LG:P1000")
        check("★ tags=None 保留旧值（'给不出'不等于'没有'）",
              row["tags_json"] == "[]", repr(row["tags_json"]))

        # 难度同理
        await s.upsert_problems("luogu", [
            Problem(platform="luogu", problem_key="LG:P1000", title="净",
                    tags=None, difficulty=None, difficulty_source="luogu_level")])
        row = await s.get_problem("luogu", "LG:P1000")
        check("难度 None 不抹掉已有值", row["difficulty"] == 2,
              repr(row["difficulty"]))

        # 分数带带来源前缀
        check("分数带带来源前缀",
              stm.bucket(1500, "cf_rating") == "cf_rating:1400-1599",
              stm.bucket(1500, "cf_rating"))
        check("不同来源的同一数值不是同一个带",
              stm.bucket(1500, "cf_rating") != stm.bucket(1500, "atcoder_irt"))
        check("难度为 None 时带为空", stm.bucket(None, "cf_rating") == "")

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 5. 输入校验
# ---------------------------------------------------------------------------

def test_validation():
    print("\n[5] 输入校验")

    async def main():
        db, s = new_store()
        await db.open()

        try:
            await s.set_handle("u1", "不存在的平台", "x")
            check("非法平台名被拒绝", False, "居然没报错")
        except ValueError as exc:
            check("非法平台名被拒绝", "不认识的平台" in str(exc), str(exc))

        try:
            await s.count_submissions("")
            check("空 user_id 被拒绝", False, "居然没报错")
        except ValueError:
            check("空 user_id 被拒绝", True)

        # 平台名大小写不敏感
        await s.set_handle("u1", "CodeForces", "x")
        check("平台名大小写不敏感",
              await s.get_handle("u1", "codeforces") == "x")

        # 比赛记录：已抓到的 rank 不被后来的 NULL 覆盖
        await s.upsert_contests("u1", "codeforces", [
            ContestRecord(platform="codeforces", contest_id="1", name="X",
                          rank=100, rating_delta=50, start_epoch=1000)])
        await s.upsert_contests("u1", "codeforces", [
            ContestRecord(platform="codeforces", contest_id="1", name="X",
                          rank=None, rating_delta=None, start_epoch=0)])
        row = (await s.list_contests("u1"))[0]
        check("已有的 rank 不被 NULL 抹掉", row["rank"] == 100, repr(row["rank"]))
        check("已有的 rating_delta 不被抹掉", row["rating_delta"] == 50,
              repr(row["rating_delta"]))
        check("start_epoch 不被 0 覆盖", row["start_epoch"] == 1000,
              repr(row["start_epoch"]))

        await db.close()

    asyncio.run(main())


def test_link_code():
    print("\n[6] 绑定码 / 网页会话安全边界")

    async def main():
        import time
        db, s = new_store()
        await db.open()

        # 生成
        code = await s.create_link_code("qq1001")
        check("码是 6 位", len(code) == 6, repr(code))
        check("不含容易看错的字符（0/O/1/I/l）",
              not (set(code) & set("01OIl")), repr(code))

        # 重复生成会作废旧码 —— 免得旧码泄漏了还能用
        code2 = await s.create_link_code("qq1001")
        check("重复生成换了新码", code2 != code, "%s vs %s" % (code, code2))
        t, uid = await s.claim_link_code(code)
        check("旧码被作废了（用不了）", t is None, repr((t, uid)))

        # 认领
        token, uid = await s.claim_link_code(code2)
        check("认领成功", bool(token) and uid == "qq1001", repr((bool(token), uid)))
        check("码只能用一次", (await s.claim_link_code(code2))[0] is None)

        # 令牌解析
        check("令牌能换回正确的 QQ 号",
              await s.resolve_web_token(token) == "qq1001")
        check("乱填的令牌解析不出来", await s.resolve_web_token("garbage") == "")
        check("空令牌解析不出来", await s.resolve_web_token("") == "")

        # 大小写 / 空格容错（码是要人在手机上照着敲的）
        code3 = await s.create_link_code("qq2002")
        t3, uid3 = await s.claim_link_code(" " + code3.lower() + " ")
        check("小写和空格也能认（手机上手打的）",
              bool(t3) and uid3 == "qq2002", repr((bool(t3), uid3)))

        # 隔离：A 的码不能认成 B
        codeA = await s.create_link_code("qq1001")
        _, uidA = await s.claim_link_code(codeA)
        check("码认出来的是它的属主", uidA == "qq1001", repr(uidA))

        # 撤销
        tokenB, _ = await s.claim_link_code(await s.create_link_code("qq2002"))
        await s.revoke_web_token(tokenB)
        check("撤销后令牌失效", await s.resolve_web_token(tokenB) == "")

        # 过期
        code4 = await s.create_link_code("qq1001", ttl_sec=-1)   # 立刻过期
        t4, msg4 = await s.claim_link_code(code4)
        check("过期的码用不了", t4 is None, repr(t4))
        check("过期时说明里提到过期", "过期" in str(msg4), repr(msg4))

        # 错误原因要能区分
        _, msg = await s.claim_link_code("ZZZZZZ")
        check("不存在的码给出可操作的说明",
              "不存在" in str(msg) and "/xcpc 绑定" in str(msg), repr(msg))
        _, msg2 = await s.claim_link_code("")
        check("空码给出说明", bool(msg2), repr(msg2))

        # 空 user_id 被拒
        try:
            await s.create_link_code("")
            check("空 user_id 被拒绝", False)
        except ValueError:
            check("空 user_id 被拒绝", True)

        await db.close()

    asyncio.run(main())


def test_dashboard_link():
    print("\n[6b] Dashboard 账号 ↔ QQ 号（网页令牌存不住时的唯一依靠）")

    async def main():
        db, s = new_store()
        await db.open()

        # 为什么需要这张表：插件页面跑在 AstrBot 带 sandbox、没有
        # allow-same-origin 的 iframe 里，`localStorage` 一读就抛
        # SecurityError —— 网页令牌**存不住**。
        # Dashboard 登录名是服务端给的，不受浏览器存储影响。
        await s.link_dashboard_user("admin", "qq1001")
        check("按 Dashboard 账号能查回 QQ 号",
              await s.resolve_dashboard_user("admin") == "qq1001")
        check("没绑过的账号查不出东西（不猜）",
              await s.resolve_dashboard_user("nobody") == "")
        check("空账号名查不出东西", await s.resolve_dashboard_user("") == "")

        # **多人**：两个 Dashboard 账号、两个 QQ 号，互不干扰
        await s.link_dashboard_user("admin2", "qq2002")
        check("第二个账号是独立的",
              await s.resolve_dashboard_user("admin2") == "qq2002")
        check("第一个账号没被动过",
              await s.resolve_dashboard_user("admin") == "qq1001")

        # 同一个人重新认领（换了 QQ 号）要覆盖，不是插第二行
        await s.link_dashboard_user("admin", "qq3003")
        check("重新认领会覆盖",
              await s.resolve_dashboard_user("admin") == "qq3003")
        rows = await db.query("SELECT username FROM dashboard_links")
        check("没有留下重复行（是 upsert 不是 insert）", len(rows) == 2,
              "实际 %d 行" % len(rows))

        # 解绑两个条件都要对上，不能顺手删掉别人
        await s.unlink_dashboard_user("admin", "qq1001")   # 已经换成 qq3003 了
        check("user_id 对不上时不删",
              await s.resolve_dashboard_user("admin") == "qq3003")
        await s.unlink_dashboard_user("admin", "qq3003")
        check("对上了才删", await s.resolve_dashboard_user("admin") == "")
        check("别人的关联不受影响",
              await s.resolve_dashboard_user("admin2") == "qq2002")

        # 空账号名不许写进去 —— 否则会变成一行谁都查得到的"公共"关联
        try:
            await s.link_dashboard_user("   ", "qq1001")
            check("空账号名被拒绝", False)
        except ValueError:
            check("空账号名被拒绝", True)

        await db.close()

    asyncio.run(main())


def test_task_log():
    print("\n[7] 打卡 / 执行率循环闭环")

    async def main():
        db, s = new_store()
        await db.open()

        await s.log_task("u1", "done")
        rows = await s.task_log("u1")
        check("记下了", len(rows) == 1 and rows[0]["status"] == "done", repr(rows))
        check("带日期", bool(rows[0]["date"]), repr(rows[0]["date"]))

        # 同一天重复记是**更新**不是追加
        await s.log_task("u1", "skipped", note="太累了")
        rows = await s.task_log("u1")
        check("同一天重复记是更新（不是新增两行）", len(rows) == 1,
              "%d 行" % len(rows))
        check("状态被更新", rows[0]["status"] == "skipped", rows[0]["status"])
        check("备注存下来了", rows[0]["note"] == "太累了", repr(rows[0]["note"]))

        # 统计
        await s.log_task("u1", "done", date="2026-10-01")
        await s.log_task("u1", "done", date="2026-10-02")
        await s.log_task("u1", "partial", date="2026-10-03")
        await s.log_task("u1", "skipped", date="2026-10-04")
        st = await s.task_stats("u1", days=14)
        check("天数对", st["days"] == 5, repr(st["days"]))
        check("分类计数对", (st["done"], st["partial"], st["skipped"]) == (2, 1, 2),
              repr((st["done"], st["partial"], st["skipped"])))
        # (2 完成 + 0.5×1 一半) / 5 = 0.5
        check("执行率把「一半」算半次", abs(st["rate"] - 0.5) < 1e-9,
              repr(st["rate"]))

        # 连续天数：最近是 skipped，所以 streak = 0
        check("连续天数从最近往回数，遇到没做就断", st["streak"] == 0,
              repr(st["streak"]))
        await s.log_task("u2", "done", date="2026-10-05")
        await s.log_task("u2", "partial", date="2026-10-06")
        st2 = await s.task_stats("u2", days=14)
        check("「一半」也算续上连续", st2["streak"] == 2, repr(st2["streak"]))

        # 没记录时不该返回 0%（那会显得像"从来没完成过"）
        st3 = await s.task_stats("u3", days=14)
        check("没记录时 rate 是 None（不是 0）", st3["rate"] is None,
              repr(st3["rate"]))
        check("没记录时 days 是 0", st3["days"] == 0)

        # 非法状态
        try:
            await s.log_task("u1", "瞎写的状态")
            check("非法状态被拒绝", False)
        except ValueError as exc:
            check("非法状态被拒绝", "不认识" in str(exc), str(exc))

        # 隔离
        check("打卡按用户隔离",
              all(r["date"] != "2026-10-05" for r in await s.task_log("u1")))

        # 空 user_id
        try:
            await s.log_task("", "done")
            check("空 user_id 被拒绝", False)
        except ValueError:
            check("空 user_id 被拒绝", True)

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 9. 活跃度（每日推送的数据来源）
# ---------------------------------------------------------------------------

def test_activity():
    print("\n[9] 活跃度：今天的提交 / 连续天数")

    async def main():
        db, s = new_store()
        await db.open()

        # 没数据时必须是个**全 0 的空壳**，不能抛异常 —— 推送靠它决定说什么
        act = await s.activity("nobody")
        check("没数据时不炸", isinstance(act, dict), repr(act))
        check("没数据时 today_sub / streak 都是 0",
              act["today_sub"] == 0 and act["streak"] == 0
              and act["last_epoch"] == 0, repr(act))
        check("没数据时 days_since_last 是 None（不是 0 —— 那会读成「今天来过」）",
              act["days_since_last"] is None, repr(act["days_since_last"]))

        day = logm.now_cn().replace(hour=0, minute=0, second=0, microsecond=0)
        mid = int(day.timestamp())
        D = 86400

        # 今天 3 条（2 条算 AC）、昨天 1 条、前天 1 条 → 连续 3 天
        await s.upsert_submissions("u1", "codeforces", [
            sub("t1", epoch=mid + 10, verdict="OK"),
            sub("t2", key="CF:1B", epoch=mid + 20, verdict="AC"),
            sub("t3", key="CF:1C", epoch=mid + 30, verdict="WRONG_ANSWER"),
            sub("y1", key="CF:1D", epoch=mid - 10, verdict="OK"),
            sub("y2", key="CF:1E", epoch=mid - 2 * D + 30, verdict="OK"),
        ])
        act = await s.activity("u1")
        check("今天的提交只数今天（不含昨天那两条）", act["today_sub"] == 3,
              repr(act["today_sub"]))
        check("OK 和 AC 都算 AC，WA 不算", act["today_ac"] == 2, repr(act["today_ac"]))
        check("连续活跃 3 天", act["streak"] == 3, repr(act["streak"]))
        check("days_since_last = 0（今天来过）", act["days_since_last"] == 0,
              repr(act["days_since_last"]))
        check("d7 / d30 是窗口内总数", act["d7"] == 5 and act["d30"] == 5,
              "%r %r" % (act["d7"], act["d30"]))

        # ★ 边界：中国时区零点前 10 秒算昨天，后 10 秒算今天。
        # 用 UTC 切法（now % 86400）会在早上 8 点前把"今天"算成昨天。
        await s.upsert_submissions("u2", "codeforces", [
            sub("m1", epoch=mid - 10, verdict="OK"),
        ])
        act2 = await s.activity("u2")
        check("★ 昨天 23:59:50 的那条**不算今天**（CN 零点边界）",
              act2["today_sub"] == 0 and act2["days_since_last"] == 1,
              repr(act2))

        # ★ 今天没提交 → streak 归零，而不是"从最近那次往回数"
        await s.upsert_submissions("u3", "codeforces", [
            sub("g1", key="CF:1F", epoch=mid - 3 * D + 3600, verdict="OK"),
            sub("g2", key="CF:1G", epoch=mid - 4 * D + 3600, verdict="OK"),
        ])
        act3 = await s.activity("u3")
        check("★ 今天没提交时 streak = 0（不是 2）—— 否则会写出「连续活跃 2 天」"
              "给一个三天前才交过题的人",
              act3["streak"] == 0 and act3["days_since_last"] == 3, repr(act3))

        # 今天有、中间空几天 → 只算到今天这一段
        await s.upsert_submissions("u4", "codeforces", [
            sub("h1", epoch=mid + 5, verdict="OK"),
            sub("h2", key="CF:1H", epoch=mid - 5 * D, verdict="OK"),
        ])
        act4 = await s.activity("u4")
        check("今天有、中间断 4 天 → streak 只算 1", act4["streak"] == 1,
              repr(act4["streak"]))
        check("昨天没交但今天交了 → days_since_last 仍是 0",
              act4["days_since_last"] == 0, repr(act4["days_since_last"]))

        check("窗口参数只影响 d7/d30，不影响 today_*",
              (await s.activity("u1", days=7))["today_sub"] == 3)

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/store.py 自测")
    print("=" * 62)
    test_isolation()
    test_idempotent()
    test_failure_keeps_cursor()
    test_difficulty_semantics()
    test_validation()
    test_link_code()
    test_dashboard_link()
    test_task_log()
    test_activity()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
