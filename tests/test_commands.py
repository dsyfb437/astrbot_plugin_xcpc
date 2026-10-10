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

**全程不联网**：需要网络的命令（/xcpc 同步 /xcpc 方案）在没有 handle / 没有数据时
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

class FakeHtmlRenderer:
    """假的 `astrbot.core.html_renderer`。

    **故意把 `use_network` 记下来** —— 那正是这里要钉死的东西：真机上
    `use_network=True` 会把正文 POST 到公共渲染服务 `t2i.soulter.top`，
    又慢（4.5s vs 0.1s）又可能把消息弄丢（拿回来的是外链）。
    """

    def __init__(self):
        self.calls = []            # 渲染过的正文
        self.network_flags = []    # 每次调用传进来的 use_network
        self.return_urls = []
        self.fail = False
        self.out = "/tmp/fake-t2i.png"

    async def render_t2i(self, text, use_network=True, return_url=False,
                         template_name=None):
        if self.fail:
            raise RuntimeError("这台机器没配文转图")
        self.calls.append(text)
        self.network_flags.append(use_network)
        self.return_urls.append(return_url)
        return self.out


RENDERER = FakeHtmlRenderer()


def _attach_renderer():
    """把假的 html_renderer 挂到 `astrbot.core` 上。**每次都要挂。**

    `test_data_root` 会把 `astrbot.core` 整个换成一个新模块，换掉之后
    `from astrbot.core import html_renderer` 就 ImportError —— 而那会让
    `_render_text_image` 静默走**网络**兜底那条路，测试看着还是绿的。
    """
    core = sys.modules.get("astrbot.core")
    if core is None:
        core = types.ModuleType("astrbot.core")
        sys.modules["astrbot.core"] = core
    parent = sys.modules.get("astrbot")
    if parent is not None:
        parent.core = core
    core.html_renderer = RENDERER


def install_stub():
    """塞一个最小的 astrbot 外壳，让 `import main` 能过。"""
    import logging
    if "astrbot" in sys.modules:
        _attach_renderer()
        return

    quiet = logging.getLogger("xcpc-cmdtest")
    quiet.addHandler(logging.NullHandler())
    quiet.setLevel(logging.CRITICAL)

    class AstrMessageEvent:
        pass

    class _Filter:
        def __init__(self):
            #: 注册过的**完整指令名**（子指令带组名前缀），用来断言"没有裸指令"
            self.commands = []
            #: 注册过的指令组名
            self.groups = []

        def command(self, *a, **kw):
            name = a[0] if a else kw.get("command_name", "")
            alias = kw.get("alias") or set()
            self.commands.extend([name] + list(alias))

            def deco(fn):
                return fn
            return deco

        def regex(self, *a, **kw):
            # 正则过滤器，**不是**指令名，别往 commands 里记
            def deco(fn):
                return fn
            return deco
        permission_type = lambda self, *a, **kw: (lambda fn: fn)  # noqa: E731

        def command_group(self, group_name, *a, **kw):
            """`@filter.command_group("xcpc")` —— 真的 AstrBot 返回的是
            一个 `RegisteringCommandable`，`@filter.command_group(...)` 这个
            装饰器执行完，函数名就被绑到那个对象上了，子指令再挂 `@xcpc.command`。
            这里照着这个形状做：装饰器返回一个带 `.command` 的组对象。
            子指令的完整名是 `组名 子名`（AstrBot::CommandFilter 就是这么拼的）。
            """
            outer = self
            outer.groups.append(group_name)

            class _Group:
                def command(self, *a, **kw):
                    name = a[0] if a else kw.get("command_name", "")
                    alias = kw.get("alias") or set()
                    outer.commands.extend(
                        ["%s %s" % (group_name, n) for n in [name] + list(alias)])

                    def deco(fn):
                        return fn
                    return deco
                regex = staticmethod(lambda *a, **kw: (lambda fn: fn))

            def deco(fn):
                return _Group()
            return deco

    class Star:
        def __init__(self, context=None):
            self.context = context
            # 文转图（真机上是 astrbot.core.star.base.Star.text_to_image）。
            # **正常路径已经不在这里了** —— 见 `_render_text_image`。这个假
            # 实现只在拿不到 html_renderer 时的那条兜底路上被用到。
            self.t2i_calls = []
            self.t2i_fail = False
            self.t2i_return_urls = []

        async def text_to_image(self, text, return_url=True):
            if self.t2i_fail:
                raise RuntimeError("这台机器没配文转图")
            self.t2i_calls.append(text)
            self.t2i_return_urls.append(return_url)
            return "http://example.invalid/t2i.png"

    class Context:
        def __init__(self):
            self.registered_web_apis = []

        def register_web_api(self, route, handler, methods, desc):
            self.registered_web_apis.append((route, handler, methods, desc))

        # 官方是 async（astrbot/core/star/context.py:329）。
        async def get_current_chat_provider_id(self, umo=""):
            return ""

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = quiet
    ev = types.ModuleType("astrbot.api.event")
    ev.AstrMessageEvent = AstrMessageEvent
    ev.filter = _Filter()

    class MessageChain:
        """真的 MessageChain 是"能串起来的消息段"。

        这里只要能记住文本 —— `/xcpc 推送测试` 那条路要断言**发出去的
        到底是什么**，而 `_push_once` 唯一会碰的就是它。
        """

        def __init__(self, *a, **kw):
            self.parts = []

        def message(self, text):
            self.parts.append(text)
            return self

        def __str__(self):
            return "".join(self.parts)

    ev.MessageChain = MessageChain
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
    _attach_renderer()


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

    def image_result(self, url):
        r = FakeResult("[image] %s" % url)
        self.results.append(r)
        return r

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

        # 指令全挂在 xcpc 组下面 —— 一个裸名都不能有。
        # 假的 `filter` 会记下每个注册过的完整名，正好用来验这件事。
        check("注册了一个指令组", set(mod.filter.groups) == {mod.GROUP_NAME},
              repr(mod.filter.groups))
        full = sorted(set(mod.filter.commands))
        check("注册了不止一个子指令", len(full) >= 20, "实际 %d" % len(full))
        bare = [n for n in full if not n.startswith(mod.GROUP_NAME + " ")]
        check("没有裸指令（全带 xcpc 前缀）", not bare, "裸的：%s" % bare[:6])
        for want in ("同步", "绑定", "帮助", "方案", "自检", "打卡"):
            check("子指令 xcpc %s" % want,
                  "%s %s" % (mod.GROUP_NAME, want) in full,
                  "没注册：%s" % want)

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

        # /xcpc 帮助
        for msg, must in (
            ("/xcpc 帮助", ["XCPC 备赛助手", "同步", "方案", "自检"]),
            ("/xcpc 帮助 同步", ["QOJ", "登录", "洛谷"]),
            ("/xcpc 帮助 绑定", ["绑定码"]),
            ("/xcpc 帮助 日志", ["日志"]),
        ):
            ev = await drive(p, "cmd_help", msg)
            body = ev.text
            missing = [m for m in must if m not in body]
            check("%s 有输出且含关键内容" % msg, body and not missing,
                  "缺 %s；实际前 150 字：%s" % (missing, body[:150]))

        # /xcpc 自检 —— 这是装完第一件事
        ev = await drive(p, "cmd_selfcheck", "/xcpc 自检")
        check("/xcpc 自检 有输出", bool(ev.text), ev.text[:120])
        check("/xcpc 自检 给出了结论", "自检" in ev.text and "合计" in ev.text,
              ev.text[:150])
        check("/xcpc 自检 指出了还没绑账号", "账号绑定" in ev.text, ev.text[:400])
        check("/xcpc 自检 给了怎么修", "→" in ev.text, ev.text[:400])

        # /xcpc 绑定 —— 生成绑定码
        ev = await drive(p, "cmd_bind", "/xcpc 绑定")
        check("/xcpc 绑定 有输出", bool(ev.text))
        check("/xcpc 绑定 给了 6 位码", "绑定码" in ev.text, ev.text[:150])
        import re
        m = re.search(r"绑定码：([A-Z2-9]{6})", ev.text)
        check("码的格式对（6 位，去了易混字符）", bool(m),
              repr(ev.text[:120]))
        if m:
            code = m.group(1)
            uid = await p.store.claim_link_code(code)
            check("生成的码真能用", bool(uid[0]) and uid[1] == "qq1001",
                  repr(uid)[:100])

        # /xcpc 我的状态
        ev = await drive(p, "cmd_mydata", "/xcpc 我的状态")
        check("/xcpc 我的状态 有输出", bool(ev.text))
        check("含四个平台", all(x in ev.text for x in
                                ("CF", "AtCoder", "QOJ", "洛谷")), ev.text[:200])
        check("没数据时明说「还没有数据」+ 下一步",
              "还没有数据" in ev.text and "/xcpc 绑定" in ev.text, ev.text[:250])

        # /xcpc 比赛
        ev = await drive(p, "cmd_contests", "/xcpc 比赛")
        check("/xcpc 比赛 有输出", bool(ev.text))
        check("没比赛时说清三个平台各自的情况",
              "CF" in ev.text and "AtCoder" in ev.text and "QOJ" in ev.text,
              ev.text[:250])

        # /xcpc 日志
        ev = await drive(p, "cmd_log", "/xcpc 日志 5")
        check("/xcpc 日志 有输出", bool(ev.text))
        check("提示已打码", "打码" in ev.text, ev.text[:120])

        # /xcpc 反馈
        ev = await drive(p, "cmd_feedback", "/xcpc 反馈 今天有点累")
        check("/xcpc 反馈 记下了", "记下了" in ev.text, ev.text[:120])
        rows = await p.store.list_feedback("qq1001")
        check("反馈真进库了", len(rows) == 1 and "累" in rows[0]["text"],
              repr(rows)[:120])
        # 空反馈要给用法
        ev = await drive(p, "cmd_feedback", "/xcpc 反馈")
        check("空 /xcpc 反馈 给用法", "例" in ev.text or "一句话" in ev.text,
              ev.text[:120])

        # /xcpc 同步 —— 没绑 handle，应该**在发请求之前**就返回
        ev = await drive(p, "cmd_sync", "/xcpc 同步")
        check("/xcpc 同步 有输出", bool(ev.text))
        check("没绑 handle 时明确说清", "绑定" in ev.text or "bind" in ev.text.lower(),
              ev.text[:300])
        check("没有假装同步成功", "✓" not in ev.text, ev.text[:300])

        # /xcpc 方案 —— 没数据，应该**在调模型之前**就返回
        ev = await drive(p, "cmd_plan", "/xcpc 方案")
        check("/xcpc 方案 有输出", bool(ev.text))
        check("没数据时明确说清，且不调模型",
              "还没有数据" in ev.text and "/xcpc 同步" in ev.text, ev.text[:300])
        check("说明里点出「不是水平是零」", "不是" in ev.text, ev.text[:350])

        # /xcpc 总结 —— 没数据也要能跑
        ev = await drive(p, "cmd_summary", "/xcpc 总结")
        check("/xcpc 总结 有输出", bool(ev.text), ev.text[:150])
        check("明说「没有数据」不等于「水平是零」",
              "没有数据" in ev.text, ev.text[:300])

        # 打卡：循环第 ⑤ 环的入口
        ev = await drive(p, "cmd_done", "/xcpc 打卡 今天做完了两道")
        check("/xcpc 打卡 有输出", bool(ev.text))
        check("明说记下了", "做完了" in ev.text, ev.text[:120])
        check("带备注", "两道" in ev.text or "做完了" in ev.text, ev.text[:150])
        rows = await p.store.task_log("qq1001")
        check("打卡真进库了", len(rows) == 1 and rows[0]["status"] == "done",
              repr(rows)[:120])
        check("备注也存了", "两道" in (rows[0]["note"] or ""), repr(rows[0]["note"]))
        check("说出了会拿去做参考", "算进去" in ev.text, ev.text[:150])

        ev = await drive(p, "cmd_partial", "/xcpc 做了一半")
        check("/xcpc 做了一半 记下了", "一半" in ev.text, ev.text[:120])

        # 连着没做 → 该提醒"改计划"而不是催人
        for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"):
            await p.store.log_task("qq1001", "skipped", date=d)
        ev = await drive(p, "cmd_skip", "/xcpc 没做 今天太忙")
        check("/xcpc 没做 记下了", "没做" in ev.text, ev.text[:120])
        check("连着没做时提醒去改计划（不是催人）",
              "量太多" in ev.text or "压小" in ev.text, ev.text[:250])
        check("说清了「连着做不完的计划等于没有计划」",
              "没有计划" in ev.text, ev.text[:300])
        check("显示了执行率", "%" in ev.text, ev.text[:200])

        # /xcpc 题库 —— **把 syncer 换成假的**，否则这个测试会去联网。
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
        ev = await drive(p, "cmd_bank", "/xcpc 题库")
        check("/xcpc 题库 有输出", bool(ev.text))
        check("报了新增题数", "11425" in ev.text, ev.text[:200])
        check("说明它有什么用", "回避" in ev.text, ev.text[:250])
        check("默认拉 CF", p.syncer.called == ["codeforces"], repr(p.syncer.called))

        # ② 指定平台
        p.syncer = FakeSyncer(True, "ok", 9673)
        await drive(p, "cmd_bank", "/xcpc 题库 atcoder")
        check("能指定平台", p.syncer.called == ["atcoder"], repr(p.syncer.called))

        # ③ 失败路径 —— **不能假装成功**
        p.syncer = FakeSyncer(False, "网络不可达：连不上", 0)
        ev = await drive(p, "cmd_bank", "/xcpc 题库")
        check("失败时明确说失败", "失败" in ev.text, ev.text[:200])
        check("说明后果（没题库就没法判回避）",
              "回避" in ev.text or "用不了" in ev.text, ev.text[:250])
        check("说清做题记录不受影响", "不受影响" in ev.text, ev.text[:300])

        p.syncer = real_syncer

        # 原有命令还在
        for name, msg in (("cmd_status", "/xcpc 状态"), ("cmd_today", "/xcpc 今天"),
                          ("cmd_lists", "/xcpc 题单"), ("cmd_format", "/xcpc 格式")):
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

        for name, msg in (("cmd_sync", "/xcpc 同步"), ("cmd_plan", "/xcpc 方案"),
                          ("cmd_mydata", "/xcpc 我的状态"), ("cmd_bind", "/xcpc 绑定")):
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

        weird = ["", "   ", "/xcpc 同步 " + "x" * 500, "/xcpc 方案 \n\n换行",
                 "/xcpc 日志 abc", "/xcpc 日志 -5", "/xcpc 日志 99999",
                 "/xcpc 帮助 " + "y" * 200, "/xcpc 反馈 ", "/xcpc 反馈 " + "z" * 5000]
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


def test_data_root():
    """数据不能在插件目录里 —— AstrBot 更新插件是先把整个插件目录删掉的。

    用户报的："每次更新都要重新绑定这好麻烦啊"。根因就在这里。
    """
    print("\n[6] 数据放哪儿 —— 不能在插件目录里")

    async def main_():
        import importlib
        install_stub()
        import main as plugin_main
        from astrbot.api.star import Context

        # --- 插件名要跟 metadata.yaml 一致，AstrBot 靠它分数据目录 ---
        meta = os.path.join(PLUGIN, "metadata.yaml")
        meta_name = ""
        if os.path.isfile(meta):
            for line in io.open(meta, encoding="utf-8"):
                if line.startswith("name:"):
                    # 行尾可能带注释，去掉它再比
                    meta_name = line.split(":", 1)[1].split("#")[0].strip()
                    break
        check("PLUGIN_NAME 和 metadata.yaml 的 name 一致",
              meta_name == plugin_main.PLUGIN_NAME,
              "metadata=%r 代码=%r" % (meta_name, plugin_main.PLUGIN_NAME))

        # --- 1. 有 StarTools 就用它给的位置（更新插件动不到那儿） ---
        star = sys.modules["astrbot.api.star"]
        fake_base = tempfile.mkdtemp(prefix="xcpc_pdata_")

        class _FakeStarTools:
            @classmethod
            def get_data_dir(cls, plugin_name=None):
                return os.path.join(fake_base, plugin_name or "?")

        star.StarTools = _FakeStarTools
        importlib.reload(plugin_main)
        p1 = plugin_main.XcpcPlugin(Context(), {"daily_push": False})
        want = os.path.join(fake_base, plugin_main.PLUGIN_NAME)
        check("有 StarTools 时数据放 data/plugin_data/<插件名>",
              p1._data_root() == want, p1._data_root())
        check("默认位置**不是**插件目录里的 data/",
              os.path.abspath(p1._data_root())
              != os.path.abspath(p1._bundled_data_root()), p1._data_root())

        # --- 2. StarTools 拿不到时退回 astrbot_path 里的那个函数 ---
        star.__dict__.pop("StarTools", None)
        alt_base = tempfile.mkdtemp(prefix="xcpc_pdata2_")
        pathmod = types.ModuleType("astrbot.core.utils.astrbot_path")
        pathmod.get_astrbot_plugin_data_path = lambda: alt_base
        core = types.ModuleType("astrbot.core")
        utils = types.ModuleType("astrbot.core.utils")
        core.utils = utils
        utils.astrbot_path = pathmod
        sys.modules.update({"astrbot.core": core, "astrbot.core.utils": utils,
                            "astrbot.core.utils.astrbot_path": pathmod})
        _attach_renderer()      # 上面把 astrbot.core 整个换掉了，得重新挂
        importlib.reload(plugin_main)
        p2 = plugin_main.XcpcPlugin(Context(), {"daily_push": False})
        check("没有 StarTools 时退回 get_astrbot_plugin_data_path()",
              p2._data_root()
              == os.path.join(alt_base, plugin_main.PLUGIN_NAME), p2._data_root())

        # --- 3. 两个都拿不到（脱离 AstrBot 手工跑）才回插件目录 ---
        sys.modules.pop("astrbot.core.utils.astrbot_path", None)
        importlib.reload(plugin_main)
        p3 = plugin_main.XcpcPlugin(Context(), {"daily_push": False})
        check("脱离 AstrBot 时退回插件目录（自测用）",
              p3._data_root() == p3._bundled_data_root(), p3._data_root())

        # --- 4. 显式配的 data_root 永远最大 ---
        mine = tempfile.mkdtemp(prefix="xcpc_mine_")
        p4 = plugin_main.XcpcPlugin(Context(), {"data_root": mine})
        check("配置里写了 data_root 就用它",
              p4._data_root() == os.path.abspath(mine), p4._data_root())

        # --- 5. 老数据搬家：只搬一次，永不覆盖 ---
        adopt = plugin_main._adopt_bundled_data

        def make(p, db=True, extra=()):
            os.makedirs(p, exist_ok=True)
            if db:
                with io.open(os.path.join(p, "xcpc.db"), "wb") as f:
                    f.write(b"OLD-DB")
            for name in extra:
                with io.open(os.path.join(p, name), "wb") as f:
                    f.write(b"x")
            return p

        old = make(os.path.join(tempfile.mkdtemp(prefix="xcpc_old_"), "data"))
        new = os.path.join(tempfile.mkdtemp(prefix="xcpc_new_"), "plugin_data")
        r = adopt(old, new)
        check("老数据搬过来了", r == (old, ""), repr(r))
        check("新位置拿到库", os.path.isfile(os.path.join(new, "xcpc.db")),
              repr(os.listdir(new) if os.path.isdir(new) else None))
        check("老位置清掉了", not os.path.exists(old))

        # 新位置已经有库 → 什么都不做，绝不覆盖
        old2 = make(os.path.join(tempfile.mkdtemp(prefix="xcpc_old2_"), "data"))
        new2 = os.path.join(tempfile.mkdtemp(prefix="xcpc_new2_"), "plugin_data")
        make(new2)
        r2 = adopt(old2, new2)
        check("新位置已有库时不动手", r2 == ("", ""), repr(r2))
        check("新位置的库没被覆盖",
              io.open(os.path.join(new2, "xcpc.db"), "rb").read() == b"OLD-DB")

        # 老位置没有库（历史遗留空目录）→ 不搬
        old3 = os.path.join(tempfile.mkdtemp(prefix="xcpc_old3_"), "data")
        os.makedirs(old3, exist_ok=True)
        r3 = adopt(old3, os.path.join(tempfile.mkdtemp(prefix="xcpc_new3_"), "p"))
        check("老位置没库时不搬（不留垃圾）", r3 == ("", ""), repr(r3))

        # 目标目录已经存在（可能只有 logs/）→ 合并进去，不删已有的东西
        old4 = make(os.path.join(tempfile.mkdtemp(prefix="xcpc_old4_"), "data"),
                    extra=("keep.txt",))
        new4 = os.path.join(tempfile.mkdtemp(prefix="xcpc_new4_"), "plugin_data")
        os.makedirs(os.path.join(new4, "logs"), exist_ok=True)
        with io.open(os.path.join(new4, "keep.txt"), "wb") as f:
            f.write(b"NEWER")
        r4 = adopt(old4, new4)
        check("目标目录已存在时合并不报错", r4 == (old4, ""), repr(r4))
        check("合并时没覆盖已有的同名文件",
              io.open(os.path.join(new4, "keep.txt"), "rb").read() == b"NEWER")
        check("合并时把库带过来了",
              os.path.isfile(os.path.join(new4, "xcpc.db")))
        check("合并时保留了目标里原有的子目录",
              os.path.isdir(os.path.join(new4, "logs")))

        # 同一路径 → 什么都不做（配置把 data_root 指到插件目录时）
        same = os.path.join(tempfile.mkdtemp(prefix="xcpc_same_"), "data")
        make(same)
        check("新旧路径相同时不动手", adopt(same, same) == ("", ""))
        check("新旧路径相同时库还在",
              os.path.isfile(os.path.join(same, "xcpc.db")))

        # --- 6. 脱离 AstrBot 时**不许搬家** ---
        # 否则跑一次测试就把开发机上真实的 <插件目录>/data 搬进临时目录了。
        # p3 是上面第 3 种情形（两个路径 API 都拿不到）造的那个实例。
        bundle = make(os.path.join(tempfile.mkdtemp(prefix="xcpc_bundle_"),
                                   "data"))
        p3._bundled_data_root = lambda: bundle
        p3.config = {"data_root": tempfile.mkdtemp(prefix="xcpc_tmp_")}
        await p3._setup_storage()
        # 注意：诊断信息本身不能抛异常 —— 搬家成功时老目录整个被删了，
        # 直接 os.listdir 会 FileNotFoundError，把"断言失败"变成"测试崩了"。
        detail = (repr(os.listdir(bundle)) if os.path.isdir(bundle)
                  else "老目录已经被搬走了")
        check("脱离 AstrBot 时不搬家（不然测试会搬走开发机上的真数据）",
              os.path.isfile(os.path.join(bundle, "xcpc.db")), detail)
        if p3.db:
            await p3.db.close()

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


def test_long_reply_image():
    """方案发图 —— 用户要的就是"长文本别在 QQ 上糊成一坨"。

    这里测的是 `_long_reply` 这一条路本身（发图 / 退回文本 / 不切条），
    不测 cmd_plan 的取数逻辑（那个在 test_loop 里）。
    """
    print("\n[7] 长文本：开关开了发图，失败退回文本")

    async def drive_long(p, text, flag, title="", md="", **conf):
        for k, v in conf.items():
            p.config[k] = v
        ev = FakeEvent(message="")
        async for _ in p._long_reply(ev, text, flag, title=title, md=md):
            pass
        return ev

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_cmdimg_")
        p, mod = await make_plugin(tmp)
        # 造一段真的"长文本"：比 max_reply_chars（默认 900）长得多
        long_text = "诊断：444 题 AC 里有标签的只有 76 题。\n" * 60
        check("测试用的文本确实够长", len(long_text) > 900, "%d 字" % len(long_text))

        # ① 开关关着 → 纯文本，而且超长会被切成多条
        ev = await drive_long(p, long_text, "plan_as_image",
                              title="今日方案", plan_as_image=False)
        check("关着开关时不发图", "[image]" not in ev.text, ev.text[:80])
        check("关着开关时按行切条", len(ev.results) > 1,
              "只发了 %d 条" % len(ev.results))
        check("关着开关时不碰文转图", not RENDERER.calls,
              repr(RENDERER.calls[:1]))

        # ② 开关打开 → 一条图，**不管文字多长都不切**
        RENDERER.calls = []
        ev = await drive_long(p, long_text, "plan_as_image",
                              title="今日方案", plan_as_image=True)
        check("开着开关时发的是图", "[image]" in ev.text, ev.text[:120])
        check("开图时只发一条（不切条）", len(ev.results) == 1,
              "发了 %d 条：%r" % (len(ev.results), ev.text[:120]))
        check("图里带了标题", RENDERER.calls == ["# 今日方案\n\n" + long_text],
              repr(RENDERER.calls[:1])[:150])
        check("图里是完整正文（没被截断）",
              bool(RENDERER.calls) and long_text in RENDERER.calls[0])

        # ③ 有 markdown 版时，图用它、文本用原来那份（两条路不能互相污染）
        RENDERER.calls = []
        ev = await drive_long(p, "纯文本正文\n      缩进续行", "plan_as_image",
                              title="今日方案", md="# 不用管这个标题\n\n- **【做题】** 正文",
                              plan_as_image=True)
        check("有 md 时图里用 md", RENDERER.calls ==
              ["# 今日方案\n\n# 不用管这个标题\n\n- **【做题】** 正文"],
              repr(RENDERER.calls[:1])[:200])
        check("有 md 时图里不出现纯文本版", "缩进续行" not in (RENDERER.calls[0] if RENDERER.calls else ""))
        RENDERER.calls = []
        ev = await drive_long(p, "纯文本正文\n      缩进续行", "plan_as_image",
                              title="今日方案", md="# 不用管这个标题",
                              plan_as_image=False)
        check("关着开关时仍然发纯文本版（不是 md）",
              "缩进续行" in ev.text and "不用管这个标题" not in ev.text,
              ev.text[:120])

        # ④ 没配文转图 → **必须退回文本**，不能让他什么都收不到
        RENDERER.fail = True
        ev = await drive_long(p, "短一点的方案", "plan_as_image",
                              title="今日方案", plan_as_image=True)
        check("文转图失败时不发空消息", bool(ev.text), "什么都没发出来")
        check("文转图失败时退回纯文本", "短一点的方案" in ev.text, ev.text[:120])
        check("文转图失败时不假装发了图", "[image]" not in ev.text, ev.text[:120])
        RENDERER.fail = False

        # ⑤ 正文是空的 → 不发图（一张空白图没有意义）
        RENDERER.calls = []
        ev = await drive_long(p, "   \n  ", "plan_as_image",
                              title="今日方案", plan_as_image=True)
        check("正文为空时不发图", not RENDERER.calls, repr(RENDERER.calls[:1]))

        # ⑥ 两个开关互不串台
        RENDERER.calls = []
        ev = await drive_long(p, "状态正文", "status_as_image",
                              title="XCPC 状态", status_as_image=True)
        check("status_as_image 独立生效", "[image]" in ev.text, ev.text[:80])
        check("status_as_image 读的是自己的开关",
              RENDERER.calls == ["# XCPC 状态\n\n状态正文"],
              repr(RENDERER.calls[:1])[:120])
        RENDERER.calls = []
        ev = await drive_long(p, "状态正文", "status_as_image",
                              title="XCPC 状态", status_as_image=False)
        check("关掉 status_as_image 后回到文本",
              "[image]" not in ev.text and "状态正文" in ev.text, ev.text[:80])

        # ⑦ 两个命令真的接到了这条路上（钉住接线，别只测了工具函数）
        src = open(os.path.join(os.path.dirname(__file__), "..", "main.py"),
                   encoding="utf-8").read()
        check("cmd_status 走 _long_reply", 'text, "status_as_image"' in src)
        check("cmd_plan 走 _long_reply 且用 plan_as_image",
              '"plan_as_image"' in src)
        check("cmd_plan 两条路都传了 markdown 版",
              src.count("md=result.plan.to_markdown()") == 2,
              "传了 %d 次" % src.count("md=result.plan.to_markdown()"))
        check("旧的内联发图代码没留两份",
              src.count("await self.text_to_image(") == 1,
              "有 %d 处" % src.count("await self.text_to_image("))

        # ⑧ **只用本机渲染，绝不走网络。**
        #    真机上 use_network=True 会把正文 POST 到公共渲染服务
        #    t2i.soulter.top：同一段正文 4.5s（网络）vs 0.1s（本机），
        #    而且 return_url=True 拿回来的是外链 —— 交给 event.image_result
        #    之后要靠适配器再去下载，那一步失败我们这边不报错，
        #    用户就是什么都收不到（"渲染失败"和"渲染很慢"同源）。
        RENDERER.calls, RENDERER.network_flags = [], []
        ev = await drive_long(p, "状态正文", "status_as_image",
                              title="XCPC 状态", status_as_image=True)
        check("发图时真的调了渲染器", RENDERER.calls == ["# XCPC 状态\n\n状态正文"],
              repr(RENDERER.calls[:1])[:120])
        check("渲染走的是本机（use_network=False）",
              RENDERER.network_flags == [False], repr(RENDERER.network_flags))
        check("走的不是 Star 的官方文转图 API（那条会 POST 到 t2i.soulter.top）",
              not p.t2i_calls, repr(p.t2i_calls[:1])[:120])
        check("渲染器返回什么就发什么（本机返回的是路径，不是外链）",
              "/tmp/fake-t2i.png" in ev.text, ev.text[:120])
        check("源码里显式关了网络渲染（调用点只有一处）",
              src.count("use_network=False)") == 1,
              "有 %d 处" % src.count("use_network=False)"))
        check("没有任何地方用默认的 use_network=True 渲染",
              src.count("html_renderer.render_t2i(") == 1
              and "html_renderer.render_t2i(body, use_network=False)" in src,
              "调用点 %d 个" % src.count("html_renderer.render_t2i("))

        # ⑨ AstrBot 换了内部结构、拿不到 html_renderer 时：
        #    **退回官方 API 也必须是 return_url=False** —— 要路径不要外链，
        #    外链那条路的失败我们看不见。
        astrbot_core = sys.modules["astrbot.core"]
        saved = getattr(astrbot_core, "html_renderer", None)
        try:
            del astrbot_core.html_renderer
            p.t2i_calls, p.t2i_return_urls = [], []
            ev = await drive_long(p, "兜底正文", "status_as_image",
                                  title="XCPC 状态", status_as_image=True)
            check("拿不到 html_renderer 时退回官方 API", p.t2i_calls == ["# XCPC 状态\n\n兜底正文"],
                  repr(p.t2i_calls[:1])[:120])
            check("兜底时也要路径不要外链（return_url=False）",
                  p.t2i_return_urls == [False], repr(p.t2i_return_urls))
        finally:
            if saved is not None:
                astrbot_core.html_renderer = saved

        await p.terminate()

    asyncio.run(main_())


def test_sync_log_report():
    """自动同步的摘要行必须带上**真正的**失败原因。

    用户贴的日志里，上一行是
      sync.fail      … pf=qoj … FAIL [凭据失效] 会话已失效（被重定向到登录页），请重新登录
    紧接着的摘要行却是
      autosync.done  … FAIL [内部错误] {"platforms": 4, "failed": "qoj"}
    ——「内部错误」既不是真原因，也没告诉他要重新登录。
    """
    print("\n[8] 自动同步摘要行：失败原因要带上来")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_cmdlog_")
        p, mod = await make_plugin(tmp)
        from core import sync as syncm
        from core import log as logm

        rec = logm.Recorder(os.path.join(tmp, "logs", "test.log"),
                            to_astrbot=False)
        rec.open()
        p.log = rec

        def res(platform, ok, kind="", detail=""):
            return syncm.PlatformResult(platform=platform, ok=ok,
                                        error_kind=kind, detail=detail)

        def report(*results):
            return syncm.SyncReport(results=list(results))

        # ① 全成功 → ok=True，没有 [错误类型]
        p._log_sync_report("u1", report(res("codeforces", True),
                                        res("atcoder", True)))
        line = rec.tail(1)[0]
        check("全成功时记的是 ok", " autosync.done " in line + " " and " ok " in line,
              line)
        check("全成功时不带 [内部错误]", "[内部错误]" not in line, line)

        # ② 一个平台凭据失效 → 摘要行必须说「凭据失效」
        p._log_sync_report("u1", report(
            res("codeforces", True), res("atcoder", True), res("luogu", True),
            res("qoj", False, "凭据失效",
                "会话已失效（被重定向到登录页），请重新登录")))
        line = rec.tail(1)[0]
        check("有平台失败时记的是 FAIL", " FAIL " in line, line)
        check("摘要行带的是真原因（凭据失效）", "[凭据失效]" in line, line)
        check("摘要行**不再**说 [内部错误]", "[内部错误]" not in line, line)
        check("摘要行说清了该怎么办（重新登录）", "重新登录" in line, line)
        check("摘要行点出是哪个平台", "qoj" in line, line)
        check("摘要行仍然带平台总数与失败名单",
              '"platforms": 4' in line and '"failed": "qoj"' in line, line)

        # ③ 失败原因是空的 → 才轮到「内部错误」兜底（而不是反过来）
        p._log_sync_report("u1", report(res("qoj", False, "", "炸了")))
        line = rec.tail(1)[0]
        check("真没有 error_kind 时才落到 [内部错误]", "[内部错误]" in line, line)
        check("兜底时也带着原始说明", "炸了" in line, line)

        # ④ 多个平台同时挂 → 每个的原因都要在
        p._log_sync_report("u1", report(
            res("qoj", False, "凭据失效", "会话已失效，请重新登录"),
            res("luogu", False, "网络故障", "连接超时")))
        line = rec.tail(1)[0]
        check("多个失败时取第一个的原因定性", "[凭据失效]" in line, line)
        check("多个失败时每个原因都在", "重新登录" in line and "连接超时" in line, line)
        check("failed 列表有两个平台", '"failed": "qoj,luogu"' in line, line)

        await p.terminate()

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 9. 每日推送 —— 数据必须来自插件自己的库
# ---------------------------------------------------------------------------

def test_push_train():
    """v0.5.23：推送原来只读**空的** file 工作区，方案一条都进不去。

    真机现场（2026-10-08）：`/xcpc 方案` 把方案写进 `plans` 表，而 22:30
    那条推送只读 `workspace/00-plan/sprint.md` —— 那个文件里还是模板原话
    「现在这个文件里一条任务都没有」。于是每天发出去的只有：

        🌙 今天的收尾

        ⚠️ 今天还没记复盘 —— 回一句「复盘」+ 内容就行，别拖过 24 小时。

    方案、打卡、提交，一条都没有。而 `format_push` 当时**一条测试都没有**。
    """
    print("\n[9] 每日推送带上插件自己的数据")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_push_")
        p, mod = await make_plugin(tmp)

        # --- _uid_from_umo：只认私聊 ---
        f = mod._uid_from_umo
        check("私聊 umo 取得到 uid",
              f("default:FriendMessage:3085596249") == "3085596249",
              repr(f("default:FriendMessage:3085596249")))
        check("群聊 umo 取不到 uid —— 那段是**群号**，不是人",
              f("default:GroupMessage:123456") == "",
              repr(f("default:GroupMessage:123456")))
        check("空 / 畸形 umo 返回空串，不抛异常",
              f("") == "" and f("x") == "" and f(None) == "",
              "%r %r %r" % (f(""), f("x"), f(None)))

        # --- format_push_train ---
        Plan, Task = mod.llmm.Plan, mod.llmm.Task
        fpt = mod.format_push_train
        plan = Plan(date="2026-10-08", assessment="诊断",
                    tasks=[Task(kind="practice", title="trees 入门",
                                problem="CF:1188A1", minutes=50, why="这是理由"),
                           Task(kind="learn", title="读套路笔记", minutes=25)])
        out = fpt(plan, {"status": "done", "note": "还行"},
                  {"today_sub": 3, "today_ac": 2, "streak": 4, "days_since_last": 0})
        check("方案列出来了", "今天的方案（2 项）" in out, out[:90])
        check("任务行有 kind 标签 + 题号 + 时长",
              "· [做题] trees 入门 → CF:1188A1 （50 分钟）" in out, out)
        check("打卡状态写出来了", "✅ 今天已打卡：做完了（还行）" in out, out)
        check("今天的提交数写出来了", "🔥 今天交了 3 条，AC 2 条。" in out, out)
        check("提交>0 才提「连续活跃」", "连续活跃 4 天。" in out, out)
        check("why 不进展推送（那是方案正文的事，推送要一眼扫完）",
              "这是理由" not in out, out)

        out2 = fpt(plan, None,
                   {"today_sub": 0, "today_ac": 0, "streak": 0, "days_since_last": 2})
        check("没打卡就说「还没打卡」——**不推断他偷懒**（v0.5.15 的教训）",
              "今天还没打卡" in out2 and "偷懒" not in out2, out2)
        check("今天没提交时给「最近一次在几天前」",
              "最近一次在 2 天前" in out2, out2)
        check("今天没提交时不显示「连续活跃」", "连续活跃" not in out2, out2)

        out3 = fpt(None, {"status": "partial", "note": ""},
                   {"today_sub": 0, "today_ac": 0, "streak": 0, "days_since_last": 9})
        check("只有打卡、没有方案时也不炸，且不说「今天还没打卡」",
              "做了一半" in out3 and "还没打卡" not in out3, out3)
        check("三样都取不到 → 空串（让调用方退回老格式，而不是发「暂无数据」）",
              fpt(None, None, None) == "" and fpt(None, None, {}) == "",
              repr(fpt(None, None, None)))

        # --- format_push(train=...) ---
        st = {"next_contest": None, "streak": {}}
        base = mod.format_push(st, {"items": []}, False)
        check("不传 train 时和以前**一模一样**（真去手写 sprint.md 的人还要用）",
              base == mod.format_push(st, {"items": []}, False, train=""), base)
        with_t = mod.format_push(st, {"items": []}, False, train="XX方案XX")
        check("train 插在标题之后、复盘提醒之前",
              with_t.index("🌙 今天的收尾") < with_t.index("XX方案XX")
              < with_t.index("复盘"), with_t)
        # 真机 dry-run 发现过两个连续空行（手机上一段话被劈成两半）——
        # 钉死：方案段和复盘提醒之间**只能有一个**空行。
        check("方案段和复盘提醒之间只有一个空行",
              "\n\n\n" not in with_t, repr(with_t))

        # --- _push_once：每个目标单独组装 ---
        uid = "qq1001"
        await p.store.save_plan(uid, mod.today_cn(), {
            "assessment": "诊断", "watch": "注意", "model_id": "t",
            "tasks": [{"kind": "practice", "title": "树入门",
                       "problem": "CF:1188A1", "minutes": 50, "why": "理由"}]})
        sent = []

        async def _send(umo, chain):
            sent.append((umo, str(chain)))

        p.context.send_message = _send
        p._subs_fallback = ["default:FriendMessage:%s" % uid, "test:GroupMessage:999"]
        n_ok, n_all = await p._push_once()
        check("两个订阅目标都发了", (n_ok, n_all) == (2, 2), "%r/%r" % (n_ok, n_all))
        friend = [t for u, t in sent if u.endswith(uid)]
        group = [t for u, t in sent if u.endswith(":999")]
        check("私聊那条带上了今天的方案 + 题号",
              bool(friend) and "今天的方案（1 项）" in friend[0]
              and "CF:1188A1" in friend[0],
              friend[0][:140] if friend else "（没发出去）")
        check("私聊那条仍然保留了老的收尾段",
              bool(friend) and "今天的收尾" in friend[0],
              friend[0][:140] if friend else "")
        check("群里那条**不夹带**某个人的方案（群里那段是群号，认不出人）",
              bool(group) and "今天的方案" not in group[0],
              group[0][:140] if group else "（没发出去）")
        check("取不到 store 时推送照常发（不能因为数据层挂了就整条不发）",
              await p._push_train_text("") == "")

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 11. 训练块命令（v0.6.0）
# ---------------------------------------------------------------------------

def test_block_cmd():
    print("\n[11] /xcpc 块")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_block_")
        p, mod = await make_plugin(tmp)
        import main as plugin_main

        # 还没开块
        ev = await drive(p, "cmd_block", "/xcpc 块")
        check("没块时说清「现在没有」", "没有训练块" in ev.text, ev.text[:150])
        check("并且告诉他不用手动设", "/xcpc" in ev.text, ev.text[:200])

        # 直接点名一个专题
        ev = await drive(p, "cmd_block", "/xcpc 块 背包")
        check("点名能开块", "背包" in ev.text, ev.text[:150])
        blk = await p.store.get_block("qq1001")
        check("★ 块真进库了", blk is not None and blk["topic"] == "dp_knapsack",
              repr(dict(blk) if blk else None))
        check("target 取自 curriculum（背包 18 道）", blk["target"] == 18,
              repr(blk["target"]))
        check("难度带取自 curriculum",
              (blk["band_lo"], blk["band_hi"]) == (1200, 1600),
              repr((blk["band_lo"], blk["band_hi"])))
        check("回了进度", "进度" in ev.text or "/" in ev.text, ev.text[:250])

        # 看一眼
        ev = await drive(p, "cmd_block", "/xcpc 块")
        check("看一眼有输出", bool(ev.text), ev.text[:120])
        check("★ 列出了完整阶梯", "线性" in ev.text and "网络流" in ev.text,
              ev.text[:400])
        check("标出了当前在哪一格", "背包" in ev.text, ev.text[:300])

        # 下一个
        ev = await drive(p, "cmd_block", "/xcpc 块 下一个")
        check("下一个有输出", bool(ev.text), ev.text[:120])
        blk2 = await p.store.get_block("qq1001")
        check("★ 真的换到下一个了", blk2["topic"] != "dp_knapsack",
              repr(blk2["topic"]))
        check("换的是阶梯上的下一个（区间 DP）", blk2["topic"] == "dp_interval",
              repr(blk2["topic"]))
        check("★ 换块会重置 started_at（进度重新起算）",
              blk2["started_at"] >= blk["started_at"],
              "%s -> %s" % (blk["started_at"], blk2["started_at"]))

        # 认不出的专题名
        ev = await drive(p, "cmd_block", "/xcpc 块 不存在的专题")
        check("认不出时给提示而不是静默", "没找到" in ev.text, ev.text[:150])

        # 关掉
        ev = await drive(p, "cmd_block", "/xcpc 块 关")
        check("关掉有输出", bool(ev.text), ev.text[:120])
        check("★ 关掉后库里真没了", await p.store.get_block("qq1001") is None, "")

        # 再关一次不该炸
        ev = await drive(p, "cmd_block", "/xcpc 块 关")
        check("重复关不炸", bool(ev.text), ev.text[:120])

        # 权限 —— FakeEvent.is_admin() 恒为 True，所以只能靠 allow_senders 拦
        p.config["allow_senders"] = ["qq1001"]
        ev = await drive(p, "cmd_block", "/xcpc 块", sender="qq9999")
        check("★ 白名单外的人拒绝", "没有权限" in ev.text, ev.text[:120])
        ev = await drive(p, "cmd_block", "/xcpc 块", sender="qq1001")
        check("白名单内的人放行", "没有权限" not in ev.text, ev.text[:120])
        p.config["allow_senders"] = []

        # ★ 裸 /xcpc 的正则要锚死首尾，否则会抢走 `/xcpc 方案`
        rx = plugin_main.BARE_CMD_RE
        for ok_msg in ("/xcpc", "xcpc", "!xcpc", "／xcpc", "  /xcpc  "):
            check("裸命令匹配 %r" % ok_msg, bool(rx.match(ok_msg)), "")
        for bad_msg in ("/xcpc 方案", "/xcpc方案", "/xcpc 打卡", "/xcpc 块",
                        "xcpcabc"):
            check("★ 不抢 %r" % bad_msg, not rx.match(bad_msg), "")

        # 裸 /xcpc 要走完整的方案流程
        ev = await drive(p, "bare_xcpc", "/xcpc")
        check("裸 /xcpc 有输出", bool(ev.text), ev.text[:150])
        check("★ 裸 /xcpc 就是方案（不用记第二条命令）",
              "没有权限" not in ev.text, ev.text[:200])

    asyncio.run(main_())


def test_vp_cmd():
    print("\n[12] /xcpc vp（v0.6.1 的 VP 场次源）")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_vpcmd_")
        p, mod = await make_plugin(tmp)
        from core import vp as vpmod

        # ---- 抓取失败：**必须说清是哪一边失败了**。
        # 只说「没有场次」会让人以为是「确实没得打」，那是两种
        # 完全不同的状态 —— 一个是网络问题，一个是真没得打。
        orig = vpmod._get_json

        async def dead(client, url):
            return None, "模拟的网络故障"
        vpmod._get_json = dead
        try:
            ev = await drive(p, "cmd_vp", "/xcpc vp 刷新")
        finally:
            vpmod._get_json = orig
        check("抓取失败时有输出", bool(ev.text), ev.text[:150])
        check("★ 说清了是哪个平台失败",
              "codeforces" in ev.text and "atcoder" in ev.text, ev.text[:300])
        check("★ 说清了失败原因", "模拟的网络故障" in ev.text, ev.text[:300])
        check("★ 没把抓取失败说成「确实没得打」",
              "全都打过" not in ev.text, ev.text[:300])

        # ---- 有数据时列出来。save_vp 刚写过，fetched_at 是新的，
        #      ensure 会直接用缓存，不会再打网络。
        vpmod._get_json = dead
        try:
            await p.store.save_vp("codeforces", [
                {"contest_id": "1123",
                 "name": "Codeforces Round 1123 (Div. 2)",
                 "division": "Div. 2", "start_epoch": 1700000000,
                 "duration_sec": 8100},
                {"contest_id": "1125",
                 "name": "Codeforces Round 1125 (Div. 3)",
                 "division": "Div. 3", "start_epoch": 1699000000,
                 "duration_sec": 7200},
            ], ok=True, rating=1713)
            ev = await drive(p, "cmd_vp", "/xcpc vp")
        finally:
            vpmod._get_json = orig
        check("有场次时列出来了", "Codeforces Round 1123" in ev.text, ev.text[:400])
        check("★ 带了赛制", "Div. 2" in ev.text, ev.text[:400])
        check("★ 带了链接（点得进去才能打）",
              "codeforces.com/contest/1123" in ev.text, ev.text[:400])
        check("★ 时长按这一场自己的算", "135" in ev.text, ev.text[:400])
        check("★ 提醒了可以刷新", "刷新" in ev.text, ev.text[:400])

        # ---- 「做过的」要被减掉（同一场里已经 AC 两道 = 剧透）
        from platforms.base import Submission
        await p.store.upsert_submissions("qq1001", "codeforces", [
            Submission(platform="codeforces", submission_id="z1",
                       problem_key="CF:1123A", verdict="OK", epoch=1000),
            Submission(platform="codeforces", submission_id="z2",
                       problem_key="CF:1123B", verdict="OK", epoch=1001),
        ])
        await p.store.save_vp("codeforces", [
            {"contest_id": "1123", "name": "Codeforces Round 1123 (Div. 2)",
             "division": "Div. 2", "start_epoch": 1700000000,
             "duration_sec": 8100},
            {"contest_id": "1125", "name": "Codeforces Round 1125 (Div. 3)",
             "division": "Div. 3", "start_epoch": 1699000000,
             "duration_sec": 7200},
        ], ok=True, rating=1713)
        ev = await drive(p, "cmd_vp", "/xcpc vp")
        check("★ 已经做过两道的 1123 被拿掉了",
              "Round 1123" not in ev.text, ev.text[:400])
        check("没做过的 1125 还在", "Round 1125" in ev.text, ev.text[:400])

        # ---- 权限
        p.config["allow_senders"] = ["qq1001"]
        ev = await drive(p, "cmd_vp", "/xcpc vp", sender="qq9999")
        check("★ 白名单外的人拒绝", "没有权限" in ev.text, ev.text[:150])
        p.config["allow_senders"] = []

        # ---- 帮助里要提得到（否则等于没这个功能）
        check("★ 帮助里有 vp 这一条", "vp" in mod.HELP_MAIN.lower(),
              mod.HELP_MAIN[:400])
        # ★ 完整指令表是「老的 HELP_MAIN 原样搬过来」，新加的命令容易
        #   只加在主帮助里、忘了同步到「更多」那一份
        check("★ 完整指令表里也有 vp", "vp" in mod.HELP_MORE.lower(),
              mod.HELP_MORE[:200])
        check("★ 完整指令表里也有「块」", "块" in mod.HELP_MORE,
              mod.HELP_MORE[:200])

    asyncio.run(main_())


def main() -> int:
    print("=" * 62)
    print("命令接线测试（astrbot 外壳，不联网）")
    print("=" * 62)
    test_init()
    test_commands()
    test_no_user_id()
    test_no_crash()
    test_data_root()
    test_config_is_read()
    test_long_reply_image()
    test_sync_log_report()
    test_push_train()
    test_block_cmd()
    test_vp_cmd()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
