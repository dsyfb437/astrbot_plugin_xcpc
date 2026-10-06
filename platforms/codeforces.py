#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codeforces 适配器。

用的是**官方公开 API**（`codeforces.com/api/*`），不需要登录、不需要 key。
实测（2026-10）：
    user.status?handle=<handle>        -> 200
    user.info?handles=<handle>         -> 200
    user.rating?handle=<handle>        -> 200
    problemset.problems                 -> 200，2.27 MB，11425 题 / 38 个 tag
    contest.list?gym=false              -> 200，2154 场

API 的坑
--------
1. **HTTP 200 也可能失败** —— 错误在 body 的 `status` 字段里
   （`{"status":"FAILED","comment":"handle not found"}`）。
   只看状态码会把"用户不存在"当成"这次没提交"，那是最危险的一类静默错误。
2. `user.status` 分页靠 `from`/`count`，单次上限 10000。
3. 限速大约 1 req/s。
"""

from __future__ import annotations

from .base import ContestRecord, Fetched, Problem, Submission

BASE = "https://codeforces.com/api"

# 单页上限（官方限制 10000，这里保守取 5000，减少单次响应体积）
PAGE = 5000
MAX_PAGES = 20


class Codeforces:
    name = "codeforces"
    supports_submissions = True
    supports_contests = True
    supports_problems = True
    min_interval = 1.0          # 官方限速约 1 req/s

    @staticmethod
    def problem_key(contest_id, index: str) -> str:
        return "CF:%s%s" % (contest_id, index)

    # ---- 内部：把 CF 的 "200 但 FAILED" 也当成失败 ---------------------
    @staticmethod
    def _unwrap(payload, what: str, status: int | None = None) -> Fetched:
        """把 CF 的响应归一成 Fetched。

        ⚠️ **CF 的错误有两种形式**，只处理一种就会漏：
          ① HTTP 200，body 里 `{"status":"FAILED","comment":"..."}`
          ② **HTTP 400**，body 同样是那个 JSON
        我第一版只处理了 ①，结果用不存在的 handle 时会先被 `get_json`
        按"HTTP 400"抛成「解析失败」—— 分类错了，用户看到的是"对面改版了"
        而不是"你这个 handle 不对"。

        实测：拿一个不存在的 handle 调 `user.status` → HTTP 400。
        """
        if not isinstance(payload, dict):
            return Fetched(ok=False, error_kind="解析失败",
                           detail="%s 返回的不是对象（HTTP %s）" % (what, status))
        st = str(payload.get("status") or "")
        if st != "OK":
            comment = str(payload.get("comment") or "未知原因")
            low = comment.lower()
            if "not found" in low or "handle" in low:
                kind = "凭据失效"
            elif "limit" in low or "too many" in low:
                kind = "限流"
            else:
                kind = "解析失败"
            return Fetched(ok=False, error_kind=kind,
                           detail="CF API 拒绝（%s，HTTP %s）：%s"
                                  % (what, status, comment))
        return Fetched(ok=True, items=payload.get("result") or [])

    async def _get_json_lenient(self, client, url: str, what: str):
        """发请求并**把 4xx 的 body 也读出来**。

        CF 的错误信息在 body 里（哪怕状态码是 400），
        所以不能直接用 `get_json`（它遇到非 2xx 就抛，body 就丢了）。
        返回 `(payload_or_None, fetched_or_None)`。
        """
        try:
            resp = await client.get(url)
        except Exception as exc:
            return None, Fetched(ok=False,
                                 error_kind=getattr(exc, "kind", "网络不可达"),
                                 detail=str(exc))
        if resp.status == 429:
            return None, Fetched(ok=False, error_kind="限流",
                                 detail="CF 限流了（HTTP 429），等几分钟再试")
        if resp.status >= 500:
            return None, Fetched(ok=False, error_kind="网络不可达",
                                 detail="CF 服务器错误（HTTP %d）" % resp.status)
        try:
            payload = resp.json()
        except (ValueError, UnicodeDecodeError):
            return None, Fetched(ok=False, error_kind="页面结构变化",
                                 detail="%s 返回的不是 JSON（HTTP %d）"
                                        % (what, resp.status))
        return payload, None

    # ---- 提交 -----------------------------------------------------------
    async def fetch_submissions(self, handle: str, since_epoch: int | None = None,
                                client=None) -> Fetched:
        if not handle:
            return Fetched(ok=False, error_kind="凭据失效", detail="没填 handle")

        out: list[Submission] = []
        newest_epoch = 0
        offset = 1
        truncated = False

        for _page in range(MAX_PAGES):
            url = ("%s/user.status?handle=%s&from=%d&count=%d"
                   % (BASE, handle, offset, PAGE))
            payload, fail = await self._get_json_lenient(client, url, "user.status")
            if fail is not None:
                return fail
            got = self._unwrap(payload, "user.status")
            if not got.ok:
                return got

            rows = got.items
            if not rows:
                break

            stop = False
            for row in rows:
                epoch = int(row.get("creationTimeSeconds") or 0)
                newest_epoch = max(newest_epoch, epoch)
                if since_epoch and epoch <= since_epoch:
                    # 结果按时间倒序，遇到旧的就可以停
                    stop = True
                    continue
                prob = row.get("problem") or {}
                cid = prob.get("contestId")
                idx = prob.get("index") or ""
                if cid is None or not idx:
                    # 有些题目没有 contestId（比如 gym 的某些），跳过而不是造一个假 key
                    continue
                rating = prob.get("rating")
                out.append(Submission(
                    platform=self.name,
                    submission_id=str(row.get("id") or ""),
                    problem_key=self.problem_key(cid, idx),
                    verdict=str(row.get("verdict") or ""),
                    epoch=epoch,
                    language=str((row.get("programmingLanguage") or ""))[:60],
                    difficulty=rating,
                    difficulty_source="cf_rating" if rating is not None else "unknown",
                ))
            if stop:
                break
            if len(rows) < PAGE:
                break
            offset += PAGE
        else:
            truncated = True

        return Fetched(items=out, ok=True, cursor=newest_epoch, truncated=truncated)

    # ---- 比赛记录 -------------------------------------------------------
    async def fetch_contests(self, handle: str, client=None) -> Fetched:
        if not handle:
            return Fetched(ok=False, error_kind="凭据失效", detail="没填 handle")
        payload, fail = await self._get_json_lenient(
            client, "%s/user.rating?handle=%s" % (BASE, handle), "user.rating")
        if fail is not None:
            return fail
        got = self._unwrap(payload, "user.rating")
        if not got.ok:
            return got

        out = []
        for row in got.items:
            # ⚠️ **CF 的 user.rating 用的是扁平字段**：`contestId` / `contestName`，
            # **不是**嵌套的 `contest: {id, name}`。
            #
            # 我第一版按嵌套解析，结果 19 条记录全是空壳（id='' name='' start=0），
            # 落库时被"没有 contest_id 就跳过"全部丢弃 —— 而**表面看起来一切正常**
            # （fetch 返回 ok=True、19 条）。这是最危险的一类 bug：数量对、状态对、内容全空。
            #
            # 两种形式都读，是因为这个 API 历史上两种都用过。
            nested = row.get("contest") if isinstance(row.get("contest"), dict) else {}
            cid = row.get("contestId")
            if cid is None:
                cid = nested.get("id")
            name = row.get("contestName") or nested.get("name") or ""
            start = row.get("ratingUpdateTimeSeconds")
            if start is None:
                start = nested.get("startTimeSeconds") or 0
            old = row.get("oldRating")
            new = row.get("newRating")
            out.append(ContestRecord(
                platform=self.name,
                contest_id=str(cid or ""),
                name=str(name or ""),
                start_epoch=int(start or 0),
                rank=row.get("rank"),
                rating_delta=(new - old) if (new is not None and old is not None) else None,
                kind="rated",
            ))
        # 全解析不出 contest_id 说明结构变了 —— **明确报错，不要返回一堆空壳**
        # （空壳会在落库时被静默丢弃，变成"同步成功但零条比赛"）
        if out and not any(c.contest_id for c in out):
            return Fetched(ok=False, error_kind="页面结构变化",
                           detail="user.rating 返回了 %d 条，但解析不出 contestId "
                                  "（CF 可能改了字段名）" % len(out))
        return Fetched(items=out, ok=True)

    # ---- 题库标注 -------------------------------------------------------
    async def fetch_problems(self, client=None) -> Fetched:
        try:
            payload = await client.get_json("%s/problemset.problems" % BASE)
        except Exception as exc:
            return Fetched(ok=False, error_kind=getattr(exc, "kind", "网络不可达"),
                           detail=str(exc))
        if not isinstance(payload, dict) or payload.get("status") != "OK":
            return Fetched(ok=False, error_kind="解析失败",
                           detail="problemset.problems 返回异常")
        result = payload.get("result") or {}
        rows = result.get("problems") or []
        out = []
        for p in rows:
            cid = p.get("contestId")
            idx = p.get("index") or ""
            if cid is None or not idx:
                continue
            rating = p.get("rating")
            out.append(Problem(
                platform=self.name,
                problem_key=self.problem_key(cid, idx),
                title=str(p.get("name") or ""),
                tags=list(p.get("tags") or []),
                difficulty=rating,
                difficulty_source="cf_rating" if rating is not None else "unknown",
            ))
        return Fetched(items=out, ok=True)
