#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/blocks.py 的自测 —— 训练块的进度与阶梯选择。

重点：
  1. ★ 进度**不存库**，每次从 submissions 算 —— 而且要按 `started_at` 切开：
     开块**之前**做过的题算"底子"，不算进度（否则进度条一开就是 8/15）
  2. ★ 数**题目数**不是提交数（同一道题错三次再 AC 只算一道）
  3. ★ `sqlite3.Row` 没有 `.get()` —— 真库返回 Row、测试喂 dict，
     写错会在**真机**上炸而测试全过
  4. 自动选块：dp 优先、跳过早就熟的子专题
  5. `parse_stamp` 解析不了时返回 None 而不是 0（0 是 1970，
     会让进度把所有历史提交都算进来）

跑法：python tests/test_blocks.py
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

from core import blocks as blocksm    # noqa: E402
from core import curriculum as cur    # noqa: E402
from core import db as dbm            # noqa: E402
from core import log as logm          # noqa: E402
from core import store as stm         # noqa: E402
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


def sub(key, verdict="OK", stamp=None, plat="codeforces", diff=1500):
    """★ 时间字段叫 `epoch`（秒），不是 `submitted_at` —— 后者是数据库列名。"""
    return Submission(platform=plat, submission_id="s%s" % key, problem_key=key,
                      verdict=verdict,
                      epoch=(blocksm.parse_stamp(stamp) or 0) if stamp else 1000,
                      difficulty=diff,
                      difficulty_source="cf_rating" if plat == "codeforces"
                      else "luogu_level")


def bank_of(pairs):
    """`[(platform, key, tags, diff, source)]` -> bank 结构。"""
    out: dict = {}
    for plat, key, tags, diff, src in pairs:
        out.setdefault(plat, {})[key] = {
            "tags": tags, "difficulty": diff, "difficulty_source": src,
            "title": key, "problem_key": key,
        }
    return out


TREE_BANK = bank_of([
    ("luogu", "LG:T1", ["树形 DP"], 4, "luogu_level"),
    ("luogu", "LG:T2", ["树形 DP"], 4, "luogu_level"),
    ("luogu", "LG:T3", ["树形 DP"], 3, "luogu_level"),
    ("luogu", "LG:K1", ["背包 DP"], 4, "luogu_level"),
    ("codeforces", "CF:T1", ["dp", "trees"], 1600, "cf_rating"),
    ("codeforces", "CF:M1", ["math"], 1500, "cf_rating"),
])


# ---------------------------------------------------------------------------
# 1. coverage
# ---------------------------------------------------------------------------

def test_coverage():
    print("\n[1] coverage：已 AC 的题按子专题归类")

    subs = [sub("LG:T1"), sub("LG:T1"), sub("LG:T2", verdict="WA"),
            sub("LG:T2", verdict="AC", plat="luogu"),
            sub("CF:T1", plat="codeforces"), sub("CF:M1", plat="codeforces")]
    cov = blocksm.coverage(subs, TREE_BANK)

    check("★ 数题目不数提交（LG:T1 交两次只算一道）",
          cov["dp_tree"] == 2, repr(cov.get("dp_tree")))
    check("★ 错过的题后来 AC 了要算（LG:T2 先 WA 后 AC）",
          int(cov.get("dp_tree", 0)) >= 2, repr(cov.get("dp_tree")))
    check("只 WA 过的题不算", cov.get("dp_knapsack", 0) == 0,
          repr(cov.get("dp_knapsack")))
    check("没标签的方向是 0", cov.get("g_flow", 0) == 0, repr(cov.get("g_flow")))
    check("每个子专题都有键（没做过是 0 不是缺失）",
          set(cov) == set(cur.BY_KEY), "")

    # 题库里没有的题（没标注）不能瞎归类
    cov2 = blocksm.coverage([sub("CF:没这题", plat="codeforces")], TREE_BANK)
    check("题库里没有的题不算进任何子专题",
          all(v == 0 for v in cov2.values()), repr({k: v for k, v in cov2.items() if v}))


# ---------------------------------------------------------------------------
# 2. progress
# ---------------------------------------------------------------------------

def test_progress():
    print("\n[2] progress：进度从 started_at 之后算")

    blk = {"module": "dp", "topic": "dp_tree", "target": 3,
           "band_lo": 1400, "band_hi": 1800,
           "started_at": "2026-09-01 10:00:00", "note": ""}
    subs = [
        sub("LG:T1", plat="luogu", stamp="2026-08-01 10:00:00"),   # 开块之前
        sub("LG:T2", plat="luogu", stamp="2026-09-02 10:00:00"),   # 开块之后
    ]
    prog = blocksm.progress(subs, TREE_BANK, blk)
    check("★ 开块之前 AC 的不算进度", prog["done"] == 1, repr(prog))
    check("★ 但记下来当「底子」", prog["before"] == 1, repr(prog))
    check("target 来自块", prog["target"] == 3, repr(prog))
    check("没吃够", prog["finished"] is False, repr(prog))
    check("recent 是最近几道", isinstance(prog["recent"], list), repr(prog["recent"]))

    # 吃够了
    subs2 = [sub("LG:T%d" % i, plat="luogu", stamp="2026-09-02 10:00:00")
             for i in (1, 2, 3)]
    prog2 = blocksm.progress(subs2, TREE_BANK, blk)
    check("★ 吃够了 finished=True", prog2["done"] == 3 and prog2["finished"] is True,
          repr(prog2))

    # ★ 不存库：同一份输入算两次结果一样，改 submissions 就跟着变
    check("同输入同输出（没有隐藏状态）",
          blocksm.progress(subs, TREE_BANK, blk) == prog, "")
    prog3 = blocksm.progress(subs + [sub("LG:T3", plat="luogu",
                                         stamp="2026-09-03 10:00:00")],
                             TREE_BANK, blk)
    check("★ 多一道 AC，进度跟着涨（不是存下来的计数器）",
          prog3["done"] == prog["done"] + 1, repr((prog["done"], prog3["done"])))

    # started_at 坏了（解析不出来）
    #
    # ★ 这里刻意选了**乐观**的一边：算不清起点的题一律算进度。
    # 反过来的话（全算"底子"）进度会永远停在 0/N —— 他做完 20 道
    # 还是看到 0/20，而且**不会自愈**。乐观那一边顶多是让他早点换块。
    bad = dict(blk, started_at="不是时间")
    progb = blocksm.progress(subs, TREE_BANK, bad)
    check("★ started_at 坏掉时一律算进度（不是算成 0 —— 那不会自愈）",
          progb["done"] == 2 and progb["before"] == 0, repr(progb))
    check("这种时候也不能炸", isinstance(progb["finished"], bool), "")

    check("空块不炸", isinstance(blocksm.progress(subs, TREE_BANK, {}), dict), "")


# ---------------------------------------------------------------------------
# 3. parse_stamp
# ---------------------------------------------------------------------------

def test_parse_stamp():
    print("\n[3] parse_stamp")

    check("认得 logm.stamp() 的格式",
          blocksm.parse_stamp(logm.stamp()) is not None,
          repr(logm.stamp()))
    check("认得 ISO 的 T 分隔",
          blocksm.parse_stamp("2026-09-01T10:00:00") is not None, "")
    check("认得只有日期", blocksm.parse_stamp("2026-09-01") is not None, "")
    check("★ 解析不了返回 None（不是 0）",
          blocksm.parse_stamp("不是时间") is None, "")
    check("None 输入返回 None", blocksm.parse_stamp(None) is None, "")
    check("空串返回 None", blocksm.parse_stamp("") is None, "")
    # ★ 0 是 1970 —— 会让"开块之前的题"判定全部失效
    check("★ 绝不返回 0 冒充成功", blocksm.parse_stamp("乱写") != 0, "")

    t1 = blocksm.parse_stamp("2026-09-01 10:00:00")
    t2 = blocksm.parse_stamp("2026-09-02 10:00:00")
    check("时间先后关系对", t1 is not None and t2 is not None and t1 < t2, "")


# ---------------------------------------------------------------------------
# 4. 自动选块
# ---------------------------------------------------------------------------

def test_choose():
    print("\n[4] 自动选块：dp 优先、跳过熟的")

    empty = {k: 0 for k in cur.BY_KEY}
    check("★ 全都没做时选 dp（它是图论的前置）",
          blocksm.pick_module([], TREE_BANK, empty) == "dp",
          blocksm.pick_module([], TREE_BANK, empty))
    check("全都没做时选阶梯第一个",
          blocksm.choose("dp", empty) == "dp_linear",
          blocksm.choose("dp", empty))

    # dp 都熟了 → 轮到 graph
    dp_done = dict(empty)
    for t in cur.topics_of("dp"):
        dp_done[t.key] = t.count + 5
    check("★ dp 吃透了才轮到图论",
          blocksm.pick_module([], TREE_BANK, dp_done) == "graph",
          blocksm.pick_module([], TREE_BANK, dp_done))
    check("图论从阶梯第一个开始",
          blocksm.choose("graph", dp_done) == "g_traverse",
          blocksm.choose("graph", dp_done))

    # 第一个子专题熟了 → 自动往下走
    one_done = dict(empty)
    one_done["dp_linear"] = cur.BY_KEY["dp_linear"].count
    check("★ 吃透一个就往下走",
          blocksm.choose("dp", one_done) == "dp_knapsack",
          blocksm.choose("dp", one_done))

    # 全都熟了 → 返回阶梯最后一个（不返回空）
    all_done = {t.key: t.count + 9 for t in cur.ALL_TOPICS}
    check("★ 全熟了也不返回空（返回阶梯最后一个）",
          blocksm.choose("dp", all_done) == cur.topics_of("dp")[-1].key,
          blocksm.choose("dp", all_done))

    # needed 的阈值
    t = cur.BY_KEY["dp_tree"]
    half = dict(empty)
    half["dp_tree"] = int(t.count * 0.5)
    check("★ 到一半就不算 needed 了",
          not blocksm.needed("dp_tree", half), repr(half["dp_tree"]))
    half["dp_tree"] = int(t.count * 0.5) - 1
    check("差一道就算 needed", blocksm.needed("dp_tree", half),
          repr(half["dp_tree"]))


# ---------------------------------------------------------------------------
# 5. describe / reason / next_block
# ---------------------------------------------------------------------------

def test_describe():
    print("\n[5] describe / reason / next_block")

    blk = {"module": "dp", "topic": "dp_tree", "target": 20,
           "band_lo": 1400, "band_hi": 1800,
           "started_at": logm.stamp(), "note": ""}
    cov = {k: 0 for k in cur.BY_KEY}
    prog = blocksm.progress([], TREE_BANK, blk)
    d = blocksm.describe(blk, cov, prog)
    check("describe 里有专题名", "树形" in d, d[:80])
    check("describe 里有进度", "0 / 20" in d or "0/20" in d, d[:120])
    check("describe 里有难度带", "1400" in d and "1800" in d, d[:160])
    check("describe 里有核心思维", cur.BY_KEY["dp_tree"].idea[:6] in d, d[:200])

    # 吃够了要提示换一个
    cov2 = dict(cov)
    cov2["dp_tree"] = 20
    prog2 = blocksm.progress([], TREE_BANK, blk)
    prog2["done"] = 20
    prog2["finished"] = True
    d2 = blocksm.describe(blk, cov2, prog2)
    check("★ 吃够了要提示 `/xcpc 块 下一个`", "下一个" in d2, d2[-160:])

    check("空块 describe 不炸", isinstance(blocksm.describe({}, cov, {}), str), "")
    check("reason 说的是人话",
          isinstance(blocksm.reason("dp_tree", cov), str)
          and blocksm.reason("dp_tree", cov).strip() != "", "")
    check("认不出的 key 也有说法",
          isinstance(blocksm.reason("没这个", cov), str), "")

    nxt = blocksm.next_block("dp", cov)
    check("next_block 在 dp 上给 dp_knapsack", nxt == ("dp", "dp_knapsack"),
          repr(nxt))
    done_dp = {k: 0 for k in cur.BY_KEY}
    for t in cur.topics_of("dp"):
        done_dp[t.key] = t.count + 1
    nxt2 = blocksm.next_block("dp", done_dp)
    check("★ dp 走完了就换到图论", nxt2[0] == "graph", repr(nxt2))

    # ★ after：从**当前这一格**往后退
    # 不传 after 时从阶梯开头算 —— 用户点名开在「背包」上再按"下一个"，
    # 会退回背包自己（第一个还没吃透的是线性 DP，它的下一个正是背包）。
    check("★ after 指定从哪一格往后退",
          blocksm.next_block("dp", cov, after="dp_knapsack")
          == ("dp", "dp_interval"),
          repr(blocksm.next_block("dp", cov, after="dp_knapsack")))
    check("不传 after 时从阶梯开头算（开新块用）",
          blocksm.next_block("dp", cov) == ("dp", "dp_knapsack"),
          repr(blocksm.next_block("dp", cov)))
    check("after 认不出时退回从开头算",
          blocksm.next_block("dp", cov, after="没这个") == ("dp", "dp_knapsack"),
          repr(blocksm.next_block("dp", cov, after="没这个")))
    check("★ after 走到阶梯末尾就换模块",
          blocksm.next_block("dp", cov, after=cur.topics_of("dp")[-1].key)[0]
          == "graph",
          repr(blocksm.next_block("dp", cov, after=cur.topics_of("dp")[-1].key)))


# ---------------------------------------------------------------------------
# 6. ★ sqlite3.Row 兼容（真机上才会炸的那一类）
# ---------------------------------------------------------------------------

def test_row_compat():
    print("\n[6] sqlite3.Row 兼容（真库返回 Row，测试一般喂 dict）")

    async def main():
        tmp = tempfile.mkdtemp(prefix="xcpc_blocks_")
        db = dbm.Database(os.path.join(tmp, "t.db"))
        ok, detail = await db.open()
        assert ok, detail
        store = stm.Store(db)
        await store.ensure_user("u1")
        await store.upsert_problems("luogu", [
            Problem("luogu", "LG:T1", "t1", tags=["树形 DP"], difficulty=4,
                    difficulty_source="luogu_level")])
        await store.upsert_submissions("u1", "luogu", [
            Submission(platform="luogu", submission_id="s1", problem_key="LG:T1",
                       verdict="AC", difficulty=4, difficulty_source="luogu_level")])

        rows = await store.list_submissions("u1")
        check("★ 真库返回的确实是 sqlite3.Row（不是 dict）",
              rows and not isinstance(rows[0], dict), type(rows[0]).__name__)
        try:
            cov = blocksm.coverage(rows, TREE_BANK)
            check("★ coverage 吃得下 sqlite3.Row", cov.get("dp_tree") == 1, repr(cov))
        except AttributeError as exc:
            check("★ coverage 吃得下 sqlite3.Row", False, str(exc))

        # 空库 / 空列表
        check("空列表不炸", all(v == 0 for v in blocksm.coverage([], TREE_BANK).values()), "")

        # set_block → get_block 拿到的是 Row，progress 也得吃得下
        await store.set_block("u1", "dp", "dp_tree", target=20,
                              band_lo=1400, band_hi=1800, note="x")
        blk = await store.get_block("u1")
        check("get_block 返回 Row", blk is not None and not isinstance(blk, dict),
              type(blk).__name__)
        prog = blocksm.progress(rows, TREE_BANK, blk)
        check("★ progress 吃得下 Row", isinstance(prog["done"], int), repr(prog))
        check("★ describe 吃得下 Row",
              isinstance(blocksm.describe(dict(blk), {}, prog), str), "")

        await db.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/blocks.py 自测")
    print("=" * 62)
    test_coverage()
    test_progress()
    test_parse_stamp()
    test_choose()
    test_describe()
    test_row_compat()
    print("\n" + "=" * 62)
    print("通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
