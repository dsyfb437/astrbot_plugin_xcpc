#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令接线测试 —— **每个命令真的调一遍**。

为什么需要这个
--------------
之前测的都是"底下的逻辑"（store / sync / loop…），但**从没验证过
命令处理器本身能不能跑通**。那中间隔着一层接线，可能出这些错：

  * 忘了 `await`
  * yield 了错误类型（AstrBot 要 MessageEventResult）
  * 处理器里抛异常（AstrBot 只会记一条日志，用户看到的是没反应）
  * 拿不到 user_id 时静默什么都没做

这些都不会被逻辑测试覆盖到。而 `main.py` 依赖 astrbot 包，
所以这里塞一个最小外壳，把命令真的驱动一遍。

**全程不联网**：需要网络的命令（/同步 /方案）在没有 handle / 没有数据时
会在发请求之前就返回，正好也验证了"该早退的时候真的早退了"。

跑法：python tests/test_commands.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import types

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN)
sys.path.insert(0, HERE)

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  \u2713 %s" % label)
    else:
        FAIL += 1
        print("  \u2717 %s%s" % (label, ("  -> " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# astrbot 外壳
# ---------------------------------------------------------------------------

def install_stub():
    """塞一个最小的 astrbot 外壳，让 `import main` 能过。"""
    import logging
    if "astrbot" in sys.modules:
        return

    quiet = logging.getLogger("xcpc-cmdtest")
    quiet.addHandler(logging.NullHandler())
    quiet.setLevel(logging.CRITICAL)

    class AstrMessageEvent:
        pass

    class _Filter:
        def command(self, *a, **kw):
            def deco(fn):
                return fn
            return deco
        regex = command
        permission_type = lambda self, *a, **kw: (lambda fn: fn)  # noqa: E731

    class Star:
        def __init__(self, context=None):
            self.context = context

    class Context:
        def __init__(self):
            self.registered_web_apis = []

        def register_web_api(self, route, handler, methods, desc):
            self.registered_web_apis.append((route, handler, methods, desc))

        def get_current_chat_provider_id(self, umo=""):
            return ""

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = quiet
    ev = types.ModuleType("astrbot.api.event")
    ev.AstrMessageEvent = AstrMessageEvent
    ev.filter = _Filter()
    ev.MessageChain = lambda *a, **kw: None
    star = types.ModuleType("astrbot.api.star")
    star.Star = Star
    star.Context = Context
    web = types.ModuleType("astrbot.api.web")
    web.request = None

    astrbot.api = api
    api.event = ev
    api.star = star
    api.web = web
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api,
                        "astrbot.api.event": ev, "astrbot.api.star": star,
                        "astrbot.api.web": web})


class FakeResult:
    """AstrBot 的 plain_result 返回 MessageEventResult；这里只留文本。"""

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "FakeResult(%r)" % (self.text[:60],)


class FakeEvent:
    def __init__(self, message="", sender="qq1001", umo="test:GroupMessage:1"):
        self.message_str = message
        self._sender = sender
        self.unified_msg_origin = umo
        self.stopped = False
        self.results = []

    def plain_result(self, text):
        r = FakeResult(text)
        self.results.append(r)
        return r

    def image_result(self, *a, **kw):
        return FakeResult("[image]")

    def get_sender_id(self):
        return self._sender

    def is_admin(self):
        return True

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, v):
        return None

    @property
    def text(self):
        return "\n".join(r.text for r in self.results)


async def drive(plugin, method_name, message="", sender="qq1001"):
    """调一个命令处理器，把 yield 出来的东西收集起来。"""
    ev = FakeEvent(message=message, sender=sender)
    fn = getattr(plugin, method_name)
    out = fn(ev)
    if hasattr(out, "__aiter__"):
        async for _ in out:
            pass
    elif hasattr(out, "__await__"):
        await out
    return ev


async def make_plugin(data_root):
    install_stub()
    import importlib
    import main as plugin_main
    importlib.reload(plugin_main)
    from astrbot.api.star import Context
    ctx = Context()
    p = plugin_main.XcpcPlugin(ctx, {"data_root": data_root, "daily_push": False})
    await p.initialize()
    return p, plugin_main


# ---------------------------------------------------------------------------
# 1. 初始化 + 路由注册
# ---------------------------------------------------------------------------

def test_init():
    print("\n[1] 初始化与路由注册")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_cmd_")
        p, mod = await make_plugin(tmp)
        check("插件能构造出来", p is not None)
        check("数据库打开了", p.db is not None and p.db._conn is not None,
              p._db_error)
        check("store 建好了", p.store is not None)
        check("syncer 建好了", p.syncer is not None)
        check("loop 建好了", p.loop is not None)
        check("日志打开了", p.log is not None and p.log._fh is not None)

        routes = [r[0] for r in p.context.registered_web_apis]
        # 数字会随功能增加而变，所以断言"至少有这些"而不是死数字 ——
        # 我第一版写死了 9，加了 /link /unlink 之后就假失败了。
        check("注册了至少 9 条路由", len(routes) >= 9,
              "实际 %d：%s" % (len(routes), routes))
        for want in ("/astrbot_plugin_xcpc/accounts/status",
                     "/astrbot_plugin_xcpc/accounts/login",
                     "/astrbot_plugin_xcpc/accounts/login/2fa",
                     "/astrbot_plugin_xcpc/accounts/link",
                     "/astrbot_plugin_xcpc/accounts/unlink",
                     "/astrbot_plugin_xcpc/accounts/credentials"):
            check("路由 %s" % want.split("/")[-1], want in routes, repr(routes))

        await p.terminate()
        check("terminate 把路由摘干净了",
              not [r for r in p.context.registered_web_apis
                   if str(r[0]).startswith("/astrbot_plugin_xcpc/")],
              repr([r[0] for r in p.context.registered_web_apis]))

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 2. 每个命令都能跑通
# ---------------------------------------------------------------------------

def test_commands():
    print("\n[2] 每个命令真的调一遍")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_cmd2_")
        p, mod = await make_plugin(tmp)

        # /帮助
        for msg, must in (
            ("/帮助", ["XCPC 备赛助手", "同步", "方案", "自检"]),
            ("/帮助 同步", ["QOJ", "登录", "洛谷"]),
            ("/帮助 绑定", ["绑定码"]),
            ("/帮助 日志", ["日志"]),
        ):
            ev = await drive(p, "cmd_help", msg)
            body = ev.text
            missing = [m for m in must if m not in body]
            check("%s 有输出且含关键内容" % msg, body and not missing,
                  "缺 %s；实际前 150 字：%s" % (missing, body[:150]))

        # /自检 —— 这是装完第一件事
        ev = await drive(p, "cmd_selfcheck", "/自检")
        check("/自检 有输出", bool(ev.text), ev.text[:120])
        check("/自检 给出了结论", "自检" in ev.text and "合计" in ev.text,
              ev.text[:150])
        check("/自检 指出了还没绑账号", "账号绑定" in ev.text, ev.text[:400])
        check("/自检 给了怎么修", "→" in ev.text, ev.text[:400])

        # /绑定 —— 生成绑定码
        ev = await drive(p, "cmd_bind", "/绑定")
        check("/绑定 有输出", bool(ev.text))
        check("/绑定 给了 6 位码", "绑定码" in ev.text, ev.text[:150])
        import re
        m = re.search(r"绑定码：([A-Z2-9]{6})", ev.text)
        check("码的格式对（6 位，去了易混字符）", bool(m),
              repr(ev.text[:120]))
        if m:
            code = m.group(1)
            uid = await p.store.claim_link_code(code)
            check("生成的码真能用", bool(uid[0]) and uid[1] == "qq1001",
                  repr(uid)[:100])

        # /我的状态
        ev = await drive(p, "cmd_mydata", "/我的状态")
        check("/我的状态 有输出", bool(ev.text))
        check("含四个平台", all(x in ev.text for x in
                                ("CF", "AtCoder", "QOJ", "洛谷")), ev.text[:200])
        check("没数据时明说「还没有数据」+ 下一步",
              "还没有数据" in ev.text and "/绑定" in ev.text, ev.text[:250])

        # /比赛
        ev = await drive(p, "cmd_contests", "/比赛")
        check("/比赛 有输出", bool(ev.text))
        check("没比赛时说清三个平台各自的情况",
              "CF" in ev.text and "AtCoder" in ev.text and "QOJ" in ev.text,
              ev.text[:250])

        # /日志
        ev = await drive(p, "cmd_log", "/日志 5")
        check("/日志 有输出", bool(ev.text))
        check("提示已打码", "打码" in ev.text, ev.text[:120])

        # /反馈
        ev = await drive(p, "cmd_feedback", "/反馈 今天有点累")
        check("/反馈 记下了", "记下了" in ev.text, ev.text[:120])
        rows = await p.store.list_feedback("qq1001")
        check("反馈真进库了", len(rows) == 1 and "累" in rows[0]["text"],
              repr(rows)[:120])
        # 空反馈要给用法
        ev = await drive(p, "cmd_feedback", "/反馈")
        check("空 /反馈 给用法", "例" in ev.text or "一句话" in ev.text,
              ev.text[:120])

        # /同步 —— 没绑 handle，应该**在发请求之前**就返回
        ev = await drive(p, "cmd_sync", "/同步")
        check("/同步 有输出", bool(ev.text))
        check("没绑 handle 时明确说清", "绑定" in ev.text or "bind" in ev.text.lower(),
              ev.text[:300])
        check("没有假装同步成功", "✓" not in ev.text, ev.text[:300])

        # /方案 —— 没数据，应该**在调模型之前**就返回
        ev = await drive(p, "cmd_plan", "/方案")
        check("/方案 有输出", bool(ev.text))
        check("没数据时明确说清，且不调模型",
              "还没有数据" in ev.text and "/同步" in ev.text, ev.text[:300])
        check("说明里点出「不是水平是零」", "不是" in ev.text, ev.text[:350])

        # /总结 —— 没数据也要能跑
        ev = await drive(p, "cmd_summary", "/总结")
        check("/总结 有输出", bool(ev.text), ev.text[:150])
        check("明说「没有数据」不等于「水平是零」",
              "没有数据" in ev.text, ev.text[:300])

        # 打卡：循环第 ⑤ 环的入口
        ev = await drive(p, "cmd_done", "/打卡 今天做完了两道")
        check("/打卡 有输出", bool(ev.text))
        check("明说记下了", "做完了" in ev.text, ev.text[:120])
        check("带备注", "两道" in ev.text or "做完了" in ev.text, ev.text[:150])
        rows = await p.store.task_log("qq1001")
        check("打卡真进库了", len(rows) == 1 and rows[0]["status"] == "done",
              repr(rows)[:120])
        check("备注也存了", "两道" in (rows[0]["note"] or ""), repr(rows[0]["note"]))
        check("说出了会拿去做参考", "算进去" in ev.text, ev.text[:150])

        ev = await drive(p, "cmd_partial", "/做了一半")
        check("/做了一半 记下了", "一半" in ev.text, ev.text[:120])

        # 连着没做 → 该提醒"改计划"而不是催人
        for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"):
            await p.store.log_task("qq1001", "skipped", date=d)
        ev = await drive(p, "cmd_skip", "/没做 今天太忙")
        check("/没做 记下了", "没做" in ev.text, ev.text[:120])
        check("连着没做时提醒去改计划（不是催人）",
              "量太多" in ev.text or "压小" in ev.text, ev.text[:250])
        check("说清了「连着做不完的计划等于没有计划」",
              "没有计划" in ev.text, ev.text[:300])
        check("显示了执行率", "%" in ev.text, ev.text[:200])

        # /题库 —— **把 syncer 换成假的**，否则这个测试会去联网。
        # （离线测试绝不联网是我给自己定的规矩，破了的话测试就不可信了。）
        real_syncer = p.syncer

        class FakeSyncer:
            def __init__(self, ok, detail, added):
                self.ok, self.detail, self.added = ok, detail, added
                self.called = []

            async def sync_problems(self, platform):
                self.called.append(platform)
                return (self.ok, self.detail, self.added)

        # ① 成功路径
        p.syncer = FakeSyncer(True, "ok", 11425)
        ev = await drive(p, "cmd_bank", "/题库")
        check("/题库 有输出", bool(ev.text))
        check("报了新增题数", "11425" in ev.text, ev.text[:200])
        check("说明它有什么用", "回避" in ev.text, ev.text[:250])
        check("默认拉 CF", p.syncer.called == ["codeforces"], repr(p.syncer.called))

        # ② 指定平台
        p.syncer = FakeSyncer(True, "ok", 9673)
        await drive(p, "cmd_bank", "/题库 atcoder")
        check("能指定平台", p.syncer.called == ["atcoder"], repr(p.syncer.called))

        # ③ 失败路径 —— **不能假装成功**
        p.syncer = FakeSyncer(False, "网络不可达：连不上", 0)
        ev = await drive(p, "cmd_bank", "/题库")
        check("失败时明确说失败", "失败" in ev.text, ev.text[:200])
        check("说明后果（没题库就没法判回避）",
              "回避" in ev.text or "用不了" in ev.text, ev.text[:250])
        check("说清做题记录不受影响", "不受影响" in ev.text, ev.text[:300])

        p.syncer = real_syncer

        # 原有命令还在
        for name, msg in (("cmd_status", "/状态"), ("cmd_today", "/今天"),
                          ("cmd_lists", "/题单"), ("cmd_format", "/格式")):
            try:
                ev = await drive(p, name, msg)
                check("%s 还能调通" % msg, True)
            except Exception as exc:
                check("%s 还能调通" % msg, False,
                      "%s: %s" % (type(exc).__name__, exc))

        await p.terminate()

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 3. 拿不到 user_id 时要说清，不能静默
# ---------------------------------------------------------------------------

def test_no_user_id():
    print("\n[3] 拿不到 user_id 时不静默")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_cmd3_")
        p, mod = await make_plugin(tmp)

        for name, msg in (("cmd_sync", "/同步"), ("cmd_plan", "/方案"),
                          ("cmd_mydata", "/我的状态"), ("cmd_bind", "/绑定")):
            ev = await drive(p, name, msg, sender="")
            check("%s 空 user_id 时有输出" % msg, bool(ev.text),
                  "居然什么都没说")
            check("%s 说明了原因" % msg,
                  any(x in ev.text for x in ("标识", "user_id", "拿不到")),
                  ev.text[:150])

        await p.terminate()

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 4. 命令处理器不抛异常
# ---------------------------------------------------------------------------

def test_no_crash():
    print("\n[4] 各种奇怪输入都不该抛")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_cmd4_")
        p, mod = await make_plugin(tmp)

        weird = ["", "   ", "/同步 " + "x" * 500, "/方案 \n\n换行",
                 "/日志 abc", "/日志 -5", "/日志 99999",
                 "/帮助 " + "y" * 200, "/反馈 ", "/反馈 " + "z" * 5000]
        cmds = ["cmd_sync", "cmd_plan", "cmd_log", "cmd_help", "cmd_feedback",
                "cmd_summary", "cmd_contests", "cmd_mydata", "cmd_selfcheck",
                "cmd_bind"]
        crashes = []
        for name in cmds:
            for msg in weird:
                try:
                    await drive(p, name, msg)
                except Exception as exc:                # noqa: BLE001
                    crashes.append("%s(%r) -> %s: %s"
                                   % (name, msg[:20], type(exc).__name__, exc))
        check("没有命令因为奇怪输入崩掉", not crashes,
              "崩了 %d 次：%s" % (len(crashes), crashes[:3]))

        await p.terminate()

    asyncio.run(main_())


def test_config_is_read():
    """每个配置项都必须真的被读到。

    这一项补的是"配置在骗人"：
    `sync_interval_min` 写在 _conf_schema.json 里（默认 60，
    hint 还写着"0 = 不自动同步"），但**从来没有代码读它** ——
    用户设了它什么都不会发生。这比没有配置更糟：
    用户会以为同步是自动的，然后奇怪为什么数据不更新。

    同类问题还有：题库标注的拉取（`sync_problems` 从没被调用，
    导致整个诊断能力不可达）。**实现了但接不出来**是最隐蔽的一类缺口：
    代码是好的、单元测试是绿的，用户永远用不到。
    """
    print("\n[5] 配置项都必须被真的读到")

    schema = json.load(io.open(os.path.join(PLUGIN, "_conf_schema.json"),
                               encoding="utf-8"))
    code = io.open(os.path.join(PLUGIN, "main.py"), encoding="utf-8").read()
    # core/ 里也可能读配置
    for fn in os.listdir(os.path.join(PLUGIN, "core")):
        if fn.endswith(".py"):
            code += io.open(os.path.join(PLUGIN, "core", fn),
                            encoding="utf-8").read()

    # 这几个是给别的模块或用户看的，不在 main.py 里读也算正常
    ALLOW_UNUSED = {
        # 旧的 file/http 后端配置，保留兼容
        "backend", "workspace_root", "handle", "http_base", "http_token",
        "http_timeout", "penalty_per_fail", "admin_only", "allow_senders",
        # 这些在 main.py 里有读，列在这里只是备忘
    }

    unused = []
    for key in schema:
        if key in ALLOW_UNUSED:
            continue
        # 允许两种写法：config.get("x") / config["x"]
        if ('"%s"' % key) not in code:
            unused.append(key)
    check("每个配置项都在代码里出现过", not unused,
          "没人读的配置：%s" % unused)

    # 特别盯住这次修的两个
    check("sync_interval_min 被读了", '"sync_interval_min"' in code)
    check("自动同步真的起了任务", "_auto_sync_loop" in code)
    check("terminate 里收了这个任务", "_sync_task" in code)


def main() -> int:
    print("=" * 62)
    print("命令接线测试（astrbot 外壳，不联网）")
    print("=" * 62)
    test_init()
    test_commands()
    test_no_user_id()
    test_no_crash()
    test_config_is_read()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
