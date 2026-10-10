#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""训练块的"选哪个 / 做到哪了"—— 纯逻辑，不碰数据库。

为什么要和 `Store` 分开：这两个问题都是**对数据的查询**，
不是对库的读写。分开之后能拿一份假数据直接测，不用起 asyncio。

背景见 `core/curriculum.py`。一句话：一道题改变不了任何东西，
一个子专题要吃 15-20 道，所以"今天练什么"不该每天重算，
而应该是**一段连续的日子里只吃一个子专题**。
"""

from __future__ import annotations

from datetime import datetime

from . import curriculum as cur
from . import log as logm


def _f(row, name: str, default=None):
    """从 dict 或 `sqlite3.Row` 里取一个字段。

    `Store.list_submissions` 返回的是 `sqlite3.Row`，而测试里传的是 dict ——
    **`.get()` 只有 dict 有**，直接写 `row.get(...)` 会在真机上炸
    （测试反而过，因为它们喂的是 dict）。`summary._field` 早就在处理这件事。
    """
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        # 既不是 dict 也不能下标 —— 那是个 dataclass（`platforms.base.Submission`）。
        # 真机走的是 sqlite3.Row，但测试和探针常常直接喂 dataclass。
        return getattr(row, name, default)


def parse_stamp(text: str):
    """把 `logm.stamp()` 的 "2026-10-08 15:25:17" 解析成 epoch。

    解析不了就返回 `None` —— **不要返回 0**：0 是 1970 年，
    拿去当"从这个时刻之后开始算"会让进度把所有历史提交都算进来，
    也就是把"这个块做了 15 道"直接判成已完成。
    """
    text = str(text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=logm.CN_TZ).timestamp())
        except ValueError:
            continue
    return None


def _tags_of(bank: dict, platform: str, key: str):
    info = ((bank or {}).get(platform) or {}).get(key)
    return (info or {}).get("tags")


def hit(topic, bank: dict, platform: str, key: str) -> bool:
    """这道题算不算这个子专题。"""
    return cur.matches(topic, platform, _tags_of(bank, platform, key))


def coverage(subs: list, bank: dict) -> dict:
    """每个子专题他**已经 AC 过多少道**。

    这张表有三个用途：
      · 自动选块时跳过"早就会了"的子专题（否则会给他排一个
        「并查集入门」的块，而他并查集已经 AC 过 40 道）；
      · 用户问"我这一块算做完了吗"时给个客观数字；
      · `/xcpc 块` 列出阶梯时标出哪些已经熟了。

    ⚠️ 这里数的是**题目数**，不是提交数 —— 同一道题错三次再 AC
    只算一道。"练过 15 道"和"提交过 15 次"不是一回事。
    """
    solved = {}
    for r in subs or []:
        v = str(_f(r, "verdict") or "").upper()
        if v not in ("OK", "AC", "ACCEPTED"):
            continue
        platform = str(_f(r, "platform") or "")
        key = str(_f(r, "problem_key") or "")
        if platform and key:
            solved[(platform, key)] = None
    out = {}
    for t in cur.ALL_TOPICS:
        n = 0
        for (platform, key) in solved:
            if hit(t, bank, platform, key):
                n += 1
        out[t.key] = n
    return out


def progress(subs: list, bank: dict, block, *, recent: int = 5) -> dict:
    """当前块做到哪了。

    **进度不存库，每次从 `submissions` 算** —— 存一个计数器就要处理
    "重复 AC 同题""补题算不算""跨平台"，而这些都是查询能回答的问题；
    存下来只会多一个会漂移的副本（v0.5.21 的教训）。

    `started_at` 之前的 AC **不算**。这是刻意的：块的语义是
    "从这个子专题开始认真练起，你吃了多少道"，
    把开块之前零星做过的算进来，进度条一开块就是 8/15，
    那这个块就白开了。
    """
    block = dict(block or {})
    topic = cur.BY_KEY.get(str(block.get("topic") or ""))
    t0 = parse_stamp(block.get("started_at"))
    target = int(block.get("target") or 0)
    done: list = []
    before = 0
    for r in subs or []:
        v = str(_f(r, "verdict") or "").upper()
        if v not in ("OK", "AC", "ACCEPTED"):
            continue
        platform = str(_f(r, "platform") or "")
        key = str(_f(r, "problem_key") or "")
        if not topic or not hit(topic, bank, platform, key):
            continue
        ep = _f(r, "epoch")
        try:
            ep = int(ep) if ep is not None else None
        except (TypeError, ValueError):
            ep = None
        if t0 is not None and ep is not None and ep < t0:
            before += 1
            continue
        done.append({
            "problem_key": key, "platform": platform, "epoch": ep or 0,
            "title": ((bank or {}).get(platform) or {}).get(key, {}).get("title") or "",
        })
    # 同一道题可能 AC 多次（换语言重交），去重后才是"做了几道"
    uniq = {}
    for d in done:
        uniq[d["problem_key"]] = d
    items = sorted(uniq.values(), key=lambda d: -int(d["epoch"] or 0))
    return {
        "done": len(items),
        "target": target or (topic.count if topic else 0),
        "before": before,
        "recent": items[:max(0, int(recent))],
        "finished": bool(target) and len(items) >= target,
    }


def needed(topic_key: str, coverage_map: dict, *, skip_ratio: float = 0.5) -> bool:
    """这个子专题**还值不值得开一个块**。

    判据是"他已经在里面 AC 过多少道"：超过 `count * skip_ratio`
    就不值得当专题练了（那已经是他会的东西）。**这是唯一会跳过阶梯的规则** ——
    除此之外一律按顺序走，因为按顺序走正是"有针对性"的前提。
    """
    t = cur.BY_KEY.get(topic_key)
    if t is None:
        return False
    have = int(coverage_map.get(topic_key, 0) or 0)
    return have < max(5, int(t.count * skip_ratio))


def pick_module(subs: list, bank: dict, coverage_map: dict) -> str:
    """先练哪个模块。

    **dp 优先**：它是图论很多算法的前置（DAG 上 DP、树形 DP、最短路计数），
    而且他的 dp 样本最少（9 题）—— 不是弱，是没做过。
    两个模块都被覆盖完了才轮到 graph 之外的情况，这里直接返回 dp。
    """
    for mod in ("dp", "graph"):
        for key in cur.topic_order(mod):
            if needed(key, coverage_map):
                return mod
    return "dp"


def choose(module: str, coverage_map: dict) -> str:
    """在这个模块的阶梯上找**第一个还值得开的**子专题。

    找不到（整个模块都熟了）就返回阶梯最后一个 —— 那时最合理的事
    是在最难的子专题上继续加量，而不是回到"没有块"的状态。
    """
    order = cur.topic_order(module)
    for key in order:
        if needed(key, coverage_map):
            return key
    return order[-1] if order else ""


def reason(topic_key: str, coverage_map: dict) -> str:
    """给用户看的一句话：为什么是它。**必须说人话，且不许编数据。**"""
    t = cur.BY_KEY.get(topic_key)
    if t is None:
        return ""
    have = int(coverage_map.get(topic_key, 0) or 0)
    if have:
        return "%s：你已经 AC 过 %d 道，但离「练熟」还差一截（这一块按 %d 道算）。" % (
            t.name, have, t.count)
    return "%s：这一块你**一道都没做过**（题库里带这个标签的题）—— 所以先别谈弱不弱，先做上 %d 道。" % (
        t.name, t.count)


def next_block(module: str, coverage_map: dict, *, after: str = "") -> tuple:
    """阶梯上的下一个子专题 `(module, topic_key)`。

    当前块做完了（`progress.finished`）就调这个。
    **做完不等于立刻换** —— 换不换由调用方决定，
    因为"15 道"本来就是个估计值，到点了但手感还生疏就该再做几道。

    `after` 是"从哪一个往后退"。**不传的话从阶梯开头算** ——
    那适合"第一次开块"，不适合"换一个"：
    用户手动点名开在「背包」上，再按"下一个"，从阶梯开头算会退回到
    背包自己（第一个 needed 是线性 DP，它的下一个正是背包）。
    真机上就是这么发现这个参数的必要的。
    """
    key = after if after in cur.BY_KEY else choose(module, coverage_map)
    nxt = cur.next_after(key)
    if nxt:
        return module, nxt
    # 这个模块走到头了，换另一个模块
    other = "graph" if module == "dp" else "dp"
    alt = choose(other, coverage_map)
    return other, alt


def describe(block, coverage_map: dict, prog: dict) -> str:
    """给 `/xcpc 块` 和方案汇总用的一小段文字。"""
    block = dict(block or {})
    topic = cur.BY_KEY.get(str(block.get("topic") or ""))
    if topic is None:
        return ""
    lines = ["【当前训练块】%s · %s" % (cur.MODULES[topic.module]["name"], topic.name)]
    if topic.idea:
        lines.append("  核心：%s" % topic.idea)
    lines.append("  进度：%d / %d 道（难度带 %d-%d）"
                 % (prog.get("done", 0), prog.get("target", 0),
                    block.get("band_lo", topic.lo), block.get("band_hi", topic.hi)))
    if prog.get("before"):
        lines.append("  （开这个块之前你在这个方向上做过 %d 道，没算进进度 —— "
                     "这一块算的是「开块之后」的量。）" % prog["before"])
    if prog.get("finished"):
        lines.append("  这个块**已经吃够了**，可以往下走了（`/xcpc 块 下一个`）。")
    return "\n".join(lines)
