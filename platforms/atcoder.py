#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AtCoder 适配器 —— 走 **AtCoder Problems（kenkoooo）的公开 API**。

为什么不用官方接口
------------------
AtCoder 官方的提交页和榜单页**都要登录**：
    /contests/abc380/submissions -> 302 到 /login?continue=...
    /contests/abc380/standings   -> 200 但正文是 <title>Sign In - AtCoder</title>

而 AtCoder Problems 是社区维护的公开服务，免登录、有结构化 JSON。实测：
    atcoder-api/v3/user/submissions?user=X&from_second=T -> 200，字段
        id / epoch_second / problem_id / contest_id / result / language / point / length
    resources/problems.json        -> 200，9668 条（contest_id / id / name / problem_index / title）
    resources/problem-models.json  -> 200，5163 条，含 IRT `difficulty`（4877 条有值）

⚠️ **这是第三方依赖**，不是官方。所以：
  * 它挂了只能影响 AtCoder 一个平台，不能让别的平台跟着失败
  * 难度是 **IRT 估计值**，和 CF rating **不是一套尺子**，所以标 `atcoder_irt`
  * API 里**没有 tag** —— `tags=None`（表示"这个平台给不出"），**不是空列表**
"""

from __future__ import annotations

from .base import ContestRecord, Fetched, Problem, Submission

API = "https://kenkoooo.com/atcoder/atcoder-api"
RES = "https://kenkoooo.com/atcoder/resources"

# 一次多拉一点：这个接口按 from_second 增量，返回该时间之后的全部
PAGE_HINT = 500


class AtCoder:
    name = "atcoder"
    supports_submissions = True
    supports_contests = True
    supports_problems = True
    min_interval = 1.0          # 社区服务，别打太狠

    @staticmethod
    def problem_key(problem_id: str) -> str:
        return "ATC:%s" % problem_id

    # ---- 提交 -----------------------------------------------------------
    async def fetch_submissions(self, handle: str, since_epoch: int | None = None,
                                client=None) -> Fetched:
        if not handle:
            return Fetched(ok=False, error_kind="凭据失效", detail="没填 handle")

        since = int(since_epoch or 0)
        url = "%s/v3/user/submissions?user=%s&from_second=%d" % (API, handle, since)
        try:
            rows = await client.get_json(url)
        except Exception as exc:
            return Fetched(ok=False, error_kind=getattr(exc, "kind", "网络不可达"),
                           detail=str(exc))

        if not isinstance(rows, list):
            # 这个服务在用户不存在时也回 200，但给的是空数组或对象 —— 区分开
            return Fetched(ok=False, error_kind="解析失败",
                           detail="AtCoder Problems 返回的不是数组（handle 可能不对）")

        out = []
        newest = since
        for row in rows:
            pid = str(row.get("problem_id") or "")
            if not pid:
                continue
            epoch = int(row.get("epoch_second") or 0)
            newest = max(newest, epoch)
            out.append(Submission(
                platform=self.name,
                submission_id=str(row.get("id") or ""),
                problem_key=self.problem_key(pid),
                verdict=str(row.get("result") or ""),
                epoch=epoch,
                language=str(row.get("language") or "")[:60],
                # 难度要从 problems 表补齐 —— 提交接口不给难度
                difficulty=None,
                difficulty_source="atcoder_irt",
            ))

        # 返回条数正好等于提示值时，可能还有更多，如实标记
        truncated = len(out) >= PAGE_HINT
        return Fetched(items=out, ok=True, cursor=newest, truncated=truncated)

    # ---- 比赛记录 -------------------------------------------------------
    #
    # **官方有个 rating 历史接口，给全了**（实测 2026-10）：
    #       GET https://atcoder.jp/users/<user>/history/json
    #       -> [{"IsRated":true, "Place":59, "OldRating":0, "NewRating":1255,
    #            "Performance":2455,
    #            "ContestName":"AtCoder Regular Contest 061",
    #            "EndTime":"2016-09-11T22:40:00+09:00"}, ...]
    #
    # 我前几版是**从提交记录反推**比赛的（因为以为官方要登录）。
    # 反推的代价：没有排名、没有 rating 变化、标题只有 contest_id、
    # 时间用首次提交时刻近似 —— 而"压力下的表现"恰恰要看排名和 rating。
    #
    # AtCoder Problems 那边**没有** rated 历史端点（`v3/user/rated` /
    # `v3/user/info` / `v3/user/ac` 等十几个路径全试过，都是 404）。
    # 所以这个走官方。
    HISTORY_URL = "https://atcoder.jp/users/%s/history/json"
    USER_URL = "https://atcoder.jp/users/%s"

    @staticmethod
    def parse_history(rows) -> list:
        """把官方 history/json 解析成 ContestRecord。

        分开成静态方法是为了**能离线测**。
        """
        from datetime import datetime
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            # ContestScreenName 形如 "arc061.contest.atcoder.jp"，取第一段当 id
            screen = str(row.get("ContestScreenName") or "")
            cid = screen.split(".", 1)[0] if screen else ""
            name = str(row.get("ContestName") or "") or cid
            if not cid and not name:
                continue

            # EndTime 是带 +09:00 的 ISO8601（AtCoder 用日本时间）
            epoch = 0
            raw_time = str(row.get("EndTime") or "")
            if raw_time:
                try:
                    dt = datetime.fromisoformat(raw_time)
                    epoch = int(dt.timestamp())
                except (ValueError, TypeError):
                    epoch = 0

            old = row.get("OldRating")
            new = row.get("NewRating")
            try:
                delta = int(new) - int(old) if (old is not None and new is not None) else None
            except (TypeError, ValueError):
                delta = None
            try:
                place = int(row.get("Place")) if row.get("Place") is not None else None
            except (TypeError, ValueError):
                place = None

            out.append(ContestRecord(
                platform="atcoder",
                contest_id=cid or name,
                name=name,
                start_epoch=epoch,
                rank=place,
                rating_delta=delta,
                kind="rated" if row.get("IsRated") else "unrated",
            ))
        return out

    async def handle_exists(self, handle: str, client) -> bool:
        """查这个人存不存在。

        **为什么需要**：history/json 对不存在的用户返回的是 `[]`（HTTP 200），
        和"这个人没打过 rated 比赛"**完全一样**。
        不区分的话，handle 填错会显示成"你没打过比赛" ——
        而真相是"你填错了"。这正是我们一直在防的那类静默错误。
        """
        try:
            resp = await client.get(self.USER_URL % handle)
        except Exception:
            return True          # 查不了就不阻断（宁可不报错，也别误报）
        return resp.status == 200

    async def fetch_contests(self, handle: str, client=None) -> Fetched:
        if not handle:
            return Fetched(ok=False, error_kind="凭据失效", detail="没填 handle")
        try:
            resp = await client.get(self.HISTORY_URL % handle)
        except Exception as exc:
            return Fetched(ok=False, error_kind=getattr(exc, "kind", "网络不可达"),
                           detail=str(exc))
        if resp.status == 404:
            return Fetched(ok=False, error_kind="凭据失效",
                           detail="AtCoder 上没有这个用户：%s" % handle)
        if not resp:
            return Fetched(ok=False, error_kind="网络不可达",
                           detail="history/json 返回 HTTP %d" % resp.status)
        try:
            rows = resp.json()
        except (ValueError, UnicodeDecodeError):
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="history/json 返回的不是 JSON")
        if not isinstance(rows, list):
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="history/json 不是数组（是 %s）"
                                  % type(rows).__name__)

        if not rows:
            # `[]` 有两种完全不同的原因，**必须分清**
            if not await self.handle_exists(handle, client):
                return Fetched(ok=False, error_kind="凭据失效",
                               detail="AtCoder 上没有这个用户：%s" % handle)
            return Fetched(items=[], ok=True)      # 确实没打过比赛

        items = self.parse_history(rows)
        if rows and not items:
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="history/json 有 %d 条但一条都解析不出来"
                                  "（AtCoder 可能改了字段名）" % len(rows))
        return Fetched(items=items, ok=True)

    # ---- 题库标注 -------------------------------------------------------
    async def fetch_problems(self, client=None) -> Fetched:
        """标题来自 problems.json，难度来自 problem-models.json，两者按 id 合并。

        **tags 一律 None** —— 这两个文件里都没有标签。编不出来。
        """
        try:
            titles = await client.get_json("%s/problems.json" % RES)
            models = await client.get_json("%s/problem-models.json" % RES)
        except Exception as exc:
            return Fetched(ok=False, error_kind=getattr(exc, "kind", "网络不可达"),
                           detail=str(exc))

        if not isinstance(titles, list):
            return Fetched(ok=False, error_kind="解析失败",
                           detail="problems.json 不是数组")
        if not isinstance(models, dict):
            models = {}

        out = []
        for p in titles:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            model = models.get(pid) or {}
            diff = model.get("difficulty") if isinstance(model, dict) else None
            try:
                diff = int(diff) if diff is not None else None
            except (TypeError, ValueError):
                diff = None
            out.append(Problem(
                platform=self.name,
                problem_key=self.problem_key(pid),
                title=str(p.get("title") or p.get("name") or ""),
                tags=None,                       # 这个平台给不出标签
                difficulty=diff,
                difficulty_source="atcoder_irt" if diff is not None else "unknown",
            ))
        return Fetched(items=out, ok=True)
