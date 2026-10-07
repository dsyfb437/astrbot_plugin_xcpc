#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/accounts.py 的自测 —— 用假 client，不联网。

重点：
  1. **会话属主隔离** —— 别人拿到 session_id 也不能操作
  2. **凭据绝不进 public() 快照**
  3. QOJ 的两步流程（token → 密码 → 2FA → cookie）
  4. 登录失败时**不把凭据标成 valid**
  5. cookie 文本解析 + 换行注入拦截
  6. 解绑只删凭据，不删做题数据

跑法：python tests/test_accounts.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import accounts as accm  # noqa: E402
from core import db as dbm         # noqa: E402
from core import store as stm      # noqa: E402
from platforms.base import Fetched, Submission  # noqa: E402

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
# 假 HTTP client
# ---------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status=200, text="", cookies=None):
        self.status = status
        self.text = text
        self.body = text.encode()
        self._cookies = cookies or {}

    def json(self):
        import json
        return json.loads(self.text)

    def __bool__(self):
        return 200 <= self.status < 300


class FakeClient:
    """按 URL 关键词决定返回什么。`cookies` 模拟登录后服务端下发的会话。

    ⚠️ **路由按"键的长度从长到短"匹配**，不是按字典顺序。

    踩过的坑：路由里同时有 `/login` 和 `/qoj.ac/login` 时，
    子串匹配会让 `/login` 先命中 `/qoj.ac/login` ——
    于是"POST 登录拿 2fa 响应"变成了"拿回登录页 HTML"，
    测试报的是"登录没有拿到 cookie"，看着像代码问题，其实是夹具匹配错了。
    精确的键应该优先。
    """

    def __init__(self, routes=None, cookies=None):
        self.routes = routes or {}
        self.cookies = dict(cookies or {})
        self.requests = []

    def set_cookies(self, c):
        # 复刻 http.py 的注入防护：含换行的丢掉
        clean = {}
        for k, v in (c or {}).items():
            if any(x in str(k) for x in "\r\n") or any(x in str(v) for x in "\r\n"):
                continue
            clean[k] = v
        self.cookies = clean

    def cookie_header(self):
        return "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())

    def _match(self, url, method="GET"):
        """路由键可以是 `"子串"` 或 `("METHOD", "子串")`。

        **需要方法区分**：QOJ 的 `GET /login` 要返回登录页（里面有 token），
        而 `POST /login` 要返回登录结果 —— 同一个 URL，两种响应。
        只按 URL 匹配的话，两者会撞在一起，表现是"登录页里找不到 token"，
        看着像代码问题。键按长度从长到短匹配，让更精确的优先。
        """
        keys = sorted(self.routes, key=lambda k: len(k[1] if isinstance(k, tuple) else k),
                      reverse=True)
        for key in keys:
            if isinstance(key, tuple):
                m, sub = key
                if m != method or sub not in url:
                    continue
            else:
                if key not in url:
                    continue
            val = self.routes[key]
            return val(self) if callable(val) else val
        return None

    async def get(self, url, **kw):
        self.requests.append(("GET", url))
        r = self._match(url, "GET")
        return r if r is not None else FakeResp(200, "")

    async def post_form(self, url, data, **kw):
        self.requests.append(("POST", url))
        r = self._match(url, "POST")
        return r if r is not None else FakeResp(200, "")


QOJ_LOGIN_PAGE = (
    '<form id="form-login" method="post">…</form>'
    '<script>$.post("/login", { _token : "TOK1234567890abcdef", '
    'username: $("#input-username").val() });</script>'
)


async def setup(routes=None, cookies=None, pre_cookies=None):
    tmp = tempfile.mkdtemp(prefix="xcpc_acc_")
    db = dbm.Database(os.path.join(tmp, "t.db"))
    ok, detail = await db.open()
    assert ok, detail
    store = stm.Store(db)
    client = FakeClient(routes=routes, cookies=cookies)

    async def factory(user_id, platform):
        # 把该用户已存的凭据装进去（复刻真实行为）
        c = FakeClient(routes=routes, cookies=dict(cookies or {}))
        if pre_cookies:
            c.set_cookies(pre_cookies)
        else:
            saved = await store.get_credentials(user_id, platform)
            if saved:
                c.set_cookies(saved)
        client.last = c
        return c

    svc = accm.AccountService(store, client_factory=factory)
    return db, store, svc, client


# ---------------------------------------------------------------------------
# 1. cookie 解析
# ---------------------------------------------------------------------------

def test_parse_cookie():
    print("\n[1] cookie 文本解析")
    cases = [
        ("SESSDATA=abc; bili_jct=def", {"SESSDATA": "abc", "bili_jct": "def"}),
        ("Cookie: SESSDATA=abc; bili_jct=def", {"SESSDATA": "abc", "bili_jct": "def"}),
        ("SESSDATA=abc\nbili_jct=def", {"SESSDATA": "abc", "bili_jct": "def"}),
        ('SESSDATA="abc"', {"SESSDATA": "abc"}),
        ("  SESSDATA = abc  ", {"SESSDATA": "abc"}),
    ]
    for raw, want in cases:
        got = accm.parse_cookie_text(raw)
        check("解析 %r" % raw[:30], got == want, "%r vs %r" % (got, want))

    check("空输入返回空", accm.parse_cookie_text("") == {})
    check("垃圾输入不崩", accm.parse_cookie_text("不是cookie") == {})

    # 换行注入。
    #
    # 我第一版的断言是「`bad` 应该被整个丢掉」—— **那是错的**。
    # 实际实现先按 `[;\n\r]+` 切分，于是：
    #     "good=1; bad=2\r\nX-Injected: 1"
    #   → ["good=1", " bad=2", "X-Injected: 1"]
    # `bad=2` 是个**干净的值**，保留它是对的；
    # `X-Injected: 1` 里没有 `=`，自然被丢弃 —— 注入被"切分"本身化解了。
    #
    # 所以真正该断言的安全属性是：**解析结果里没有任何控制字符**，
    # 而不是"某个特定的键被丢掉"。
    for raw in ("good=1; bad=2\r\nX-Injected: 1",
                "SESSDATA=abc\r\n\r\nEvil: 1",
                "a=1\nb=2\nc=3"):
        got = accm.parse_cookie_text(raw)
        bad = {k: v for k, v in got.items()
               if any(c in k for c in "\r\n\x00") or any(c in v for c in "\r\n\x00")}
        check("解析结果无控制字符（%r）" % raw[:22], not bad, repr(bad))

    got = accm.parse_cookie_text("good=1; bad=2\r\nX-Injected: 1")
    check("干净的值被保留", got.get("good") == "1")
    check("注入出来的字段没被当成 cookie", "X-Injected" not in got, repr(got))

    check("cookie_status 判空", accm.cookie_status({}) == "unbound")
    check("cookie_status 判有", accm.cookie_status({"a": "1"}) == "valid")


# ---------------------------------------------------------------------------
# 2. 会话属主隔离
# ---------------------------------------------------------------------------

def test_session_owner():
    print("\n[2] 会话属主隔离")

    async def main():
        db, store, svc, _ = await setup(routes={
            ("GET", "/login"): FakeResp(200, QOJ_LOGIN_PAGE),
            ("POST", "/login"): lambda c: (c.cookies.update({"__client_id": "x"})
                                           or FakeResp(200, "{}")),
        })
        snap = await svc.start_login("qq1001", "qoj",
                                     {"username": "u", "password": "p"})
        sid = snap["session_id"]
        check("A 的登录成功", snap["state"] == "ok", snap["message"])

        # B 拿到同一个 session_id
        try:
            await svc.session_state(sid, "qq2002")
            check("B 不能读 A 的会话", False, "居然能读")
        except accm.AccountError as exc:
            check("B 不能读 A 的会话", "不属于你" in str(exc), str(exc))

        try:
            await svc.cancel(sid, "qq2002")
            check("B 不能取消 A 的会话", False, "居然能取消")
        except accm.AccountError:
            check("B 不能取消 A 的会话", True)

        try:
            await svc.submit_2fa(sid, "qq2002", "123456")
            check("B 不能提交 A 的两步验证", False, "居然能提交")
        except accm.AccountError:
            check("B 不能提交 A 的两步验证", True)

        # A 自己能操作
        st = await svc.session_state(sid, "qq1001")
        check("A 自己能读", st["session_id"] == sid)

        # 不存在的会话
        try:
            await svc.session_state("nope", "qq1001")
            check("不存在的会话报错", False)
        except accm.AccountError:
            check("不存在的会话报错", True)

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. 凭据不进快照
# ---------------------------------------------------------------------------

def test_no_credential_leak():
    print("\n[3] 凭据不进 public 快照")

    async def main():
        db, store, svc, _ = await setup(routes={
            ("GET", "/login"): FakeResp(200, QOJ_LOGIN_PAGE),
            ("POST", "/login"): lambda c: (c.cookies.update(
                {"__client_id": "SUPER_SECRET_SESSION"}) or FakeResp(200, "{}")),
        })
        snap = await svc.start_login("qq1001", "qoj",
                                     {"username": "u", "password": "p"})
        check("快照里没有 cookies 字段", "cookies" not in snap, repr(snap.keys()))
        check("快照里没有 ctx 字段", "ctx" not in snap, repr(snap.keys()))
        check("快照的字符串里没有密钥",
              "SUPER_SECRET" not in repr(snap), repr(snap))

        # 状态接口也不能泄漏
        st = await svc.status("qq1001")
        check("状态接口里没有密钥",
              "SUPER_SECRET" not in repr(st), repr(st)[:300])
        check("状态接口有 platform 列表", len(st["platforms"]) == 4)

        # 但 get_cookies 应该拿得到（那是同步层要用的）
        ck = await svc.get_cookies("qq1001", "qoj")
        check("get_cookies 能拿到（给同步层用）",
              ck.get("__client_id") == "SUPER_SECRET_SESSION", repr(ck)[:80])

        # repr 里也不该有
        for s in svc._sessions.values():
            check("Session repr 不含 cookie", "SUPER_SECRET" not in repr(s),
                  repr(s)[:200])

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. QOJ 两步流程
# ---------------------------------------------------------------------------

def test_qoj_2fa():
    print("\n[4] QOJ 两步验证")

    async def main():
        # 第一次登录返回"要 2fa"
        db, store, svc, _ = await setup(routes={
            "/login/2fa": lambda c: (c.cookies.update({"__client_id": "after2fa"})
                                     or FakeResp(200, "{}")),
            "/login": FakeResp(200, QOJ_LOGIN_PAGE),
        })
        # 让 /login POST 返回 need 2fa（不带 cookie）
        svc._auth["qoj"].origin = "https://qoj.ac"
        # 用一个自定义 route：POST 到 /login 时不给 cookie
        db2, store2, svc2, _ = await setup(routes={
            ("GET", "/login"): FakeResp(200, QOJ_LOGIN_PAGE),
            ("POST", "/login/2fa"): lambda c: (c.cookies.update(
                {"__client_id": "ok2fa"}) or FakeResp(200, "{}")),
            ("POST", "/login"): FakeResp(200, '{"msg":"2fa"}'),
        })
        snap = await svc2.start_login("qq1001", "qoj",
                                      {"username": "u", "password": "p"})
        check("识别出需要两步验证", snap["state"] == "need_2fa", snap["message"])
        check("快照里带 need_2fa 标记", snap.get("need_2fa") is True)
        check("快照里没有 token", "TOK1234" not in repr(snap), repr(snap))
        check("还没落库", await store2.get_credentials("qq1001", "qoj") == {})

        # 没填验证码
        snap2 = await svc2.submit_2fa(snap["session_id"], "qq1001", "")
        check("空验证码时保持 need_2fa", snap2["state"] == "need_2fa", snap2["message"])

        # 填对了
        snap3 = await svc2.submit_2fa(snap["session_id"], "qq1001", "123456")
        check("两步验证后登录成功", snap3["state"] == "ok", snap3["message"])
        ck = await store2.get_credentials("qq1001", "qoj")
        check("凭据落库了", ck.get("__client_id") == "ok2fa", repr(ck)[:80])

        # 会话过期（丢了 token）要如实报
        s = svc2._sessions[snap["session_id"]]
        s.ctx.pop("_token", None)
        s.state = accm.S_NEED_2FA
        snap4 = await svc2.submit_2fa(snap["session_id"], "qq1001", "999999")
        check("丢了 token 时提示重新登录",
              snap4["state"] == "failed" and "重新登录" in snap4["message"],
              snap4["message"])

        await db.close()
        await db2.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 5. 失败不写凭据
# ---------------------------------------------------------------------------

def test_failure_no_persist():
    print("\n[5] 登录失败不写凭据")

    async def main():
        db, store, svc, _ = await setup(routes={
            ("GET", "/login"): FakeResp(200, QOJ_LOGIN_PAGE),
            ("POST", "/login"): FakeResp(200, '{"msg":"failed"}'),
        })
        snap = await svc.start_login("qq1001", "qoj",
                                     {"username": "u", "password": "wrong"})
        check("密码错时失败", snap["state"] == "failed", snap["message"])
        check("说明里点出密码不对", "密码" in snap["message"], snap["message"])
        check("没有把凭据标成 valid",
              await store.get_credentials("qq1001", "qoj") == {})
        st = await store.credential_status("qq1001")
        check("状态里没有 qoj 的记录（不是 unbound 之外的东西）",
              "qoj" not in st or st["qoj"]["status"] != "valid", repr(st))

        # 拿不到 token（改版）
        db2, store2, svc2, _ = await setup(routes={
            ("GET", "/login"): FakeResp(200, "<html>没有 token</html>"),
        })
        snap2 = await svc2.start_login("qq1001", "qoj",
                                       {"username": "u", "password": "p"})
        check("找不到 token 时失败", snap2["state"] == "failed", snap2["message"])
        check("说明里提到改版", "改版" in snap2["message"], snap2["message"])
        await db2.close()
        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 6. CF/AtCoder：填 handle 要真的验证
# ---------------------------------------------------------------------------

def test_handle_only():
    print("\n[6] CF / AtCoder 的 handle 验证")

    async def main():
        db, store, svc, _ = await setup()
        # 换掉 cf 的 fetcher，模拟"handle 不存在"
        async def bad_fetch(handle, client):
            return Fetched(ok=False, error_kind="凭据失效", detail="handle 不存在")
        svc._auth["codeforces"] = accm.HandleOnly(
            platform="codeforces", fetcher=bad_fetch)

        snap = await svc.start_login("qq1001", "codeforces", {"handle": "nobody"})
        check("handle 不存在时明确失败", snap["state"] == "failed", snap["message"])
        check("说明里带分类", "凭据失效" in snap["message"], snap["message"])
        check("没写 handle 进库",
              await store.get_handle("qq1001", "codeforces") == "")

        # 正常情况
        async def good_fetch(handle, client):
            return Fetched(ok=True, items=[Submission("codeforces", "1", "CF:1A")])
        svc._auth["codeforces"] = accm.HandleOnly(
            platform="codeforces", fetcher=good_fetch)
        snap2 = await svc.start_login("qq1001", "codeforces", {"handle": "alice"})
        check("验证通过", snap2["state"] == "ok", snap2["message"])
        check("说明里报了拉到几条", "1 条" in snap2["message"], snap2["message"])
        check("handle 落库了",
              await store.get_handle("qq1001", "codeforces") == "alice")

        # 格式校验
        snap3 = await svc.start_login("qq1001", "codeforces", {"handle": "a"})
        check("太短时拒绝", snap3["state"] == "failed", snap3["message"])
        snap4 = await svc.start_login("qq1001", "codeforces", {"handle": "a b c"})
        check("含空格时拒绝", snap4["state"] == "failed", snap4["message"])
        snap5 = await svc.start_login("qq1001", "codeforces", {"handle": ""})
        check("空 handle 拒绝", snap5["state"] == "failed", snap5["message"])

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 7. 手动导入 Cookie
# ---------------------------------------------------------------------------

def test_manual_cookie():
    print("\n[7] 手动导入 Cookie（洛谷的路子）")

    async def main():
        db, store, svc, _ = await setup()
        # 一个必填 + 一个选填 —— 把 optional 那条分支也走到
        async def ok_verify(cookies, client):
            return True, "验证通过"
        svc._auth["luogu"] = accm.ManualCookie(
            platform="luogu", required=("__client_id",),
            optional=("_uid",), verifier=ok_verify)

        snap = await svc.start_login(
            "qq1001", "luogu", {"__client_id": "xyz", "_uid": "123456"})
        check("导入成功", snap["state"] == "ok", snap["message"])
        ck = await store.get_credentials("qq1001", "luogu")
        check("凭据落库", ck.get("__client_id") == "xyz", repr(ck)[:80])
        check("选填的字段填了就存", ck.get("_uid") == "123456", repr(ck)[:80])

        # 选填的没填 → 不塞空值进库
        await svc.start_login("qq1002", "luogu", {"__client_id": "only"})
        ck2 = await store.get_credentials("qq1002", "luogu")
        check("选填的没填就不往库里塞空串",
              ck2 == {"__client_id": "only"}, repr(ck2)[:80])

        # 必填漏了 → 点名
        snap15 = await svc.start_login("qq1003", "luogu", {"_uid": "1"})
        check("漏必填时点名", snap15["state"] == "failed"
              and "__client_id" in snap15["message"], snap15["message"])

        # 一个框都不填 —— 有必填项时报的是缺哪个
        snap2 = await svc.start_login("qq1004", "luogu", {})
        check("空输入时失败", snap2["state"] == "failed", snap2["message"])
        check("说明里点名缺的字段", "__client_id" in snap2["message"],
              snap2["message"])

        # 全是选填的平台：一个都不填时说"一个 cookie 都没填"
        svc._auth["luogu"] = accm.ManualCookie(
            platform="luogu", optional=("a",), verifier=ok_verify)
        snap25 = await svc.start_login("qq1005", "luogu", {})
        check("没有必填项时说明里说清一个都没填",
              "一个 cookie 都没填" in snap25["message"], snap25["message"])

        # 验证不通过
        async def bad_verify(cookies, client):
            return False, "这段 Cookie 用不了"
        svc._auth["luogu"] = accm.ManualCookie(
            platform="luogu", required=("__client_id",), verifier=bad_verify)
        snap3 = await svc.start_login("qq1006", "luogu", {"__client_id": "a"})
        check("验证不过时失败", snap3["state"] == "failed", snap3["message"])
        check("验证不过时不落库",
              await store.get_credentials("qq1006", "luogu") == {})

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 8. 解绑
# ---------------------------------------------------------------------------

def test_logout():
    print("\n[8] 解绑")

    async def main():
        db, store, svc, _ = await setup()
        await store.set_credentials("qq1001", "qoj", {"__client_id": "x"})
        await store.set_handle("qq1001", "qoj", "someone")
        await store.upsert_submissions("qq1001", "qoj", [
            Submission("qoj", "1", "QOJ:1")])

        r = await svc.logout("qq1001", "qoj")
        check("返回 unbound", r["status"] == "unbound")
        check("凭据被删", await store.get_credentials("qq1001", "qoj") == {})
        check("做题数据**没有**被删（解绑不该清历史）",
              await store.count_submissions("qq1001", "qoj") == 1,
              "%d" % await store.count_submissions("qq1001", "qoj"))

        # 只影响自己
        await store.set_credentials("qq2002", "qoj", {"__client_id": "y"})
        await svc.logout("qq1001", "qoj")
        check("不影响别人的凭据",
              (await store.get_credentials("qq2002", "qoj")).get("__client_id") == "y")

        try:
            await svc.logout("", "qoj")
            check("空 user_id 被拒绝", False)
        except accm.AccountError:
            check("空 user_id 被拒绝", True)

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 9. 取消会清掉半截凭据
# ---------------------------------------------------------------------------

def test_cancel():
    print("\n[9] 取消登录")

    async def main():
        db, store, svc, _ = await setup(routes={
            ("GET", "/login"): FakeResp(200, QOJ_LOGIN_PAGE),
            ("POST", "/login"): FakeResp(200, '{"msg":"2fa"}'),
        })
        snap = await svc.start_login("qq1001", "qoj",
                                     {"username": "u", "password": "p"})
        s = svc._sessions[snap["session_id"]]
        s.cookies["half"] = "way"      # 模拟"已经拿到一半"
        r = await svc.cancel(snap["session_id"], "qq1001")
        check("状态是 cancelled", r["state"] == "cancelled", r["message"])
        check("半截凭据被清掉", not svc._sessions[snap["session_id"]].cookies)
        check("没落库", await store.get_credentials("qq1001", "qoj") == {})
        await db.close()

    asyncio.run(main())


def _page_fields() -> dict:
    """从 pages/accounts/app.js 里抠出每个平台的表单字段名。

    页面是手写的 JS，后端是 Python —— 两边各写一份 cookie 名字，
    很容易改了一边忘了另一边（用户填了框，后端根本不看）。
    这个函数让测试能把两边对一遍。
    """
    path = os.path.join(HERE, "..", "pages", "accounts", "app.js")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        body = src.split("var PLATFORMS = [", 1)[1].split("\n  ];", 1)[0]
    except IndexError:
        return {}
    out: dict = {}
    cur = None
    for m in re.finditer(r'id:\s*"(\w+)"|k:\s*"([^"]+)"', body):
        if m.group(1):
            cur = m.group(1)
            out[cur] = []
        elif cur:
            out[cur].append(m.group(2))
    return out


# ---------------------------------------------------------------------------
# 10. 绑完得显示"已绑定"（真机反馈：CF / AtCoder 绑完还是未绑定）
# ---------------------------------------------------------------------------

def test_bound_status():
    print("\n[10] 绑定状态（handle-only 平台 + 洛谷的 uid）")

    async def main():
        db, store, svc, _ = await setup()

        # 这条盯着两边别走散：谁被当成 handle-only，谁就必须真的是 HandleOnly
        auths = accm.default_authenticators()
        handle_only = tuple(sorted(p for p, a in auths.items()
                                   if isinstance(a, accm.HandleOnly)))
        check("store.HANDLE_ONLY 和 HandleOnly 认证器一致",
              handle_only == tuple(sorted(stm.HANDLE_ONLY)),
              "%r vs %r" % (handle_only, stm.HANDLE_ONLY))

        # --- CF：没有 credentials 行，绑定成功的标志就是那一列 handle
        async def cf_fetch(handle, client):
            return Fetched(ok=True, items=[Submission("codeforces", "1", "CF:1A")])
        svc._auth["codeforces"] = accm.HandleOnly(
            platform="codeforces", fetcher=cf_fetch)

        got = await svc.start_login("qq1001", "codeforces", {"handle": "tourist"})
        check("CF 绑定成功", got["state"] == "ok", got["message"])
        check("CF 的 handle 落库",
              await store.get_handle("qq1001", "codeforces") == "tourist")
        check("CF 没有 credentials 行（它本来就不需要登录）",
              await store.get_credentials("qq1001", "codeforces") == {})

        plat = {p["platform"]: p for p in (await svc.status("qq1001"))["platforms"]}
        check("CF 在页面上是**已绑定**，不是未绑定",
              plat["codeforces"]["status"] == "valid", plat["codeforces"]["status"])
        check("CF 的 handle 也回给页面了", plat["codeforces"]["handle"] == "tourist")
        check("没绑的 AtCoder 仍然是未绑定",
              plat["atcoder"]["status"] == "unbound", plat["atcoder"]["status"])

        # 解绑必须真的解掉 —— 否则按钮点了跟没点一样
        await svc.logout("qq1001", "codeforces")
        plat2 = {p["platform"]: p for p in (await svc.status("qq1001"))["platforms"]}
        check("CF 解绑后回到未绑定",
              plat2["codeforces"]["status"] == "unbound", plat2["codeforces"]["status"])
        check("CF 解绑后 handle 也清空了",
              await store.get_handle("qq1001", "codeforces") == "")
        check("解绑没波及别人",
              await store.get_handle("qq1001", "atcoder") == "")

        # --- 洛谷：uid 得从 _uid 里认出来，否则同步永远说"还没绑定 handle"
        async def ok_verify(cookies, client):
            return True, "验证通过"
        luogu_auth = auths["luogu"]
        check("洛谷的认证器认得 _uid（认不出来同步就是死的）",
              getattr(luogu_auth, "uid_cookie", "") == "_uid", repr(luogu_auth))
        check("洛谷要求 __client_id 和 _uid 两个字段",
              tuple(getattr(luogu_auth, "required", ())) == ("__client_id", "_uid"),
              repr(getattr(luogu_auth, "required", ())))
        check("洛谷不要求填 C3VK（那是 CDN 挑战 cookie，http.py 自己会解）",
              "C3VK" not in tuple(getattr(luogu_auth, "cookie_names", ())),
              repr(getattr(luogu_auth, "cookie_names", ())))
        luogu_auth.verifier = ok_verify     # 只换验证器，其余配置留真的
        svc._auth["luogu"].verifier = ok_verify

        # 一个 cookie 一个框
        r = await svc.start_login("qq1001", "luogu",
                                  {"__client_id": "xyz", "_uid": "123456"})
        check("洛谷按字段导入成功", r["state"] == "ok", r["message"])
        check("洛谷 uid 从 _uid 框里认出来了",
              await store.get_handle("qq1001", "luogu") == "123456",
              await store.get_handle("qq1001", "luogu"))
        check("回话里说了用到哪几个 cookie",
              "用到 2 个 cookie" in r["message"], r["message"])
        check("回话里**没有**回显 cookie 的值", "xyz" not in r["message"], r["message"])
        check("存下来的凭据就是那两个字段",
              await store.get_credentials("qq1001", "luogu")
              == {"__client_id": "xyz", "_uid": "123456"},
              repr(await store.get_credentials("qq1001", "luogu")))
        plat3 = {p["platform"]: p for p in (await svc.status("qq1001"))["platforms"]}
        check("洛谷页面显示已绑定",
              plat3["luogu"]["status"] == "valid", plat3["luogu"]["status"])

        # 少填一个就点名，不能含糊
        r3 = await svc.start_login("qq3003", "luogu", {"__client_id": "xyz"})
        check("只填了 __client_id 时失败", r3["state"] == "failed", r3["message"])
        check("失败时点名 _uid", "_uid" in r3["message"], r3["message"])
        check("失败时不落库", await store.get_credentials("qq3003", "luogu") == {})
        r4 = await svc.start_login("qq4004", "luogu", {"_uid": "123"})
        check("只填了 _uid 时点名 __client_id",
              r4["state"] == "failed" and "__client_id" in r4["message"],
              r4["message"])
        check("一个都不填时也说清楚",
              (await svc.start_login("qq5005", "luogu", {}))["state"] == "failed")

        # 容忍三种填法：值 / 名字=值 / 整条 Cookie 串
        for who, fields in (
                ("qq6006", {"__client_id": "__client_id=full", "_uid": "8"}),
                ("qq7007", {"__client_id": "a=b; __client_id=spill; _uid=9"}),
                ("qq8008", {"__client_id": "justvalue", "_uid": "_uid=10"}),
        ):
            rr = await svc.start_login(who, "luogu", fields)
            check("容忍填法 %s" % who, rr["state"] == "ok", rr["message"])
        check("名字=值 会被剥掉前缀",
              (await store.get_credentials("qq6006", "luogu"))["__client_id"] == "full",
              repr(await store.get_credentials("qq6006", "luogu")))
        check("整条粘进来会从里面捞（包括别的框）",
              await store.get_handle("qq7007", "luogu") == "9",
              await store.get_handle("qq7007", "luogu"))
        check("裸值原样存",
              (await store.get_credentials("qq8008", "luogu"))["__client_id"]
              == "justvalue")

        # 换行注入还是得挡（值的最后一道闸门在 http.py，这里先挡）
        bad = await svc.start_login(
            "qq9009", "luogu", {"__client_id": "ok\r\nX-Injected: 1", "_uid": "1"})
        check("带换行的值被拒", bad["state"] == "failed", bad["message"])

        # --- 页面上的字段必须和后端认的名字一致，否则用户填了也没人读
        page = _page_fields()
        for pf in ("qoj", "luogu"):
            a = auths[pf]
            cp = a.cookie_path if isinstance(a, accm.EitherOf) else a
            names = set(cp.cookie_names)
            have = set(page.get(pf, ()))
            check("%s：页面上的 cookie 框和后端认的名字一致（%s）"
                  % (pf, "、".join(sorted(names))), names <= have,
                  "页面字段 %r" % sorted(have))
            leftovers = have - names - {"username", "password", "handle"}
            check("%s：页面上没有后端不认的字段" % pf, not leftovers,
                  "多余字段 %r" % sorted(leftovers))
        check("洛谷页面上不再有那个大 Cookie 框",
              "cookies" not in set(page.get("luogu", ())), repr(page.get("luogu")))

        await db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 11. 一个平台两条登录路（QOJ：密码 或 Cookie）
# ---------------------------------------------------------------------------

def test_either_of():
    print("\n[11] QOJ 的密码登录 / Cookie 导入两条路")

    async def main():
        db, store, svc, _ = await setup()

        auth = svc._auth["qoj"]
        check("qoj 挂的是 EitherOf", isinstance(auth, accm.EitherOf), repr(auth))
        check("两步验证仍然可达（EitherOf 必须透传 submit_2fa）",
              hasattr(auth, "submit_2fa"))
        check("cookie 那条路认得 uoj_username",
              getattr(auth.cookie_path, "uid_cookie", "") == "uoj_username")

        async def ok_verify(cookies, client):
            return True, "验证通过，拉到 2 条记录"
        auth.cookie_path.verifier = ok_verify

        r = await svc.start_login("qq1001", "qoj",
                                  {"__client_id": "x", "uoj_username": "alice"})
        check("填了 cookie 就走 cookie 那条路", r["state"] == "ok", r["message"])
        check("QOJ 用户名落成 qoj_uid",
              await store.get_handle("qq1001", "qoj") == "alice",
              await store.get_handle("qq1001", "qoj"))
        check("QOJ 凭据落库",
              (await store.get_credentials("qq1001", "qoj")).get("__client_id") == "x")

        # 只填一半也算走了 cookie 那条路，然后点名缺什么
        r15 = await svc.start_login("qq1501", "qoj", {"__client_id": "x"})
        check("只填一个 cookie 时点名 uoj_username",
              r15["state"] == "failed" and "uoj_username" in r15["message"],
              r15["message"])

        # 一个 cookie 框都不填 → 仍然走原来的密码路
        r2 = await svc.start_login("qq2002", "qoj", {"username": "", "password": ""})
        check("没填 cookie 时走密码路（报用户名密码没填）",
              r2["state"] == "failed" and r2["message"] == "用户名和密码都要填",
              r2["message"])

        # 只填用户名、不填密码：还是密码路，不能因为漏填就走了别的路
        r3 = await svc.start_login("qq3003", "qoj", {"username": "alice"})
        check("只填用户名时仍报密码没填",
              r3["message"] == "用户名和密码都要填", r3["message"])

        await db.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/accounts.py 自测（假 client，不联网）")
    print("=" * 62)
    test_parse_cookie()
    test_session_owner()
    test_no_credential_leak()
    test_qoj_2fa()
    test_failure_no_persist()
    test_handle_only()
    test_manual_cookie()
    test_logout()
    test_cancel()
    test_bound_status()
    test_either_of()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
