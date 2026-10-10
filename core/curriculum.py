"""专题训练阶梯 —— DP 和图论两个模块。

为什么要这张表
==============

v0.6.0 之前，方案引擎只知道"你的 dp 通过率偏低"，于是每天塞一道 dp 就完事。
用户的原话是：

    像这样子推荐一个两个题练一下我感觉根本没效果啊，也没有针对性，
    每次似乎都是从整体做题情况出发

他是对的。**一道题改变不了任何东西** —— 一个子专题要吃 15-20 道才谈得上
入门，而"每天一道、还跨三个方向"意味着每个方向一天 0.5 道。这是训练量的
算术问题，方案层怎么优化都绕不过去。

对症的不是"再聪明一点地挑一道题"，而是把模块拆成**有序的子专题**，
一个子专题连着吃，吃完了再走下一个。所以有了这张表。

他还说了两件事，这张表也照做了：
  · 「把量拉起来的方法应该是 vp」—— VP 由 `core/vp.py` 管，和这张表配合：
    VP 暴露缺口 → 表里挑对应子专题补 → 再 VP 检验。
  · 「你主要从 dp 和图论两个模块出发」—— 所以只有这两个模块。

标签从哪来（这条决定了整张表怎么写）
====================================

**洛谷的标签是细的，CF 的标签是粗的。**

    洛谷：树形 DP 358 · 背包 DP 265 · 状压 DP 259 · 区间 DP 113 ·
          数位 DP 98 · 线性 DP 52 · 最短路 430 · 并查集 392 ·
          拓扑排序 145 · 二分图 166 · 强连通分量 72 · 网络流 225
    CF  ：dp 2538（就这一个）· graphs 1232 · trees 980 · dsu 418 ·
          shortest paths 295 · flows 162 · graph matchings 106 · 2-sat 42

所以每种子专题给了两条匹配规则：

  `lg`  洛谷标签 —— **命中任意一个**就算（它们本来就是并列的说法）
  `cf`  CF 标签 —— **必须全部命中**（用来表达组合，例如树形 DP = dp + trees）

两边都没有的子专题，说明在当前题库里挑不出题 —— 这是要**报出来**的事实，
不是可以糊过去然后给用户排几道不相干的题。

难度带（`lo` / `hi`）怎么定的
=============================

用户的真实分布：800-999 有 34 题（80% 通过）、1200-1399 有 17 题（46%）、
1600-1799 有 10 题（35%），再往上几乎空白，天花板约 1500。
**他自己说「简单题我其实做得很快」** —— 所以这里不排 1000 以下的题。

每个子专题的 `lo` 是"这个子专题入门需要的思维难度"，`hi` 是"吃完这个子专题
应该够得着的难度"。**新子专题从 `lo` 起步**，哪怕他整体水平更高 ——
因为"没见过这个套路"和"rating 低"是两件事。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Topic:
    """一个子专题。`key` 是稳定 id，会写进库，**改名字不要改 key**。"""

    key: str
    module: str            # "dp" / "graph"
    name: str              # 中文名，给用户看的
    idea: str = ""         # 一句话：这个子专题的核心思维是什么
    needs: tuple = ()      # 前置子专题的 key（软约束，只用来排序和提示）
    lg: tuple = ()         # 洛谷标签，命中任意一个即可
    cf: tuple = ()         # CF 标签，必须全部命中
    lo: int = 1200         # 建议难度带下界（CF rating）
    hi: int = 1600         # 建议难度带上界
    count: int = 15        # 建议题量：吃透这个子专题大概要多少道
    refs: tuple = ()       # 参考资料（URL）


#: 模块顺序。**dp 排在前面** —— 用户点名的就是这两个，而 dp 是图论里
#: 很多算法的前置（DAG 上 DP、树形 DP、最短路计数都靠它）。
MODULES: dict[str, dict] = {
    "dp": {
        "name": "动态规划",
        "why": "dp 是图论很多算法的前置（DAG 上 DP、树形 DP、最短路计数），"
               "而且你的 dp 样本只有 9 题 —— 不是弱，是没做过。",
    },
    "graph": {
        "name": "图论",
        "why": "图论的子专题在 CF 上都有专属标签，题目干净、套路清晰，"
               "是最容易靠「堆量」看到进步的一块。",
    },
}


# ======================================================================
# DP 阶梯
# ======================================================================
# 顺序是刻意的：线性 → 背包 → 区间 → 树形 → 状压 → 数位 → 期望 → 优化。
# 前四个是"按状态设计方式"分类（一条线 / 一个容量 / 一段区间 / 一棵子树），
# 后面才是"按题目形态"分类。先会设计状态，再谈优化。
DP_TOPICS: tuple[Topic, ...] = (
    Topic(
        key="dp_linear", module="dp", name="线性 DP / 递推",
        idea="状态是一条线：dp[i] 只跟前几个位置有关。**先把状态定义写清楚再想转移** —— "
             "多数人卡住不是转移难，是状态压根没定义对。",
        lg=("线性 DP", "递推", "线性递推"), cf=(),
        lo=1000, hi=1400, count=15,
        refs=("https://oi-wiki.org/dp/basic/",),
    ),
    Topic(
        key="dp_knapsack", module="dp", name="背包",
        idea="在「容量」这一维上做取舍。**01 / 完全 / 多重 / 分组** 四种只是循环顺序不同，"
             "但顺序错了答案就错 —— 先想清楚「这件物品能不能重复取」。",
        needs=("dp_linear",),
        lg=("背包 DP",), cf=(),
        lo=1200, hi=1600, count=18,
        refs=("https://oi-wiki.org/dp/knapsack/",),
    ),
    Topic(
        key="dp_interval", module="dp", name="区间 DP",
        idea="状态是一段区间 dp[l][r]，**从短区间往长区间推**。"
             "关键词是「合并」 —— 把两个相邻的已解决区间拼起来。",
        needs=("dp_linear",),
        lg=("区间 DP",), cf=(),
        lo=1400, hi=1800, count=15,
        refs=("https://oi-wiki.org/dp/interval/",),
    ),
    Topic(
        key="dp_tree", module="dp", name="树形 DP",
        idea="在树上做 dp：**先递归孩子、再合并到父亲**。「dp[u][…]」里的额外维度"
             "通常表示「选不选 u」或「子树里用了多少」。换根 DP 是它的进阶。",
        needs=("dp_linear",),
        lg=("树形 DP",), cf=("dp", "trees"),
        lo=1400, hi=1800, count=20,
        refs=("https://oi-wiki.org/dp/tree/",),
    ),
    Topic(
        key="dp_bitmask", module="dp", name="状压 DP",
        idea="把「选了哪些」压成一个整数。**状态数是 2^n，所以 n 一定很小（≤20）** —— "
             "看到 n ≤ 20 就该想到它。子集枚举是 O(3^n)，别写成 O(4^n)。",
        needs=("dp_linear",),
        lg=("状压 DP", "轮廓线 DP"), cf=("dp", "bitmasks"),
        lo=1500, hi=1900, count=18,
        refs=("https://oi-wiki.org/dp/state/",),
    ),
    Topic(
        key="dp_digit", module="dp", name="数位 DP",
        idea="按位从高到低填数，状态里记「有没有顶到上界」。**模板是固定的**，"
             "难点全在「要统计什么」怎么放进状态。",
        needs=("dp_linear",),
        lg=("数位 DP",), cf=(),
        lo=1500, hi=1900, count=12,
        refs=("https://oi-wiki.org/dp/number/",),
    ),
    Topic(
        key="dp_prob", module="dp", name="概率 / 期望 DP",
        idea="**期望可以线性叠加**，所以拆成「每一步的贡献 × 到达它的概率」。"
             "倒着推（从终态往回）往往比正着推好写。",
        needs=("dp_linear",),
        lg=("期望", "概率论"), cf=("dp", "probabilities"),
        lo=1600, hi=2000, count=12,
        refs=("https://oi-wiki.org/dp/probability/",),
    ),
    Topic(
        key="dp_matrix", module="dp", name="矩阵加速（矩阵快速幂）",
        idea="转移式固定、次数巨大（n 到 1e18）时，**把一次转移写成矩阵，答案就是矩阵的幂**。"
             "难点是「把状态拼成向量」和「确认转移与下标无关」。",
        needs=("dp_linear",),
        lg=("矩阵加速", "矩阵乘法"), cf=(),
        lo=1500, hi=1900, count=10,
        refs=("https://oi-wiki.org/math/linear-algebra/matrix/",),
    ),
    Topic(
        key="dp_opt", module="dp", name="DP 优化（单调队列 / 斜率）",
        idea="转移式长得像 dp[i] = min/max(f(j)) + g(i) 时，**别去优化 dp，去优化那个 f**。"
             "先写出朴素转移、确认它是 O(n²)，再决定用哪种数据结构压掉一维。",
        needs=("dp_linear", "dp_interval"),
        lg=("动态规划优化", "单调队列", "斜率优化", "决策单调性",
           "凸完全单调性（wqs 二分）"), cf=(),
        lo=1800, hi=2200, count=12,
        refs=("https://oi-wiki.org/dp/opt/",),
    ),
)


# ======================================================================
# 图论阶梯
# ======================================================================
# 顺序：遍历 → 并查集 → 拓扑 → 最短路 → 生成树 → 二分图 → 强连通 → 树上 → 差分约束/2-SAT → 网络流。
# "并查集"排在拓扑前面，是因为它几乎所有连通性题都要用，而且是 Kruskal 的前置。
GRAPH_TOPICS: tuple[Topic, ...] = (
    Topic(
        key="g_traverse", module="graph", name="图的遍历与连通块",
        idea="DFS / BFS 本身不难，**难的是「图上要维护什么」**。"
             "先想清楚：进一个点的时候要更新哪些量、回溯的时候要不要撤销。",
        lg=("深度优先搜索 DFS", "广度优先搜索 BFS", "连通块", "图遍历"),
            cf=("dfs and similar",),
        lo=1000, hi=1400, count=12,
        refs=("https://oi-wiki.org/graph/dfs/",),
    ),
    Topic(
        key="g_dsu", module="graph", name="并查集",
        idea="维护「谁和谁是一伙的」。带权并查集维护的是**两个点之间的差**，"
             "扩展域（种类并查集）维护的是**敌人 / 朋友**这种二元关系。",
        needs=("g_traverse",),
        lg=("并查集",), cf=("dsu",),
        lo=1200, hi=1600, count=15,
        refs=("https://oi-wiki.org/ds/dsu/",),
    ),
    Topic(
        key="g_toposort", module="graph", name="拓扑排序 / DAG 上 DP",
        idea="DAG 上的 DP **就是线性 DP** —— 拓扑序给出了一个合法的「从左到右」。"
             "看到「依赖」「先后」就往这里想。",
        needs=("g_traverse",),
        lg=("拓扑排序",), cf=(),
        lo=1300, hi=1700, count=12,
        refs=("https://oi-wiki.org/graph/topo/",),
    ),
    Topic(
        key="g_shortest", module="graph", name="最短路",
        idea="**先判有没有负权，再选算法**：非负 → Dijkstra，有负 → Bellman-Ford / SPFA，"
             "多源 → Floyd。最短路计数、次短路、分层图都是它的变形。",
        needs=("g_traverse",),
        lg=("最短路",), cf=("shortest paths",),
        lo=1400, hi=1800, count=20,
        refs=("https://oi-wiki.org/graph/shortest-path/",),
    ),
    Topic(
        key="g_mst", module="graph", name="最小生成树",
        idea="Kruskal = 排序 + 并查集，Prim = 最短路式的贪心。"
             "**瓶颈生成树、次小生成树、Kruskal 重构树**都是它的延伸。",
        needs=("g_dsu",),
        lg=("生成树",), cf=(),
        lo=1400, hi=1800, count=15,
        refs=("https://oi-wiki.org/graph/mst/",),
    ),
    Topic(
        key="g_bipartite", module="graph", name="二分图",
        idea="染色判二分图只是入口，**真正考的是匹配**（匈牙利 / König 定理："
             "最小点覆盖 = 最大匹配）。看到「配对」「分配」就想它。",
        needs=("g_traverse",),
        lg=("二分图",), cf=("graph matchings",),
        lo=1500, hi=1900, count=15,
        refs=("https://oi-wiki.org/graph/bi-graph/",),
    ),
    Topic(
        key="g_scc", module="graph", name="强连通分量 / 割点割边",
        idea="Tarjan 一个框架解决四个问题（SCC、割点、桥、双连通），"
             "**分清楚 dfn 和 low 各自代表什么**就不会混。SCC 缩点之后图变成 DAG。",
        needs=("g_traverse",),
        lg=("强连通分量", "双连通分量", "圆方树"), cf=(),
        lo=1600, hi=2000, count=15,
        refs=("https://oi-wiki.org/graph/scc/",),
    ),
    Topic(
        key="g_tree", module="graph", name="树上问题（LCA / 差分 / dfs 序）",
        idea="树上路径问题**先想能不能变成「点到根」的差**："
             "dis(u,v) = dis(u) + dis(v) - 2·dis(lca)。dfs 序把子树变成一段连续区间。",
        needs=("g_traverse",),
        lg=("树的遍历", "树的直径", "最近公共祖先 LCA", "树链剖分",
            "点分治", "虚树", "树上启发式合并", "基环树"), cf=("trees",),
        lo=1500, hi=1900, count=20,
        refs=("https://oi-wiki.org/graph/lca/",),
    ),
    Topic(
        key="g_2sat", module="graph", name="差分约束 / 2-SAT",
        idea="两个都是**把逻辑关系变成图上的边**：差分约束建最短路（或最长路），"
             "2-SAT 建「选了 A 就必须选 B」然后跑 SCC。建模比算法难得多。",
        needs=("g_shortest", "g_scc"),
        lg=("差分约束", "2-SAT"), cf=("2-sat",),
        lo=1700, hi=2100, count=8,
        refs=("https://oi-wiki.org/graph/diff-constraints/",
              "https://oi-wiki.org/graph/2-sat/"),
    ),
    Topic(
        key="g_flow", module="graph", name="网络流",
        idea="**先会建模，再谈 Dinic**。判断是不是流题：有「容量」「分配」「最大收益」，"
             "而且能写成「每个单位货物从源流向汇」。费用流是它的进阶。",
        needs=("g_dsu", "g_bipartite"),
        lg=("网络流", "费用流", "上下界网络流"), cf=("flows",),
        lo=1800, hi=2200, count=12,
        refs=("https://oi-wiki.org/graph/flow/",),
    ),
)


#: 全部子专题，按模块和顺序排好。
ALL_TOPICS: tuple[Topic, ...] = DP_TOPICS + GRAPH_TOPICS

#: key → Topic，查得快一点。
BY_KEY: dict[str, Topic] = {t.key: t for t in ALL_TOPICS}


def topics_of(module: str) -> tuple[Topic, ...]:
    """某个模块的全部子专题（按阶梯顺序）。"""
    return tuple(t for t in ALL_TOPICS if t.module == module)


def topic_order(module: str) -> tuple[str, ...]:
    """某个模块的子专题 key，按阶梯顺序。"""
    return tuple(t.key for t in topics_of(module))


def next_after(key: str) -> str:
    """阶梯上的下一个子专题 key。到顶了就返回空串。"""
    order = topic_order(BY_KEY[key].module) if key in BY_KEY else ()
    if key not in order:
        return ""
    i = order.index(key)
    return order[i + 1] if i + 1 < len(order) else ""


def first_of(module: str) -> str:
    """某个模块的第一个子专题 key。"""
    order = topic_order(module)
    return order[0] if order else ""


def unmet_needs(key: str, done: set[str]) -> tuple[str, ...]:
    """还没做过的前置子专题。**只是提醒，不阻止** —— 用户想跳就跳。"""
    t = BY_KEY.get(key)
    if t is None:
        return ()
    return tuple(k for k in t.needs if k not in done)


# ======================================================================
# 匹配规则
# ======================================================================
# ⚠️ 匹配用的是**题库里的原始标签**（`problems.tags_json`），不是
# `summary._canon_tags()` 归一之后的那套。原因：
#
#   `TAG_ALIAS` 把洛谷的细标签**故意压成了 CF 的粗名字**
#   （`树形 DP` → `dp`、`状压 DP` → `dp`、`背包 DP` → `dp`）——
#   那是为了跨平台做统计（"你的 dp 差"必须把两个平台的 dp 算在一起）。
#   但**专题训练要的正是那份被压掉的粒度**：归一之后，"树形 DP" 和
#   "背包 DP" 是同一个 `dp`，子专题就没法区分了。
#
# 所以：统计用归一的（`summary`），挑题用原始的（这里）。
# 两套词表各司其职，**别把它们接起来**。

#: CF rating → 洛谷 1-7 档的粗映射。
#
# 两套尺子本来就不能换算（提示词硬规则第 3 条明令禁止跨平台比难度），
# 这里只是**选人门槛**用的近似 —— 洛谷档位粒度粗（1-7），错一档的代价
# 远小于"整个子专题挑不出题"。**不要拿这个去比较两个平台的难度。**
_LG_LEVELS = ((1000, 2), (1400, 3), (1800, 4), (2100, 5), (2400, 6))


def lg_level(cf_rating: int) -> int:
    """CF rating 大致对应洛谷第几档。选人门槛用，不做展示。"""
    for lo, lv in _LG_LEVELS:
        if cf_rating < lo:
            return lv
    return 7


def band_for(topic: "Topic", platform: str) -> tuple:
    """某个平台上，这个子专题的难度带 `(lo, hi)`。

    洛谷是 1-7 档、CF 是 rating —— 同一条 `lo`/`hi` 在两个平台上要
    分别翻译，**不能直接拿去比**。
    """
    if platform == "luogu":
        return lg_level(topic.lo), lg_level(topic.hi)
    return topic.lo, topic.hi


def matches(topic: "Topic", platform: str, tags) -> bool:
    """这道题的标签算不算这个子专题。

    · 洛谷：`lg` **命中任意一个**即可（它们本来就是并列的说法）
    · CF  ：`cf` **必须全部命中**（用来表达组合，例如树形 DP = dp + trees）

    `cf=()` 表示**这个平台表达不了这个子专题**。这是刻意留空的，不是漏了 ——
    CF 只有 38 个粗标签，`拓扑排序` / `背包` / `强连通分量` 它都没有对应词。
    硬凑一个（例如拿 `graphs` 当"拓扑排序"）会把不相干的题标成专练题，
    比挑不出题更糟。这些子专题**只从洛谷挑**。
    """
    if not tags:
        return False
    tags = set(tags)
    if platform == "luogu":
        return any(t in tags for t in topic.lg)
    if platform == "codeforces":
        return bool(topic.cf) and all(t in tags for t in topic.cf)
    return False


def has_source(topic: "Topic", platform: str) -> bool:
    """这个平台上有没有可用的标签规则。

    ★ **只对洛谷和 CF 返回 True。** 一开始图省事写成了
    `bool(topic.lg) if platform == "luogu" else bool(topic.cf)` ——
    于是 AtCoder / QOJ 会继承 CF 的答案，而 `matches()` 对它们返回 False。
    两个函数对同一个平台给出相反的回答，`pick_topic_candidates` 就会
    在 AtCoder 上"有货但一道都匹配不上"。**AtCoder 根本没有标签。**
    """
    if platform == "luogu":
        return bool(topic.lg)
    if platform == "codeforces":
        return bool(topic.cf)
    return False


def flat_text() -> str:
    """整张表的纯文本版，给 `/xcpc 帮助 更多` 和排查用。"""
    out = []
    for mod, meta in MODULES.items():
        out.append("【%s】%s" % (meta["name"], meta["why"]))
        for t in topics_of(mod):
            out.append("  %-14s %s  %d-%d  %d 题"
                       % (t.name, t.key, t.lo, t.hi, t.count))
            if t.idea:
                out.append("      %s" % t.idea)
        out.append("")
    return "\n".join(out).rstrip()
