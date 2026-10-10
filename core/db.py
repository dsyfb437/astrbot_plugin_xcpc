#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存储层：建表、迁移、连接、完整性校验。

为什么用 SQLite 而不是 JSON 文件
--------------------------------
这个插件是**多用户**的（每个 QQ 号绑定自己的账号）。多用户 + 定时同步 =
并发读写，JSON 文件在这种场景下会写坏（读到半个文件、后写覆盖先写）。
SQLite 的 WAL 模式让读写不互斥，而且有事务保证。

为什么不用 `context.get_db()`
-----------------------------
AstrBot 确实提供了数据库（`context.get_db()` → `BaseDatabase`，SQLAlchemy async +
`sqlite+aiosqlite`），但**它的表全是 AstrBot 自己的**（会话 / 消息历史 / 平台统计 /
知识库）。往别人的 schema 里加表，会在 AstrBot 升级时炸。
参考的 `codeforces_helper` 插件也是自己建库。

所以：**自己一个库文件**，和 AstrBot 的库完全分开。

线程模型
--------
`sqlite3` 是同步的，直接调用会阻塞事件循环。这里用
**单连接 + `check_same_thread=False` + 一把 asyncio 锁 + `asyncio.to_thread`**：
所有读写串行化，代价可忽略（单人量级，而且 WAL 下读很快）。

设计上分成两层：
  * **同步核心**（`*_sync` 函数）：接收一个 connection，纯逻辑，**可以脱离 asyncio 单测**
  * **异步包装**（`Database` 类）：加锁 + 丢进线程池
这样测试不需要 event loop，也就能覆盖得更细。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# 用 PRAGMA user_version 做迁移版本号。
# 加新表/加列时：**不要改老语句**，在后面追加一条 _MIGRATIONS 项。
SCHEMA_VERSION = 7

_TABLES_V1 = """
-- 用户与账号绑定。
-- handle 放在这里而不是配置里 —— 配置是全局的，而每个 QQ 号绑自己的账号。
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,      -- QQ 号（event.get_sender_id()）
    cf_handle       TEXT,
    atcoder_handle  TEXT,
    luogu_uid       TEXT,
    qoj_uid         TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 凭据：每人每平台一行。
-- cookie_blob 永不进日志、永不进 API 响应（见 core/log.py 的掩码规则）。
CREATE TABLE IF NOT EXISTS credentials (
    user_id     TEXT NOT NULL,
    platform    TEXT NOT NULL,
    cookie_blob TEXT,
    status      TEXT NOT NULL DEFAULT 'unbound',   -- unbound|valid|expired|error
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, platform)
);

-- 练习提交。去重键 = (user_id, platform, submission_id)
CREATE TABLE IF NOT EXISTS submissions (
    user_id           TEXT NOT NULL,
    platform          TEXT NOT NULL,
    submission_id     TEXT NOT NULL,
    problem_key       TEXT NOT NULL,
    verdict           TEXT,
    epoch             INTEGER,
    difficulty        INTEGER,
    difficulty_source TEXT,
    PRIMARY KEY (user_id, platform, submission_id)
);
CREATE INDEX IF NOT EXISTS idx_sub_user_epoch ON submissions(user_id, epoch);

-- 比赛记录 —— **独立一条流**（字段、去重键、用途都和练习提交不同）
CREATE TABLE IF NOT EXISTS contests (
    user_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    contest_id    TEXT NOT NULL,
    name          TEXT,
    start_epoch   INTEGER,
    rank          INTEGER,
    solved        INTEGER,
    rating_delta  INTEGER,
    problems_json TEXT,
    PRIMARY KEY (user_id, platform, contest_id)
);
CREATE INDEX IF NOT EXISTS idx_contest_user_time ON contests(user_id, start_epoch);

-- 题库标注 —— **全局共享**，不按用户分（同一道题的难度/标签对所有人都一样）
CREATE TABLE IF NOT EXISTS problems (
    platform          TEXT NOT NULL,
    problem_key       TEXT NOT NULL,
    title             TEXT,
    tags_json         TEXT,
    difficulty        INTEGER,
    difficulty_source TEXT,
    PRIMARY KEY (platform, problem_key)
);

-- LLM 给出的方案（保留历史，便于"上一版方案"和前后对比）
CREATE TABLE IF NOT EXISTS plans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    date         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_user_date ON plans(user_id, date DESC);

-- 用户的反馈（一句话 / 打卡）
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    date       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fb_user_date ON feedback(user_id, date DESC);

-- 同步断点：每人每平台一行
CREATE TABLE IF NOT EXISTS sync_state (
    user_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    last_epoch    INTEGER,
    last_page     INTEGER,
    last_ok_at    TEXT,
    error_kind    TEXT,
    error_detail  TEXT,
    PRIMARY KEY (user_id, platform)
);
"""

# 迁移表：(目标版本, SQL 列表)
# 加字段时**不要**去改 _TABLES_V1，加在这里，否则老库升不上来。
_MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [s for s in _TABLES_V1.split(";") if s.strip()]),
    (2, [
        # 「绑定码」：QQ 里生成 → 网页里输入 → 证明网页背后的人确实是这个 QQ。
        #
        # 为什么需要这个：AstrBot 的插件页面 token 只绑到「插件+页面」，
        # **不带用户身份**（`build_initial_context` 里只解出 plugin_name / page_name /
        # locale）。而 `request.username` 是 Dashboard 的登录名，不是 QQ 号。
        #
        # 所以网页上没法"知道"你是谁 —— 必须让你证明。
        # 绑定码就是证明：只有能看到 QQ 消息的人才拿得到这个码。
        #
        # 这比"配置里填个 QQ 号"强得多：后者谁都填得了，填错了还会
        # 把 A 的账号绑成 B 的。
        """CREATE TABLE IF NOT EXISTS link_codes (
            code        TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  INTEGER NOT NULL,
            used_at     TEXT
        )""",
        """CREATE INDEX IF NOT EXISTS idx_link_exp ON link_codes(expires_at)""",
        # 网页会话：认领绑定码之后发的令牌，页面每次请求带上。
        # 令牌是随机的（token_urlsafe(32)），安全性等价于 session cookie。
        """CREATE TABLE IF NOT EXISTS web_sessions (
            token       TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  INTEGER NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_web_sess_exp ON web_sessions(expires_at)""",
    ]),
    (3, [
        # 「今天做了没」—— 循环第 ⑤ 环的落点。
        #
        # 没有这张表的话，反馈闭环是断的：
        # 用户能说"今天有点累"（主观感受），但**没法说"我做完了"**，
        # 于是计划引擎永远不知道上一版方案有没有被执行 ——
        # 那它就不是"动态调整"，只是每天重新猜一次。
        #
        # 一天一行。重复打卡是**更新**而不是新增（一天只有一个状态）。
        """CREATE TABLE IF NOT EXISTS task_log (
            user_id     TEXT NOT NULL,
            date        TEXT NOT NULL,
            status      TEXT NOT NULL,       -- done / partial / skipped
            note        TEXT,
            plan_id     INTEGER,             -- 对应哪一版方案（可空）
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (user_id, date)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_tasklog_user ON task_log(user_id, date DESC)""",
    ]),
    (4, [
        # 「Dashboard 登录名 → QQ 号」。**这张表才是"关联能记住"的原因。**
        #
        # 插件页面跑在 WebUI 的 iframe 里，那个 iframe 带 sandbox 但
        # **没有 allow-same-origin**（AstrBot 的
        # `dashboard/src/views/PluginViewPage.vue`），页面因此处于"不透明源"：
        # `window.localStorage` 一读一写都抛 SecurityError ——
        # 网页令牌**根本存不住**，而存不住的令牌等于没有。
        #
        # 表现就是：点了「关联」，提示"已关联到 QQ xxx"，可下一次请求不带令牌，
        # 页面又退回"先关联 QQ 号"，日志面板也永远读不出来。
        #
        # 好在 **Dashboard 登录名是服务端的事实**：插件路由能拿到
        # `request.username`（`astrbot/api/web.py` 的 `PluginRequest` 带进来的，
        # 由 `require_plugin_scope` 守门），完全不依赖浏览器存储。
        # 认领绑定码时把「这个账号 = 这个 QQ 号」记下来，之后按账号查回去。
        """CREATE TABLE IF NOT EXISTS dashboard_links (
            username    TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dashlink_user ON dashboard_links(user_id)""",
    ]),
    (5, [
        # ---- 评测结果词表修复（2026-10-08）----
        #
        # 洛谷的 `status` 是**数字**，旧代码直接 `str()` 存了进去。
        # 而判断 AC 的 `core/summary.py:_is_ac()` 只认 OK / AC / ACCEPTED
        # （CF 发 "OK"、AtCoder 发 "AC"）—— 于是 333 条真 AC 被算成
        # "一次都没过"，用户看到「洛谷 733 提交 0 AC」。
        #
        # 代码已经改了（`platforms/luogu.py:_lg_verdict`），但**老行不会自己变**：
        # 游标早已推进，下次同步只拉新的，这 700 多行会永远是错的。
        # 所以在库里一次性翻新。幂等 —— 翻完就没有 '12'/'14'/'2' 了。
        """UPDATE submissions SET verdict='AC'
           WHERE platform='luogu' AND verdict='12'""",
        """UPDATE submissions SET verdict='WA'
           WHERE platform='luogu' AND verdict='14'""",
        """UPDATE submissions SET verdict='CE'
           WHERE platform='luogu' AND verdict='2'""",
        # ---- QOJ 的脏数据必须**删掉**，翻新没有意义 ----
        #
        # 旧代码抓的是 `/submissions`（**漏了 `?submitter=`**），那是 QOJ 的
        # **全站最近提交**，不是这个人的。存进来的是陌生人的记录
        # （lrmlrm、pino、Crazyouth…），而所谓 "verdict" 抓的是那一行里
        # 第一个"长得像英文单词"的单元格 —— 于是**提交者用户名**和
        # **语言**（`C++26`）被当成了评测结果。
        # 这些行没有一条属于用户，翻新没有意义，只能清掉重拉。
        """DELETE FROM submissions WHERE platform='qoj'""",
        # 游标一起清掉 —— 不清的话重拉时"比游标旧的"会被整片跳过，
        # 反而把他真正的记录也挡在外面。清空之后下一次同步是**全量**。
        """UPDATE sync_state SET last_epoch=NULL, last_ok_at=NULL
           WHERE platform='qoj'""",
    ]),
    (6, [
        # ---- 「训练块」：一次只吃一个子专题（v0.6.0）----
        #
        # 为什么要有这张表：v0.6.0 之前方案引擎是**无状态**的 ——
        # 每天的方案独立生成，唯一输入是全局统计，所以输出当然每天长得一样。
        # 用户的原话：
        #
        #     像这样子推荐一个两个题练一下我感觉根本没效果啊，也没有针对性，
        #     每次似乎都是从整体做题情况出发
        #
        # 他说得对。一道题改变不了任何东西 —— 一个子专题要吃 15-20 道才谈得上
        # 入门，而"每天一道、还跨三个方向"意味着每个方向一天 0.5 道。
        # 这是训练量的算术问题，方案层怎么优化都绕不过去。
        #
        # 所以引入"训练块"：**一段连续的日子里只吃一个子专题**，
        # 吃够了（target 道）再走阶梯上的下一个。
        #
        # 每个用户同时只有**一个**块（主键就是 user_id）——
        # "同时开三个专题"正是要治的病。
        #
        # 进度**不存这张表**：它由 `submissions` 里 `started_at` 之后、
        # 命中该子专题标签的 AC 题数算出来（`Store.block_progress`）。
        # 存一个计数器就要处理"重复 AC 同题""补题算不算""跨平台"——
        # 而这些都是查询能回答的问题，存下来只会多一个会漂移的副本。
        """CREATE TABLE IF NOT EXISTS blocks (
            user_id     TEXT PRIMARY KEY,
            module      TEXT NOT NULL,      -- dp / graph（curriculum.MODULES 的 key）
            topic       TEXT NOT NULL,      -- curriculum 里 Topic.key（稳定 id）
            target      INTEGER NOT NULL,   -- 这个子专题打算吃多少道
            band_lo     INTEGER NOT NULL,   -- 难度带（CF rating）
            band_hi     INTEGER NOT NULL,
            started_at  TEXT NOT NULL,      -- 进度从这个时刻之后开始算
            updated_at  TEXT NOT NULL,
            note        TEXT                -- 为什么选它（给用户看的理由）
        )""",
        """CREATE INDEX IF NOT EXISTS idx_blocks_topic ON blocks(topic)""",
    ]),

    # ---------------- v7：VP 场次候选（v0.6.1） ----------------
    #
    # v0.6.0 的 prompt 里 VP 那条规则只能写泛指，因为库里 `contests`
    # 表**只存已经参加过的比赛** —— 没有任何"还没打但可以打"的候选。
    # 模型没有这个数据，写具体场次就一定是编的。
    #
    # 数据源是两个公开接口（都不用登录）：
    #   · CF      `contest.list?gym=false`（410KB / 2155 场）
    #   · AtCoder `kenkoooo.com/atcoder/resources/contests.json`（1.0MB）
    #
    # ★ 两张表而不是一张：**元数据和数据分开**。
    # `vp_cache` 那一行记的是"上一次**尝试**抓取的结果"——
    # 包括失败。只在成功时记时间的话，抓一次失败就会让之后每一次
    # 调用都重试，把一个"偶尔慢"变成"每次都慢三秒"。
    (7, [
        """CREATE TABLE IF NOT EXISTS vp_contests (
            platform     TEXT NOT NULL,     -- codeforces / atcoder
            contest_id   TEXT NOT NULL,
            name         TEXT,
            division     TEXT,              -- Div. 2 / Educational / ABC …
            start_epoch  INTEGER,
            duration_sec INTEGER,
            PRIMARY KEY (platform, contest_id)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_vp_start
               ON vp_contests(platform, start_epoch)""",
        """CREATE TABLE IF NOT EXISTS vp_cache (
            platform   TEXT PRIMARY KEY,
            fetched_at INTEGER NOT NULL,    -- **尝试**时间，不是成功时间
            ok         INTEGER NOT NULL,
            detail     TEXT,
            rating     INTEGER              -- 抓列表时顺手抓的他的 rating
        )""",
    ]),
]


# ---------------------------------------------------------------------------
# 同步核心（可脱离 asyncio 测试）
# ---------------------------------------------------------------------------

def connect_sync(path: str) -> sqlite3.Connection:
    """打开（或创建）数据库，设好 WAL 等 pragma。

    `check_same_thread=False`：因为我们把调用丢进线程池，
    连接会跨线程用 —— 串行化由 `Database` 的锁负责，不靠 sqlite 自己的检查。

    ⚠️ **文件不是数据库时，pragma 本身就会抛** —— 所以这里必须兜住并把连接关掉，
    否则调用方以为"打开了"，实际拿着一个废连接。
    （这个坑是自测抓出来的：损坏文件的用例原本直接在 `PRAGMA journal_mode` 上崩。）
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        # WAL：读写不互斥（定时同步在读的同时，命令还能写）
        conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL：WAL 下这个档位既快又不会在断电时损坏
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # 多用户并发写时，等锁而不是立刻报 database is locked
        conn.execute("PRAGMA busy_timeout=30000")
        # 真正碰一下 schema —— 有些损坏要到读表才暴露
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        try:
            conn.close()
        except Exception:
            pass
        raise sqlite3.DatabaseError("不是有效的 SQLite 数据库：%s" % exc) from exc
    return conn


def schema_version_sync(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate_sync(conn: sqlite3.Connection) -> int:
    """把库升到 `SCHEMA_VERSION`。幂等：已经是最新就什么都不做。

    返回升级后的版本号。
    """
    current = schema_version_sync(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            "数据库版本 %d 比本插件支持的 %d 还新 —— 你是不是降级了插件？"
            "请先升级插件，不要直接改库。" % (current, SCHEMA_VERSION))
    for version, statements in _MIGRATIONS:
        if version <= current:
            continue
        with conn:                       # 一个版本一个事务，失败整体回滚
            for sql in statements:
                conn.execute(sql)
            conn.execute("PRAGMA user_version=%d" % version)
        current = version
    return current


def integrity_check_sync(conn: sqlite3.Connection) -> tuple[bool, str]:
    """启动时校验。返回 (是否完好, 说明)。

    **不做静默重建** —— 坏了要让人知道，因为坏掉的库里可能还有能救的数据。
    """
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return False, "无法执行完整性检查：%s" % exc
    messages = [str(r[0]) for r in rows]
    if messages == ["ok"]:
        return True, "ok"
    return False, "；".join(messages[:5])


def table_names_sync(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# 异步包装
# ---------------------------------------------------------------------------

class Database:
    """单连接 + 一把锁 + 线程池。所有读写串行。

    为什么串行：SQLite 在多写者下要靠 `busy_timeout` 排队，串行化更可预测，
    而且这个插件的量级（一个人几十条提交/天）根本不需要并发。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("数据库还没打开（先 await db.open()）")
        return self._conn

    async def open(self) -> tuple[bool, str]:
        """打开 + 迁移 + 完整性校验。返回 (是否可用, 说明)。

        **任何失败都返回 (False, 说明)，不抛异常** —— 上层要能把它变成
        一句给用户看的话 + 一条日志，而不是让插件在启动时崩掉。
        """
        def _do():
            conn = connect_sync(self.path)
            healthy, detail = integrity_check_sync(conn)
            if not healthy:
                conn.close()
                return None, False, detail
            version = migrate_sync(conn)
            return conn, True, "ok（schema v%d）" % version

        try:
            conn, ok, detail = await asyncio.to_thread(_do)
        except sqlite3.Error as exc:
            # 文件损坏 / 权限不对 / 被别的进程锁死，都走这里
            return False, "%s: %s" % (type(exc).__name__, exc)
        except OSError as exc:
            return False, "打不开数据库文件：%s" % exc
        if not ok:
            return False, detail
        self._conn = conn
        return True, detail

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    # ---- 通用执行入口 -------------------------------------------------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        async with self._lock:
            await asyncio.to_thread(self._execute_sync, sql, tuple(params))

    async def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        rows = [tuple(r) for r in seq]
        if not rows:
            return
        async with self._lock:
            await asyncio.to_thread(self._executemany_sync, sql, rows)

    async def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(self._query_sync, sql, tuple(params))

    async def query_one(self, sql: str, params: Iterable[Any] = ()):
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def transaction(self, fn) -> Any:
        """在事务里跑一个同步函数：`fn(conn) -> result`。异常整体回滚。"""
        async with self._lock:
            return await asyncio.to_thread(self._transaction_sync, fn)

    # ---- 供 to_thread 调用的同步体（不对外） ---------------------------
    def _execute_sync(self, sql: str, params: tuple) -> None:
        with self.conn:
            self.conn.execute(sql, params)

    def _executemany_sync(self, sql: str, rows: list[tuple]) -> None:
        with self.conn:
            self.conn.executemany(sql, rows)

    def _query_sync(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _transaction_sync(self, fn) -> Any:
        # isolation_level 默认是 ""（隐式事务），用显式 BEGIN 更可控
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(self.conn)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()
        return result
