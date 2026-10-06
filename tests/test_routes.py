#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Web 路由测试 —— 补上让「绑定页填了码却说没填」溜过去的那块空白。

为什么之前没抓到
----------------
`test_commands.py` 驱动的是**聊天命令**（`cmd_*`），
从来**没碰过 Web 路由**（`account_*`）。

而那个 bug 恰恰在路由里：AstrBot 的 `request` 代理，
它的 `json()` / `body()` / `form()` **全是协程**。
我写成同步的 `v = v()`，拿到 coroutine 对象，
`isinstance(v, dict)` 永远假 → 静默返回 {} → body 恒为空。

聊天命令的测试再多也碰不到这条路径。

所以这里做两件事
1. **假 request 严格模仿真的**：方法是 **async** 的。
   假成同步的，就等于把 bug 藏起来。
2. 驱动真实的 handler，断言它真的读到了 body。
"""

from __future__ import annotations

import asyncio
import inspect
import io
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
# 假 request —— **严格模仿 AstrBot 的 PluginRequest**
#
# 真的长这样（从官方 wheel 读出来的）：
#     class PluginRequest:
#         async def body(self) -> bytes: ...
#         async def json(self, default=None): ...
#         async def form(self) -> PluginMultiDict: ...
#         self.query = PluginMultiDict(...)   # .get() 是同步的
#         self.cookies: dict
#         self.method / self.path / self.headers / self.content_type
#
# **方法必须是 async。** 假成同步的，就等于把这个 bug 藏起来。
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, json_body=None, raw_body=None, query=None,
                 cookies=None, form_body=None, method="POST", username=None):
        self._json = json_body
        self._raw = raw_body
        self._form = form_body
        self.method = method
        self.path = "/api/plugin/page/astrbot_plugin_xcpc/accounts/x"
        self.headers = {}
        self.content_type = "application/json" if json_body is not None else None
        self.cookies = cookies or {}
        self.query = _MD(query or {})
        # Dashboard 登录名。**服务端**给的，不依赖浏览器存储 ——
        # 真的 PluginRequest 里就是 `self.username = username`
        # （astrbot/api/web.py，由 require_plugin_scope 守门）。
        self.username = username
        self.json_calls = 0
        self.body_calls = 0

    async def json(self, default=None):
        self.json_calls += 1
        await asyncio.sleep(0)          # 真协程会有切换点
        if self._json is None:
            return default
        return self._json

    async def body(self):
        self.body_calls += 1
        await asyncio.sleep(0)
        if self._raw is not None:
            return self._raw
        if self._json is not None:
            import json as _j
            return _j.dumps(self._json).encode("utf-8")
        return b""

    async def form(self):
        await asyncio.sleep(0)
        return _MD(self._form or {})


class _MD:
    """模仿 PluginMultiDict：.get() 同步，带 keys()。"""

    def __init__(self, d):
        self._d = dict(d)

    def get(self, k, default=None, type=None):
        v = self._d.get(k, default)
        if type is not None and v is not None:
            try:
                return type(v)
            except (TypeError, ValueError):
                return default
        return v

    def keys(self):
        return list(self._d.keys())


def install_stub():
    import logging
    if "astrbot" in sys.modules:
        return
    quiet = logging.getLogger("xcpc-routetest")
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


async def make_plugin(data_root):
    install_stub()
    import importlib
    import main as plugin_main
    importlib.reload(plugin_main)
    from astrbot.api.star import Context
    p = plugin_main.XcpcPlugin(Context(), {"data_root": data_root})
    await p.initialize()
    return p


def bind(p, req):
    """把假 request 接到插件上。"""
    p._route_request = lambda: req


# ---------------------------------------------------------------------------
# 0. 静态检查：读请求体的方法必须是协程
# ---------------------------------------------------------------------------

def test_is_async():
    print("\n[0] 读请求体的方法必须是 async")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_rt_")
        p = await make_plugin(tmp)
        check("_route_body 是协程函数（真的 API 是 async 的）",
              inspect.iscoroutinefunction(p._route_body),
              "同步的话 v() 拿到的是 coroutine 对象，body 会恒为空")
        check("_route_token 是协程函数",
              inspect.iscoroutinefunction(p._route_token))
        check("_route_user_id 是协程函数",
              inspect.iscoroutinefunction(p._route_user_id))
        check("_route_username 是同步的（它只是读请求属性，没有 await）",
              not inspect.iscoroutinefunction(p._route_username))
        await p.terminate()

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 1. 真的能从请求里读到 body
# ---------------------------------------------------------------------------

def test_body_is_read():
    print("\n[1] 能从请求里读到 body（就是那个 bug）")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_rt2_")
        p = await make_plugin(tmp)

        # JSON body
        bind(p, FakeRequest(json_body={"code": "ABC123"}))
        got = await p._route_body()
        check("JSON body 读出来了", got == {"code": "ABC123"}, repr(got))

        # 原始 body（前端可能不走 json()）
        bind(p, FakeRequest(raw_body=b'{"code":"XYZ789"}'))
        got = await p._route_body()
        check("原始 body 也能解出来", got == {"code": "XYZ789"}, repr(got))

        # form-urlencoded
        bind(p, FakeRequest(raw_body=b"code=FORM01"))
        got = await p._route_body()
        check("form 编码也能解出来", got == {"code": "FORM01"}, repr(got))

        # 完全没有 body → 空 dict，不抛
        bind(p, FakeRequest())
        got = await p._route_body()
        check("没有 body 时返回空 dict（不抛）", got == {}, repr(got))

        # 坏 JSON → 空 dict，不抛
        bind(p, FakeRequest(raw_body=b"{not json"))
        got = await p._route_body()
        check("坏 JSON 时返回空 dict（不抛）", got == {}, repr(got))

        await p.terminate()

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 2. /link 端到端：填了码就该关联成功
# ---------------------------------------------------------------------------

def test_link_route():
    print("\n[2] 绑定码路由端到端")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_rt3_")
        p = await make_plugin(tmp)
        await p.store.ensure_user("qq1001")

        code = await p.store.create_link_code("qq1001")
        check("码生成了", len(code) == 6, repr(code))

        # 这一步就是用户遇到的场景
        bind(p, FakeRequest(json_body={"code": code}))
        res = await p.account_link()
        check("填了码就能关联成功（不再说「没填」）",
              bool(res.get("ok")) and bool(res.get("web_token")),
              repr(res))
        check("关联到了正确的 QQ 号", res.get("user_id") == "qq1001",
              repr(res.get("user_id")))

        # 换来的令牌能用
        token = res.get("web_token")
        bind(p, FakeRequest(json_body={"_wt": token}))
        uid = await p._route_user_id()
        check("令牌能换回 QQ 号", uid == "qq1001", repr(uid))

        # 错误码要报错，不是静默
        bind(p, FakeRequest(json_body={"code": "ZZZZZZ"}))
        res = await p.account_link()
        check("错的码报错", not res.get("ok") and bool(res.get("error")),
              repr(res))

        # 一个字母都没填
        bind(p, FakeRequest(json_body={}))
        res = await p.account_link()
        check("真没填时报错（这次才是对的）",
              not res.get("ok") and "没填" in str(res.get("error")),
              repr(res))

        await p.terminate()

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 2b. 关联必须落在服务端 —— 用户报的那个 bug
#
# 现象：网页提示"已关联到 QQ xxx"，可状态页还是"先关联 QQ 号"，
# 日志面板也一直读不出来。
#
# 根因不在绑定码逻辑，而在**令牌存不住**：AstrBot 把插件页面放进带 sandbox、
# 没有 allow-same-origin 的 iframe 里（dashboard/src/views/PluginViewPage.vue），
# 页面是不透明源，`window.localStorage` 一读一写都抛 SecurityError。
#
# 所以下面这些假 request **故意一个字都不带 `_wt`** ——
# 这正是沙箱 iframe 里发出来的真实请求。
# ---------------------------------------------------------------------------

def test_link_survives_without_token():
    print("\n[2b] 令牌存不住时，关联还得记住")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_rt4_")
        p = await make_plugin(tmp)
        await p.store.ensure_user("qq3003")
        code = await p.store.create_link_code("qq3003")

        # 认领的那一刻请求里有「Dashboard 登录名」，这是服务端事实
        bind(p, FakeRequest(json_body={"code": code}, username="admin"))
        res = await p.account_link()
        check("关联成功", bool(res.get("ok")), repr(res))
        check("并且记在了服务端（remembered=True）",
              res.get("remembered") is True, repr(res))

        # ① 关掉浏览器再打开：localStorage 里什么都没有，只有 Dashboard 登录名
        bind(p, FakeRequest(json_body={}, username="admin"))
        uid = await p._route_user_id()
        check("没有令牌也能认出 QQ 号", uid == "qq3003", repr(uid))
        st = await p.account_status()
        check("状态页不再退回 need_link",
              st.get("user_id") == "qq3003" and not st.get("need_link"),
              repr(st)[:200])

        # ② 日志面板同理（原来这里一直显示"读日志失败"）
        lg = await p.account_log()
        check("日志面板读得出来",
              "lines" in lg and not lg.get("need_link"), repr(lg)[:200])

        # ③ 别人的 Dashboard 账号不该蹭到
        bind(p, FakeRequest(json_body={}, username="someone-else"))
        check("没绑过的账号认不出身份（不猜）",
              await p._route_user_id() == "")

        # ④ 解绑之后服务端那条关联也要断掉
        bind(p, FakeRequest(json_body={}, username="admin"))
        await p.account_unlink()
        check("解绑后服务端关联也没了", await p._route_user_id() == "")

        await p.terminate()

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 3. 其它路由也要能跑通
# ---------------------------------------------------------------------------

def test_other_routes():
    print("\n[3] 其它路由")

    async def main_():
        tmp = tempfile.mkdtemp(prefix="xcpc_rt4_")
        p = await make_plugin(tmp)

        # 没令牌 → need_link，不是崩
        bind(p, FakeRequest(method="GET"))
        res = await p.account_status()
        check("没关联时 status 返回 need_link",
              res.get("need_link") is True, repr(res)[:120])
        check("并且给了怎么做", bool(res.get("message")), repr(res)[:120])

        # 有关联 → 有平台列表
        await p.store.ensure_user("qq2002")
        tok, _ = await p.store.claim_link_code(
            await p.store.create_link_code("qq2002"))
        bind(p, FakeRequest(query={"_wt": tok}, method="GET"))
        res = await p.account_status()
        check("关联后 status 有平台列表",
              isinstance(res.get("platforms"), list) and not res.get("need_link"),
              repr(res)[:150])

        # 令牌从 query 也能取（GET 没有 body）
        bind(p, FakeRequest(query={"_wt": tok}, method="GET"))
        check("GET 时令牌从 query 取", await p._route_token() == tok)

        # 令牌从 cookie 也能取
        bind(p, FakeRequest(cookies={"xcpc_wt": tok}, method="GET"))
        check("令牌从 cookie 也能取", await p._route_token() == tok)

        # logout / unlink 不该抛
        for name in ("account_logout", "account_unlink"):
            bind(p, FakeRequest(json_body={"platform": "codeforces"}))
            try:
                await getattr(p, name)()
                check("%s 不抛" % name, True)
            except Exception as exc:
                check("%s 不抛" % name, False, "%s: %s" % (type(exc).__name__, exc))

        # 日志路由
        bind(p, FakeRequest(query={"n": "5"}, method="GET"))
        try:
            r = await p.account_log()
            check("日志路由能跑", r is not None)
        except Exception as exc:
            check("日志路由能跑", False, "%s: %s" % (type(exc).__name__, exc))

        await p.terminate()

    asyncio.run(main_())


# ---------------------------------------------------------------------------
# 4. 假 request 必须和真的形状一致
# ---------------------------------------------------------------------------

def test_fake_matches_real():
    print("\n[4] 假 request 和真的一致（否则等于把 bug 藏起来）")

    async def main_():
        req = FakeRequest(json_body={"a": 1})
        for name in ("json", "body", "form"):
            m = getattr(req, name, None)
            check("假 request 的 %s() 是协程" % name,
                  m is not None and inspect.iscoroutinefunction(m),
                  "真 API 里它是 async 的")
        check("query.get 是同步的（真的也是）",
              not inspect.iscoroutinefunction(req.query.get))

    asyncio.run(main_())


def main() -> int:
    print("=" * 62)
    print("Web 路由测试")
    print("=" * 62)
    test_is_async()
    test_body_is_read()
    test_link_route()
    test_link_survives_without_token()
    test_other_routes()
    test_fake_matches_real()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
