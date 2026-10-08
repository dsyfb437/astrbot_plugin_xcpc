#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把几千条提交压成**一页给 LLM 看的汇总**。

这个模块的职责边界（很重要）
----------------------------
**它只描述，不判断。** 判断交给 LLM。

具体说：
  * 它算"你在 dp 上的实际通过率是 73%，而同难度带的期望是 84%，差 11 个点，
    样本 15 条，可信度中" —— 这是描述。
  * 它**不**算"所以你该多练 dp" —— 那是 LLM 的活。

为什么坚持这条线：如果让规则去"评估"人，那就是把一套死规则伪装成智能；
用户看到的建议会变得无法追问、也无法改进。

数学部分借鉴 `02-tools/cf_analyze.py`（已实测过的实现），但这里是独立实现，
不依赖那个文件 —— 插件要能脱离工作区单独装。

难度不跨平台换算
----------------
CF rating、AtCoder 的 IRT 估计值、洛谷的 1-7 档是**三套尺子**。
本模块**只在同一 `difficulty_source` 内部做统计**，
跨来源的数字并列展示但**不合成一个分数**。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# 可信度阈值：样本太少时不要给出"结论"，只给"还看不出来"
MIN_SAMPLE_LOW = 8
MIN_SAMPLE_MID = 20
MIN_SAMPLE_HIGH = 50

# 分数带宽度。**只对 CF rating 成立** —— 三套尺子不能共用一套档。
BAND = 200

# 洛谷的 1-7 档是**另一套尺子**：档宽就是 1，而且每档有名字。
LUOGU_BAND_NAMES = {
    1: "入门", 2: "普及-", 3: "普及/提高-", 4: "普及+/提高",
    5: "提高+/省选-", 6: "省选/NOI-", 7: "NOI/NOI+/CTSC",
}

# AtCoder 的 IRT 是估计值，可正可负，档宽 400（社区习惯的段位步长）。
ATC_BAND = 400

# 难度回避的判定阈值：
# 某 tag 的"中位难度"比该来源整体中位难度高出这么多，就算偏难的方向
AVOID_GAP = 300
# 在他**有标签的**已 AC 题里占比低于这个值，算"碰得少"。
#
# ⚠️ 分母是"有标签的题"，不是"全部 AC 题"。洛谷的题不在题库里 → 没有标签，
# 拿它当分母会把 share 稀释成 1/5，凭空多出一堆"回避方向"。
# 这个数要能解释清楚：**我们只在看得见标签的那部分数据上做判断。**
AVOID_SHARE = 0.04

# ---- 「绕不过去」的方向 ------------------------------------------------
#
# ⚠️ 排序**不能只看 gap**。gap 回答的是"这个方向比整体难多少"，
# 但它**没回答"这个方向在 XCPC 里多常见"**。
#
# 真机反馈：模型拿到的是 `string suffix structures`（用户一共只碰过 1 题），
# 用户看完直接问「dp 的权重是不是该大一点」—— 他 dp 也只做了几道，
# 但**一场区域赛里 dp 几乎必然出现，后缀结构可能一整年碰不到一次**。
# 同样的 25 分钟花在 dp 上，期望收益高得多。
#
# 这张表只是"至少绕不过去"的那一批，**不是重要性排序** ——
# 真正的频率信号来自题库题量（见排序那段的 `sqrt(题量/最大题量)`）。
AVOID_CORE_TAGS = {
    "dp", "graphs", "data structures", "greedy", "math",
    "implementation", "trees", "dsu", "shortest paths",
    "binary search", "sortings", "constructive algorithms",
}
AVOID_CORE_BOOST = 1.8      # 核心方向的 score 再乘这个（只拉开"核心 vs 冷门"）
# 频率项的权重**不是常数**，见下面排序那段的推导：
#   score = (gap / AVOID_GAP) × sqrt(题量 / 最大题量) × 核心加成
# 早期版本用 `1 + 0.3×占比`（最多 1.3 倍），被证明压不住 gap —— 已废弃。


# ---- 把各平台的标签名归一到一套词表 ------------------------------------
#
# 为什么需要
# ----------
# 题库里现在有两个来源：CF 的 39 个粗标签（`dp` / `trees` / `graphs`…，
# 全英文），洛谷的 262 个细标签（`动态规划 DP` / `树形数据结构` /
# `并查集`…，全中文）。**它们是两套词表，描述的是同一批能力。**
#
# 不归一的话会出两个问题，而且都很安静：
#   1. `solved_by_tag` 会把同一个方向拆成两行 —— CF 的 `dp` 和洛谷的
#      `动态规划 DP` 各数各的，两边的题量都偏低，谁都不像"练过"。
#   2. `tagged_solved`（分母）会把洛谷题算进去，而分子只认得 CF 名字，
#      **同一个数字的分子分母来自两套词表** —— 比不算还糟。
#
# 真机背景：他 444 题 AC 里"有标签的只有 76 题"，那 76 全是 CF。
# 洛谷那 333 道 AC 因为词表不通，在标签分析里根本不存在。
#
# 只映射**能对上**的。洛谷那些 CF 没有对应概念的（`莫队` / `Lyndon 分解` /
# `KTT`…）**原样保留** —— 它们会以洛谷名字出现在弱项列表里，这没问题：
# 那个列表本来就是"他自己这些年的方向分布"，多几个中文名不损失什么，
# 而硬塞进一个不准确的 CF 名字才是真的丢信息。
#
# ⚠️ 只在**分析时**归一，**题库里存的还是站点原话**（`store.upsert_problems`
# 存的是 `tags_json`）。理由：题库是"站点说了什么"的记录，归一化是
# "我们怎么理解"的决策 —— 后者会变，前者不该跟着变。
TAG_ALIAS = {
    # --- 基础 ---
    "模拟": "implementation",
    "枚举": "brute force",
    "暴力数据结构": "brute force",
    "排序": "sortings",
    "离散化": "sortings",
    "构造": "constructive algorithms",
    "Ad-hoc": "constructive algorithms",
    "分类讨论": "constructive algorithms",
    "位运算": "bitmasks",
    "前缀和": "data structures",
    "差分": "data structures",
    "高精度": "math",
    # --- 字符串 ---
    "字符串": "strings",
    "字符串（入门）": "strings",
    "哈希 hashing": "hashing",
    "哈希表": "hashing",
    "字典树 Trie": "string suffix structures",
    "AC 自动机": "string suffix structures",
    "后缀数组 SA": "string suffix structures",
    "后缀树": "string suffix structures",
    "KMP 算法": "string suffix structures",
    "Z 函数": "string suffix structures",
    "Manacher 算法": "string suffix structures",
    "有限状态自动机": "string suffix structures",
    # --- 动态规划 ---
    "动态规划 DP": "dp",
    "递推": "dp",
    "线性 DP": "dp",
    "背包 DP": "dp",
    "树形 DP": "dp",
    "状压 DP": "dp",
    "数位 DP": "dp",
    "区间 DP": "dp",
    "动态 DP": "dp",
    "DP 套 DP": "dp",
    "轮廓线 DP": "dp",
    "动态规划优化": "dp",
    "矩阵加速": "dp",
    "斜率优化": "dp",
    "决策单调性": "dp",
    # --- 搜索 ---
    "搜索": "brute force",
    "深度优先搜索 DFS": "dfs and similar",
    "广度优先搜索 BFS": "dfs and similar",
    "递归": "dfs and similar",
    "剪枝": "brute force",
    "记忆化搜索": "dp",
    "启发式搜索": "brute force",
    "迭代加深搜索": "brute force",
    "折半搜索 meet in the middle": "meet-in-the-middle",
    "随机化": "probabilistic",
    "模拟退火": "probabilistic",
    # --- 图论 ---
    "图论": "graphs",
    "图论建模": "graphs",
    "图遍历": "graphs",
    "拓扑排序": "graphs",
    "连通块": "graphs",
    "生成树": "graphs",
    "强连通分量": "graphs",
    "双连通分量": "graphs",
    "欧拉回路": "graphs",
    "Tarjan": "graphs",
    "仙人掌": "graphs",
    "基环树": "graphs",
    "最短路": "shortest paths",
    "差分约束": "shortest paths",
    "Floyd 算法": "shortest paths",
    "二分图": "graph matchings",
    "一般图的最大匹配": "graph matchings",
    "网络流": "flows",
    "最大流最小割定理": "flows",
    "最小割": "flows",
    "费用流": "flows",
    "上下界网络流": "flows",
    "2-SAT": "2-sat",
    # --- 树 ---
    "树形数据结构": "trees",
    "树论": "trees",
    "树的遍历": "trees",
    "树的直径": "trees",
    "树的重心": "trees",
    "最近公共祖先 LCA": "trees",
    "树链剖分": "trees",
    "虚树": "trees",
    "笛卡尔树": "trees",
    "圆方树": "trees",
    "Kruskal 重构树": "trees",
    # --- 数据结构 ---
    "线性数据结构": "data structures",
    "栈": "data structures",
    "队列": "data structures",
    "优先队列": "data structures",
    "单调队列": "data structures",
    "单调栈": "data structures",
    "链表": "data structures",
    "堆": "data structures",
    "线段树": "data structures",
    "树状数组": "data structures",
    "平衡树": "data structures",
    "分块": "data structures",
    "ST 表": "data structures",
    "莫队": "data structures",
    "倍增": "data structures",
    "离线处理": "data structures",
    "可持久化": "data structures",
    "可持久化线段树": "data structures",
    "线段树合并": "data structures",
    "树套树": "data structures",
    "bitset": "data structures",
    "并查集": "dsu",
    "可并堆": "dsu",
    # --- 分治 / 二分 ---
    "分治": "divide and conquer",
    "cdq 分治": "divide and conquer",
    "点分治": "divide and conquer",
    "线段树分治": "divide and conquer",
    "整体二分": "divide and conquer",
    "二分": "binary search",
    "三分": "ternary search",
    "双指针 two-pointer": "two pointers",
    # --- 数学 ---
    "数学": "math",
    "数论": "number theory",
    "素数判断": "number theory",
    "最大公约数 gcd": "number theory",
    "扩展欧几里德算法": "number theory",
    "线性筛法": "number theory",
    "欧拉函数": "number theory",
    "逆元": "number theory",
    "不定方程": "number theory",
    "进制": "number theory",
    "中国剩余定理 CRT": "chinese remainder theorem",
    "Lucas 定理": "combinatorics",
    "组合数学": "combinatorics",
    "排列组合": "combinatorics",
    "容斥原理": "combinatorics",
    "鸽笼原理": "combinatorics",
    "二项式定理": "combinatorics",
    "Catalan 数": "combinatorics",
    "Stirling 数": "combinatorics",
    "Fibonacci 数列": "combinatorics",
    "概率论": "probabilistic",
    "期望": "probabilistic",
    "随机游走 Markov Chain": "probabilistic",
    "矩阵运算": "matrices",
    "矩阵乘法": "matrices",
    "线性代数": "matrices",
    "高斯消元": "matrices",
    "线性基": "matrices",
    "快速傅里叶变换 FFT": "fft",
    "快速数论变换 NTT": "fft",
    "快速沃尔什变换 FWT": "fft",
    "博弈论": "games",
    "SG 函数": "games",
    "Nim 积": "games",
    "博弈树": "games",
    # --- 几何 ---
    "计算几何": "geometry",
    "平面几何": "geometry",
    "凸包": "geometry",
    "叉积": "geometry",
    "向量": "geometry",
    "扫描线": "geometry",
    "旋转卡壳": "geometry",
    "半平面交": "geometry",
    "线段相交": "geometry",
}


def _canon_tags(tags):
    """把一道题的标签归一到统一词表（见 `TAG_ALIAS`）。

    `None` 表示"这个平台给不出标签"，和"标签是空列表"是两回事，
    原样透传 —— `tagged_solved` 那个分母靠这个区分。
    归一之后**去重**：洛谷一道题可能同时有 `动态规划 DP` 和 `线性 DP`，
    都映到 `dp`，重复计数会把 `solved_by_tag` 灌水。
    """
    if tags is None:
        return None
    out = []
    for t in tags:
        name = TAG_ALIAS.get(t, t)
        if name not in out:
            out.append(name)
    return out


def _band(d: int | None, source: str = "cf_rating") -> str:
    """把难度分档。**分档方式按来源走** —— 三套尺子不能共用一套档。

    ⚠️ 真机翻车现场（v0.5.14 修）：洛谷的 difficulty 是 **1-7 档**，
    却按 CF 的 200 一档去分，于是 733 条洛谷提交**全部落进「0-199」**，
    汇总里赫然写着「【洛谷 1-7 档】0-199：308 题」。
    模型照抄，用户看到的就是「洛谷 308 题全是 0-199 档」。

    讽刺的是提示词硬规则第 3 条**明令禁止**跨平台比较难度 ——
    结果是我们在**渲染层**先破的戒。规则写在提示词里，尺子却错在代码里。
    """
    if d is None:
        return ""
    d = int(d)
    if source == "luogu_level":
        return "%d 档（%s）" % (d, LUOGU_BAND_NAMES.get(d, "未知"))
    if source == "atcoder_irt":
        lo = (d // ATC_BAND) * ATC_BAND
        return "%d~%d" % (lo, lo + ATC_BAND - 1)
    lo = (d // BAND) * BAND
    return "%d-%d" % (lo, lo + BAND - 1)


def _median(xs: list[int]) -> float | None:
    ys = sorted(x for x in xs if x is not None)
    if not ys:
        return None
    n = len(ys)
    return float(ys[n // 2]) if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2.0


def _confidence(n: int) -> str:
    if n >= MIN_SAMPLE_HIGH:
        return "高"
    if n >= MIN_SAMPLE_MID:
        return "中"
    if n >= MIN_SAMPLE_LOW:
        return "低"
    return "样本不足"


def shrunk_rate(ok: int, total: int, prior_rate: float, prior_weight: float = 10.0) -> float:
    """经验贝叶斯收缩后的通过率。

    **为什么不能直接 ok/total**：做了 2 题对了 2 题 = 100%，
    会和做了 200 题对了 170 题（85%）被当成同一回事 ——
    前者其实什么都说明不了。

    做法：把观测往"先验通过率"拉，拉的力度随样本量减小而增大
    （等价于在观测里掺 `prior_weight` 个虚拟样本）。

    `prior_weight=10` 是个保守值：样本 10 条时收缩一半，50 条时只收缩 1/6。
    """
    total = max(0, int(total))
    ok = max(0, min(int(ok), total))
    if total == 0:
        return float(prior_rate)
    return (ok + prior_rate * prior_weight) / (total + prior_weight)


@dataclass
class TagStat:
    tag: str
    solved: int = 0            # 去重后的 AC 题数
    submissions: int = 0       # 该 tag 下的提交数
    accepted: int = 0          # 其中 AC 的提交数
    actual_rate: float = 0.0   # 原始通过率
    adjusted_rate: float = 0.0 # 收缩后
    gap: float = 0.0           # 与整体的差（正=比整体强）
    confidence: str = "样本不足"
    median_difficulty: float | None = None   # 他做过的这些题的中位难度

    def line(self) -> str:
        parts = ["%s：AC %d 题 / 提交 %d" % (self.tag, self.solved, self.submissions)]
        if self.submissions >= MIN_SAMPLE_LOW:
            parts.append("通过率 %.0f%%（校正后 %.0f%%，整体基准 %.0f%%）"
                         % (self.actual_rate * 100, self.adjusted_rate * 100,
                            (self.adjusted_rate - self.gap) * 100))
            parts.append("差 %+.1f 个点" % (self.gap * 100))
        parts.append("可信度 %s" % self.confidence)
        if self.median_difficulty is not None:
            parts.append("做过的题中位难度 %.0f" % self.median_difficulty)
        return "  " + "，".join(parts)


@dataclass
class Summary:
    """一页汇总。`to_text()` 的输出是喂给 LLM 的东西。"""
    user_id: str = ""
    generated_at: str = ""
    days_to_contest: int | None = None
    contest_name: str = ""

    total_submissions: int = 0
    total_solved: int = 0
    platforms: list[dict] = field(default_factory=list)
    by_source: dict = field(default_factory=dict)     # 按难度来源分组的统计
    weak_tags: list[TagStat] = field(default_factory=list)
    strong_tags: list[TagStat] = field(default_factory=list)
    avoided: list[dict] = field(default_factory=list)  # 难度回避
    recent: dict = field(default_factory=dict)         # 最近活跃度
    execution: dict = field(default_factory=dict)      # 上一版方案执行得怎么样
    contests: dict = field(default_factory=dict)
    feedback: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)     # 口径说明 / 数据缺口
    # 供 LLM 挑题的候选（已去重、排好序）
    candidates: list[dict] = field(default_factory=list)

    def to_text(self, max_chars: int = 6000) -> str:
        """给 LLM 看的一页。**控制长度**，超了就截断候选池而不是截断诊断。"""
        L: list[str] = []
        L.append("# 训练数据汇总（生成于 %s）" % self.generated_at)
        if self.contest_name and self.days_to_contest is not None:
            L.append("距「%s」还有 %d 天。" % (self.contest_name, self.days_to_contest))
        L.append("")

        L.append("## 总量")
        L.append("提交 %d 条，去重后 AC %d 题。" % (self.total_submissions, self.total_solved))
        for p in self.platforms:
            L.append("  %s：提交 %d（AC %d）" % (p["name"], p["total"], p["ac"]))
        L.append("")

        L.append("## 难度分布（**按来源分开，不跨平台换算**）")
        for src, info in self.by_source.items():
            bands = info.get("bands") or {}
            if not bands:
                continue
            L.append("  【%s，%s，中位难度 %s】"
                     % (src, info.get("label", ""), info.get("median")))
            for b, c in sorted(bands.items(), key=lambda kv: _band_key(kv[0])):
                L.append("    %s：%d 题" % (b, c))
        L.append("")

        if self.weak_tags:
            L.append("## 相对薄弱（按与整体基准的差排序）")
            for t in self.weak_tags[:8]:
                L.append(t.line())
            L.append("")

        if self.strong_tags:
            L.append("## 相对擅长")
            for t in self.strong_tags[:5]:
                L.append(t.line())
            L.append("")

        if self.avoided:
            L.append("## ⚠ 疑似难度回避（**重点**）")
            L.append("  判据：这些方向的题在题库里偏难，而他碰得明显少。")
            L.append("  注意区分「不感兴趣」和「不敢碰」—— 后者才是要处理的。")
            # **只展示前几条**。样本小的时候（比如总共才 90 道 AC），
            # 大多数 tag 都会"看起来像回避" —— 一次给 20 条等于没给，
            # 模型会挑花眼，用户也不知道先动哪个。
            # 按"高出整体中位多少"排序（`avoided` 已经排好），
            # 挑最突出的几条，其余只报个数。
            top = self.avoided[:6]
            L.append("  **这一段是按「难度差 × 这个方向有多常见」排的，不是只按难度差。**"
                     " 一场区域赛里 dp / 图论几乎必然出现，冷门考点可能一整年碰不到一次 ——"
                     " 所以**先补排在前面的**。带「绕不过去」标记的是核心方向。")
            for a in top:
                L.append("  %s：只做过 %d 题（%.1f%%，分母是 %s 道**有标签的** AC 题），"
                         "该类题在题库里有 %s 道，中位难度 %s，比整体中位高 %.0f%s"
                         % (a["tag"], a["count"], a["share"] * 100,
                            a.get("share_base") or 0, a["bank_count"],
                            a["bank_median"], a["gap"],
                            "　← **绕不过去的方向**" if a.get("core") else ""))
            if len(self.avoided) > len(top):
                rest = "、".join(x["tag"] for x in self.avoided[len(top):])
                L.append("  （另有 %d 个方向也符合这个特征：%s。"
                         "样本小的时候这些不太可靠，**先看上面几个**）"
                         % (len(self.avoided) - len(top), rest[:220]))
            L.append("")

        if self.execution:
            e = self.execution
            L.append("## 上一版方案的执行情况（**最重要的一段**）")
            if e.get("rate") is None:
                # ⚠️ **一条打卡记录都没有。**
                # 以前这里会渲染成「最近 0 天：完成 0 天，做了一半 0 天，没做 0 天」——
                # 那句话在模型眼里就长成"上一版方案 0 天打卡"，于是它写下
                # 「之前排的量没接住」「执行率 0/0 是排多的信号」。
                # **0/0 不是"执行率低"，是"没有数据"。** 拿"没人打卡"去论证
                # "量排多了"，等于用没发生的事责怪他。
                L.append("  **这一节现在没有任何数据 —— 他到目前为止一次卡都没打过"
                         "（`/xcpc 打卡` 一次没用过）。**")
                L.append("  **不要据此判断他的执行力。**「上一版没接住」「排多了」"
                         "「执行率 0/0」这类话一个字都不要写 —— 没有数据就是没有数据。"
                         "想让它以后有数据，就在方案末尾提醒他做完打一次卡。")
            else:
                L.append("  这是他自己打的卡，不是猜的。**排计划时必须先看这个。**")
                L.append("  最近 %d 天：完成 %d 天，做了一半 %d 天，没做 %d 天。"
                         % (e.get("days", 0), e.get("done", 0),
                            e.get("partial", 0), e.get("skipped", 0)))
                L.append("  执行率 %.0f%%，连续完成 %d 天。"
                         % (e["rate"] * 100, e.get("streak", 0)))
                # 给一句**解读**，但不替模型下结论
                if e["rate"] < 0.4:
                    L.append("  （执行率偏低 —— 先想想是不是量排多了，"
                             "而不是「他不努力」。连着做不完的计划等于没有计划。）")
                elif e["rate"] > 0.85:
                    L.append("  （执行率很高 —— 可以考虑加一点量，"
                             "或者把难度往上推一档。）")
            for r in (e.get("recent") or []):
                tag = {"done": "完成", "partial": "做了一半",
                       "skipped": "没做"}.get(r.get("status"), r.get("status"))
                line = "    %s  %s" % (r.get("date", ""), tag)
                if r.get("note"):
                    line += "  —— %s" % str(r["note"])[:80]
                L.append(line)
            L.append("")

        if self.recent:
            L.append("## 最近活跃度")
            L.append("  最近 7 天提交 %d 条，最近 30 天 %d 条，连续活跃 %d 天。"
                     % (self.recent.get("d7", 0), self.recent.get("d30", 0),
                        self.recent.get("streak", 0)))
            if self.recent.get("last_epoch"):
                L.append("  最后一次提交：%s" % self.recent["last_at"])
            L.append("")

        if self.contests:
            L.append("## 比赛记录")
            L.append("  共 %d 场。" % self.contests.get("total", 0))
            for c in (self.contests.get("recent") or [])[:5]:
                L.append("    %s  %s" % (c.get("name", "")[:40], c.get("extra", "")))
            L.append("")

        if self.feedback:
            L.append("## 用户最近的反馈（**他自己说的话，权重最高**）")
            for f in self.feedback[:10]:
                L.append("  [%s] %s" % (f.get("date", ""), f.get("text", "")[:200]))
            L.append("")

        if self.candidates:
            # 把"这道题命中哪个回避方向"直接标在候选池上。
            #
            # 真机翻车现场：候选池前 12 条全是 trees（因为回避方向被排到最前），
            # 模型就一口气排了三道树题 —— 而提示词里明明写着「一道就够」。
            # **把判据摊在模型眼前，比在提示词里多写一句"不要贪多"管用。**
            avoid_tags = {a["tag"].lower(): a["tag"] for a in self.avoided[:6]}
            L.append("## 候选题目（**只能从这里挑，题号不得编造**）")
            if avoid_tags:
                L.append("  标了「回避方向」的是上面那几条里的题 —— "
                         "**最多挑一道**，别一场比赛全排同一个方向。")
            for c in self.candidates[:40]:
                mark = ""
                if avoid_tags:
                    hit = [avoid_tags[t.lower()] for t in (c.get("tags") or [])
                           if t.lower() in avoid_tags]
                    if hit:
                        mark = "　← 回避方向：" + "、".join(hit)
                L.append("  %s  %s  难度 %s(%s)%s%s"
                         % (c["problem_key"], (c.get("title") or "")[:44],
                            c.get("difficulty"), c.get("difficulty_source"),
                            ("  标签 " + "/".join(c["tags"])) if c.get("tags") else "",
                            mark))
            L.append("")

        if self.notes:
            L.append("## 口径与数据缺口（**必须如实转达，不要当成'零'**）")
            for n in self.notes:
                L.append("  · %s" % n)

        text = "\n".join(L)
        if len(text) > max_chars:
            # 超长时**优先砍候选池**，保留诊断 —— 诊断才是要它判断的东西
            cut = text.find("## 候选题目")
            if cut > 0:
                head = text[:cut]
                tail = text[cut:]
                keep = max(0, max_chars - len(head) - 200)
                text = head + tail[:keep] + "\n  …（候选池过长已截断）"
        return text


def _band_key(b: str) -> int:
    """分档名 → 排序用的数字。

    ⚠️ 不能再用 `b.split("-")[0]`：洛谷的档名里带减号
    （「3 档（普及/提高-）」），split 完 `int()` 会炸成 0，
    于是所有档挤在一起。直接用正则取**开头那个整数**。
    """
    m = re.match(r"\s*(-?\d+)", b or "")
    try:
        return int(m.group(1)) if m else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# 从数据库构建
# ---------------------------------------------------------------------------

async def build(store, user_id: str, *, platform_names: dict | None = None,
                bank: dict | None = None, contest_days: int | None = None,
                contest_name: str = "", candidates: list | None = None) -> Summary:
    """从 store 读数据并汇总。

    `bank` 是题库标注（`{platform: {problem_key: {tags, difficulty, ...}}}`），
    用来判断"这个方向的题在题库里偏不偏难" —— 这是**难度回避**判定的依据。

    所有读取都经过 store 的 user_id 闸门，所以不可能读到别人的数据。
    """
    from . import log as logm

    platform_names = platform_names or {
        "codeforces": "CF", "atcoder": "AtCoder", "qoj": "QOJ", "luogu": "洛谷"}
    s = Summary(user_id=user_id, generated_at=logm.stamp(),
                days_to_contest=contest_days, contest_name=contest_name)

    subs = await store.list_submissions(user_id, limit=100000)
    s.total_submissions = len(subs)

    # 平台统计
    stats = {_field(r, "platform"): r for r in await store.platform_stats(user_id)}
    for p, n in platform_names.items():
        st = stats.get(p)
        if st:
            s.platforms.append({"platform": p, "name": n,
                                "total": st["total"], "ac": st["ac"] or 0})

    if not subs:
        s.notes.append("这个账号还没有任何提交数据 —— 先同步。"
                       "**注意：「没有数据」不等于「水平是零」**，"
                       "只能说明还没拉到。")
        # ⚠️ **提前返回之前，先把打卡记录读出来。**
        #
        # 踩过的坑：执行情况的读取原来放在函数末尾，而这个 early return
        # 在它前面 —— 于是"没同步过数据但打了卡"的用户，
        # 那几天的打卡被**完全忽略**，汇总里一个字都没有。
        # 对刚上手的人来说这恰恰是最需要被看到的信号。
        # （测试抓到的：u2 打了 6 天卡、没有提交记录，汇总里没有执行情况。）
        try:
            s.execution = await store.task_stats(user_id, days=14)
        except Exception:
            s.execution = {}
        return s

    # ---- 按难度来源分组统计 ------------------------------------------
    # 关键：**不跨来源合并**。CF rating 和 AtCoder IRT 不是一套尺子。
    by_src: dict[str, list] = defaultdict(list)
    for r in subs:
        src = _field(r, "difficulty_source") or "unknown"
        by_src[src].append(r)

    SRC_LABEL = {"cf_rating": "CF rating（官方）",
                 "atcoder_irt": "AtCoder IRT 估计值（社区，可正可负）",
                 "luogu_level": "洛谷 1-7 档",
                 "qoj_none": "QOJ（不公开难度）",
                 "unknown": "来源不明"}

    for src, rows in by_src.items():
        ac_problems = {_field(r, "problem_key") for r in rows if _is_ac(_field(r, "verdict"))}
        bands = Counter()
        diffs = []
        for key in ac_problems:
            d = None
            for r in rows:
                if _field(r, "problem_key") == key and _field(r, "difficulty") is not None:
                    d = _field(r, "difficulty")
                    break
            if d is not None:
                bands[_band(d, src)] += 1
                diffs.append(d)
        s.by_source[src] = {
            "label": SRC_LABEL.get(src, src),
            "bands": dict(bands),
            "median": _median(diffs) if diffs else None,
            "n_with_difficulty": len(diffs),
        }
        if src == "atcoder_irt" and diffs:
            s.notes.append("AtCoder 的难度是 IRT 估计值（可正可负），"
                           "**不能和 CF rating 直接比大小**。")
        if src == "unknown":
            s.notes.append("有 %d 条提交没有难度信息（题目不在题库里，"
                           "或那个平台不公开难度）。" % len(rows))

    # ---- 整体通过率基准（同来源内）-----------------------------------
    def ac_rate(rows) -> tuple[int, int]:
        total = len(rows)
        ok = sum(1 for r in rows if _is_ac(_field(r, "verdict")))
        return ok, total

    primary = "cf_rating" if "cf_rating" in by_src else max(
        by_src, key=lambda k: len(by_src[k]))
    p_ok, p_total = ac_rate(by_src[primary])
    prior = (p_ok / p_total) if p_total else 0.5

    # ---- 每个 tag 的表现 ---------------------------------------------
    # tag 从题库标注里取；题库里没有的题（比如 QOJ）就没有 tag，
    # 这时**不能当成"没有标签"，只能当成"不知道"**。
    bank = bank or {}
    prob_tags: dict[str, list[str] | None] = {}
    prob_diff: dict[str, int | None] = {}
    for _plat, probs in bank.items():
        for key, info in (probs or {}).items():
            # 归一：CF 的 `dp` 和洛谷的 `动态规划 DP` 是同一个方向（见 TAG_ALIAS）。
            # 不归一的话分子分母会来自两套词表 —— 见 `_canon_tags` 的说明。
            prob_tags[key] = _canon_tags(info.get("tags"))
            prob_diff[key] = info.get("difficulty")

    tag_rows: dict[str, list] = defaultdict(list)
    tagged_problems = 0
    for r in subs:
        tags = prob_tags.get(_field(r, "problem_key"))
        if tags is None:
            continue          # 不知道标签 —— 不计入任何 tag（不是"标签为空"）
        tagged_problems += 1
        for t in tags:
            tag_rows[t].append(r)

    if not prob_tags:
        s.notes.append("题库标注还没拉，所以**没法按算法标签分析**。"
                       "先同步一次题库（CF 的 topicset 有 38 个标签）。")

    all_stats: list[TagStat] = []
    for tag, rows in tag_rows.items():
        ok, total = ac_rate(rows)
        ac_probs = {_field(r, "problem_key") for r in rows if _is_ac(_field(r, "verdict"))}
        diffs = [prob_diff[k] for k in ac_probs if prob_diff.get(k) is not None]
        adj = shrunk_rate(ok, total, prior)
        all_stats.append(TagStat(
            tag=tag, solved=len(ac_probs), submissions=total, accepted=ok,
            actual_rate=(ok / total) if total else 0.0,
            adjusted_rate=adj, gap=adj - prior,
            confidence=_confidence(total),
            median_difficulty=_median(diffs) if diffs else None,
        ))

    all_stats.sort(key=lambda t: t.gap)
    s.weak_tags = [t for t in all_stats if t.submissions >= MIN_SAMPLE_LOW][:10]
    s.strong_tags = [t for t in reversed(all_stats)
                     if t.submissions >= MIN_SAMPLE_LOW][:6]

    # ---- 难度回避 ---------------------------------------------------
    # 判据：这个方向的题**在题库里偏难**，而他碰得**明显少**。
    # 只算碰得少的，不然"练得多"的 tag 也会被算进来。
    solved_keys = {_field(r, "problem_key") for r in subs if _is_ac(_field(r, "verdict"))}
    total_solved = len(solved_keys)
    s.total_solved = total_solved

    if total_solved >= 8 and prob_tags:
        # 该来源整体的题库中位难度
        src_probs = []
        for _plat, probs in bank.items():
            for key, info in (probs or {}).items():
                if info.get("difficulty_source") == primary and info.get("difficulty") is not None:
                    src_probs.append((key, info))
        overall_med = _median([d for _k, i in src_probs for d in [i.get("difficulty")]])

        if overall_med is not None:
            # 每个 tag 在**题库里**的中位难度（不是他做过的，是整个题库）
            bank_tag_diff: dict[str, list[int]] = defaultdict(list)
            for _k, info in src_probs:
                for t in (info.get("tags") or []):
                    bank_tag_diff[t].append(info["difficulty"])

            solved_by_tag = Counter()
            for key in solved_keys:
                for t in (prob_tags.get(key) or []):
                    solved_by_tag[t] += 1

            # ★ 分母是「**有标签**的已 AC 题」，不是「全部 AC 题」。
            #
            # 真机翻车现场：他 AC 了 444 题，但题库只有 CF 的（洛谷那 308 道
            # 题根本不在题库里 → 没有标签）。拿 444 当分母的话，
            # 一道 ds 题的 share = 1/444 = 0.2%，看着像"完全没碰过"，
            # 而实际上在他**看得见标签的**题里要高得多。
            # 更要命的是模型会把分母当成 444，输出「trees 和 graphs 各 0 题」，
            # 用户一看就知道不对（他做过的题里明明有树）。
            # **宁可说"我们只统计到 76 道"，也不要拿一个假的分母去吓人。**
            tagged_solved = sum(1 for k in solved_keys if prob_tags.get(k))

            for tag, diffs in bank_tag_diff.items():
                if len(diffs) < 20:        # 题库里这个 tag 题太少，不判
                    continue
                med = _median(diffs)
                if med is None:
                    continue
                count = solved_by_tag.get(tag, 0)
                share = (count / tagged_solved) if tagged_solved else 0.0
                gap = med - overall_med
                if gap >= AVOID_GAP and share < AVOID_SHARE:
                    s.avoided.append({
                        "tag": tag, "count": count, "share": share,
                        "share_base": tagged_solved,
                        "bank_median": med, "overall_median": overall_med,
                        "gap": gap, "bank_count": len(diffs),
                    })
            # ★ 排序 = 难度差 × 这个方向有多常见。
            #
            # 光按 `gap` 排，会把「题库中位难度最高」的方向顶到最前面 ——
            # 而那通常是又偏又难的冷门考点（后缀结构、fft…）。
            #
            # v0.5.13 试过一版：gap × 核心加成 × (1 + 0.3×题量占比)。
            # **真机验证下来没用** —— dp 从第 5 名挪到第 5 名，一步没动：
            #   trees 1205 / ds 1119 / **fft 1115** / graphs 1031 / **dp 936**
            # fft 全题库只有 117 道（占 1%），却因为中位难度高（2900）
            # 稳稳压住 dp（2538 道，占 22%）。毛病出在**频率项只值 1.3 倍，
            # 而 gap 的跨度有 2.75 倍** —— 频率根本没参与竞争。
            #
            # v0.5.14 改成**占比开平方**当乘数，让它和 gap 同一个量级：
            #   score = (gap / AVOID_GAP) × sqrt(题量 / 最大题量) × 核心加成
            # 开平方是刻意的：dp 2538 道 vs fft 117 道差 21 倍，
            # 直接乘会把冷门方向彻底清零，而"冷门"不等于"不用补"。
            # 开方后压到 4.6 倍，两边都还在桌上。
            # 实测排序（用户真机数据，公式手算与代码输出逐条一致）：
            #   trees 3.029 / graphs 3.000 / dsu 1.409 /
            #   combinatorics 1.389 / divide and conquer 1.281 / shortest paths 1.182
            # ⚠️ `top_bank` 取的是**回避列表内部**的最大题量（这次是 graphs 1208），
            # 不是题库全量的最大 tag —— 列表已经滤过一遍（难且没碰过），
            # 拿全量的 `implementation`（两千多道）当分母会把所有 score 压扁。
            top_bank = max((a["bank_count"] for a in s.avoided), default=0)
            for a in s.avoided:
                a["core"] = a["tag"].lower() in AVOID_CORE_TAGS
                score = a["gap"] / float(AVOID_GAP)
                if top_bank:
                    # 题库题量是**数据里的真实频率信号**（题库是 CF 全量），
                    # 不是我手写的一张"重要性表"。
                    score *= math.sqrt(a["bank_count"] / float(top_bank))
                if a["core"]:
                    score *= AVOID_CORE_BOOST
                a["score"] = score
            s.avoided.sort(key=lambda a: (-a["score"], a["tag"]))
            if s.avoided:
                s.notes.append(
                    "「疑似难度回避」只是一个**假设**，不是结论 —— "
                    "也可能是不感兴趣、或者没有合适的题源。请结合用户自己的说法判断。")
                s.notes.append(
                    "「碰得少」的统计**只覆盖题库里有标签的题**（题库目前只有 CF）。"
                    "洛谷 / AtCoder 的题没有标签，不在这个统计里 —— "
                    "**不要把「只做过 0 题」说成「他完全没练过这个方向」**，"
                    "只能说「在我们看得见标签的这部分数据里没练过」。")

    # ---- 活跃度 -------------------------------------------------------
    import time
    now = int(time.time())
    epochs = [_field(r, "epoch") for r in subs if _field(r, "epoch")]
    if epochs:
        last = max(epochs)
        d7 = sum(1 for e in epochs if now - e <= 7 * 86400)
        d30 = sum(1 for e in epochs if now - e <= 30 * 86400)
        # 连续活跃天数
        days = sorted({e // 86400 for e in epochs}, reverse=True)
        streak = 1 if days else 0
        for i in range(1, len(days)):
            if days[i - 1] - days[i] == 1:
                streak += 1
            else:
                break
        s.recent = {
            "d7": d7, "d30": d30, "streak": streak, "last_epoch": last,
            "last_at": logm.stamp() if not last else _fmt_epoch(last),
        }

    # ---- 比赛 ---------------------------------------------------------
    try:
        contests = await store.list_contests(user_id, limit=50)
    except Exception:
        contests = []
    if contests:
        cnt = len(contests)
        deltas = [c["rating_delta"] for c in contests if c["rating_delta"] is not None]
        recent = []
        for c in contests[:5]:
            extra = []
            if c["rank"]:
                extra.append("rank %s" % c["rank"])
            if c["rating_delta"] is not None:
                extra.append("Δ%+d" % c["rating_delta"])
            recent.append({"name": c["name"] or c["contest_id"],
                           "extra": "  ".join(extra)})
        s.contests = {"total": cnt, "recent": recent,
                      "delta_sum": sum(deltas) if deltas else None,
                      "delta_n": len(deltas)}

    # ---- 上一版方案的执行情况 ---------------------------------------
    # **这是循环闭环的关键一环。** 没有它的话，计划引擎永远不知道
    # 上一版方案有没有被执行 —— 那它就不是"动态调整"，只是每天重新猜一次。
    try:
        s.execution = await store.task_stats(user_id, days=14)
    except Exception:
        s.execution = {}

    # ---- 上一版方案的执行情况 ---------------------------------------
    # **这是循环闭环的关键一环。** 没有它的话，计划引擎永远不知道
    # 上一版方案有没有被执行 —— 那它就不是"动态调整"，只是每天重新猜一次。
    try:
        s.execution = await store.task_stats(user_id, days=14)
    except Exception:
        s.execution = {}

    # ---- 反馈 ---------------------------------------------------------
    try:
        fb = await store.list_feedback(user_id, days=14)
    except Exception:
        fb = []
    s.feedback = [{"date": r["date"], "text": r["text"]} for r in fb]

    # ---- 候选池 -------------------------------------------------------
    s.candidates = candidates or []

    if tagged_problems == 0 and prob_tags:
        s.notes.append("所有题目都不在题库标注里，所以按标签的分析为空 —— "
                       "**这不是「没有薄弱项」**，是「还没法判断」。")

    return s


def _is_ac(verdict: str) -> bool:
    return str(verdict or "").upper() in ("OK", "AC", "ACCEPTED")


def _fmt_epoch(epoch: int) -> str:
    from . import log as logm
    from datetime import datetime
    return datetime.fromtimestamp(int(epoch), logm.CN_TZ).strftime("%Y-%m-%d %H:%M")


def _field(obj, name: str, default=None):
    """从"数据库 Row"或"dataclass 对象"里统一取值。

    **两种都要支持**，因为调用方有两种：
      * `store.list_submissions()` 返回 `sqlite3.Row`（下标访问）
      * 测试和适配器传的是 `Submission` dataclass（属性访问）

    之前这里只写下标访问，测试传 dataclass 时直接
    `TypeError: 'Submission' object is not subscriptable` 崩掉 ——
    而真实调用路径（走数据库）是好的，所以这个 bug 只会在测试里露头。
    与其让调用方记住"该传哪种"，不如两种都认。
    """
    try:
        return obj[name]
    except (TypeError, KeyError, IndexError):
        pass
    return getattr(obj, name, default)


def pick_candidates(subs: list, bank: dict, *, want_tags: list[str] | None = None,
                    limit: int = 40, min_difficulty: int | None = None,
                    max_difficulty: int | None = None,
                    source: str = "cf_rating") -> list[dict]:
    """从题库里挑**没做过**的候选题。

    **这是规则做的（约束满足），不是 LLM 做的。** LLM 只负责从这里面挑，
    以及解释为什么挑这几道 —— 它不能凭空编题号。

    排序偏好：
      1. 指定 tag 的排前面（用于"强制注入回避方向"）
      2. 难度落在给定区间内
      3. 难度靠近区间中段（太简单没意思，太难做不出）
    """
    solved = {_field(r, "problem_key") for r in subs
              if _is_ac(_field(r, "verdict", ""))}
    want = set(want_tags or [])
    lo = min_difficulty if min_difficulty is not None else 0
    hi = max_difficulty if max_difficulty is not None else 10 ** 9
    mid = (lo + hi) / 2.0

    out = []
    for _plat, probs in (bank or {}).items():
        for key, info in (probs or {}).items():
            if key in solved:
                continue
            if info.get("difficulty_source") != source:
                continue
            d = info.get("difficulty")
            if d is None or not (lo <= d <= hi):
                continue
            tags = info.get("tags") or []
            hit = len(want & set(tags))
            out.append({
                "problem_key": key, "title": info.get("title") or "",
                "tags": tags, "difficulty": d,
                "difficulty_source": info.get("difficulty_source"),
                "_hit": hit, "_dist": abs(d - mid),
            })
    # tag 命中多的优先；命中数相同时，**标签少的优先**；最后看难度离区间中段多远。
    #
    # 「标签少优先」是 v0.5.14 加的。真机翻车现场：模型把
    # `CF:1511C Yet Another Card Deck`（标签 brute force / data structures /
    # implementation / trees）讲成"1100 的树入门题"，用户一看就知道不对 ——
    # 那是一道数组题，`trees` 只是它五个标签里的一个。
    # **标签是"这题会用到"，不是"这题练这个"。** 标签越少，越接近"专练"。
    # 命中 0 个 tag 的常规候选不参与这条（否则没标签的题会全冒到前面）。
    out.sort(key=lambda c: (-c["_hit"],
                            len(c["tags"]) if c["_hit"] else 0,
                            c["_dist"]))
    for c in out:
        c.pop("_hit", None)
        c.pop("_dist", None)
    return out[:limit]
