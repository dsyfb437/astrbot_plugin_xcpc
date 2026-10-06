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
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# 可信度阈值：样本太少时不要给出"结论"，只给"还看不出来"
MIN_SAMPLE_LOW = 8
MIN_SAMPLE_MID = 20
MIN_SAMPLE_HIGH = 50

# 分数带宽度
BAND = 200

# 难度回避的判定阈值：
# 某 tag 的"中位难度"比该来源整体中位难度高出这么多，就算偏难的方向
AVOID_GAP = 300
# 在这个 tag 上的题量占总量的比例低于这个值，算"碰得少"
AVOID_SHARE = 0.04


def _band(d: int | None, size: int = BAND) -> str:
    if d is None:
        return ""
    lo = (int(d) // size) * size
    return "%d-%d" % (lo, lo + size - 1)


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
            for a in top:
                L.append("  %s：只做过 %d 题（占全部 %.1f%%），"
                         "该类题在题库里的中位难度 %s，比整体中位高 %.0f"
                         % (a["tag"], a["count"], a["share"] * 100,
                            a["bank_median"], a["gap"]))
            if len(self.avoided) > len(top):
                rest = "、".join(x["tag"] for x in self.avoided[len(top):])
                L.append("  （另有 %d 个方向也符合这个特征：%s。"
                         "样本小的时候这些不太可靠，**先看上面几个**）"
                         % (len(self.avoided) - len(top), rest[:220]))
            L.append("")

        if self.execution:
            e = self.execution
            L.append("## 上一版方案的执行情况（**最重要的一段**）")
            L.append("  这是他自己打的卡，不是猜的。**排计划时必须先看这个。**")
            L.append("  最近 %d 天：完成 %d 天，做了一半 %d 天，没做 %d 天。"
                     % (e.get("days", 0), e.get("done", 0),
                        e.get("partial", 0), e.get("skipped", 0)))
            if e.get("rate") is not None:
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
            L.append("## 候选题目（**只能从这里挑，题号不得编造**）")
            for c in self.candidates[:40]:
                L.append("  %s  %s  难度 %s(%s)%s"
                         % (c["problem_key"], (c.get("title") or "")[:44],
                            c.get("difficulty"), c.get("difficulty_source"),
                            ("  标签 " + "/".join(c["tags"])) if c.get("tags") else ""))
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
    try:
        return int(b.split("-")[0])
    except (ValueError, IndexError):
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
                bands[_band(d)] += 1
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
            prob_tags[key] = info.get("tags")
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

            for tag, diffs in bank_tag_diff.items():
                if len(diffs) < 20:        # 题库里这个 tag 题太少，不判
                    continue
                med = _median(diffs)
                if med is None:
                    continue
                count = solved_by_tag.get(tag, 0)
                share = count / total_solved
                gap = med - overall_med
                if gap >= AVOID_GAP and share < AVOID_SHARE:
                    s.avoided.append({
                        "tag": tag, "count": count, "share": share,
                        "bank_median": med, "overall_median": overall_med,
                        "gap": gap, "bank_count": len(diffs),
                    })
            s.avoided.sort(key=lambda a: -a["gap"])
            if s.avoided:
                s.notes.append(
                    "「疑似难度回避」只是一个**假设**，不是结论 —— "
                    "也可能是不感兴趣、或者没有合适的题源。请结合用户自己的说法判断。")

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
    # tag 命中多的优先，其次难度靠近区间中段
    out.sort(key=lambda c: (-c["_hit"], c["_dist"]))
    for c in out:
        c.pop("_hit", None)
        c.pop("_dist", None)
    return out[:limit]
