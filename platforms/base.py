#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台适配层的共同约定。

两条贯穿全层的规则
------------------
1. **难度不跨平台换算。**
   CF 是 rating（800–3500），AtCoder 的 AtCoder Problems 给的是 IRT 估计值
   （可以是负数，尺度完全不同），洛谷是 1–7 档。
   这三套**不可直接比较**，硬换算会静默污染所有统计。
   所以每条记录都带 `difficulty_source`，说明这个数字是哪来的；
   跨平台比较时比"分数带"，不比数值。

2. **拿不到就说拿不到。**
   AtCoder 的 API 里**没有 tag**（`problems.json` 只有标题，
   `problem-models.json` 只有难度）。那就 `tags=None`，**不编**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# 难度来源，写在每条记录里。
# 有这些值：cf_rating / atcoder_irt / luogu_level / qoj_none
DIFF_SOURCES = ("cf_rating", "atcoder_irt", "luogu_level", "qoj_none", "unknown")


@dataclass
class Submission:
    """一次练习提交。"""
    platform: str
    submission_id: str
    problem_key: str            # 形如 "CF:1234D" / "ATC:abc380_d"
    verdict: str = ""           # OK / WRONG_ANSWER / ...
    epoch: int = 0
    language: str = ""
    difficulty: int | None = None
    difficulty_source: str = "unknown"

    @property
    def accepted(self) -> bool:
        return self.verdict.upper() in ("OK", "AC", "ACCEPTED")


@dataclass
class ContestRecord:
    """一次比赛记录 —— **和练习提交是两条独立的流**。

    字段、去重键、用途都不同：练习提交看"刷了多少题"，
    比赛记录看"在压力下的表现"。混在一起两边都不准。
    """
    platform: str
    contest_id: str
    name: str = ""
    start_epoch: int = 0
    rank: int | None = None
    solved: int | None = None
    rating_delta: int | None = None
    problems: list[dict] = field(default_factory=list)
    kind: str = "rated"          # rated / gym / vp / practice


@dataclass
class Problem:
    """题库标注。`difficulty` 是**原生值**，`difficulty_source` 说明尺度。"""
    platform: str
    problem_key: str
    title: str = ""
    tags: list[str] | None = None      # None = 这个平台给不出，**不是空列表**
    difficulty: int | None = None
    difficulty_source: str = "unknown"


@dataclass
class Fetched:
    """一次 fetch 的结果。带上失败分类，便于上层记日志和回报。"""
    items: list = field(default_factory=list)
    ok: bool = True
    error_kind: str = ""
    detail: str = ""
    # 增量拉取用的游标（下一次从哪继续）
    cursor: Any = None
    truncated: bool = False     # 是否因为分页/上限被截断


class Platform(Protocol):
    """适配器协议。

    `supports_submissions=False` 的平台（如 QOJ 未登录时）不该被当成"零提交"，
    上层要能区分"没有"和"拿不到"。
    """
    name: str
    supports_submissions: bool
    supports_contests: bool
    supports_problems: bool

    async def fetch_submissions(self, handle: str, since_epoch: int | None = None,
                                client: Any = None) -> Fetched: ...

    async def fetch_contests(self, handle: str,
                             client: Any = None) -> Fetched: ...

    async def fetch_problems(self, client: Any = None) -> Fetched: ...
