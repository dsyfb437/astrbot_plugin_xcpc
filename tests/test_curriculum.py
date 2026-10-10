#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/curriculum.py 的自测 —— 专题阶梯。

重点：
  1. 阶梯本身要自洽（key 唯一、needs 指向存在的 key、难度带 lo<hi）
  2. ★ **每个子专题至少有一个平台挑得出题** —— 挑不出题的专题写进阶梯
     就是骗人：用户点了它，候选池是空的
  3. ★ CF 的标签太粗，很多子专题只能靠洛谷表达 —— 那些必须 lg 非空
  4. 难度带要**按平台翻译**（CF 的 1000-1400 不能直接去卡洛谷的 1-7 档）
  5. 匹配规则：洛谷 any、CF all、其它平台一律 False

跑法：python tests/test_curriculum.py
"""

from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import curriculum as cur    # noqa: E402

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


# ---------------------------------------------------------------------------
# 1. 阶梯自洽
# ---------------------------------------------------------------------------

def test_shape():
    print("\n[1] 阶梯自洽")

    check("两个模块", set(cur.MODULES) == {"dp", "graph"}, repr(set(cur.MODULES)))
    check("dp 排在 graph 前面（它是图论很多算法的前置）",
          list(cur.MODULES)[0] == "dp", repr(list(cur.MODULES)))

    keys = [t.key for t in cur.ALL_TOPICS]
    check("key 不重复", len(keys) == len(set(keys)),
          repr([k for k in keys if keys.count(k) > 1]))
    check("阶梯不是空的", len(cur.ALL_TOPICS) >= 15, "%d 个" % len(cur.ALL_TOPICS))

    bad_mod = [t.key for t in cur.ALL_TOPICS if t.module not in cur.MODULES]
    check("module 都是已知的", not bad_mod, repr(bad_mod))

    bad_band = [t.key for t in cur.ALL_TOPICS if not (t.lo < t.hi)]
    check("难度带 lo < hi", not bad_band, repr(bad_band))

    bad_count = [t.key for t in cur.ALL_TOPICS if not (5 <= t.count <= 30)]
    check("建议题量在 5-30 之间（少了没意义，多了做不完）", not bad_count,
          repr(bad_count))

    missing = [t.key for t in cur.ALL_TOPICS for n in t.needs if n not in cur.BY_KEY]
    check("needs 指向存在的 key", not missing, repr(missing))

    self_need = [t.key for t in cur.ALL_TOPICS if t.key in t.needs]
    check("没有自环", not self_need, repr(self_need))

    check("每个专题都有一句话核心思维",
          all(t.idea.strip() for t in cur.ALL_TOPICS), "")
    check("每个专题都有参考资料",
          all(t.refs for t in cur.ALL_TOPICS),
          repr([t.key for t in cur.ALL_TOPICS if not t.refs]))

    check("BY_KEY 和 ALL_TOPICS 对得上",
          set(cur.BY_KEY) == set(keys), "")

    check("flat_text 列了所有专题",
          all(t.name in cur.flat_text() for t in cur.ALL_TOPICS), "")


# ---------------------------------------------------------------------------
# 2. 每个专题都得挑得出题
# ---------------------------------------------------------------------------

def test_every_topic_has_a_source():
    print("\n[2] 每个子专题至少有一个平台挑得出题")

    # ★ 这条是在防"阶梯很好看但点了没题"。
    # v0.5.19 有过一模一样的翻车：洛谷适配器写着 `supports_problems = True`
    # 却根本没有 `fetch_problems` —— 能力位和行为不一致，而没人发现。
    no_source = [t.key for t in cur.ALL_TOPICS
                 if not (cur.has_source(t, "luogu") or cur.has_source(t, "codeforces"))]
    check("★ 没有「两个平台都挑不出题」的子专题", not no_source, repr(no_source))

    # CF 只有 38 个粗标签 —— 拓扑排序 / 背包 / 强连通分量这些词它**没有**。
    # 那些子专题只能靠洛谷，所以 lg 必须非空。
    # 反过来，硬凑一个近义的 CF 标签（拿 graphs 当拓扑排序）会把不相干的题
    # 标成专练题 —— 比挑不出题更糟，所以宁可 cf=()。
    both_empty = [t.key for t in cur.ALL_TOPICS if not t.lg and not t.cf]
    check("★ 没有「标签两个平台都空着」的子专题", not both_empty, repr(both_empty))

    lg_only = [t.key for t in cur.ALL_TOPICS if not t.cf]
    check("确实有一批子专题是洛谷独有的（CF 词表太粗）", len(lg_only) >= 5,
          repr(lg_only))
    check("★ 洛谷独有的那些 lg 必须非空",
          all(cur.BY_KEY[k].lg for k in lg_only), "")


# ---------------------------------------------------------------------------
# 3. 难度带按平台翻译
# ---------------------------------------------------------------------------

def test_band_for():
    print("\n[3] 难度带按平台分别翻译")

    t = cur.BY_KEY["dp_linear"]           # CF 1000-1400
    check("CF 的带子原样返回",
          cur.band_for(t, "codeforces") == (1000, 1400),
          repr(cur.band_for(t, "codeforces")))
    lg = cur.band_for(t, "luogu")
    check("★ 洛谷的带子落在 1-7 档里",
          isinstance(lg, tuple) and len(lg) == 2 and 1 <= lg[0] <= 7 and 1 <= lg[1] <= 7,
          repr(lg))
    check("★ 洛谷的带子不是把 1000/1400 照抄过去",
          lg != (1000, 1400), repr(lg))
    check("洛谷的 lo <= hi", lg[0] <= lg[1], repr(lg))

    # 单调性：CF 越难，洛谷档位不该越简单
    keys = ["dp_linear", "dp_knapsack", "dp_tree", "dp_opt"]
    levels = [cur.band_for(cur.BY_KEY[k], "luogu")[0] for k in keys]
    check("CF 难度递增 → 洛谷档位不下降", levels == sorted(levels), repr(levels))

    check("lg_level 有边界值", cur.lg_level(999) == 2 and cur.lg_level(1000) == 3
          and cur.lg_level(1400) == 4, repr([cur.lg_level(x)
                                             for x in (999, 1000, 1400)]))


# ---------------------------------------------------------------------------
# 4. 匹配规则
# ---------------------------------------------------------------------------

def test_matches():
    print("\n[4] 匹配规则")

    # 洛谷：命中任意一个即可
    t = cur.BY_KEY["dp_prob"]             # lg = 期望 / 概率论
    check("★ 洛谷命中一个就算（any）",
          cur.matches(t, "luogu", ["期望", "线段树"]), "")
    check("洛谷一个都不中就否",
          not cur.matches(t, "luogu", ["线段树", "贪心"]), "")
    check("洛谷空标签是否", not cur.matches(t, "luogu", []), "")

    # CF：必须全部命中
    t2 = cur.BY_KEY["dp_bitmask"]         # cf = dp + bitmasks
    check("★ CF 要全部命中（all）",
          cur.matches(t2, "codeforces", ["dp", "bitmasks", "math"]), "")
    check("★ CF 只中一半是否（半个标签等于没练）",
          not cur.matches(t2, "codeforces", ["dp"]), "")
    check("CF 空标签是否", not cur.matches(t2, "codeforces", []), "")

    # cf=() 的子专题在 CF 上永远匹配不上
    t3 = cur.BY_KEY["dp_knapsack"]
    check("★ cf 为空的子专题在 CF 上匹配不上（宁可没有，也不要错题）",
          not cur.matches(t3, "codeforces", ["dp", "math"]), "")
    check("同一个子专题在洛谷上匹配得上",
          cur.matches(t3, "luogu", ["背包 DP"]), "")

    # 其它平台一律 False（AtCoder 没有标签）
    check("AtCoder 一律不匹配",
          not cur.matches(t, "atcoder", ["期望"]), "")
    check("未知平台不匹配", not cur.matches(t, "qoj", ["期望"]), "")

    # ★ has_source 和 matches 必须对同一个平台给出一致的回答。
    # 一开始 has_source 写成 `bool(topic.cf)` 兜底，于是 AtCoder 上
    # 「有货」但「一道都匹配不上」—— 这个 bug 就是这条断言抓出来的。
    # 拿专题**自己的**标签去问 matches：有来源 ⇔ 拿自己的标签匹配得上。
    check("★ 有来源 ⇔ 拿自己的洛谷标签匹配得上",
          all(cur.has_source(x, "luogu")
              == cur.matches(x, "luogu", list(x.lg))
              for x in cur.ALL_TOPICS), "")
    check("★ 有来源 ⇔ 拿自己的 CF 标签匹配得上",
          all(cur.has_source(x, "codeforces")
              == cur.matches(x, "codeforces", list(x.cf))
              for x in cur.ALL_TOPICS), "")
    check("★ 同一个专题「has_source 说没有」时 matches 也必须说没有",
          all(not cur.matches(x, "codeforces", list(x.cf))
              for x in cur.ALL_TOPICS if not cur.has_source(x, "codeforces")), "")
    check("AtCoder 没有标签，所以没有来源",
          not cur.has_source(t, "atcoder") and not cur.has_source(t, "qoj"), "")


# ---------------------------------------------------------------------------
# 5. 阶梯导航
# ---------------------------------------------------------------------------

def test_navigation():
    print("\n[5] 阶梯导航")

    dp = cur.topics_of("dp")
    check("dp 阶梯有序", [t.key for t in dp] == [t.key for t in cur.DP_TOPICS], "")
    check("graph 阶梯有序",
          [t.key for t in cur.topics_of("graph")] == [t.key for t in cur.GRAPH_TOPICS], "")

    check("first_of 是第一个", cur.first_of("dp") == dp[0].key, cur.first_of("dp"))

    nxt = cur.next_after(dp[0].key)
    check("next_after 给下一个的 key", nxt == dp[1].key, repr(nxt))
    check("★ next_after 走到头返回空串（不是绕回开头）",
          cur.next_after(dp[-1].key) == "", repr(cur.next_after(dp[-1].key)))
    check("next_after 认不出的 key 返回空串", cur.next_after("没这个") == "",
          repr(cur.next_after("没这个")))

    # unmet_needs 只是提醒，不阻止
    t = cur.BY_KEY["dp_tree"]
    check("没做过前置时能报出来", cur.unmet_needs("dp_tree", set()) == tuple(t.needs),
          repr(cur.unmet_needs("dp_tree", set())))
    check("做过前置就清空", not cur.unmet_needs("dp_tree", set(t.needs)), "")
    check("认不出的 key 不炸", cur.unmet_needs("没这个", set()) == (), "")

    check("topic_order 是 key 的有序元组",
          cur.topic_order("dp") == tuple(t.key for t in dp),
          repr(cur.topic_order("dp")[:3]))
    check("认不出的模块给空元组", cur.topic_order("没这个") == (), "")


def main() -> int:
    print("=" * 62)
    print("core/curriculum.py 自测")
    print("=" * 62)
    test_shape()
    test_every_topic_has_a_source()
    test_band_for()
    test_matches()
    test_navigation()
    print("\n" + "=" * 62)
    print("通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
