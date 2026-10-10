#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据访问层 —— 多用户隔离的最后一道闸。

设计上的一个硬约束
------------------
**每个函数的第一个参数都是 `user_id`，没有任何"不带 user_id 的读取接口"。**

这不是风格问题。多用户系统最危险的 bug 是"忘了加 where user_id"，
而那种 bug 在测试里往往看不出来（单人测试时数据本来就只有一份）。
把 `user_id` 做成必填首参，漏掉的写法在**语法上就不成立**，
比靠人记得写 where 可靠得多。

难度与标签
----------
`submissions` 里存的是**原生难度 + difficulty_source**，
不做跨平台换算（CF rating / AtCoder IRT / 洛谷 1-7 是三套尺子）。
要跨平台比的时候比"分数带"，见 `bucket()`。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from . import db as dbm

# 合法的平台名。写错的平台名会被拒绝，而不是静静地存进去查不出来。
PLATFORMS = ("codeforces", "atcoder", "qoj", "luogu")

# 不用登录、没有凭据的平台：它们的 credentials 表里**永远不会有行**，
# 绑定成功的唯一标志就是"users 表里有 handle"。判断绑定状态时必须认这一点，
# 否则界面上永远是"未绑定"（v0.5.1 的真机反馈）。
# tests/test_accounts.py 里有一条用例盯着它和 authenticator 的类型别走散。
HANDLE_ONLY = ("codeforces", "atcoder")

# 原生难度的"分数带"宽度。跨平台比较时用它，而不是直接比数值。
BUCKET_SIZE = 200


def bucket(difficulty: int | None, source: str = "") -> str:
    """把原生难度归到分数带。**跨平台比较只用这个，不比数值。**

    不同来源的带子**不通用** —— 所以带子里带上来源前缀，
    免得有人把 `cf:1600-1799` 和 `atcoder_irt:1600-1799` 当成一回事。
    """
    if difficulty is None:
        return ""
    lo = (int(difficulty) // BUCKET_SIZE) * BUCKET_SIZE
    return "%s:%d-%d" % (source or "unknown", lo, lo + BUCKET_SIZE - 1)


def _check_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    if p not in PLATFORMS:
        raise ValueError("不认识的平台：%r（只支持 %s）" % (platform, "、".join(PLATFORMS)))
    return p


def _require_user(user_id: str) -> str:
    """空 `user_id` **必须报错，不能静默放行**。

    这是自测抓出来的：最初 `count_submissions("")` 会老老实实执行
    `WHERE user_id=''`，返回 0 —— 也就是把"调用方忘了传 user_id"
    伪装成"这个人没有数据"。**在多人系统里这是最危险的一类错误**：
    它不报错、不崩溃，只是安静地给一个看起来正常的错答案。

    所以每个公开方法都先过这道闸。
    """
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError(
            "user_id 不能为空 —— 这是多用户隔离的关键，"
            "空值会把「忘了传」伪装成「没有数据」。调用方必须显式传 QQ 号。")
    return uid


class Store:
    """基于 `Database` 的数据访问。

    所有写操作都是**幂等**的（`INSERT ... ON CONFLICT ... DO UPDATE`），
    所以重复同步不会产生重复数据。
    """

    def __init__(self, db: dbm.Database, now_fn=None) -> None:
        self.db = db
        self._now = now_fn or _default_now

    # ==================================================================
    # 用户与账号绑定
    # ==================================================================
    async def ensure_user(self, user_id: str) -> None:
        """确保 users 里有这一行。第一次见到某个 QQ 号时调用。"""
        if not user_id:
            raise ValueError("user_id 不能为空")
        now = self._now()
        await self.db.execute(
            "INSERT INTO users (user_id, created_at, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO NOTHING", (user_id, now, now))

    async def get_user(self, user_id: str):
        user_id = _require_user(user_id)
        return await self.db.query_one("SELECT * FROM users WHERE user_id=?", (user_id,))

    _HANDLE_COL = {
        "codeforces": "cf_handle",
        "atcoder": "atcoder_handle",
        "luogu": "luogu_uid",
        "qoj": "qoj_uid",
    }

    async def set_handle(self, user_id: str, platform: str, handle: str) -> None:
        """绑定/更新某平台的 handle。**只影响这个 user_id。**"""
        user_id = _require_user(user_id)
        col = self._HANDLE_COL[_check_platform(platform)]
        await self.ensure_user(user_id)
        await self.db.execute(
            "UPDATE users SET %s=?, updated_at=? WHERE user_id=?" % col,
            ((handle or "").strip(), self._now(), user_id))

    async def get_handle(self, user_id: str, platform: str) -> str:
        user_id = _require_user(user_id)
        col = self._HANDLE_COL[_check_platform(platform)]
        row = await self.db.query_one(
            "SELECT %s AS h FROM users WHERE user_id=?" % col, (user_id,))
        return (row["h"] or "") if row else ""

    async def handles(self, user_id: str) -> dict[str, str]:
        user_id = _require_user(user_id)
        row = await self.get_user(user_id)
        if not row:
            return {p: "" for p in PLATFORMS}
        return {p: (row[c] or "") for p, c in self._HANDLE_COL.items()}

    # ==================================================================
    # 凭据（永不进日志 / 永不进 API 响应）
    # ==================================================================
    async def set_credentials(self, user_id: str, platform: str,
                              cookie_blob: dict | str, status: str = "valid") -> None:
        """存凭据。

        `cookie_blob` 可以是 dict（会被 JSON 序列化）或已经序列化好的字符串。
        **调用方负责不要把这个值写进日志** —— log.py 的掩码是第二道防线。
        """
        user_id = _require_user(user_id)
        p = _check_platform(platform)
        blob = cookie_blob if isinstance(cookie_blob, str) else json.dumps(
            cookie_blob or {}, ensure_ascii=False)
        await self.ensure_user(user_id)
        await self.db.execute(
            "INSERT INTO credentials (user_id, platform, cookie_blob, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, platform) DO UPDATE SET "
            "cookie_blob=excluded.cookie_blob, status=excluded.status, "
            "updated_at=excluded.updated_at",
            (user_id, p, blob, status, self._now()))

    async def get_credentials(self, user_id: str, platform: str) -> dict:
        """**只在需要发请求时调用**。返回值绝不外传。"""
        user_id = _require_user(user_id)
        row = await self.db.query_one(
            "SELECT cookie_blob, status FROM credentials WHERE user_id=? AND platform=?",
            (user_id, _check_platform(platform)))
        if not row:
            return {}
        try:
            return json.loads(row["cookie_blob"] or "{}")
        except (ValueError, TypeError):
            return {}

    async def credential_status(self, user_id: str) -> dict[str, dict]:
        """给界面看的**脱敏**状态 —— 只回状态和时间，不回 blob。"""
        user_id = _require_user(user_id)
        rows = await self.db.query(
            "SELECT platform, status, updated_at FROM credentials WHERE user_id=?",
            (user_id,))
        return {r["platform"]: {"status": r["status"], "updated_at": r["updated_at"]}
                for r in rows}

    async def account_page_payload(self, user_id: str) -> dict:
        """账号绑定页要的全部数据，**一次取完**。

        这个方法的存在是为了让 Web 路由不用自己写 SQL。
        main.py 里出现裸 SQL，就意味着"多用户隔离"的逻辑散到了两个地方 ——
        而散出去的副本迟早会漏掉 user_id。集中在这里，只有一处要维护。

        （我第一版就是在路由里直接写的 SQL，被静态检查拦下来了。）
        """
        user_id = _require_user(user_id)
        handles = await self.handles(user_id)
        creds = await self.credential_status(user_id)
        platforms = []
        for pf in PLATFORMS:
            st = creds.get(pf) or {}
            handle = handles.get(pf) or ""
            status = st.get("status") or ""
            if not status and pf in HANDLE_ONLY:
                # CF / AtCoder 没有凭据行 —— 有 handle 就是绑好了。
                # 之前这里只看 credentials.status，于是这两个平台
                # 明明提示"验证通过"，卡片上却一直是"未绑定"。
                status = "valid" if handle else "unbound"
            platforms.append({
                "platform": pf,
                "handle": handle,
                "status": status or "unbound",
                "updated_at": st.get("updated_at"),
            })
        # 注意：**只回 handle 和状态**，绝不回 cookie / 密码 / token
        return {"user_id": user_id, "platforms": platforms}

    async def clear_credentials(self, user_id: str, platform: str) -> None:
        user_id = _require_user(user_id)
        await self.db.execute(
            "DELETE FROM credentials WHERE user_id=? AND platform=?",
            (user_id, _check_platform(platform)))

    # ==================================================================
    # 提交
    # ==================================================================
    async def upsert_submissions(self, user_id: str, platform: str,
                                 items: Iterable[Any]) -> int:
        """幂等写入。返回**新增**的条数（已存在的会更新，不计入新增）。"""
        user_id = _require_user(user_id)
        p = _check_platform(platform)
        rows = []
        for s in items:
            sid = str(getattr(s, "submission_id", "") or "")
            if not sid:
                continue          # 没有 id 就没法去重，宁可不写
            rows.append((
                user_id, p, sid,
                str(getattr(s, "problem_key", "") or ""),
                str(getattr(s, "verdict", "") or ""),
                int(getattr(s, "epoch", 0) or 0),
                getattr(s, "difficulty", None),
                str(getattr(s, "difficulty_source", "unknown") or "unknown"),
            ))
        if not rows:
            return 0

        before = await self.count_submissions(user_id, p)
        await self.db.executemany(
            "INSERT INTO submissions "
            "(user_id, platform, submission_id, problem_key, verdict, epoch, "
            " difficulty, difficulty_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, platform, submission_id) DO UPDATE SET "
            "problem_key=excluded.problem_key, verdict=excluded.verdict, "
            "epoch=excluded.epoch, "
            "difficulty=COALESCE(excluded.difficulty, submissions.difficulty), "
            "difficulty_source=excluded.difficulty_source",
            rows)
        after = await self.count_submissions(user_id, p)
        return max(0, after - before)

    async def count_submissions(self, user_id: str, platform: str = "") -> int:
        user_id = _require_user(user_id)
        if platform:
            row = await self.db.query_one(
                "SELECT COUNT(*) AS n FROM submissions WHERE user_id=? AND platform=?",
                (user_id, _check_platform(platform)))
        else:
            row = await self.db.query_one(
                "SELECT COUNT(*) AS n FROM submissions WHERE user_id=?", (user_id,))
        return int(row["n"]) if row else 0

    async def list_submissions(self, user_id: str, platform: str = "",
                               limit: int = 200, offset: int = 0) -> list:
        """按时间倒序。**永远带 user_id。**"""
        user_id = _require_user(user_id)
        if platform:
            return await self.db.query(
                "SELECT * FROM submissions WHERE user_id=? AND platform=? "
                "ORDER BY epoch DESC, submission_id DESC LIMIT ? OFFSET ?",
                (user_id, _check_platform(platform), int(limit), int(offset)))
        return await self.db.query(
            "SELECT * FROM submissions WHERE user_id=? "
            "ORDER BY epoch DESC, submission_id DESC LIMIT ? OFFSET ?",
            (user_id, int(limit), int(offset)))

    async def accepted_problems(self, user_id: str, platform: str = "") -> set[str]:
        """去重后的"已 AC 题目"集合。"""
        user_id = _require_user(user_id)
        if platform:
            rows = await self.db.query(
                "SELECT DISTINCT problem_key FROM submissions "
                "WHERE user_id=? AND platform=? AND UPPER(verdict) IN ('OK','AC','ACCEPTED')",
                (user_id, _check_platform(platform)))
        else:
            rows = await self.db.query(
                "SELECT DISTINCT problem_key FROM submissions "
                "WHERE user_id=? AND UPPER(verdict) IN ('OK','AC','ACCEPTED')",
                (user_id,))
        return {r["problem_key"] for r in rows}

    async def platform_stats(self, user_id: str) -> list[dict]:
        """每个平台的提交数 / AC 数 / 最近一次提交时间。给 /xcpc 状态 用。"""
        user_id = _require_user(user_id)
        rows = await self.db.query(
            "SELECT platform, COUNT(*) AS total, "
            "  SUM(CASE WHEN UPPER(verdict) IN ('OK','AC','ACCEPTED') THEN 1 ELSE 0 END) AS ac, "
            "  MAX(epoch) AS last_epoch "
            "FROM submissions WHERE user_id=? GROUP BY platform", (user_id,))
        return [dict(r) for r in rows]

    # ==================================================================
    # 比赛记录（和提交是两条独立的流）
    # ==================================================================
    async def upsert_contests(self, user_id: str, platform: str,
                              items: Iterable[Any]) -> int:
        user_id = _require_user(user_id)
        p = _check_platform(platform)
        rows = []
        for c in items:
            cid = str(getattr(c, "contest_id", "") or "")
            if not cid:
                continue
            rows.append((
                user_id, p, cid,
                str(getattr(c, "name", "") or ""),
                int(getattr(c, "start_epoch", 0) or 0),
                getattr(c, "rank", None),
                getattr(c, "solved", None),
                getattr(c, "rating_delta", None),
                json.dumps(getattr(c, "problems", None) or [], ensure_ascii=False),
            ))
        if not rows:
            return 0
        before = await self._count_table("contests", user_id, p)
        await self.db.executemany(
            "INSERT INTO contests "
            "(user_id, platform, contest_id, name, start_epoch, rank, solved, "
            " rating_delta, problems_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, platform, contest_id) DO UPDATE SET "
            "name=excluded.name, "
            # 已抓到的值不要被后来的 NULL 覆盖 —— AtCoder 反推出来的记录
            # 天然没有 rank / rating_delta，重复同步不能把已有数据抹掉
            "start_epoch=COALESCE(NULLIF(excluded.start_epoch,0), contests.start_epoch), "
            "rank=COALESCE(excluded.rank, contests.rank), "
            "solved=COALESCE(excluded.solved, contests.solved), "
            "rating_delta=COALESCE(excluded.rating_delta, contests.rating_delta), "
            "problems_json=excluded.problems_json",
            rows)
        after = await self._count_table("contests", user_id, p)
        return max(0, after - before)

    async def list_contests(self, user_id: str, platform: str = "",
                            limit: int = 30) -> list:
        user_id = _require_user(user_id)
        if platform:
            return await self.db.query(
                "SELECT * FROM contests WHERE user_id=? AND platform=? "
                "ORDER BY start_epoch DESC LIMIT ?",
                (user_id, _check_platform(platform), int(limit)))
        return await self.db.query(
            "SELECT * FROM contests WHERE user_id=? ORDER BY start_epoch DESC LIMIT ?",
            (user_id, int(limit)))

    async def _count_table(self, table: str, user_id: str, platform: str) -> int:
        if table not in ("contests", "submissions"):
            raise ValueError("表名不允许：%r" % table)
        row = await self.db.query_one(
            "SELECT COUNT(*) AS n FROM %s WHERE user_id=? AND platform=?" % table,
            (user_id, platform))
        return int(row["n"]) if row else 0

    # ==================================================================
    # 题库标注（全局共享，不按用户分）
    # ==================================================================
    async def upsert_problems(self, platform: str, items: Iterable[Any]) -> int:
        p = _check_platform(platform)
        rows = []
        for x in items:
            key = str(getattr(x, "problem_key", "") or "")
            if not key:
                continue
            tags = getattr(x, "tags", None)
            rows.append((
                p, key,
                str(getattr(x, "title", "") or "")[:300],
                # None 表示"这个平台给不出标签"，和"空列表"是两回事
                None if tags is None else json.dumps(list(tags), ensure_ascii=False),
                getattr(x, "difficulty", None),
                str(getattr(x, "difficulty_source", "unknown") or "unknown"),
            ))
        if not rows:
            return 0
        before = await self.count_problems(p)
        await self.db.executemany(
            "INSERT INTO problems "
            "(platform, problem_key, title, tags_json, difficulty, difficulty_source) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(platform, problem_key) DO UPDATE SET "
            "title=excluded.title, "
            "tags_json=COALESCE(excluded.tags_json, problems.tags_json), "
            "difficulty=COALESCE(excluded.difficulty, problems.difficulty), "
            "difficulty_source=excluded.difficulty_source",
            rows)
        after = await self.count_problems(p)
        return max(0, after - before)

    async def count_problems(self, platform: str = "") -> int:
        if platform:
            row = await self.db.query_one(
                "SELECT COUNT(*) AS n FROM problems WHERE platform=?",
                (_check_platform(platform),))
        else:
            row = await self.db.query_one("SELECT COUNT(*) AS n FROM problems")
        return int(row["n"]) if row else 0

    async def get_problem(self, platform: str, problem_key: str):
        return await self.db.query_one(
            "SELECT * FROM problems WHERE platform=? AND problem_key=?",
            (_check_platform(platform), problem_key))

    async def problems_missing_info(self, platform: str, limit: int = 200) -> list:
        """挑出**还没有难度**的题 —— 用于"按缺口补齐题库"。"""
        return await self.db.query(
            "SELECT problem_key FROM problems "
            "WHERE platform=? AND difficulty IS NULL LIMIT ?",
            (_check_platform(platform), int(limit)))

    # ==================================================================
    # 同步断点
    # ==================================================================
    async def get_sync_state(self, user_id: str, platform: str):
        user_id = _require_user(user_id)
        return await self.db.query_one(
            "SELECT * FROM sync_state WHERE user_id=? AND platform=?",
            (user_id, _check_platform(platform)))

    async def all_sync_states(self, user_id: str) -> dict[str, dict]:
        user_id = _require_user(user_id)
        rows = await self.db.query(
            "SELECT * FROM sync_state WHERE user_id=?", (user_id,))
        return {r["platform"]: dict(r) for r in rows}

    async def save_sync_ok(self, user_id: str, platform: str,
                           last_epoch: int | None = None,
                           last_page: int | None = None) -> None:
        user_id = _require_user(user_id)
        p = _check_platform(platform)
        await self.ensure_user(user_id)
        await self.db.execute(
            "INSERT INTO sync_state "
            "(user_id, platform, last_epoch, last_page, last_ok_at, error_kind, error_detail) "
            "VALUES (?, ?, ?, ?, ?, '', '') "
            "ON CONFLICT(user_id, platform) DO UPDATE SET "
            "last_epoch=COALESCE(excluded.last_epoch, sync_state.last_epoch), "
            "last_page=COALESCE(excluded.last_page, sync_state.last_page), "
            "last_ok_at=excluded.last_ok_at, error_kind='', error_detail=''",
            (user_id, p, last_epoch, last_page, self._now()))

    async def save_sync_error(self, user_id: str, platform: str,
                              error_kind: str, detail: str = "") -> None:
        """记失败。

        **刻意不动 `last_epoch`** —— 失败时若把断点推进了，
        下次同步就会从错的位置开始，**静默漏掉一段数据**。
        """
        user_id = _require_user(user_id)
        p = _check_platform(platform)
        await self.ensure_user(user_id)
        await self.db.execute(
            "INSERT INTO sync_state "
            "(user_id, platform, error_kind, error_detail) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, platform) DO UPDATE SET "
            "error_kind=excluded.error_kind, error_detail=excluded.error_detail",
            (user_id, p, str(error_kind or "内部错误")[:40], str(detail or "")[:500]))

    # ==================================================================
    # 绑定码 / 网页会话
    #
    # 用途：把「网页上的一次绑定操作」关联到「某个 QQ 号」。
    #
    # 为什么需要：AstrBot 的插件页面 token 只绑到「插件+页面」，不带用户身份
    # （`build_initial_context` 里只解出 plugin_name / page_name / locale）；
    # 而 `request.username` 是 Dashboard 登录名，不是 QQ 号。
    # 所以网页无法"知道"你是谁 —— 必须让你证明。
    #
    # 绑定码就是证明：在 QQ 里生成，只有能看到 QQ 消息的人才拿得到。
    # 这比"配置里填个 QQ 号"强得多：后者谁都填得了，填错了还会把
    # A 的账号绑成 B 的。
    # ==================================================================
    async def create_link_code(self, user_id: str, ttl_sec: int = 600) -> str:
        """给这个 QQ 号生成一个绑定码。**同一个人重复调用会作废旧码**。"""
        import secrets
        import time
        user_id = _require_user(user_id)
        now = int(time.time())
        # 先作废这个人之前没用的码 —— 免得旧码泄漏了还能用
        await self.db.execute(
            "DELETE FROM link_codes WHERE user_id=? AND used_at IS NULL", (user_id,))
        # 去掉容易看错的字符（0/O、1/I/l），码是要人在手机上照着敲的
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(6))
            row = await self.db.query_one(
                "SELECT code FROM link_codes WHERE code=?", (code,))
            if not row:
                break
        await self.db.execute(
            "INSERT INTO link_codes (code, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)", (code, user_id, self._now(), now + int(ttl_sec)))
        return code

    async def claim_link_code(self, code: str, ttl_sec: int = 7 * 86400):
        """用绑定码换一个网页会话令牌。

        返回 `(token, user_id)`；码不对/过期/已用过则返回 `(None, 原因)`。
        """
        import secrets
        import time
        code = str(code or "").strip().upper().replace(" ", "").replace("-", "")
        if not code:
            return None, "没填绑定码"
        row = await self.db.query_one("SELECT * FROM link_codes WHERE code=?", (code,))
        if not row:
            return None, "这个绑定码不存在 —— 在 QQ 里发 /xcpc 绑定 拿一个新的"
        if row["used_at"]:
            return None, "这个绑定码已经用过了 —— 在 QQ 里发 /xcpc 绑定 拿一个新的"
        now = int(time.time())
        if int(row["expires_at"]) < now:
            return None, "绑定码过期了（10 分钟内有效）—— 在 QQ 里发 /xcpc 绑定 重新拿"

        user_id = row["user_id"]
        token = secrets.token_urlsafe(32)
        # 一个码只能用一次
        await self.db.execute("UPDATE link_codes SET used_at=? WHERE code=?",
                              (self._now(), code))
        await self.db.execute(
            "INSERT INTO web_sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user_id, self._now(), now + int(ttl_sec)))
        # 顺手清掉过期的
        await self.db.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
        await self.db.execute("DELETE FROM link_codes WHERE expires_at < ?", (now,))
        return token, user_id

    async def resolve_web_token(self, token: str) -> str:
        """把网页令牌换成 QQ 号。无效/过期返回空串。"""
        import time
        token = str(token or "").strip()
        if not token:
            return ""
        row = await self.db.query_one(
            "SELECT user_id, expires_at FROM web_sessions WHERE token=?", (token,))
        if not row:
            return ""
        if int(row["expires_at"]) < int(time.time()):
            await self.db.execute("DELETE FROM web_sessions WHERE token=?", (token,))
            return ""
        return row["user_id"]

    async def revoke_web_token(self, token: str) -> None:
        await self.db.execute("DELETE FROM web_sessions WHERE token=?",
                              (str(token or "").strip(),))

    # ---- Dashboard 账号 ↔ QQ 号 -----------------------------------------
    #
    # 网页令牌存不住（插件页面是沙箱 iframe，localStorage 抛异常），
    # 所以「关联」必须落在服务端。Dashboard 登录名不依赖浏览器存储，
    # 是唯一可靠的锚点。
    async def link_dashboard_user(self, username: str, user_id: str) -> None:
        """把 Dashboard 登录名和 QQ 号绑起来。同一个人重复认领会覆盖。"""
        username = str(username or "").strip()
        if not username:
            raise ValueError("拿不到 Dashboard 登录名，无法记住这次关联")
        user_id = _require_user(user_id)
        now = self._now()
        await self.db.execute(
            "INSERT INTO dashboard_links (username, user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET user_id=excluded.user_id, "
            "updated_at=excluded.updated_at",
            (username, user_id, now, now))

    async def resolve_dashboard_user(self, username: str) -> str:
        """按 Dashboard 登录名查回 QQ 号。没绑过返回空串（不猜）。"""
        username = str(username or "").strip()
        if not username:
            return ""
        row = await self.db.query_one(
            "SELECT user_id FROM dashboard_links WHERE username=?", (username,))
        return row["user_id"] if row else ""

    async def unlink_dashboard_user(self, username: str, user_id: str) -> None:
        """解掉「这个 Dashboard 账号 = 这个 QQ 号」这一条。

        **两个条件都要对上。** 只按 username 删的话，万一片面状态不一致
        （比如页面拿到的名字和当初绑的不是同一个人），就会连别人的关联
        一起抹掉 —— 解绑是破坏性操作，宁可少删。
        """
        username = str(username or "").strip()
        user_id = str(user_id or "").strip()
        if not username or not user_id:
            return
        await self.db.execute(
            "DELETE FROM dashboard_links WHERE username=? AND user_id=?",
            (username, user_id))

    async def users_with_handles(self) -> list[str]:
        """列出**至少绑了一个平台**的用户。

        给自动同步用 —— 只同步绑过账号的人，
        不然每次定时任务都要遍历一堆从没用过插件的人。
        """
        rows = await self.db.query(
            "SELECT user_id FROM users WHERE "
            "COALESCE(cf_handle,'')<>'' OR COALESCE(atcoder_handle,'')<>'' OR "
            "COALESCE(luogu_uid,'')<>'' OR COALESCE(qoj_uid,'')<>''")
        return [r["user_id"] for r in rows]

    # ==================================================================
    # 今天做了没（循环第 ⑤ 环）
    # ==================================================================
    # 合法状态。**"没做"和"做了一部分"要分开** ——
    # 合成一个"未完成"会丢掉重要区别：前者是没开始，后者是开始了但没做完，
    # 计划引擎对这两种该有不同反应（少排一点 vs 换个更小的切分）。
    TASK_STATUS = ("done", "partial", "skipped")

    async def log_task(self, user_id: str, status: str, date: str = "",
                       note: str = "", plan_id: int | None = None) -> None:
        """记「今天做了没」。**同一天重复记是更新，不是追加。**"""
        user_id = _require_user(user_id)
        st = str(status or "").strip().lower()
        if st not in self.TASK_STATUS:
            raise ValueError("不认识的状态：%r（只能是 %s）"
                             % (status, "/".join(self.TASK_STATUS)))
        from . import log as logm
        day = date or logm.now_cn().strftime("%Y-%m-%d")
        await self.ensure_user(user_id)
        await self.db.execute(
            "INSERT INTO task_log (user_id, date, status, note, plan_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, date) DO UPDATE SET "
            "status=excluded.status, note=excluded.note, "
            "plan_id=COALESCE(excluded.plan_id, task_log.plan_id), "
            "updated_at=excluded.updated_at",
            (user_id, day, st, str(note or "")[:500], plan_id, self._now()))

    async def task_log(self, user_id: str, days: int = 14) -> list:
        """最近若干天的执行记录，**按日期倒序**。"""
        user_id = _require_user(user_id)
        return await self.db.query(
            "SELECT date, status, note, plan_id FROM task_log WHERE user_id=? "
            "ORDER BY date DESC LIMIT ?", (user_id, max(1, int(days))))

    async def task_stats(self, user_id: str, days: int = 14) -> dict:
        """执行率统计。给汇总用 —— **计划引擎要看到"上一版执行得怎么样"**。"""
        user_id = _require_user(user_id)
        rows = await self.task_log(user_id, days)
        if not rows:
            return {"days": 0, "done": 0, "partial": 0, "skipped": 0,
                    "rate": None, "streak": 0, "recent": []}
        counts = {"done": 0, "partial": 0, "skipped": 0}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        n = len(rows)
        # "做了一部分"算半次 —— 比"要么 0 要么 1"更贴近现实
        rate = (counts["done"] + 0.5 * counts["partial"]) / n
        # 连续完成天数（从最近一天往回数，partial 也算续上）
        streak = 0
        for r in rows:
            if r["status"] in ("done", "partial"):
                streak += 1
            else:
                break
        return {"days": n, "done": counts["done"], "partial": counts["partial"],
                "skipped": counts["skipped"], "rate": rate, "streak": streak,
                "recent": [dict(r) for r in rows[:7]]}

    async def activity(self, user_id: str, days: int = 30) -> dict:
        """最近提交活跃度 —— 给每日推送用。

        为什么不直接复用 `summary.build()`：那段要拉全量提交、整个题库、
        标签表，只为在推送里写一句"今天交了 3 条"不值当。这里只碰
        `submissions`，走 `idx_sub_user_epoch` 索引。

        `today_sub` 的边界是**中国时区的零点**。用 `now % 86400` 那种
        UTC 切法会在早上 8 点前把"今天"算成昨天 —— 推送是晚上发的，
        差一天就是「今天交了没」答错。
        """
        user_id = _require_user(user_id)
        import time as _time
        from datetime import datetime
        from . import log as logm
        now = int(_time.time())
        midnight = logm.now_cn().replace(hour=0, minute=0, second=0, microsecond=0)
        t0 = int(midnight.timestamp())
        rows = await self.db.query(
            "SELECT epoch, UPPER(verdict) AS v FROM submissions "
            "WHERE user_id=? AND epoch IS NOT NULL AND epoch >= ?",
            (user_id, min(t0, now - max(1, int(days)) * 86400)))
        eps = [(int(r["epoch"]), r["v"] in ("OK", "AC", "ACCEPTED")) for r in rows]
        today = [ok for e, ok in eps if e >= t0]

        def _day(ep: int):
            return datetime.fromtimestamp(ep, logm.CN_TZ).date()

        have = sorted({_day(e) for e, _ in eps}, reverse=True)
        # 只有**今天就有提交**时才谈"连续"。一个叫 `streak` 的字段紧挨着
        # `days_since_last`，调用方迟早会拿它去写"连续活跃 3 天" —— 而那个
        # 人上一次交题可能是一周前。今天没交，就是断了。
        streak = 0
        if have and have[0] == _day(t0):
            streak = 1
            for i in range(1, len(have)):
                if (have[i - 1] - have[i]).days == 1:
                    streak += 1
                else:
                    break
        return {
            "today_sub": len(today), "today_ac": sum(1 for ok in today if ok),
            "d7": sum(1 for e, _ in eps if now - e <= 7 * 86400),
            "d30": sum(1 for e, _ in eps if now - e <= 30 * 86400),
            "streak": streak,
            "last_epoch": max((e for e, _ in eps), default=0),
            "days_since_last": (_day(t0) - have[0]).days if have else None,
        }


    # ==================================================================
    # 训练块（v0.6.0）
    # ==================================================================
    # 「一段连续的日子里只吃一个子专题」。每个用户同时只有**一个**块 ——
    # "同时开三个专题"正是要治的病（见 core/curriculum.py 的模块注释）。
    async def get_block(self, user_id: str):
        """当前训练块。没有就返回 `None`。"""
        user_id = _require_user(user_id)
        return await self.db.query_one(
            "SELECT * FROM blocks WHERE user_id=?", (user_id,))

    async def set_block(self, user_id: str, module: str, topic: str, *,
                        target: int = 15, band_lo: int = 1200,
                        band_hi: int = 1600, note: str = "") -> None:
        """开一个训练块（或改当前块的参数）。

        ⚠️ **同一个子专题重复设置时保留原来的 `started_at`** ——
        进度是"从这个时刻之后 AC 了多少道"，每调一次就把起点推到今天的话，
        用户永远停在 0/N。只有**换了子专题**才重新起算。
        """
        user_id = _require_user(user_id)
        await self.ensure_user(user_id)
        now = self._now()
        old = await self.get_block(user_id)
        started = now
        if old is not None and old["topic"] == topic and old["module"] == module:
            started = old["started_at"] or now
        await self.db.execute(
            "INSERT INTO blocks "
            "(user_id, module, topic, target, band_lo, band_hi, started_at, updated_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "module=excluded.module, topic=excluded.topic, target=excluded.target, "
            "band_lo=excluded.band_lo, band_hi=excluded.band_hi, "
            "started_at=excluded.started_at, updated_at=excluded.updated_at, "
            "note=excluded.note",
            (user_id, str(module), str(topic), int(target), int(band_lo),
             int(band_hi), started, now, str(note or "")[:300]))

    async def clear_block(self, user_id: str) -> None:
        user_id = _require_user(user_id)
        await self.db.execute("DELETE FROM blocks WHERE user_id=?", (user_id,))

    # ==================================================================
    # VP 场次候选（v0.6.1）
    # ==================================================================
    #: 认得这两个平台的比赛列表。`core/vp.py` 里也有同名常量 ——
    #: 这里再写一遍是为了**不带循环依赖**地校验参数。
    _VP_PLATFORMS = ("codeforces", "atcoder")

    def _check_vp_platform(self, platform: str) -> str:
        p = (platform or "").strip().lower()
        if p not in self._VP_PLATFORMS:
            raise ValueError("VP 只支持 %s，收到 %r"
                             % ("/".join(self._VP_PLATFORMS), platform))
        return p

    async def vp_state(self, platform: str) -> dict:
        """上一次**尝试**抓取比赛列表的结果。没抓过返回 `{}`。

        ⚠️ `fetched_at` 是**尝试**时间，成功失败都更新 ——
        只在成功时更新的话，抓一次失败就会让之后每一次调用都重试。
        """
        p = self._check_vp_platform(platform)
        row = await self.db.query_one(
            "SELECT * FROM vp_cache WHERE platform=?", (p,))
        if not row:
            return {}
        return {
            "platform": p,
            "fetched_at": int(row["fetched_at"] or 0),
            "ok": bool(row["ok"]),
            "detail": row["detail"] or "",
            "rating": int(row["rating"] or 0),
        }

    async def save_vp(self, platform: str, items, *, ok: bool = True,
                      detail: str = "", rating: int | None = None) -> int:
        """整批替换某个平台的候选，并记下这次尝试的结果。返回写入条数。

        **整批替换而不是 upsert** —— 列表是外部给的快照，删掉的场次
        就该消失。留着的话会推荐一场已经被 CF 下架的比赛。
        """
        p = self._check_vp_platform(platform)
        rows = []
        for it in items or []:
            cid = str((it or {}).get("contest_id") or "").strip()
            if not cid:
                continue
            rows.append((
                p, cid,
                str(it.get("name") or ""),
                str(it.get("division") or ""),
                int(it.get("start_epoch") or 0),
                int(it.get("duration_sec") or 0),
            ))
        await self.db.execute("DELETE FROM vp_contests WHERE platform=?", (p,))
        if rows:
            # ★ 是 `executemany`，不是 `execute_many`（写错在真机上
            # 一存就 AttributeError，而当时还没有测试覆盖到这一行）
            await self.db.executemany(
                "INSERT OR REPLACE INTO vp_contests "
                "(platform, contest_id, name, division, start_epoch, duration_sec) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
        old = await self.db.query_one(
            "SELECT rating FROM vp_cache WHERE platform=?", (p,))
        keep_rating = int((old or {})["rating"] or 0) if old else 0
        if rating is None:
            rating = keep_rating
        await self.db.execute(
            "INSERT INTO vp_cache (platform, fetched_at, ok, detail, rating) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(platform) DO UPDATE SET "
            "fetched_at=excluded.fetched_at, ok=excluded.ok, "
            "detail=excluded.detail, rating=excluded.rating",
            # ★ 是模块级的 `_epoch()`，不是 `self._epoch()`
            (p, _epoch(), 1 if ok else 0, str(detail or "")[:300],
             int(rating or 0)))
        return len(rows)

    async def list_vp(self, platform: str, limit: int = 0) -> list:
        """某个平台的候选，**新的在前**。"""
        p = self._check_vp_platform(platform)
        sql = ("SELECT * FROM vp_contests WHERE platform=? "
               "ORDER BY start_epoch DESC")
        params: tuple = (p,)
        if int(limit) > 0:
            sql += " LIMIT ?"
            params = (p, int(limit))
        return await self.db.query(sql, params)

    # ==================================================================
    # 方案与反馈
    # ==================================================================
    async def save_plan(self, user_id: str, date: str, payload: Any) -> int:
        user_id = _require_user(user_id)
        await self.ensure_user(user_id)
        await self.db.execute(
            "INSERT INTO plans (user_id, date, payload_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, date, json.dumps(payload, ensure_ascii=False), self._now()))
        row = await self.db.query_one("SELECT last_insert_rowid() AS id")
        return int(row["id"]) if row else 0

    async def latest_plan(self, user_id: str, date: str = ""):
        """取最近一版方案。`date` 为空则取任意最近一版。

        用途之一是"LLM 失败时保留上一版方案" —— 所以必须能取到旧的。
        """
        user_id = _require_user(user_id)
        if date:
            return await self.db.query_one(
                "SELECT * FROM plans WHERE user_id=? AND date=? "
                "ORDER BY id DESC LIMIT 1", (user_id, date))
        return await self.db.query_one(
            "SELECT * FROM plans WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))

    async def plan_history(self, user_id: str, limit: int = 14) -> list:
        user_id = _require_user(user_id)
        return await self.db.query(
            "SELECT id, date, created_at FROM plans WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?", (user_id, int(limit)))

    async def add_feedback(self, user_id: str, date: str, text: str) -> int:
        user_id = _require_user(user_id)
        await self.ensure_user(user_id)
        await self.db.execute(
            "INSERT INTO feedback (user_id, date, text, created_at) VALUES (?, ?, ?, ?)",
            (user_id, date, str(text or "")[:2000], self._now()))
        row = await self.db.query_one("SELECT last_insert_rowid() AS id")
        return int(row["id"]) if row else 0

    async def list_feedback(self, user_id: str, days: int = 14) -> list:
        user_id = _require_user(user_id)
        return await self.db.query(
            "SELECT date, text, created_at FROM feedback WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?", (user_id, max(1, int(days)) * 10))


def _default_now() -> str:
    from . import log as logm
    return logm.stamp()


def _epoch() -> int:
    """当前时间戳（秒）。

    `_default_now()` 给的是**给人看的字符串**（本地时区），而
    `vp_cache.fetched_at` 要拿来做减法算 TTL —— 字符串没法减。
    两个都要，所以两个都有。
    """
    import time
    return int(time.time())
