#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""astrbot_plugin_xcpc —— 把 XCPC 备赛工作区接进 QQ / Telegram / Discord。

场景
----
打完一场 5 小时 VP，坐地铁回学校。手机打开聊天窗口发一段话：

    复盘
    比赛: CF Round 1024
    类型: VP
    过题: 3
    罚时: 145
    贡献: 想出 A、C
    想歪: 看到区间修改就反射性上线段树，其实是差分+前缀和
    套路: 反悔贪心用堆维护
    A 思路 想了一个小时没往图论上想
    E 实现 独立想出 P1 边界写挂了

机器人回一句「已记录 → 04-review/2026-10-06-CF-Round-1024.md」。
回到宿舍文件已经在那了，接着让 DSH 整理成正式复盘。

**想不起来打了啥也行**：QOJ 抓不到（实测 403 + ``robots.txt`` 禁止
``/submission/``），但榜单页和提交记录页就在那里 —— 直接粘进来：

    复盘
    比赛: QOJ VP 2026-10-06
    0:12  A  Accepted
    0:45  B  Wrong Answer
    0:52  B  Wrong Answer
    1:10  B  Accepted

过题数 / AC 顺序 / 罚时（带算式，并标明是按几次算的）会被自动补上，
「逐题记录」的题号也有了；手打的字段优先，不会被粘贴内容覆盖。
不确定解析对不对就先发 ``/解析`` —— 那个只回报，不落盘。

本文件只负责"接到 AstrBot 上"：指令注册、消息发送、定时任务。
真正读写工作区/解析复盘的逻辑在 ``xcpc_core.py``（不依赖 astrbot，可单独自测）。

用到的 AstrBot API 全部来自官方文档与源码，来源见 README.md 的「API 来源」一节。
"""

from __future__ import annotations

import asyncio
import glob
import os
import re
import traceback
from datetime import timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 新的 core 包（存储 / 日志 / 平台适配）。
#
# 为什么要 try/except 而不是直接相对导入：
#   * AstrBot **按包加载**插件 → `from .core import ...` 正确
#   * 但 selftest.py 是把 main **当顶层模块**导入的（`import main`）→
#     相对导入会抛 "attempted relative import with no known parent package"
#
# 直接用相对导入会让原有 210 项自测全挂（已经挂过一次）。两边都要能用，
# 所以按导入方式退而求其次 —— 这不是将就，是因为同一个文件确实会被两种方式加载。
try:
    from .core import accounts as accm
    from .core import db as dbm
    from .core import llm as llmm
    from .core import log as logm
    from .core import loop as loopm
    from .core import selfcheck as scm
    from .core import store as stm
    from .core import sync as syncm
except ImportError:                       # 作为顶层模块导入（自测 / 手工调试）
    from core import accounts as accm     # type: ignore[no-redef]
    from core import db as dbm            # type: ignore[no-redef]
    from core import llm as llmm          # type: ignore[no-redef]
    from core import log as logm          # type: ignore[no-redef]
    from core import loop as loopm        # type: ignore[no-redef]
    from core import selfcheck as scm    # type: ignore[no-redef]
    from core import store as stm         # type: ignore[no-redef]
    from core import sync as syncm        # type: ignore[no-redef]

# 既能作为包被 AstrBot 加载（data.plugins.astrbot_plugin_xcpc.main），
# 也能在同一目录下被直接 import —— 后者方便脱离 AstrBot 调试。
try:
    from .xcpc_core import (
        CONTRIB_CHOICES,
        DEFAULT_PER_FAIL,
        KIND_CHOICES,
        PROBLEM_KINDS,
        UPSOLVE_CHOICES,
        ReviewDraft,
        WorkspaceFS,
        XcpcHttp,
        empty_contest_result,
        format_contest_report,
        format_parse_reply,
        looks_like_paste,
        merge_contest_into_draft,
        now_cn,
        parse_review_text,
        split_per_fail_directive,
        today_cn,
    )
except ImportError:  # pragma: no cover - 只在非包方式加载时走到
    from xcpc_core import (  # type: ignore[no-redef]
        CONTRIB_CHOICES,
        DEFAULT_PER_FAIL,
        KIND_CHOICES,
        PROBLEM_KINDS,
        UPSOLVE_CHOICES,
        ReviewDraft,
        WorkspaceFS,
        XcpcHttp,
        empty_contest_result,
        format_contest_report,
        format_parse_reply,
        looks_like_paste,
        merge_contest_into_draft,
        now_cn,
        parse_review_text,
        split_per_fail_directive,
        today_cn,
    )

#: 触发"自动识别复盘文本"的正则。故意写得很保守 —— 只在出现**强标志**时才抢消息，
#: 免得把正常聊天抢过来、让 LLM 答不了话。
#: 命中后 main.py 里还会再检查一次"是不是以指令前缀开头"，避免和 /复盘 重复记录。
REVIEW_TRIGGER_RE = (
    r"(?m)^\s*(?:复盘|vp\s*复盘|复盘记录|记录复盘)\s*$"
    r"|^\s*(?:比赛|过题|罚时|排名)\s*[:：=]"
)

HELP_TEXT = """XCPC 备赛助手 —— 能用的指令：

/复盘 <内容>      记一场 VP / 比赛（也可以直接发一段带「比赛:」的话）
/解析 <粘贴内容>  只解析榜单/提交记录给你看，不落盘 —— 先验一遍再 /复盘
/随手记 <内容>    丢进 03-log/inbox.md 收件箱，回来再整理
/今天             今天那组任务（读 00-plan/sprint.md）
/状态             核心指标：rating / 已 AC / 连续天数 / 个人 KPI
/题单             当前题单的前几道
/刷新             重新抓 CF 数据并重跑诊断（只 file 后端可用）
/订阅 /退订       开启或关闭每天晚上那条推送
/推送测试         立刻推一条今天的汇报（不用等到推送时间）
/格式             复盘该怎么写（含可复制的模板）
/帮助             这条

复盘内容按行写就行，例如：

比赛: CF Round 1024
类型: VP
过题: 3
罚时: 145
想歪: 看到区间修改就反射性上线段树，其实是差分
A 思路 想了一小时没往图论上想
E 实现 独立想出 P1 边界写挂了

**QOJ 抓不到，但你可以直接粘**：打完 VP 把榜单或提交记录页面的内容
整段复制，跟在 /复盘 后面发过来 —— 过题数、AC 顺序、罚时（带你算出来的
算式）和逐题题号会自动补上，手打的字段优先、不会被覆盖。
正文第一行单独写个 cf，罚时就按 Codeforces 规则（10 分钟/次）算。

认不出来的句子会原样收进「想歪的地方」，一个字都不会丢。
"""

FORMAT_TEXT = """复盘模板（照抄，把冒号后面填上就行）：

比赛: CF Round 1024
日期: 2026-10-06        ← 不填就是今天
类型: VP                ← {kinds}
过题: 3
罚时: 145
排名: 123
顺序: A>C>B
贡献: 想出 A、C
想歪: 看到区间修改就反射性上线段树，其实是差分+前缀和
模式: 看到 n≤2e5 就想数据结构
套路: 反悔贪心用堆维护
补题: E 已 AC；D 还没补

然后逐题一行（题号 + 卡点类型 + 说明）：

A 思路 想了一小时没往图论上想
E 实现 独立想出 P1 边界写挂了

卡点类型：{pk}
写「独立想出」会自动算进 KPI；写 P0/P1/已AC 会自动算补题率。

**懒得回忆就粘榜单/提交记录**：把 QOJ 或 CF 页面上那几十行原样粘在
最后一节，/复盘 会自动补 过题 / AC 顺序 / 罚时（附算式）和逐题题号，
你手打过的字段一个都不会被覆盖。想先看看解析对不对就发 /解析。
"""


# Web 路由前缀。AstrBot 用插件名做命名空间，必须和 metadata.yaml 的 name 一致。
_ROUTE_PREFIX = "astrbot_plugin_xcpc"

class XcpcPlugin(Star):
    """XCPC 备赛助手插件。

    注意：AstrBot 会先尝试 ``plugin_cls(context=..., config=...)``，
    TypeError 时退回 ``plugin_cls(context=...)``，
    所以 ``config`` 必须有默认值。（见 astrbot/core/star/star_manager.py）
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        # AstrBotConfig 继承自 dict；这里按普通 dict 用，免得绑死某个版本的类型名
        self.config = config or {}
        self._push_task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._backend = None    # 懒加载
        # 存储与日志（在 initialize 里真正打开；这里只建对象，
        # 因为 __init__ 里不该做 IO —— 构造失败会让 AstrBot 认为插件加载失败）
        self.db: dbm.Database | None = None
        self.log: logm.Recorder | None = None
        self.store: stm.Store | None = None
        self.syncer: syncm.Syncer | None = None
        self.loop: loopm.Loop | None = None
        self.accounts: accm.AccountService | None = None
        self._db_error = ""

    # ======================================================================
    # 生命周期
    # ======================================================================
    async def initialize(self) -> None:
        """插件激活后由 AstrBot 自动调用。

        定时推送就挂在这里 —— ``context.register_task()`` 已被标记 deprecated，
        官方现在的建议是"在 initialize() 里起后台任务"。

        默认**开**：``daily_push`` 在 ``_conf_schema.json`` 里默认 true。
        以前默认关，是因为调研到"QQ 官方机器人不支持主动推送"、怕用户开了没反应；
        现在确认走的是 **QQ 个人号（NapCat → aiocqhttp）**，主动推送是支持的，
        所以按默认开处理。没人订阅时 ``_push_once()`` 只会写一行日志，不会发东西。
        """
        if self.config.get("daily_push", True):
            self._push_task = asyncio.create_task(self._daily_push_loop())
            logger.info("[xcpc] 每日推送已启动，时间 %s",
                        self.config.get("push_time", "22:30"))

        # 自动同步。
        #
        # ⚠️ 这个配置项原来是**骗人的**：`sync_interval_min` 写在
        # _conf_schema.json 里（默认 60，hint 还写着"0 = 不自动同步"），
        # 但**从来没有代码去读它** —— 用户设了它什么都不会发生。
        # 这种"配置在骗人"比没有配置更糟：用户会以为同步是自动的。
        try:
            interval = int(self.config.get("sync_interval_min") or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval > 0:
            self._sync_task = asyncio.create_task(self._auto_sync_loop(interval))
            logger.info("[xcpc] 自动同步已启动，每 %d 分钟一次", interval)

        await self._setup_storage()
        self._register_account_routes()

    # ======================================================================
    # 自动同步
    # ======================================================================
    async def _auto_sync_loop(self, interval_min: int) -> None:
        """定时同步所有**绑过账号**的用户。

        几个刻意的取舍：
        * **只同步绑过 handle 的人** —— 不然每次都要遍历一堆从没用过的人
        * 第一个循环不立刻跑，先等一个周期（插件刚启动时往往还在配东西）
        * 每个用户之间错开几秒，别在同一秒集中打各站点（会被限流）
        * **任何异常都不能让循环退出** —— 循环死掉的话自动同步就静默失效了，
          而那正是这个功能要解决的问题
        """
        interval = max(300, int(interval_min) * 60)     # 硬下限 5 分钟
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            try:
                if self.store is None:
                    continue
                users = await self.store.users_with_handles()
                for uid in users:
                    try:
                        report = await self.syncer.sync(uid)
                        if self.log:
                            fails = [r.platform for r in report.failures()]
                            self.log.event("autosync.done", user_id=uid,
                                           ok=not fails,
                                           platforms=len(report.results),
                                           failed=",".join(fails))
                    except Exception as exc:             # noqa: BLE001
                        if self.log:
                            self.log.event("autosync.fail", user_id=uid, ok=False,
                                           error_kind="内部错误", detail=str(exc))
                    await asyncio.sleep(3)               # 用户之间错开
            except asyncio.CancelledError:
                raise
            except Exception as exc:                     # noqa: BLE001
                # **兜底**：循环绝不能因为一次异常就退出
                logger.warning("[xcpc] 自动同步这轮出错，下轮继续：%s", exc)
                if self.log:
                    self.log.event("autosync.loop_error", ok=False,
                                   error_kind="内部错误", detail=str(exc))

    # ======================================================================
    # 存储与日志
    # ======================================================================
    def _data_root(self) -> str:
        """数据根目录。

        优先用配置里的 data_root；没配就用插件自己的 data 目录
        （AstrBot 里插件目录可写，而且跟着插件走，卸载时一起清掉）。
        """
        root = str(self.config.get("data_root") or "").strip()
        if root:
            return os.path.abspath(os.path.expanduser(root))
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    async def _setup_storage(self) -> None:
        """打开日志与数据库。**失败不抛异常** —— 插件仍要能加载，
        只是把失败原因记下来，等用户用到相关命令时如实告诉他。"""
        root = self._data_root()
        self.log = logm.Recorder(
            os.path.join(root, "logs", "xcpc.log"),
            level=str(self.config.get("log_level") or "info"),
        )
        ok, detail = self.log.open()
        if not ok:
            logger.warning("[xcpc] 日志打不开：%s", detail)
        self.log.event("boot", detail="插件加载", data_root=root)

        self.db = dbm.Database(os.path.join(root, "xcpc.db"))
        ok, detail = await self.db.open()
        if not ok:
            self._db_error = detail
            # 明确报错，**不静默重建**（坏库里可能还有能救的数据）
            logger.error("[xcpc] 数据库打开失败：%s", detail)
            self.log.event("db.open", ok=False, error_kind="数据库错误", detail=detail)
        else:
            self._db_error = ""
            self.store = stm.Store(self.db)
            self.syncer = syncm.Syncer(
                self.db, self.store, recorder=self.log,
                rate_scale=float(self.config.get("rate_limit_scale") or 1.0))
            self.loop = loopm.Loop(self.db, self.store, recorder=self.log,
                                   syncer=self.syncer)
            self.log.event("db.open", detail=detail)

    async def terminate(self) -> None:
        """插件被禁用/重载时调用 —— 必须把后台任务收干净，否则热重载会漏任务。"""
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
            try:
                await self._push_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        self._push_task = None

        # 自动同步任务也要收干净，否则热重载会漏任务
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        self._sync_task = None

        # 摘掉自己注册的 Web 路由。
        # 只摘「路由前缀匹配 **且** handler 绑在本实例上」的，
        # 免得误删别的插件、或者同一插件的另一个实例的路由。
        try:
            routes = self.context.registered_web_apis
            routes[:] = [
                r for r in routes
                if not (str(r[0]).startswith("/%s/" % _ROUTE_PREFIX)
                        and getattr(r[1], "__self__", None) is self)
            ]
        except Exception as exc:      # 老版本没有 registered_web_apis
            logger.debug("[xcpc] 摘路由跳过：%s", exc)

        if self.db is not None:
            await self.db.close()
            self.db = None
        if self.log is not None:
            self.log.event("shutdown", detail="插件卸载")
            self.log.close()
            self.log = None
        logger.info("[xcpc] 已停止。")

    # ======================================================================
    # WebUI 路由（账号绑定页）
    # ======================================================================
    def _register_account_routes(self) -> None:
        """把账号绑定页要用的 6 条路由挂上去。

        形态照 `astrbot_plugin_listen_music`（B 站登录插件）：
        页面用 iframe 挂在 AstrBot WebUI 里，通过 bridge 调这些接口。
        """
        prefix = "/%s/accounts" % _ROUTE_PREFIX
        specs = [
            ("/link", self.account_link, ["POST"], "用绑定码关联 QQ 号"),
            ("/unlink", self.account_unlink, ["POST"], "断开关联"),
            ("/status", self.account_status, ["GET"], "账号绑定状态"),
            ("/login", self.account_login, ["POST"], "开始登录"),
            ("/login/2fa", self.account_twofa, ["POST"], "提交两步验证码"),
            ("/login/<session_id>/events", self.account_events, ["GET"], "登录状态"),
            ("/login/<session_id>/cancel", self.account_cancel, ["POST"], "取消登录"),
            ("/logout", self.account_logout, ["POST"], "解绑账号"),
            ("/credentials", self.account_credentials, ["POST"], "手动导入 Cookie"),
            ("/log", self.account_log, ["GET"], "读最近日志"),
        ]
        for path, handler, methods, desc in specs:
            try:
                self.context.register_web_api(prefix + path, handler, methods, desc)
            except Exception as exc:
                logger.warning("[xcpc] 路由 %s 注册失败：%s", path, exc)

    # ---- 处理函数 --------------------------------------------------------
    #
    # 关于返回：`astrbot.api.web` 提供 `json_response` / `error_response`。
    # 但它是较新版本才有的模块，而插件声明支持 >=4.26；
    # 这里做**防御性导入**：拿不到就退化成普通 dict（AstrBot 会自己序列化），
    # 而不是让插件在 import 阶段就炸掉。

    # ---- 账号绑定路由（接 core/accounts.py）-----------------------------
    #
    # 页面用 `window.AstrBotPluginPage` 桥调这些接口。
    # **所有返回都是脱敏的** —— 不含 cookie / token / 密码，
    # 这一点由 `Session.public()` 保证（见 core/accounts.py 的注释）。

    async def _acct(self):
        """懒加载登录服务。失败时抛 AccountError，由各路由统一转成 JSON。"""
        if self.accounts is not None:
            return self.accounts
        if self.store is None:
            raise accm.AccountError("数据库不可用：%s" % (self._db_error or "未初始化"),
                                    "数据库错误")

        async def factory(user_id, platform):
            return await self.syncer.make_client(user_id, platform)

        self.accounts = accm.AccountService(
            self.store, recorder=self.log, client_factory=factory)
        return self.accounts

    _NEED_LINK = ("网页还没和你的 QQ 号关联。\n"
                  "  1. 在 QQ 里发 /绑定，拿到一个 6 位绑定码\n"
                  "  2. 在下面「关联 QQ 号」里输入它\n"
                  "（这样设计是为了确保操作的是**你自己的**账号 —— "
                  "网页本身不知道你是谁。）")

    async def account_link(self):
        """POST accounts/link —— 用绑定码换网页令牌。"""
        body = await self._route_body()
        code = str(body.get("code") or "").strip()
        if self.store is None:
            return {"ok": False, "error": "数据库不可用"}
        try:
            token, uid = await self.store.claim_link_code(code)
        except Exception as exc:                        # noqa: BLE001
            return {"ok": False, "error": "认领失败：%s" % exc}
        if not token:
            # uid 位置这时是原因说明
            return {"ok": False, "error": uid}
        if self.log:
            self.log.event("web.link_ok", user_id=uid)
        return {"ok": True, "web_token": token, "user_id": uid}

    async def account_unlink(self):
        """POST accounts/unlink —— 断开网页与 QQ 号的关联。"""
        token = await self._route_token()
        if token and self.store is not None:
            try:
                await self.store.revoke_web_token(token)
            except Exception:
                pass
        return {"ok": True}

    async def account_status(self):
        """GET accounts/status —— 页面加载时读这个。"""
        user_id = await self._route_user_id()
        if not user_id:
            # **不是错误，是还没关联** —— 说清怎么做，别让人猜
            return {"user_id": "", "platforms": [], "need_link": True,
                    "message": self._NEED_LINK}
        try:
            acct = await self._acct()
            payload = await acct.status(user_id)
            payload["routes"] = {"login": "accounts/login",
                                 "twofa": "accounts/login/2fa",
                                 "link": "accounts/link"}
            return payload
        except accm.AccountError as exc:
            return {"error": str(exc), "user_id": user_id, "platforms": []}
        except Exception as exc:                        # noqa: BLE001
            return {"error": "读状态失败：%s" % exc, "user_id": user_id,
                    "platforms": []}

    async def account_login(self):
        """POST accounts/login —— `{platform, fields:{...}}`"""
        user_id = await self._route_user_id()
        if not user_id:
            return {"ok": False, "need_link": True, "error": self._NEED_LINK}
        body = await self._route_body()
        platform = str(body.get("platform") or "").strip()
        fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
        if not platform:
            return {"ok": False, "error": "没指定平台"}
        try:
            acct = await self._acct()
            return await acct.start_login(user_id, platform, fields)
        except accm.AccountError as exc:
            return {"ok": False, "error": str(exc), "error_kind": exc.kind}
        except Exception as exc:                        # noqa: BLE001
            if self.log:
                self.log.event("auth.route_fail", user_id=user_id, platform=platform,
                               ok=False, error_kind="内部错误", detail=str(exc))
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    async def account_events(self):
        """GET accounts/login/<session_id>/events

        这个版本**不做 SSE 流** —— 登录步骤少（最多两步），
        页面轮询 `/login/<sid>` 就够了，SSE 在这里是过度设计。
        保留路由是为了和参考插件的形状一致，将来真要流式再加。
        """
        user_id = await self._route_user_id()
        if not user_id:
            return {"ok": False, "need_link": True, "error": self._NEED_LINK}
        sid = self._route_path_param("session_id")
        try:
            acct = await self._acct()
            return await acct.session_state(sid, user_id)
        except accm.AccountError as exc:
            return {"ok": False, "error": str(exc), "error_kind": exc.kind}

    async def account_twofa(self):
        """POST accounts/login/2fa —— `{session_id, code}`"""
        user_id = await self._route_user_id()
        if not user_id:
            return {"ok": False, "need_link": True, "error": self._NEED_LINK}
        body = await self._route_body()
        try:
            acct = await self._acct()
            return await acct.submit_2fa(str(body.get("session_id") or ""),
                                         user_id, str(body.get("code") or ""))
        except accm.AccountError as exc:
            return {"ok": False, "error": str(exc), "error_kind": exc.kind}

    async def account_cancel(self):
        """POST accounts/login/<session_id>/cancel"""
        user_id = await self._route_user_id()
        if not user_id:
            return {"ok": False, "need_link": True, "error": self._NEED_LINK}
        sid = (self._route_path_param("session_id")
               or str((await self._route_body()).get("session_id") or ""))
        try:
            acct = await self._acct()
            return await acct.cancel(sid, user_id)
        except accm.AccountError as exc:
            return {"ok": False, "error": str(exc), "error_kind": exc.kind}

    async def account_logout(self):
        """POST accounts/logout —— `{platform}`"""
        user_id = await self._route_user_id()
        if not user_id:
            return {"ok": False, "need_link": True, "error": self._NEED_LINK}
        platform = str((await self._route_body()).get("platform") or "").strip()
        if not platform:
            return {"ok": False, "error": "没指定平台"}
        try:
            acct = await self._acct()
            return await acct.logout(user_id, platform)
        except accm.AccountError as exc:
            return {"ok": False, "error": str(exc), "error_kind": exc.kind}

    async def account_credentials(self):
        """POST accounts/credentials —— `{platform, cookies}` 手动导入

        **这不是降级路径，是设计内的路径。** 洛谷的自动登录还没打通，
        而用户在浏览器里登录后复制 Cookie 完全可行，同样能拿到完整数据。
        """
        user_id = await self._route_user_id()
        if not user_id:
            return {"ok": False, "need_link": True, "error": self._NEED_LINK}
        body = await self._route_body()
        platform = str(body.get("platform") or "").strip()
        if not platform:
            return {"ok": False, "error": "没指定平台"}
        try:
            acct = await self._acct()
            return await acct.start_login(user_id, platform,
                                          {"cookies": body.get("cookies") or ""})
        except accm.AccountError as exc:
            return {"ok": False, "error": str(exc), "error_kind": exc.kind}

    async def account_log(self):
        """GET accounts/log?n=40 —— 页面上的日志面板。"""
        user_id = await self._route_user_id()
        if not user_id:
            return {"ok": False, "need_link": True, "error": self._NEED_LINK}
        raw = self._route_param("n")
        try:
            n = int(raw) if raw else 40
        except (TypeError, ValueError):
            n = 40
        if self.log is None:
            return {"lines": ["（日志未初始化）"]}
        return {"lines": self.log.tail(n, user_id=user_id)}

    # ---- 路由参数读取 ----------------------------------------------------
    def _route_request(self):
        """拿 `astrbot.api.web.request`。拿不到就返回 None。"""
        try:
            from astrbot.api.web import request
            return request
        except Exception:
            return None

    def _route_param(self, name: str):
        req = self._route_request()
        if req is None:
            return None
        for attr in ("query_params", "args", "query"):
            holder = getattr(req, attr, None)
            if holder is None:
                continue
            try:
                v = holder.get(name) if hasattr(holder, "get") else None
                if v is not None:
                    return v
            except Exception:
                continue
        try:
            return req.get(name) if hasattr(req, "get") else None
        except Exception:
            return None

    def _route_path_param(self, name: str) -> str:
        for attr in ("path_params", "path"):
            holder = getattr(self._route_request(), attr, None) if self._route_request() else None
            if holder is None:
                continue
            try:
                if hasattr(holder, "get"):
                    v = holder.get(name)
                    if v:
                        return str(v)
            except Exception:
                continue
        return ""

    async def _route_body(self) -> dict:
        """读 POST 的请求体。**必须是 async。**

        ⚠️ AstrBot 暴露的 `request` 是个代理，它的
        `json()` / `body()` / `form()` **全是协程**：

            async def json(self, default=None): ...
            async def body(self) -> bytes: ...

        我第一版写成同步的，干的是 `v = v()` —— 拿到的是一个
        coroutine 对象，`isinstance(v, dict)` 永远为假，于是
        **静默返回 `{}`**。

        表现就是：绑定页里填了 6 位码、点「关联」，回一句
        「没填绑定码」。而**每一层都没报错** —— handler 正常返回、
        HTTP 200、日志干净，只是 body 恒为空。

        顺带一提：原来那句 `except Exception: continue` 连
        `coroutine was never awaited` 的 RuntimeWarning 都吞了。
        **宽泛的 except 会把"我写错了"伪装成"没有数据"。**

        支持三种编码：JSON / 原始 body / form。
        """
        req = self._route_request()
        if req is None:
            return {}

        # ① JSON —— 页面走的就是这条
        try:
            data = await req.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        # ② 原始 body：自己解 JSON，不行就按 form 解
        raw = None
        try:
            raw = await req.body()
        except Exception:
            raw = None
        if isinstance(raw, (bytes, bytearray)) and raw:
            text = raw.decode("utf-8", "replace")
            import json as _json
            try:
                parsed = _json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
            try:
                from urllib.parse import parse_qs
                # 只在**看起来像** query string 时才当 form 解。
                #
                # `parse_qs("{not json", keep_blank_values=True)` 会把
                # `{not json` 变成一个合法的 key，于是垃圾输入伪装成了
                # `{"{not json": ""}` —— 后面 `body.get("code")` 照样是空，
                # 但排查时会被这个假数据带偏。
                # （测试抓到的。）
                stripped = text.strip()
                looks_like_form = (
                    "=" in stripped
                    and not stripped.startswith(("{", "[", "<", '"'))
                )
                if looks_like_form:
                    q = parse_qs(stripped, keep_blank_values=True)
                    if q:
                        return {k: v[0] for k, v in q.items() if v}
            except Exception:
                pass

        # ③ 表单（multipart / urlencoded）
        try:
            form = await req.form()
            if form is not None:
                keys = getattr(form, "keys", None)
                if callable(keys):
                    out = {}
                    for k in form.keys():
                        out[k] = form.get(k)
                    if out:
                        return out
        except Exception:
            pass

        return {}

    async def _route_token(self) -> str:
        """从请求里取网页令牌（页面用 localStorage 存着，每次带过来）。"""
        body = await self._route_body()
        if isinstance(body, dict):
            t = str(body.get("_wt") or "")
            if t:
                return t
        t = str(self._route_param("_wt") or "")
        if t:
            return t
        try:
            ck = getattr(self._route_request(), "cookies", None)
            if isinstance(ck, dict):
                return str(ck.get("xcpc_wt") or "")
        except Exception:
            pass
        return ""

    async def _route_user_id(self) -> str:
        """当前网页会话对应的 **QQ 号**。拿不到就返回空串。

        网页上没有 QQ 号，而 AstrBot 的插件页面 token 只绑到「插件+页面」、
        **不带用户身份**（`build_initial_context` 里只解出 plugin_name /
        page_name / locale）。`request.username` 是 Dashboard 登录名，也不是 QQ 号。
        —— 这两条是我读了 AstrBot 源码确认的，不是猜的。

        所以：**页面必须先认领一个绑定码**（在 QQ 里发 `/绑定` 拿到），
        服务端验证后发一个网页令牌，之后每次请求带上它。

        为什么不在配置里填个 QQ 号了事：那样**谁都填得了**，
        填错了还会把 A 的账号绑成 B 的 —— 多用户系统里最严重的一类错误。
        绑定码至少证明了「能看见这个 QQ 的消息」。

        ⚠️ **返回空串而不是兜底身份。** 上层会因此拿到明确错误，
        而不是静默操作了别人的数据。
        """
        token = await self._route_token()
        if not token or self.store is None:
            return ""
        try:
            return await self.store.resolve_web_token(token)
        except Exception as exc:                        # noqa: BLE001
            if self.log:
                self.log.event("web.token_fail", ok=False, error_kind="数据库错误",
                               detail=str(exc))
            return ""

    # ======================================================================
    # 后端 & 配置
    # ======================================================================
    @property
    def backend(self):
        """按配置返回 file 或 http 后端（只建一次）。"""
        if self._backend is None:
            mode = (self.config.get("backend") or "file").lower()
            if mode == "http":
                self._backend = XcpcHttp(
                    base=self.config.get("http_base") or "http://127.0.0.1:8787",
                    token=self.config.get("http_token") or "",
                    timeout=float(self.config.get("http_timeout", 20)),
                )
            else:
                root = self.config.get("workspace_root") or ""
                if not root:
                    raise RuntimeError(
                        "还没配置 workspace_root —— 去 WebUI 的插件配置里填上 "
                        "xcpc 工作区的绝对路径。")
                self._backend = WorkspaceFS(
                    root=root,
                    handle=self._legacy_handle(),
                )
        return self._backend

    def _legacy_handle(self) -> str:
        """旧 backend（file / http）要的 Codeforces 用户名。

        **拿不到就报错，不猜。**

        这里原来是 `or "<某个写死的用户名>"` —— 那是个 bug：
        别人装了插件、配置留空的话，会拿**别人的**用户名去找数据文件，
        找不到还以为是自己的数据不存在。
        """
        h = str(self.config.get("handle") or "").strip()
        if not h:
            raise RuntimeError(
                "还没配置 handle。\n"
                "只有 backend = file / http 时才需要它 —— "
                "去 WebUI 的插件配置里填上你的 Codeforces 用户名。\n"
                "（用默认的新核心不需要，直接 /绑定 就行。）")
        return h

    def _sender_allowed(self, event: AstrMessageEvent) -> bool:
        """白名单 + 管理员校验。

        这个插件会**往真实文件里写字**，所以默认只认管理员；如果配了
        ``allow_senders``，则只有名单里的 QQ 号能用。两道都过不了就拒绝。
        """
        allow = self.config.get("allow_senders") or []
        sender = str(event.get_sender_id() or "")
        if allow:
            return sender in [str(x) for x in allow]
        if self.config.get("admin_only", True):
            try:
                return bool(event.is_admin())
            except Exception:
                return False
        return True

    def _chunk(self, text: str, limit: int | None = None) -> list:
        """按行切分长文本。

        为什么需要：QQ（OneBot v11 / NapOneBot）单条消息有长度上限，
        超长会被协议端截断或直接发送失败。这里按行边界切，不切坏一行。
        """
        limit = limit or int(self.config.get("max_reply_chars", 900))
        if len(text) <= limit:
            return [text]
        chunks, buf = [], ""
        for line in text.splitlines(keepends=True):
            if len(buf) + len(line) > limit and buf:
                chunks.append(buf.rstrip("\n"))
                buf = ""
            # 单行本身就超长 → 硬切
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            buf += line
        if buf.strip():
            chunks.append(buf.rstrip("\n"))
        return chunks or [text[:limit]]

    async def _reply(self, event: AstrMessageEvent, text: str):
        """发一条（或几条）纯文本，并阻止 LLM 再答一遍。"""
        for part in self._chunk(text):
            yield event.plain_result(part)
        event.should_call_llm(False)

    # ======================================================================
    # 指令：复盘
    # ======================================================================
    @filter.command("复盘")
    async def cmd_review(self, event: AstrMessageEvent):
        """记录一场 VP / 比赛的复盘。"""
        if not self._sender_allowed(event):
            yield event.plain_result("没有权限。")
            event.stop_event()
            return
        body = _strip_command(event.message_str, "复盘")
        async for r in self._do_review(event, body):
            yield r
        event.stop_event()

    @filter.regex(REVIEW_TRIGGER_RE)
    async def auto_review(self, event: AstrMessageEvent):
        """直接发一段结构化的话也能记 —— 不用先打 /复盘。

        触发条件（见模块顶部 REVIEW_TRIGGER_RE）：单独一行「复盘」，
        或者出现「比赛:」「过题:」「罚时:」「排名:」这类强标志。
        """
        text = (event.message_str or "").strip()
        # 明显的指令消息交回给指令处理器，否则 /复盘 会被记两次
        first = text.splitlines()[0].strip() if text else ""
        if first[:1] in ("/", "／", "!", "！", "."):
            return
        if not self._sender_allowed(event):
            return
        async for r in self._do_review(event, text):
            yield r
        event.stop_event()

    async def _do_review(self, event: AstrMessageEvent, body: str):
        """复盘的公共实现：解析 → 落盘 → 回报。

        解析分两步：先按用户手打的键值/逐题行解析，再试着把他**粘进来的
        榜单/提交记录**解析出来**只补空的字段**。顺序不能反 ——
        手打的「过题: 3」是他对着屏幕数的，不能被粘贴内容顶掉。
        合并规则在 ``xcpc_core.merge_contest_into_draft``（那边有自测守着）。
        """
        if not body.strip():
            yield event.plain_result(
                "要记什么？把内容跟在后面，例如：\n"
                "复盘\n比赛: CF Round 1024\n过题: 3\n罚时: 145\n"
                "想歪: 看到区间修改就反射性上线段树\nA 思路 想了一小时\n\n"
                "懒得回忆也行：把 QOJ 的榜单/提交记录整段粘在后面，"
                "过题/罚时/AC 顺序我帮你补。")
            return

        per_fail, text = split_per_fail_directive(body, self._per_fail())
        pasted = await self._parse_paste(text, per_fail)
        try:
            draft = parse_review_text(text, default_date=today_cn())
            report = merge_contest_into_draft(draft, pasted)
            filename = await self._save_review(draft)
        except Exception as exc:  # 绝不让插件因为一次写失败就崩掉
            logger.error("[xcpc] 写复盘失败: %s\n%s", exc, traceback.format_exc())
            yield event.plain_result("写复盘失败了：%s" % exc)
            return

        lines = ["已记录 → `04-review/%s`" % filename, ""]
        lines.append("· 比赛：%s (%s)" % (draft.name, draft.date))
        if draft.solved or draft.penalty or draft.rank:
            lines.append("· 结果：排名 %s ｜ 过题 %s ｜ 罚时 %s"
                         % (draft.rank or "-", draft.solved or "-",
                            draft.penalty or "-"))
        if draft.problems:
            lines.append("· 逐题：%d 条" % len(draft.problems))
        lines.append("· 独立想出：%d 题" % draft.independent_count)

        # 粘贴解析的账要报清楚：哪个数字是自动补的、罚时按什么规则估的
        extra = format_contest_report(report)
        if extra:
            lines.append("")
            lines.extend(extra)
        elif pasted.get("error") and looks_like_paste(text):
            # 只有"看着像粘的"才多嘴 —— 老工作区没 02-tools 时，
            # 普通复盘不该每次都糊一行错误提示
            lines.append("")
            lines.append("（这段看着像是从榜单/提交记录复制来的，"
                         "但自动解析没成：%s）" % pasted["error"])

        if not draft.mine and not draft.upsolve:
            lines.append("")
            lines.append("（没写「贡献」和「补题」—— 这两个是路线图的头号 KPI，"
                         "下次补一句就行）")
        async for r in self._reply(event, "\n".join(lines)):
            yield r

    # ======================================================================
    # 指令：解析（只读，不落盘）
    # ======================================================================
    @filter.command("解析", alias={"粘贴解析", "parse"})
    async def cmd_parse(self, event: AstrMessageEvent):
        """只解析粘贴进来的榜单/提交记录，**不写任何文件**。

        为什么单独做一个指令：他刚打完 VP，第一件想确认的事是"解析出来对不对"
        —— 直接 /复盘 会先落一个文件，解析歪了还得去删。这个指令只回报，
        顺便把"没能理解的行"原样列出来，让解析结果当场可核对。

        顺带这也解释了为什么这里刷得很细：/解析 的输出就是 /复盘 会写进
        文件的东西，先在这儿看一眼比事后翻文件快。
        """
        if not self._sender_allowed(event):
            yield event.plain_result("没有权限。")
            event.stop_event()
            return
        body = _strip_command(event.message_str, "解析", "粘贴解析", "parse")
        if not body.strip():
            yield event.plain_result(
                "把榜单/提交记录粘在后面。例如：\n"
                "解析\n0:12  A  Accepted\n0:45  B  Wrong Answer\n"
                "1:10  B  Accepted\n\n"
                "（CF 的话在正文第一行单独写个 cf，罚时就按 10 分钟/次算）")
            event.stop_event()
            return

        per_fail, text = split_per_fail_directive(body, self._per_fail())
        parsed = await self._parse_paste(text, per_fail)
        async for r in self._reply(event, format_parse_reply(parsed)):
            yield r
        event.stop_event()

    async def _save_review(self, draft: ReviewDraft) -> str:
        save = self.backend.save_review(draft)
        if asyncio.iscoroutine(save):
            return await save
        return save

    # ======================================================================
    # 粘贴解析（榜单 / 提交记录）—— /复盘 和 /解析 共用
    # ======================================================================
    def _per_fail(self) -> int:
        """罚时规则里"每次失败罚几分钟"。

        ICPC / QOJ = 20，Codeforces = 10 —— **默认 20**，因为他的主力场景是
        QOJ 上的组队 VP。这个数字错了会让 KPI 静默算错，所以每一句回报里
        都会带算式和"按几次算的"，让人能当场核对。
        """
        try:
            return int(self.config.get("penalty_per_fail") or DEFAULT_PER_FAIL)
        except (TypeError, ValueError):
            return DEFAULT_PER_FAIL

    async def _parse_paste(self, text: str, per_fail: int) -> dict:
        """让后端解析粘贴内容，**失败也返回 ok=False 的 dict，不抛异常**。

        为什么把异常都吃掉：``/复盘`` 的主路径是用户手打的结构化文本，
        粘贴解析只是加分项 —— 老工作区没有 ``02-tools``、http 后端那边没开机、
        版本旧到没这个端点，都不该让"复盘记不下来"。
        ``/解析`` 会把这个 error 原样显示出来，所以不会静默失败。
        """
        try:
            backend = self.backend
        except Exception as exc:      # workspace_root 没配之类的
            return empty_contest_result("后端不可用：%s" % exc)

        fn = getattr(backend, "parse_contest_text", None)
        if fn is None:
            return empty_contest_result("这个后端还不支持粘贴解析")
        try:
            result = fn(text, per_fail=per_fail)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            logger.error("[xcpc] 解析粘贴内容失败: %s\n%s",
                         exc, traceback.format_exc())
            return empty_contest_result("解析失败：%s" % exc)
        if not isinstance(result, dict):
            return empty_contest_result(
                "解析返回值不是 dict（%s）" % type(result).__name__)
        return result

    # ======================================================================
    # 指令：随手记
    # ======================================================================
    @filter.command("随手记", alias={"记", "inbox"})
    async def cmd_note(self, event: AstrMessageEvent):
        """把想到的东西丢进收件箱（03-log/inbox.md）。"""
        if not self._sender_allowed(event):
            yield event.plain_result("没有权限。")
            event.stop_event()
            return
        text = _strip_command(event.message_str, "随手记", "记", "inbox")
        if not text.strip():
            yield event.plain_result("要记什么？用法：/随手记 看到区间修改先想差分")
            event.stop_event()
            return
        try:
            res = self.backend.append_inbox(text)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:
            logger.error("[xcpc] 写收件箱失败: %s", exc)
            yield event.plain_result("写收件箱失败了：%s" % exc)
            event.stop_event()
            return
        yield event.plain_result("已进收件箱（%d 字）。回来跟我说「整理收件箱」就行。"
                                 % len(text))
        event.stop_event()

    # ======================================================================
    # 指令：今天
    # ======================================================================
    @filter.command("今天", alias={"today", "待办"})
    async def cmd_today(self, event: AstrMessageEvent):
        """今天那组任务（读 00-plan/sprint.md）。"""
        try:
            info = self.backend.today_tasks()
            if asyncio.iscoroutine(info):
                info = await info
        except Exception as exc:
            logger.error("[xcpc] 读今天任务失败: %s", exc)
            yield event.plain_result("读不到今天的安排：%s" % exc)
            event.stop_event()
            return

        items = info.get("items") or []
        pending = [i for i in items if not i.get("done")]
        lines = ["📅 %s ｜ %s" % (info.get("date"), info.get("title") or "")]
        lines.append("")
        if not items:
            lines.append("（这一组是空的）")
        else:
            for i in pending[:12]:
                lines.append("☐ %s" % i.get("text"))
            if len(pending) > 12:
                lines.append("… 还有 %d 条" % (len(pending) - 12))
            if not pending:
                lines.append("✅ 这一组勾完了。")
            lines.append("")
            lines.append("完成 %s / %s" % (info.get("done", 0), info.get("total", 0)))
        async for r in self._reply(event, "\n".join(lines)):
            yield r
        event.stop_event()

    # ======================================================================
    # 指令：状态
    # ======================================================================
    @filter.command("状态", alias={"status", "st"})
    async def cmd_status(self, event: AstrMessageEvent):
        """核心指标：rating / 已 AC / 连续天数 / 个人 KPI。"""
        try:
            st = self.backend.status()
            if asyncio.iscoroutine(st):
                st = await st
        except Exception as exc:
            logger.error("[xcpc] 读状态失败: %s", exc)
            yield event.plain_result("读不到状态：%s" % exc)
            event.stop_event()
            return

        try:
            legacy_handle = self._legacy_handle()
        except RuntimeError as exc:
            yield event.plain_result(str(exc))
            event.stop_event()
            return
        text = format_status(st, legacy_handle)

        # 可选：渲染成图片（AstrBot 自带文转图；没配就自动退回文本）
        if self.config.get("status_as_image", False):
            try:
                url = await self.text_to_image(text)
                yield event.image_result(url)
                event.should_call_llm(False)
                event.stop_event()
                return
            except Exception as exc:
                logger.warning("[xcpc] 文转图失败，退回纯文本: %s", exc)

        async for r in self._reply(event, text):
            yield r
        event.stop_event()

    # ======================================================================
    # 指令：题单
    # ======================================================================
    @filter.command("题单", alias={"list", "lists"})
    async def cmd_lists(self, event: AstrMessageEvent):
        """当前题单的前几道。"""
        try:
            lists = self.backend.problem_lists()
            if asyncio.iscoroutine(lists):
                lists = await lists
        except Exception as exc:
            logger.error("[xcpc] 读题单失败: %s", exc)
            yield event.plain_result("读不到题单：%s" % exc)
            event.stop_event()
            return

        if not lists:
            yield event.plain_result(
                "还没有题单。在电脑上跑一次 `python 02-tools/train_list.py` 就有了。")
            event.stop_event()
            return

        limit = int(self.config.get("list_preview", 5))
        out = []
        for lst in lists[:3]:
            items = lst.get("items") or []
            done = sum(1 for i in items if i.get("done"))
            out.append("📋 %s（%d/%d）" % (lst.get("title"), done, len(items)))
            todo = [i for i in items if not i.get("done")][:limit]
            if not todo:
                out.append("  ✅ 全做完了")
            for it in todo:
                out.append("  · [%s] %s %s"
                           % (it.get("rating", "?"), it.get("label", ""),
                              it.get("name", "")))
                if it.get("url"):
                    out.append("    %s" % it["url"])
            out.append("")
        async for r in self._reply(event, "\n".join(out)):
            yield r
        event.stop_event()

    # ======================================================================
    # 指令：刷新数据
    # ======================================================================
    @filter.command("刷新", alias={"refresh"})
    async def cmd_refresh(self, event: AstrMessageEvent):
        """重新抓 CF 数据并重跑诊断（只有 file 后端支持）。"""
        if not self._sender_allowed(event):
            yield event.plain_result("没有权限。")
            event.stop_event()
            return
        if getattr(self.backend, "backend_name", "") != "file":
            yield event.plain_result(
                "http 后端请在工作区那边点看板上的「更新数据」，"
                "或者用 /api/refresh。")
            event.stop_event()
            return

        yield event.plain_result("开始抓取，可能要一两分钟…")
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._run_refresh)
        except Exception as exc:
            logger.error("[xcpc] 刷新失败: %s\n%s", exc, traceback.format_exc())
            yield event.plain_result("刷新失败：%s" % exc)
            event.stop_event()
            return
        yield event.plain_result(result)
        event.stop_event()

    def _run_refresh(self) -> str:
        """同步跑 cf_fetch.py + cf_analyze.py（放到线程池里，别堵事件循环）。"""
        import subprocess
        import sys

        root = self.backend.root
        tools = os.path.join(root, "02-tools")
        handle = self._legacy_handle()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for script, label in (("cf_fetch.py", "抓取 CF 数据"),
                              ("cf_analyze.py", "重跑诊断")):
            path = os.path.join(tools, script)
            if not os.path.exists(path):
                return "找不到 %s" % path
            proc = subprocess.run(
                [sys.executable, path, handle],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=300, env=env, cwd=tools)
            if proc.returncode != 0:
                return "%s 失败（退出码 %s）—— 检查网络。" % (label, proc.returncode)
        return "✅ 数据已更新。"

    # ======================================================================
    # 指令：订阅 / 退订 / 帮助 / 格式
    # ======================================================================
    @filter.command("订阅", alias={"subscribe"})
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """订阅每天晚上那条推送。"""
        if not self._sender_allowed(event):
            yield event.plain_result("没有权限。")
            event.stop_event()
            return
        umo = event.unified_msg_origin
        subs = await self._load_subscribers()
        if umo not in subs:
            subs.append(umo)
            await self._save_subscribers(subs)
        yield event.plain_result(
            "已订阅。每天 %s 推一条（记得在插件配置里把「每日推送」打开）。"
            % self.config.get("push_time", "22:30"))
        event.stop_event()

    @filter.command("退订", alias={"unsubscribe"})
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """取消推送。"""
        umo = event.unified_msg_origin
        subs = await self._load_subscribers()
        if umo in subs:
            subs.remove(umo)
            await self._save_subscribers(subs)
        yield event.plain_result("已退订。")
        event.stop_event()

    @filter.command("推送测试", alias={"pushtest", "测试推送"})
    async def cmd_push_test(self, event: AstrMessageEvent):
        """立刻推一条今天的汇报 —— 不用等到 22:30。

        存在的理由：每日推送是**定时**触发的，装完插件根本没法当场验证。
        以前只能"等到晚上看有没有来"，来了才知道对不对；
        QQ 个人号（NapCat/aiocqhttp）支持主动消息，所以这件事完全可以当场测。
        """
        if not self._sender_allowed(event):
            yield event.plain_result("没有权限。")
            event.stop_event()
            return
        if not self.config.get("daily_push", True):
            yield event.plain_result(
                "每日推送现在是关的 —— 先在 WebUI 插件配置里把「每日推送」打开，再测。")
            event.stop_event()
            return
        try:
            sent, total = await self._push_once()
        except Exception as exc:
            logger.error("[xcpc] 测试推送失败: %s\n%s", exc, traceback.format_exc())
            yield event.plain_result("测试推送失败：%s" % exc)
            event.stop_event()
            return
        if not total:
            yield event.plain_result(
                "订阅列表是空的，没东西可推 —— 先在**想收到推送的那个会话**里"
                "发一次 /订阅（私聊就订阅私聊，群就订阅群），再回来测。")
        elif sent == total:
            yield event.plain_result("✅ 推送完成：%d/%d 个会话都发出去了。" % (sent, total))
        else:
            yield event.plain_result(
                "⚠️ 只成功了 %d/%d 个会话。失败的看 AstrBot 日志 —— "
                "最常见的原因是那个平台不支持主动消息（QQ **官方**机器人就不支持）。"
                % (sent, total))
        event.stop_event()

    @filter.command("格式", alias={"模板", "format"})
    async def cmd_format(self, event: AstrMessageEvent):
        """复盘该怎么写。"""
        text = FORMAT_TEXT.format(kinds="/".join(KIND_CHOICES),
                                  pk=" / ".join(PROBLEM_KINDS))
        async for r in self._reply(event, text):
            yield r
        event.stop_event()

    # 注意：`cmd_help` 定义在上面「帮助」那一节里。
    # **不要在这里再定义一个** —— Python 里后定义的会覆盖先定义的，
    # 我加新版时就踩过：新的写在前面，旧的写在后面，结果生效的是旧的。
    # （同一个坑之前在 pages/accounts/app.js 踩过一次，见那里的注释。）

    # ======================================================================
    # 多用户：每个命令都按 QQ 号隔离
    # ======================================================================
    def _uid(self, event: AstrMessageEvent) -> str:
        """当前用户的 QQ 号。**拿不到就返回空串，不猜。**

        所有数据访问都用它做隔离键。空串会让 store 的闸门抛错，
        调用方把它变成一句明确的话（"拿不到你的用户标识"）——
        而不是静默操作了一个错的身份。

        ⚠️ **不要退回用 `unified_msg_origin`。** 我原来写了个这样的"退化"：
        拿不到 sender_id 就用会话 ID 顶上。**那是错的** ——
        在群里 `unified_msg_origin` 标识的是**群**，不是人
        （形如 `aiocqhttp:GroupMessage:123456`，123456 是群号）。
        用它当身份意味着**同一个群里所有人共用同一个身份**，
        绑的账号会全部串在一起 —— 这正是"会绑错人"那类错误。

        这个 bug 是命令接线测试抓出来的：sender 为空时 /绑定 居然
        照样生成了码。宁可明确报错，也不要猜一个身份。
        """
        try:
            return str(event.get_sender_id() or "").strip()
        except Exception:
            return ""

    def _store_or_error(self):
        """返回 (store, None) 或 (None, 错误文案)。"""
        if self.store is None:
            return None, ("数据库不可用：%s\n"
                          "看看 /日志 20，或检查 data_root 配置和目录权限。"
                          % (self._db_error or "未初始化"))
        return self.store, None

    _PLATFORM_ALIAS = {
        "cf": "codeforces", "codeforces": "codeforces",
        "atc": "atcoder", "atcoder": "atcoder",
        "qoj": "qoj", "luogu": "luogu", "洛谷": "luogu",
    }
    _PLATFORM_NAME = {"codeforces": "CF", "atcoder": "AtCoder",
                      "qoj": "QOJ", "luogu": "洛谷"}

    @filter.command("同步", alias={"sync"})
    async def cmd_sync(self, event: AstrMessageEvent):
        """立即同步各平台。用法：/同步 [cf|atcoder|qoj|luogu]"""
        store, err = self._store_or_error()
        if err:
            yield event.plain_result(err)
            return

        uid = self._uid(event)
        try:
            await store.ensure_user(uid)
        except ValueError as exc:
            yield event.plain_result("拿不到你的用户标识，没法同步：%s" % exc)
            return

        text = (event.message_str or "").strip()
        wanted = None
        m = re.search(r"(\S+)\s*$", text)
        if m and m.group(1).lower() in self._PLATFORM_ALIAS:
            wanted = [self._PLATFORM_ALIAS[m.group(1).lower()]]

        yield event.plain_result("开始同步…（顺序拉，可能要等十几秒）")
        try:
            report = await self.syncer.sync(uid, wanted)
        except Exception as exc:                       # noqa: BLE001
            if self.log:
                self.log.event("sync.cmd_fail", user_id=uid, ok=False,
                               error_kind="内部错误", detail=str(exc))
            yield event.plain_result("同步出错：%s\n看 /日志 20 有细节。" % exc)
            return

        lines = ["同步结果", report.text()]
        fails = report.failures()
        if fails:
            kinds = sorted({f.error_kind for f in fails})
            lines.append("")
            lines.append("失败类型：" + "、".join(kinds))
            if "凭据失效" in kinds:
                lines.append("→ 有平台需要登录或重新登录，见 /绑定")
            if "挑战未过" in kinds:
                lines.append("→ 被站点风控挡了，过一会儿再试；洛谷可改用手动导入 Cookie")
            if "限流" in kinds:
                lines.append("→ 被限速了，等几分钟再同步")
        yield event.plain_result("\n".join(lines))

    @filter.command("绑定", alias={"bind", "账号"})
    async def cmd_bind(self, event: AstrMessageEvent):
        """生成一个绑定码，拿到网页上去认领。

        为什么要走这一步：网页没有 QQ 号，而 AstrBot 的插件页面 token
        **不带用户身份**（只绑到「插件+页面」—— 读源码确认的）。
        所以必须让你证明「这个网页背后的人确实是这个 QQ」，
        绑定码就是证明：只有能看到这条 QQ 消息的人才拿得到。
        """
        store, err = self._store_or_error()
        if err:
            yield event.plain_result(err)
            return
        uid = self._uid(event)
        try:
            await store.ensure_user(uid)
            code = await store.create_link_code(uid)
        except ValueError as exc:
            yield event.plain_result("拿不到你的用户标识：%s" % exc)
            return
        except Exception as exc:                        # noqa: BLE001
            yield event.plain_result("生成绑定码失败：%s\n看 /日志 20。" % exc)
            return

        yield event.plain_result(
            "你的绑定码：%s\n"
            "\n"
            "接下来：\n"
            "  1. 打开 AstrBot WebUI → 插件 → XCPC 备赛助手 → 账号绑定\n"
            "  2. 在页面顶部「关联 QQ 号」里输入这个码\n"
            "  3. 然后就能在页面里绑定 CF / AtCoder / QOJ / 洛谷 了\n"
            "\n"
            "⚠️ 10 分钟内有效，且只能用一次。\n"
            "为什么要有这一步：网页本身不知道你是谁，而绑定码只有能看到"
            "这条消息的人才拿得到 —— 这样才不会把别人的账号绑到你名下。"
            % code)

    @filter.command("我的状态", alias={"mystatus", "数据"})
    async def cmd_mydata(self, event: AstrMessageEvent):
        """看已同步的数据概况。"""
        store, err = self._store_or_error()
        if err:
            yield event.plain_result(err)
            return
        uid = self._uid(event)
        try:
            await store.ensure_user(uid)
            handles = await store.handles(uid)
            stats = await store.platform_stats(uid)
            states = await store.all_sync_states(uid)
            contests = await store.list_contests(uid, limit=3)
        except ValueError as exc:
            yield event.plain_result("拿不到你的用户标识：%s" % exc)
            return

        by_pf = {s["platform"]: s for s in stats}
        lines = ["我的数据（QQ %s）" % uid, ""]
        for p in ("codeforces", "atcoder", "qoj", "luogu"):
            h = handles.get(p) or "（未绑定）"
            s = by_pf.get(p)
            line = "  %-8s %-16s" % (self._PLATFORM_NAME[p], h)
            line += ("提交 %d（AC %d）" % (s["total"], s["ac"] or 0)) if s else "还没同步过"
            st = states.get(p)
            if st and st.get("error_kind"):
                line += "  ⚠ %s" % st["error_kind"]
            elif st and st.get("last_ok_at"):
                line += "  ✓ %s" % str(st["last_ok_at"])[5:16]
            lines.append(line)

        if contests:
            lines.append("")
            lines.append("最近比赛：")
            for c in contests:
                d = (c["name"] or c["contest_id"] or "")[:34]
                extra = ""
                if c["rank"]:
                    extra += " rank %s" % c["rank"]
                if c["rating_delta"] is not None:
                    extra += " Δ%+d" % c["rating_delta"]
                lines.append("  %s%s" % (d, extra))
        if not any(by_pf.values()):
            lines += ["", "还没有数据 —— 先 /绑定 填 handle，然后 /同步。"]
        yield event.plain_result("\n".join(lines))

    @filter.command("比赛", alias={"contests"})
    async def cmd_contests(self, event: AstrMessageEvent):
        """看比赛记录（和练习提交是分开的两条流）。"""
        store, err = self._store_or_error()
        if err:
            yield event.plain_result(err)
            return
        uid = self._uid(event)
        try:
            await store.ensure_user(uid)
            rows = await store.list_contests(uid, limit=15)
        except ValueError as exc:
            yield event.plain_result("拿不到你的用户标识：%s" % exc)
            return
        if not rows:
            yield event.plain_result(
                "还没有比赛记录。\n"
                "· CF 的 rated 比赛会在 /同步 时一起抓\n"
                "· AtCoder 的比赛是从提交记录反推的"
                "（参加了但一道没提交的看不到，排名和 rating 变化也拿不到）\n"
                "· QOJ 的比赛记录还没做")
            return
        lines = ["比赛记录（最近 %d 场）" % len(rows), ""]
        for c in rows:
            d = (c["name"] or c["contest_id"] or "")[:38]
            extra = []
            if c["rank"]:
                extra.append("rank %s" % c["rank"])
            if c["solved"]:
                extra.append("过 %s 题" % c["solved"])
            if c["rating_delta"] is not None:
                extra.append("Δ%+d" % c["rating_delta"])
            lines.append("  [%s] %s" % (self._PLATFORM_NAME.get(c["platform"],
                                                              c["platform"]), d))
            if extra:
                lines.append("        " + "  ".join(extra))
        yield event.plain_result("\n".join(lines))

    @filter.command("日志", alias={"log"})
    async def cmd_log(self, event: AstrMessageEvent):
        """最近 n 条日志。凭据已打码，可以直接贴出来。"""
        n = 20
        m = re.search(r"(\d+)\s*$", (event.message_str or "").strip())
        if m:
            n = max(1, min(int(m.group(1)), 100))
        if self.log is None:
            yield event.plain_result("日志没初始化（可能 data_root 不可写）。")
            return
        uid = self._uid(event)
        lines = self.log.tail(n, user_id=uid)
        if not lines:
            yield event.plain_result("还没有日志。")
            return
        body = "\n".join(lines)
        yield event.plain_result("最近 %d 条日志（Cookie/密码/token 已打码）：\n%s"
                                 % (len(lines), body))

    @filter.command("方案", alias={"plan", "下一步", "今天做什么"})
    async def cmd_plan(self, event: AstrMessageEvent):
        """跑一轮循环：聚合 → 汇总 → 模型评估 → 给你下一步。

        这是这个插件的核心。**它会真的调模型**（花 token），所以默认**不**自动同步 ——
        用 /同步 先把数据弄新，再来要方案。
        """
        store, err = self._store_or_error()
        if err:
            yield event.plain_result(err)
            return
        uid = self._uid(event)
        try:
            await store.ensure_user(uid)
        except ValueError as exc:
            yield event.plain_result("拿不到你的用户标识：%s" % exc)
            return

        text = (event.message_str or "").strip()
        # 把命令词之后的剩余部分当成"额外要求"
        extra = ""
        m = re.search(r"(?:方案|plan|下一步|今天做什么)\s*(.*)$", text, re.S)
        if m and m.group(1).strip():
            extra = m.group(1).strip()

        has_data = await store.count_submissions(uid) > 0
        if not has_data:
            yield event.plain_result(
                "还没有数据，先做两步：\n"
                "  1. /绑定      —— 去网页填 handle\n"
                "  2. /同步      —— 把记录拉下来\n"
                "然后再 /方案。\n\n"
                "（**不是「你的水平是零」，是「我还没拿到数据」** —— "
                "这两件事完全不同。）")
            return

        yield event.plain_result("正在汇总数据并问模型…（十几秒）")
        try:
            result = await self.loop.run(
                self.context, uid, umo=event.unified_msg_origin,
                model_id=str(self.config.get("llm_provider_id") or ""),
                constraints=extra,
                auto_sync=False,          # 先同步再要方案，别在这里偷偷拉
                max_minutes=int(self.config.get("plan_max_minutes") or 200))
        except Exception as exc:                       # noqa: BLE001
            if self.log:
                self.log.event("plan.cmd_fail", user_id=uid, ok=False,
                               error_kind="内部错误", detail=str(exc))
            yield event.plain_result("生成方案时出错：%s\n看 /日志 30。" % exc)
            return

        if not result.ok:
            # **不降级**：明确报错。有上一版就说明是旧的，没有就只说错误。
            head = "没能生成方案：[%s] %s" % (result.error_kind, result.detail)
            if result.used_previous:
                head += ("\n\n下面是**你上一次的方案**，不是新生成的。"
                         "修好问题后再 /方案。")
            else:
                head += "\n\n看 /日志 30 有细节。"
            yield event.plain_result(head)
            if result.plan:
                for chunk in self._chunk(result.plan.to_text()):
                    yield event.plain_result(chunk)
            return

        for chunk in self._chunk(result.plan.to_text()):
            yield event.plain_result(chunk)

    @filter.command("反馈", alias={"feedback", "说一句"})
    async def cmd_feedback(self, event: AstrMessageEvent):
        """记一句反馈，下一轮方案会带上它。

        比如：/反馈 今天有点累，明天少安排点
              /反馈 这题我看了题解才会
        """
        store, err = self._store_or_error()
        if err:
            yield event.plain_result(err)
            return
        uid = self._uid(event)
        text = (event.message_str or "").strip()
        m = re.search(r"(?:反馈|feedback|说一句)\s*(.*)$", text, re.S)
        body = (m.group(1).strip() if m else "")
        if not body:
            yield event.plain_result(
                "/反馈 <一句话>  —— 记一句，下一轮方案会带上\n"
                "  例：/反馈 今天有点累，明天少安排点\n"
                "  例：/反馈 这题我看了题解才会")
            return
        try:
            await store.ensure_user(uid)
            await self.loop.feedback(uid, body)
        except ValueError as exc:
            yield event.plain_result("拿不到你的用户标识：%s" % exc)
            return
        yield event.plain_result("记下了：「%s」\n下次 /方案 会带上这句。" % body[:100])

    @filter.command("总结")
    async def cmd_summary(self, event: AstrMessageEvent):
        """看**模型看到的那份汇总**（不调模型，不花 token）。

        用途：方案看着不对时，先看看喂给模型的数据对不对 ——
        大部分"模型胡说"其实是"数据没同步"或"口径没标注"。
        """
        store, err = self._store_or_error()
        if err:
            yield event.plain_result(err)
            return
        uid = self._uid(event)
        try:
            await store.ensure_user(uid)
            prep = await self.loop.prepare(uid, auto_sync=False)
        except ValueError as exc:
            yield event.plain_result("拿不到你的用户标识：%s" % exc)
            return
        except Exception as exc:                       # noqa: BLE001
            yield event.plain_result("汇总失败：%s" % exc)
            return
        for chunk in self._chunk(prep["summary"].to_text(max_chars=3500)):
            yield event.plain_result(chunk)

    @filter.command("自检", alias={"selfcheck", "诊断"})
    async def cmd_selfcheck(self, event: AstrMessageEvent):
        """装完先跑这个：一次把"哪儿不对、怎么修"说清。

        比逐个试快得多 —— 不然 `/同步` 失败是网络问题还是没绑定、
        `/方案` 失败是没模型还是没数据，每个都要猜。
        """
        uid = self._uid(event)
        try:
            report = await scm.run(
                store=self.store, db=self.db, recorder=self.log,
                context=self.context,
                config=self.config,
                umo=event.unified_msg_origin,
                user_id=uid)
        except Exception as exc:                       # noqa: BLE001
            yield event.plain_result(
                "自检本身出错了（这挺尴尬的）：%s: %s\n请把这条报给我。"
                % (type(exc).__name__, exc))
            return
        for chunk in self._chunk(report.to_text()):
            yield event.plain_result(chunk)

    @filter.command("打卡", alias={"done", "做完了"})
    async def cmd_done(self, event: AstrMessageEvent):
        """标记今天做完了。用法：/打卡 [一句话]

        这是**循环的第 ⑤ 环** —— 没有它的话，计划引擎永远不知道
        上一版方案有没有被执行，那它就不是"动态调整"，只是每天重新猜一次。
        """
        yield await self._log_task(event, "done", "/打卡")

    @filter.command("做了一半", alias={"partial", "半"})
    async def cmd_partial(self, event: AstrMessageEvent):
        """标记今天只做了一部分。"""
        yield await self._log_task(event, "partial", "/做了一半")

    @filter.command("没做", alias={"skip", "skip今天"})
    async def cmd_skip(self, event: AstrMessageEvent):
        """标记今天没做。

        **如实打卡比"看起来努力"有用得多。** 连着几天没做的话，
        该改的是计划（量排多了），不是你的意志力 ——
        但前提是系统知道真实情况。
        """
        yield await self._log_task(event, "skipped", "/没做")

    async def _log_task(self, event: AstrMessageEvent, status: str, cmd: str):
        store, err = self._store_or_error()
        if err:
            return event.plain_result(err)
        uid = self._uid(event)
        text = (event.message_str or "").strip()
        m = re.search(re.escape(cmd.lstrip("/")) + r"\s*(.*)$", text, re.S)
        note = (m.group(1).strip() if m else "")
        try:
            await store.ensure_user(uid)
            await store.log_task(uid, status, note=note)
            stats = await store.task_stats(uid, days=14)
        except ValueError as exc:
            return event.plain_result("拿不到你的用户标识：%s" % exc)
        except Exception as exc:                        # noqa: BLE001
            return event.plain_result("记录失败：%s\n看 /日志 20。" % exc)

        if self.log:
            self.log.event("task.log", user_id=uid, status=status,
                           chars=len(note))

        label = {"done": "做完了", "partial": "做了一半",
                 "skipped": "没做"}[status]
        lines = ["记下了：今天%s。" % label]
        if stats.get("rate") is not None:
            lines.append("最近 %d 天：完成 %d，一半 %d，没做 %d（执行率 %.0f%%）"
                         % (stats["days"], stats["done"], stats["partial"],
                            stats["skipped"], stats["rate"] * 100))
        if status == "skipped" and stats.get("skipped", 0) >= 3:
            # 连着没做 → 该动的是计划，不是催人
            lines.append("")
            lines.append("连着几天没做 —— 下次 /方案 时直接说一句"
                         "「最近量太多了」，让它把计划压小一点。"
                         "**连着做不完的计划等于没有计划。**")
        if status in ("done", "partial"):
            lines.append("下次 /方案 会把这条算进去。")
        return event.plain_result("\n".join(lines))

    @filter.command("题库", alias={"bank", "problems"})
    async def cmd_bank(self, event: AstrMessageEvent):
        """拉取题库标注（难度 + 标签）。

        **这是全局的**（同一道题的难度标签对所有人都一样），所以拉一次就够。
        `/同步` 会在它是空的时候自动拉 —— 这个命令是给"想强制刷新"用的。

        没有题库的话：按标签的分析、**难度回避判定**、候选题，
        全都用不了。
        """
        store, err = self._store_or_error()
        if err:
            yield event.plain_result(err)
            return
        text = (event.message_str or "").strip()
        m = re.search(r"(?:题库|bank|problems)\s*(\S+)?", text, re.I)
        pf = ""
        if m and m.group(1):
            pf = self._PLATFORM_ALIAS.get(m.group(1).lower(), "")
        pf = pf or "codeforces"

        try:
            before = await store.count_problems(pf)
        except Exception:
            before = 0
        yield event.plain_result(
            "正在拉 %s 的题库标注…（CF 有两万多题，第一次要十几秒）" % pf)
        try:
            ok, detail, added = await self.syncer.sync_problems(pf)
        except Exception as exc:                        # noqa: BLE001
            yield event.plain_result("拉取失败：%s\n看 /日志 20。" % exc)
            return
        after = await store.count_problems(pf)
        if ok:
            yield event.plain_result(
                "题库已更新：%s\n现在共 %d 题（新增 %d）。\n"
                "\n有了它才能判「难度回避」、也才有候选题可推。"
                % (pf, after, added))
        else:
            yield event.plain_result(
                "题库拉取失败：[%s]\n"
                "现有 %d 题。\n"
                "**没有题库的话，按标签的分析和候选题都用不了** —— "
                "但做题记录不受影响。" % (detail, after))

    @filter.command("帮助", alias={"help", "xcpc"})
    async def cmd_help(self, event: AstrMessageEvent):
        """分组帮助。**写清每个平台的门槛** —— 不写清你以后会被自己坑。"""
        text = (event.message_str or "").strip()
        topic = ""
        m = re.search(r"(?:帮助|help|xcpc)\s*(\S+)", text, re.I)
        if m:
            topic = m.group(1).strip()

        if topic in ("同步", "sync", "平台", "platform"):
            yield event.plain_result(
                "/同步 [平台]  —— 立即同步\n\n"
                "  不带参数：同步所有已绑定的平台\n"
                "  带参数：  /同步 cf    /同步 atcoder    /同步 qoj    /同步 洛谷\n\n"
                "各平台的门槛（实测，不是猜的）：\n"
                "  CF       不需要登录，官方公开 API\n"
                "  AtCoder  不需要登录，但走的是**社区服务**不是官方\n"
                "           ⚠️ 它的 API 里**没有算法标签**，所以标签是空的\n"
                "  QOJ      **必须登录**。提交记录和榜单未登录时看不到\n"
                "  ⚠️ 洛谷   **必须登录**，而且自动登录还没打通，要手动导入 Cookie\n\n"
                "同步是**增量**的：只拉上次成功之后的新记录，不会重复。\n"
                "失败时断点**不会**被推进，所以下次从正确的位置续上。\n\n"
                "失败分类（一眼看出该怎么办）：\n"
                "  凭据失效     → 去 /绑定 重新登录\n"
                "  限流         → 等几分钟再试\n"
                "  挑战未过     → 被站点风控挡了，过会儿再试\n"
                "  页面结构变化 → 对面改版了，跟我说一声\n"
                "  网络不可达   → 网络问题\n"
                "  解析失败     → 拿到了响应但解析不出来，多半也是改版")
            return

        if topic in ("绑定", "bind", "登录", "账号"):
            yield event.plain_result(
                "/绑定  —— 拿一个绑定码\n\n"
                "网页本身不知道你是谁（AstrBot 的插件页面不带用户身份），\n"
                "所以第一次用网页前要证明一次：\n"
                "  1. 在这里发 /绑定，拿到 6 位码\n"
                "  2. 打开 AstrBot WebUI → 插件 → XCPC 备赛助手 → 账号绑定\n"
                "  3. 在页面顶部「关联 QQ 号」里输入那个码\n\n"
                "码 10 分钟内有效，且只能用一次。\n\n"
                "为什么不直接在配置里填 QQ 号：那样谁都填得了，\n"
                "填错了还会把别人的账号绑到你名下。")
            return

        if topic in ("日志", "log"):
            yield event.plain_result(
                "/日志 [n]  —— 看最近 n 条日志（默认 20）\n\n"
                "什么时候用：同步失败了、想看看它到底在干什么。\n"
                "凭据（Cookie / 密码 / token）在写进日志**之前**就已经打码，\n"
                "所以可以直接贴出来求助。")
            return

        yield event.plain_result(
            "XCPC 备赛助手\n"
            "在 QQ 里同步你的做题记录，然后每天告诉你下一步做什么。\n"
            "\n"
            "【先做这个】\n"
            "  /自检             装完先跑这个：哪儿不对、怎么修\n"            "  /绑定             拿绑定码（网页关联用）\n"
            "\n"
            "【数据】\n"
            "  /同步 [平台]      立即同步\n"
            "  /我的状态         已同步的数据概况\n"
            "  /比赛             比赛记录（和练习提交分开的两条流）\n"
            "  /日志 [n]         最近日志，出问题时看这个\n"
            "\n"
            "【练什么】这是核心\n"
            "  /方案             跑一轮：汇总数据 → 问模型 → 给你下一步\n"
            "                    可以带要求：/方案 这周别安排 VP\n"
            "  /打卡 [一句话]    今天做完了     ← 循环靠它闭环\n"
            "  /做了一半         只做了一部分\n"
            "  /没做 [原因]      今天没做\n"
            "  /反馈 <一句话>    记一句感受（例：/反馈 今天有点累）\n"
            "  /总结             看**模型看到的那份汇总**（不花 token）\n"
            "\n"
            "【复盘】\n"
            "  /解析 <粘贴>      QOJ 榜单/提交记录直接粘进来\n"
            "                    自动补全过题、罚时、AC 顺序\n"
            "  /复盘             写一篇复盘\n"
            "  /随手记 <内容>    记一笔，不用管格式\n"
            "  /今天  /状态  /题单   原有功能\n"
            "\n"
            "【订阅】\n"
            "  /订阅  /退订      每日推送\n"
            "  /推送测试         立刻推一条试试\n"
            "\n"
            "看某一组细节：/帮助 同步    /帮助 绑定    /帮助 日志\n"
            "\n"
            "⚠️ 三个要知道的限制：\n"
            "  · QOJ 的提交记录**必须登录**才能看\n"
            "  · AtCoder 走社区服务，且它的 API 里**没有算法标签**\n"
            "  · 洛谷要**手动导入 Cookie**（自动登录还没打通）\n"
            "  详见 /帮助 同步")

    # ======================================================================
    # 定时推送
    # ======================================================================
    async def _load_subscribers(self) -> list:
        """读订阅列表。

        优先用 AstrBot 官方的插件 KV 存储（数据目录在 AstrBot 那边，
        插件重装/更新不会丢）。取不到就退回内存，保证不崩。
        """
        try:
            data = await self.get_kv_data("subscribers", [])
            return list(data or [])
        except Exception as exc:
            logger.warning("[xcpc] 读订阅列表失败，退回内存: %s", exc)
            return list(getattr(self, "_subs_fallback", []))

    async def _save_subscribers(self, subs: list) -> None:
        self._subs_fallback = list(subs)
        try:
            await self.put_kv_data("subscribers", list(subs))
        except Exception as exc:
            logger.warning("[xcpc] 存订阅列表失败，只留在内存: %s", exc)

    async def _daily_push_loop(self) -> None:
        """睡到下一个推送时刻 → 推一条 → 再睡。"""
        while True:
            try:
                delay = _seconds_until(self.config.get("push_time", "22:30"))
                logger.info("[xcpc] 下一次推送在 %.1f 分钟后", delay / 60)
                await asyncio.sleep(delay)
                await self._push_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[xcpc] 推送出错（不影响下一轮）: %s\n%s",
                             exc, traceback.format_exc())
                await asyncio.sleep(60)   # 出错别疯狂重试

    async def _push_once(self) -> tuple:
        """组装并发送今天的汇报。

        返回 ``(成功数, 目标数)`` —— ``/推送测试`` 要拿它报账；
        ``_daily_push_loop`` 不用，忽略即可。
        """
        from astrbot.api.event import MessageChain

        targets = await self._load_subscribers()
        manual = (self.config.get("push_umo") or "").strip()
        if manual and manual not in targets:
            targets.append(manual)
        if not targets:
            logger.info("[xcpc] 没有订阅者，跳过本次推送。")
            return 0, 0

        try:
            st = self.backend.status()
            if asyncio.iscoroutine(st):
                st = await st
            today = self.backend.today_tasks()
            if asyncio.iscoroutine(today):
                today = await today
            text = format_push(st, today, await self._today_reviewed())
        except Exception as exc:
            logger.error("[xcpc] 组装推送内容失败: %s", exc)
            return 0, len(targets)

        chain = MessageChain().message(text)
        sent = 0
        for umo in targets:
            try:
                await self.context.send_message(umo, chain)
                sent += 1
            except Exception as exc:
                # 平台不支持主动消息（例如 qq_official）就会走到这里。
                # 只记日志、不影响其它目标，也不让推送循环挂掉。
                logger.error("[xcpc] 推送到 %s 失败: %s", umo, exc)
        return sent, len(targets)

    async def _today_reviewed(self) -> bool:
        """今天记过复盘没有 —— 推送里要用这句话来催。

        交给后端自己判断：``file`` 后端扫 ``04-review/``；``http`` 后端看
        ``/api/data`` 的 ``contests`` 表（见 ``xcpc_core.reviewed_today``）。
        两个后端都没有这个能力时，退回老的 glob 方式。
        """
        fn = getattr(self.backend, "today_reviewed", None)
        if fn is not None:
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    res = await res
                return bool(res)
            except Exception as exc:
                logger.warning("[xcpc] 判断今天有没有复盘失败: %s", exc)

        # 兜底：老写法，只在 file 后端（有 root）时有效
        root = getattr(self.backend, "root", None)
        if not root:
            return False
        try:
            pattern = os.path.join(root, "04-review", "%s-*.md" % today_cn())
            return bool(glob.glob(pattern))
        except Exception:
            return False


# ==========================================================================
# 纯函数（不依赖 AstrBot，方便单测）
# ==========================================================================
def _strip_command(text: str, *names: str) -> str:
    """把 ``/复盘 xxx`` 里的指令前缀摘掉，返回后面的正文（保留换行）。"""
    text = text or ""
    lines = text.splitlines()
    if not lines:
        return ""
    first = lines[0].strip()
    for name in names:
        for prefix in ("/", "／", "!", "！", "#", ""):
            head = prefix + name
            if first == head:
                return "\n".join(lines[1:]).strip()
            if first.startswith(head + " ") or first.startswith(head + "\u3000"):
                rest = first[len(head):].strip()
                return "\n".join([rest] + lines[1:]).strip()
    return text.strip()


def _seconds_until(hhmm: str) -> float:
    """距离下一个北京时间 HH:MM 还有多少秒。"""
    try:
        hh, mm = [int(x) for x in str(hhmm).split(":")[:2]]
    except (ValueError, TypeError):
        hh, mm = 22, 30
    hh = min(max(hh, 0), 23)
    mm = min(max(mm, 0), 59)
    now = now_cn()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)   # timedelta 会正确跨月/跨年
    return max(5.0, (target - now).total_seconds())


def format_status(st: dict, handle: str) -> str:
    """把 status() 的返回值排版成聊天里好看的文本。"""
    streak = st.get("streak") or {}
    kpi = st.get("kpi") or {}
    out = ["📊 %s" % (st.get("handle") or handle)]

    rating = st.get("rating")
    maxr = st.get("max_rating")
    rank = st.get("rank") or ""
    out.append("· rating %s%s%s" % (
        rating if rating is not None else "—",
        "（最高 %s）" % maxr if maxr else "",
        " ｜ %s" % rank if rank else "",
    ))

    solved_all = st.get("solved_all_time")
    solved_api = st.get("solved_api")
    if solved_all:
        out.append("· 已 AC %s 道（主页口径；API 只看到 %s）"
                   % (solved_all, solved_api if solved_api is not None else "—"))
    else:
        out.append("· 已 AC %s 道（API 口径，未含未公开比赛）"
                   % (solved_api if solved_api is not None else "—"))

    if streak.get("days_active"):
        gap = streak.get("days_since_last")
        if gap is None:
            tail = ""
        elif gap == 0:
            tail = "今天已提交"
        elif gap >= 3:
            tail = "⚠️ 已断档 %d 天" % gap
        else:
            tail = "%d 天前提交" % gap
        out.append("· 连续 %s 天（最长 %s）%s"
                   % (streak.get("current_streak", 0),
                      streak.get("max_streak", 0),
                      " ｜ " + tail if tail else ""))

    if kpi.get("sessions"):
        out.append("· 复盘 %d 场 ｜ 均独立想出 %.1f 题 ｜ 难度上限 %s"
                   % (kpi["sessions"], kpi.get("avg_independent", 0.0),
                      kpi.get("difficulty_ceiling") or "待填"))
        if kpi.get("p0p1_total"):
            out.append("· P0/P1 补题完成率 %.0f%%"
                       % (100 * kpi.get("p0p1_rate", 0.0)))
    else:
        out.append("· 还没有复盘记录 —— QOJ 的团队 VP 抓不到数据，只能靠你填")

    nc = st.get("next_contest")
    if nc:
        out.append("· ⏳ %s 还有 %d 天" % (nc["name"], nc["days"]))
    if st.get("generated_at"):
        out.append("")
        out.append("（数据时间 %s）" % st["generated_at"])
    return "\n".join(out)


def format_push(st: dict, today: dict, reviewed: bool) -> str:
    """每天晚上那条推送的内容。"""
    lines = ["🌙 今天的收尾", ""]

    nc = st.get("next_contest")
    if nc:
        lines.append("⏳ %s 还有 %d 天" % (nc["name"], nc["days"]))

    items = today.get("items") or []
    pending = [i for i in items if not i.get("done")]
    if items:
        lines.append("")
        lines.append("📅 %s" % (today.get("title") or ""))
        for i in pending[:6]:
            lines.append("☐ %s" % i.get("text"))
        if len(pending) > 6:
            lines.append("… 还有 %d 条" % (len(pending) - 6))
        if not pending:
            lines.append("✅ 今天的都勾完了")

    lines.append("")
    if reviewed:
        lines.append("✅ 今天的复盘已记录。")
    else:
        lines.append("⚠️ 今天还没记复盘 —— 回一句「复盘」+ 内容就行，别拖过 24 小时。")

    streak = st.get("streak") or {}
    if streak.get("days_since_last") == 0:
        lines.append("🔥 今天已提交，连续 %s 天。" % streak.get("current_streak", 0))
    elif streak.get("days_since_last") is not None:
        gap = streak["days_since_last"]
        if gap >= 3:
            lines.append("🚨 已经 %d 天没提交了 —— 睡前碰一道题，把连续拉回来。" % gap)
        else:
            lines.append("⏰ %d 天没提交了，今天补一道。" % gap)
    return "\n".join(lines)
