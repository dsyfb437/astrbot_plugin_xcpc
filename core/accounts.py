#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多用户登录服务。

形态参考 `astrbot_plugin_listen_music`（B 站登录插件）：
**一个通用 `AccountService` + 每个平台一份 authenticator**。

为什么是这个形态
----------------
四个平台的登录差异很大：
  * CF / AtCoder —— 根本不用登录，填个 handle 就行
  * QOJ          —— 用户名密码 + CSRF token，**可能要两步验证**
  * 洛谷          —— 有 CDN 反爬，自动登录还没打通，走手动导入 Cookie

如果没有"通用 service"，这些差异会散进路由、页面、命令各处，
每加一个平台就要改四个地方。抽出来的话，加平台 = 加一份 authenticator。

会话的属主（owner）
-------------------
**每个登录会话都属于发起它的那个 QQ 号**（`owner_id`）。
别人拿到 session_id 也不能操作 —— 否则群里任何人都能替你登录/登出。

凭据出口
--------
只有 `get_cookies()` 会吐出 cookie，而且**只给同步层用**。
所有给界面/命令用的方法都只返回**状态**，不含任何凭据原文。
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from . import import_platform
from . import log as logm

# 会话多久没动作就作废（登录页开着忘了关的情况）
SESSION_TTL = 600

# 登录会话的状态
S_PENDING = "pending"        # 刚开始，等第一步
S_NEED_2FA = "need_2fa"      # 需要两步验证码
S_WORKING = "working"        # 正在请求
S_OK = "ok"                  # 成功
S_FAILED = "failed"          # 失败
S_CANCELLED = "cancelled"

TERMINAL = (S_OK, S_FAILED, S_CANCELLED)


class AccountError(Exception):
    def __init__(self, message: str, kind: str = "内部错误"):
        super().__init__(message)
        self.kind = kind if kind in logm.ERROR_KINDS else "内部错误"


@dataclass
class Session:
    """一次登录会话。**带 owner_id** —— 别人不能碰。"""
    session_id: str
    owner_id: str
    platform: str
    state: str = S_PENDING
    message: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    # 平台自己需要跨步骤带的东西（比如 QOJ 的 _token）。
    # **不进日志、不进 API 响应。**
    ctx: dict = field(default_factory=dict, repr=False)
    # 登录成功后的 cookie。**同上：只在内部流转。**
    cookies: dict = field(default_factory=dict, repr=False)

    def public(self, include_2fa_prompt: bool = True) -> dict:
        """给界面看的快照。

        **绝不含 ctx / cookies。** 这是硬约束 ——
        任何在这里漏出去的东西都会进浏览器、进日志、进截图。
        """
        d = {
            "session_id": self.session_id,
            "platform": self.platform,
            "state": self.state,
            "message": self.message,
            "terminal": self.state in TERMINAL,
        }
        if include_2fa_prompt and self.state == S_NEED_2FA:
            d["need_2fa"] = True
        return d

    def expired(self, now: float | None = None) -> bool:
        return ((now or time.time()) - self.updated_at) > SESSION_TTL


# ---------------------------------------------------------------------------
# cookie 文本解析（手动导入用）
# ---------------------------------------------------------------------------

def parse_cookie_text(text: str) -> dict[str, str]:
    """把浏览器里复制出来的 Cookie 文本解析成 dict。

    能处理几种常见形式：
        SESSDATA=abc; bili_jct=def
        Cookie: SESSDATA=abc; bili_jct=def        （带前缀）
        每行一个 `name=value`                        （开发者工具里复制的那种）

    **含 `\\r` 或 `\\n` 的值会被丢掉** —— 那是 header 注入的经典手法，
    而这些值最终会被拼进 HTTP 头。（http.py 里也有一道同样的闸门，
    两道都要有：不能只指望下层记得检查。）
    """
    out: dict[str, str] = {}
    if not text:
        return out
    s = str(text).strip()
    # 去掉可能的 "Cookie:" 前缀
    s = re.sub(r"(?i)^\s*cookie\s*:\s*", "", s)
    for part in re.split(r"[;\n\r]+", s):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not name or not value:
            continue
        # 名字和值都不许含控制字符
        if any(c in name for c in "\r\n\x00") or any(c in value for c in "\r\n\x00"):
            continue
        out[name] = value
    return out


def _cookie_field(raw: str, name: str) -> str:
    """把某个输入框里的东西变成这个 cookie 的值。

    每个 cookie 一个框，用户照着开发者工具里的名字填就行，不用自己拼分号。
    但为了让复制粘贴的人少返工，这三种填法都认：

        就是值              `abc123`
        `名字=值`           `_uid=123456`          （从工具里整行复制的）
        整条 Cookie 串      `a=b; _uid=123456`     （粘错了框也能救回来）

    含控制字符的一律丢掉 —— 这些值最后会被拼进 HTTP 头，
    这是 header 注入的经典入口。（`http.py` 里还有一道同样的闸门。）
    """
    raw = str(raw or "").strip()
    if not raw or "\x00" in raw:
        return ""
    if any(c in raw for c in ";\r\n"):
        return str(parse_cookie_text(raw).get(name) or "").strip()
    if raw.lower().startswith(name.lower() + "="):
        raw = raw[len(name) + 1:]
    return raw.strip().strip(";").strip().strip('"').strip("'")


def cookie_status(cookies: dict) -> str:
    """判断一份 cookie 像不像"能用的登录态"。

    只看**有没有关键字段**，不去发请求验证（那要联网，且各站点判据不同）。
    返回 `valid` / `unbound`。
    """
    if not cookies:
        return "unbound"
    return "valid"


# ---------------------------------------------------------------------------
# 各平台的 authenticator
# ---------------------------------------------------------------------------

@dataclass
class HandleOnly:
    """CF / AtCoder：不用登录，只需要一个 handle。

    「登录」这个动作在这里退化成「校验 handle 存不存在」——
    但**不能假装成功**：handle 打错的话，同步时会一直返回"零条"，
    而那看起来像"这人没练过"，不像"填错了"。
    """

    platform: str
    fetcher: Any = None      # async (handle, client) -> Fetched

    async def start(self, svc, session: Session, fields: dict) -> Session:
        handle = str(fields.get("handle") or fields.get("username") or "").strip()
        if not handle:
            session.state = S_FAILED
            session.message = "没填 handle"
            return session
        if len(handle) < 3 or len(handle) > 32 or " " in handle:
            session.state = S_FAILED
            session.message = "handle 格式不对（3-32 个字符，不能有空格）"
            return session

        # 真的去问一次，确认这个 handle 存在
        session.state = S_WORKING
        session.message = "正在验证…"
        client = await svc.make_client(session.owner_id, self.platform)
        try:
            got = await self.fetcher(handle, client)
        except Exception as exc:                       # noqa: BLE001
            session.state = S_FAILED
            session.message = "验证失败：%s" % exc
            return session

        if not got.ok:
            session.state = S_FAILED
            session.message = "%s（%s）" % (got.detail or "验证失败", got.error_kind)
            return session

        n = len(got.items)
        session.ctx["handle"] = handle
        session.state = S_OK
        session.message = "验证通过，拉到 %d 条记录" % n
        return session


# QOJ / UOJ 的会话 cookie 名。
#
# **不是 `__client_id`** —— 那是洛谷的（`platforms/luogu.py` 用），QOJ 上
# 根本没有这个 cookie。这里曾经写成 `__client_id` 过，是照着洛谷那条
# 抄过来的，白白把"登录成功"判成失败。
#
# 上游 `vfleaking/uoj` 的 `web/app/models/Session.php` 里是
# `session_name('UOJSESSID')`；qoj.ac 部署时加了 `__Host-` 前缀，实发头就是
# `set-cookie: __Host-UOJSESSID=…; path=/; secure; HttpOnly; SameSite=Lax`。
# 两个名字都认，别人自建的 UOJ 可能没前缀。
QOJ_SESSION_COOKIES = ("__Host-UOJSESSID", "UOJSESSID")


def has_qoj_session(jar: dict) -> bool:
    """这个 cookie 罐里有 QOJ 的会话吗。"""
    for key in jar or {}:
        if key in QOJ_SESSION_COOKIES or str(key).endswith("UOJSESSID"):
            return True
    return False


@dataclass
class QojLogin:
    """QOJ：用户名 + 密码，**可能要两步验证**。

    实测流程（2026-10）：
      1. `GET /login` → 页面里内联 JS 带 CSRF：
             $.post('/login', { _token : "Q4Aw…", ... })
      2. `POST /login` 带 `{_token, username, password}`
      3. 若服务端返回 `msg == '2fa'` → 需要走 `/login/2fa`
      4. 成功时**会下发会话 cookie**（靠这个判断，不靠状态码 ——
         登录失败也回 200，只在 JSON 里给 msg）

    ⚠️ 必须用浏览器 UA（curl UA 会被 Cloudflare 403）。
    这个由 `http.py` 的默认头保证。
    """

    platform: str = "qoj"
    origin: str = "https://qoj.ac"
    _token_re = re.compile(r"""_token\s*:\s*["']([^"']{8,200})["']""")

    async def _fetch_token(self, client) -> str:
        resp = await client.get(self.origin + "/login")
        m = self._token_re.search(resp.text)
        return m.group(1) if m else ""

    async def start(self, svc, session: Session, fields: dict) -> Session:
        username = str(fields.get("username") or "").strip()
        password = str(fields.get("password") or "")
        if not username or not password:
            session.state = S_FAILED
            session.message = "用户名和密码都要填"
            return session

        session.state = S_WORKING
        session.message = "正在登录…"
        client = await svc.make_client(session.owner_id, self.platform)

        token = await self._fetch_token(client)
        if not token:
            session.state = S_FAILED
            session.message = ("登录页里找不到 CSRF token —— "
                               "QOJ 可能改版了。请把这条报给我。")
            return session

        payload = {"_token": token, "username": username, "password": password}
        resp = await client.post_form(self.origin + "/login", payload)
        body = resp.text[:2000]

        # 成功判据：**拿到了会话 cookie**。不看状态码。
        got_cookies = dict(client.cookies)
        if has_qoj_session(got_cookies):
            session.cookies = got_cookies
            session.ctx["username"] = username
            session.state = S_OK
            session.message = "登录成功"
            return session

        low = body.lower()
        if "2fa" in low or "two-factor" in low:
            # **把 token 留在 ctx 里**，下一步要用；它是凭据，不进日志
            session.ctx["_token"] = token
            session.ctx["username"] = username
            session.state = S_NEED_2FA
            session.message = "这个账号开了两步验证，请输入验证码"
            return session
        if "failed" in low or "invalid" in low or "wrong" in low:
            session.state = S_FAILED
            session.message = "用户名或密码不对"
            return session

        session.state = S_FAILED
        session.message = ("登录没有拿到会话 cookie，原因不明。"
                           "把这条连同 /xcpc 日志 30 一起报给我。")
        return session

    async def submit_2fa(self, svc, session: Session, code: str) -> Session:
        code = str(code or "").strip()
        if not code:
            session.state = S_NEED_2FA
            session.message = "验证码是空的"
            return session
        token = session.ctx.get("_token") or ""
        if not token:
            session.state = S_FAILED
            session.message = "会话过期了（丢了两步验证的 token），请重新登录"
            return session

        session.state = S_WORKING
        session.message = "正在验证…"
        client = await svc.make_client(session.owner_id, self.platform)
        client.set_cookies({})       # 从头开始，别带上半截状态
        resp = await client.post_form(self.origin + "/login/2fa",
                                      {"_token": token, "code": code})
        got_cookies = dict(client.cookies)
        if has_qoj_session(got_cookies):
            session.cookies = got_cookies
            session.state = S_OK
            session.message = "登录成功"
            return session
        if resp.status in (400, 401, 403):
            session.state = S_NEED_2FA
            session.message = "验证码不对，再试一次"
            return session
        session.state = S_FAILED
        session.message = "两步验证失败（HTTP %d）" % resp.status
        return session


@dataclass
class ManualCookie:
    """手动导入 Cookie —— 每个 cookie 一个输入框，不用自己拼分号。

    **这不是降级路径，是设计内的路径。** 洛谷的自动登录还没打通，
    QOJ 也没有 OAuth（登录页上就账号密码两个框，实测过），
    而用户在浏览器里登录后复制 Cookie 是完全可行的 ——
    同样能拿到完整数据，只是步骤多一点。

    `required` / `optional` 里写的是 **cookie 名**，同时也是表单字段名 ——
    页面照着这两个元组渲染输入框，这边照着同样的名字取值，一处定义两处用。

    洛谷的 `C3VK` **不在这里**：那是 CDN 的挑战 cookie，值 5 分钟就过期
    （源码里写死 `max-age=300`），`core/http.py` 会自己解出来装上，
    让用户去填是白填。

    `uid_cookie` 是这个站点把**用户 id** 放在哪个 cookie 里（洛谷是 `_uid`）。
    洛谷的提交记录得靠 `/record/list?user=<uid>` 才拉得到，认不出来的话
    `users.luogu_uid` 就是空的，同步时会说"还没绑定 handle" ——
    明明刚提示过"验证通过"。所以这里取不到就**直接失败**，不装作成功。
    """

    platform: str
    required: tuple = ()      # 必填的 cookie 名
    optional: tuple = ()      # 选填的，填了就带上
    verifier: Any = None      # async (cookies, client) -> (ok, message)
    uid_cookie: str = ""      # 用户 id 在哪个 cookie 里（空 = 不在 cookie 里）
    needs_uid: bool = False   # 认不出用户 id 就别装作成功（提交记录按用户查）

    @property
    def cookie_names(self) -> tuple:
        """表单上要出现的输入框，顺序就是这个顺序。"""
        return tuple(self.required) + tuple(self.optional)

    async def start(self, svc, session: Session, fields: dict) -> Session:
        raws = {n: str(fields.get(n) or "") for n in self.cookie_names}
        # 有人会把整条 Cookie 串（带分号的那种）粘进某一个框 ——
        # 那就顺手从里面把别的字段捞出来，别让人白填一遍。
        spill: dict[str, str] = {}
        for raw in raws.values():
            if any(c in raw for c in ";\r\n"):
                spill.update(parse_cookie_text(raw))

        cookies: dict[str, str] = {}
        missing: list[str] = []
        for name in self.cookie_names:
            value = _cookie_field(raws.get(name, ""), name) or spill.get(name, "")
            if value:
                cookies[name] = value
            elif name in self.required:
                missing.append(name)

        if missing:
            session.state = S_FAILED
            session.message = ("缺关键字段：%s。填了的字段有 %s —— "
                               "这些 cookie 登录之后才有，"
                               "没登录的话开发者工具里根本看不到。"
                               % ("、".join(missing),
                                  "、".join(sorted(cookies)) or "（一个都没有）"))
            return session
        if not cookies:
            session.state = S_FAILED
            session.message = "一个 cookie 都没填"
            return session

        # 用户 id 从哪来：优先 uid_cookie（洛谷的 `_uid`、QOJ 的 `uoj_username`），
        # 拿不到就退回表单上那个名字框 —— 有人只抄了会话 cookie，
        # 不该因为少抄一个就整个绑不上。
        uid = ""
        if self.uid_cookie:
            uid = str(cookies.get(self.uid_cookie) or "").strip()
        if not uid:
            uid = str(fields.get("username") or fields.get("handle") or "").strip()

        session.state = S_WORKING
        session.message = "正在验证…"
        client = await svc.make_client(session.owner_id, self.platform)
        client.set_cookies(cookies)
        if self.verifier is not None:
            try:
                ok, msg = await self.verifier(cookies, client)
            except Exception as exc:                   # noqa: BLE001
                session.state = S_FAILED
                session.message = "验证时出错：%s" % exc
                return session
            if not ok:
                session.state = S_FAILED
                session.message = msg or "这段 Cookie 用不了"
                return session
            session.message = msg or "验证通过"

        if self.needs_uid and not uid:
            session.state = S_FAILED
            where = ("没填 `%s`，也没在上面那个名字框里填名字"
                     % self.uid_cookie) if self.uid_cookie else \
                    "没在上面那个名字框里填名字"
            session.message = ("%s，认不出你是谁 —— "
                               "这个平台的提交记录得按用户查。" % where)
            return session

        session.cookies = cookies
        if uid:
            session.ctx["handle"] = uid
        # 把"用到了哪几个字段"说出来 —— 页面只回这一句话，
        # 用户看不见结果的话，少填一个就只能靠猜（值本身绝不回显）
        session.message = "%s（用到 %d 个 cookie：%s）" % (
            session.message, len(cookies), "、".join(sorted(cookies)))
        session.state = S_OK
        return session


@dataclass
class EitherOf:
    """同一平台有两条登录路：填了 Cookie 走 Cookie 导入，否则走主路径。

    QOJ 就属于这种 —— 没有 OAuth 可跳（登录页只有账号密码两个框），
    但你可以先在自己的浏览器里登录，把 Cookie 贴进来，
    **密码一次都不用经过这台服务器**。

    `submit_2fa` 之类只有主路径才有的方法用 `__getattr__` 透传，
    否则 `AccountService.submit_2fa` 里的 `hasattr(auth, "submit_2fa")`
    会变成假 —— 两步验证就静悄悄地没了。
    """

    primary: Any = None
    cookie_path: Any = None

    async def start(self, svc, session: Session, fields: dict) -> Session:
        # 判断依据：Cookie 那几个框里有没有填东西。填了就走 Cookie 那条路。
        names = tuple(getattr(self.cookie_path, "cookie_names", ()) or ())
        if self.cookie_path is not None and any(
                str(fields.get(n) or "").strip() for n in names):
            return await self.cookie_path.start(svc, session, fields)
        return await self.primary.start(svc, session, fields)

    def __getattr__(self, name):
        # 只在正常查找失败后才走到这里，所以主路径的方法（比如 submit_2fa）都能用
        primary = self.__dict__.get("primary")
        if primary is None:
            raise AttributeError(name)
        return getattr(primary, name)


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class AccountService:
    """登录会话管理 + 凭据落库。

    **所有公开方法都要求 `owner_id`**（QQ 号）——
    和 store 一样的设计：让"忘了隔离"在语法上就不成立。
    """

    def __init__(self, store, recorder: logm.Recorder | None = None,
                 client_factory=None, authenticators: dict | None = None) -> None:
        self.store = store
        self.recorder = recorder
        # `client_factory(user_id, platform) -> HttpClient`（异步）
        # 做成可注入的，测试能塞假 client
        self._client_factory = client_factory
        self._sessions: dict[str, Session] = {}
        self._auth = authenticators or default_authenticators()

    async def make_client(self, user_id: str, platform: str):
        """造一个带该用户凭据的 HTTP client。

        凭据**只在这一处**被读出来，读完立刻装进 client，
        不经过任何会打日志的路径。
        """
        if self._client_factory is None:
            raise AccountError("没有配置 client 工厂", "内部错误")
        return await self._client_factory(user_id, platform)

    # ---- 会话 ---------------------------------------------------------
    def _new_session(self, owner_id: str, platform: str) -> Session:
        now = time.time()
        s = Session(session_id=secrets.token_urlsafe(12), owner_id=owner_id,
                    platform=platform, created_at=now, updated_at=now)
        self._sessions[s.session_id] = s
        return s

    def _get(self, session_id: str, owner_id: str) -> Session:
        """取会话。**属主不对就拒绝** —— 否则群里任何人都能操作别人的登录。"""
        s = self._sessions.get(str(session_id or ""))
        if s is None:
            raise AccountError("登录会话不存在（可能已过期）", "凭据失效")
        if s.owner_id != str(owner_id or ""):
            # 记一笔：这可能是有人在试探别人的会话
            if self.recorder:
                self.recorder.event("auth.session_denied", user_id=owner_id,
                                    platform=s.platform, ok=False,
                                    error_kind="凭据失效",
                                    detail="有人尝试操作不属于自己的登录会话")
            raise AccountError("这个登录会话不属于你", "凭据失效")
        if s.expired() and s.state not in TERMINAL:
            s.state = S_FAILED
            s.message = "登录超时（放着太久没操作），请重新开始"
        s.updated_at = time.time()
        return s

    def _gc(self) -> None:
        """清掉过期会话，别让 dict 无限长。"""
        now = time.time()
        dead = [k for k, v in self._sessions.items()
                if v.expired(now) and v.state in TERMINAL]
        for k in dead:
            self._sessions.pop(k, None)

    # ---- 公开操作 -----------------------------------------------------
    async def start_login(self, owner_id: str, platform: str,
                          fields: dict) -> dict:
        """开始一次登录。返回**脱敏**快照。"""
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise AccountError("user_id 不能为空", "内部错误")
        auth = self._auth.get(platform)
        if auth is None:
            raise AccountError("不支持的平台：%s" % platform, "内部错误")

        self._gc()
        session = self._new_session(owner_id, platform)
        if self.recorder:
            self.recorder.event("auth.start", user_id=owner_id, platform=platform)

        try:
            session = await auth.start(self, session, fields or {})
        except Exception as exc:                        # noqa: BLE001
            session.state = S_FAILED
            session.message = "%s: %s" % (type(exc).__name__, exc)
            if self.recorder:
                self.recorder.event("auth.error", user_id=owner_id, platform=platform,
                                    ok=False, error_kind=getattr(exc, "kind", "内部错误"),
                                    detail=str(exc))

        session.updated_at = time.time()
        await self._persist_if_ok(owner_id, platform, session)
        return session.public()

    async def submit_2fa(self, session_id: str, owner_id: str, code: str) -> dict:
        session = self._get(session_id, owner_id)
        auth = self._auth.get(session.platform)
        if auth is None or not hasattr(auth, "submit_2fa"):
            raise AccountError("这个平台不需要两步验证", "内部错误")
        try:
            session = await auth.submit_2fa(self, session, code)
        except Exception as exc:                        # noqa: BLE001
            session.state = S_FAILED
            session.message = "%s: %s" % (type(exc).__name__, exc)
        session.updated_at = time.time()
        await self._persist_if_ok(session.owner_id, session.platform, session)
        return session.public()

    async def session_state(self, session_id: str, owner_id: str) -> dict:
        return self._get(session_id, owner_id).public()

    async def cancel(self, session_id: str, owner_id: str) -> dict:
        s = self._get(session_id, owner_id)
        if s.state not in TERMINAL:
            s.state = S_CANCELLED
            s.message = "已取消"
        s.cookies = {}          # 立刻丢掉可能已经拿到一半的凭据
        if self.recorder:
            self.recorder.event("auth.cancel", user_id=owner_id, platform=s.platform)
        return s.public()

    async def logout(self, owner_id: str, platform: str) -> dict:
        """解绑：删掉凭据**和** handle，也就是这个平台的全部登录状态。

        注意**只删凭据，不删做题数据** —— 解绑账号不该把历史记录也清掉。

        handle 必须一起清：CF / AtCoder 根本没有凭据行，它们的"绑定"
        就是那一列 handle，只删 credentials 等于什么都没做（界面还是"已绑定"）。
        """
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise AccountError("user_id 不能为空", "内部错误")
        await self.store.clear_credentials(owner_id, platform)
        await self.store.set_handle(owner_id, platform, "")
        if self.recorder:
            self.recorder.event("auth.logout", user_id=owner_id, platform=platform)
        return {"ok": True, "platform": platform, "status": "unbound"}

    async def status(self, owner_id: str) -> dict:
        """给界面看的状态。**只有状态和时间，没有凭据。**"""
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise AccountError("user_id 不能为空", "内部错误")
        return await self.store.account_page_payload(owner_id)

    async def get_cookies(self, owner_id: str, platform: str) -> dict:
        """**只在同步时调用。** 返回值绝不外传。"""
        return await self.store.get_credentials(owner_id, platform)

    # ---- 内部 ---------------------------------------------------------
    async def _persist_if_ok(self, owner_id: str, platform: str,
                             session: Session) -> None:
        """登录成功了才落库。

        ⚠️ **不能要求"必须有 cookie"才落库**。
        CF / AtCoder 根本不需要登录，它们成功的标志是**有一个合法的 handle**，
        cookie 是空的。我第一版写成 `if state != OK or not cookies: return`，
        结果那两个平台的 handle 永远存不进去 —— 用户看到"验证通过"，
        转头 `/xcpc 同步` 却说"还没绑定 handle"。**说了成功却没保存**是最气人的那类 bug。

        失败时**什么都不写** —— 尤其不能把"上次的凭据"标记成 valid，
        那会让用户以为还登着。
        """
        if session.state != S_OK:
            return
        handle = (session.ctx.get("handle") or session.ctx.get("username") or "").strip()
        if not session.cookies and not handle:
            # 既没凭据也没 handle，那这次"成功"没有可保存的东西
            return
        try:
            if session.cookies:
                await self.store.set_credentials(owner_id, platform,
                                                 session.cookies, status="valid")
            if handle:
                col = {"codeforces": "cf_handle", "atcoder": "atcoder_handle",
                       "qoj": "qoj_uid", "luogu": "luogu_uid"}.get(platform)
                if col:
                    await self.store.set_handle(owner_id, platform, handle)
            if self.recorder:
                # ⚠️ 只记"成功"和平台，**不记 cookie**
                self.recorder.event("auth.ok", user_id=owner_id, platform=platform,
                                    has_handle=bool(handle),
                                    has_credentials=bool(session.cookies))
        except Exception as exc:                        # noqa: BLE001
            session.state = S_FAILED
            session.message = "登录成功了但没存下来：%s" % exc
            if self.recorder:
                self.recorder.event("auth.persist_fail", user_id=owner_id,
                                    platform=platform, ok=False,
                                    error_kind="数据库错误", detail=str(exc))


def default_authenticators() -> dict:
    """四个平台的默认 authenticator。

    ⚠️ 平台适配器一律走 `import_platform()`，**不能**写
    `from platforms.atcoder import AtCoder` —— AstrBot 按包加载插件时
    `platforms` 不是顶层模块，真机上就是这里炸的（绑定页报
    「读状态失败：No module named 'platforms'」）。见 core/__init__.py。
    """
    AtCoder = import_platform("atcoder").AtCoder
    Codeforces = import_platform("codeforces").Codeforces

    cf = Codeforces()
    atc = AtCoder()

    async def cf_fetch(handle, client):
        return await cf.fetch_submissions(handle, None, client)

    async def atc_fetch(handle, client):
        return await atc.fetch_submissions(handle, None, client)

    async def luogu_verify(cookies, client):
        """洛谷：随便拉一个公开题目页，看会不会 401。"""
        Luogu = import_platform("luogu").Luogu
        lg = Luogu()
        got = await lg.fetch_problem("P1001", client)
        if got.ok:
            return True, "验证通过"
        if got.error_kind == "凭据失效":
            return False, "这段 Cookie 用不了（服务端说未登录）"
        return False, "%s：%s" % (got.error_kind, got.detail)

    async def qoj_verify(cookies, client):
        """QOJ：直接拉一次提交列表。

        用真接口验，不用小聪明 —— 它能过，`/xcpc 同步` 就能过。
        """
        Qoj = import_platform("qoj").Qoj
        qj = Qoj()
        got = await qj.fetch_submissions("", None, client)
        if got.ok:
            return True, "验证通过，拉到 %d 条记录" % len(got.items)
        if got.error_kind == "凭据失效":
            return False, "这段 Cookie 用不了（服务端说没登录）"
        return False, "%s：%s" % (got.error_kind, got.detail)

    return {
        "codeforces": HandleOnly(platform="codeforces", fetcher=cf_fetch),
        "atcoder": HandleOnly(platform="atcoder", fetcher=atc_fetch),
        "qoj": EitherOf(
            primary=QojLogin(),
            # 表单上只有会话 cookie 一个 cookie 框。qoj.ac 实发的会话 cookie
            # 是 `__Host-UOJSESSID`（见 `QOJ_SESSION_COOKIES`）。
            #
            # 上游 UOJ 的 `Auth::login()` 里还会 `Cookie::safeSet('uoj_username'…)`
            # / `safeSet('uoj_remember_token'…)` 各下发一个 10 年期的 cookie
            # （`web/app/models/Auth.php`，登录控制器调的是默认 `$remember = true`），
            # **但真机上没见过**，所以不在表单上摆两个填不满的框。
            # 它们只值"会话过期后服务端能自己恢复登录态"这一点好处，
            # 少抄了也不影响 —— `Auth::initMyUser()` 先看的就是
            # `$_SESSION['username']`，也就是会话 cookie 里那个。
            #
            # uid 不在 cookie 里（`uid_cookie=""`），由表单上的用户名框给。
            # QOJ 的提交记录得存个用户名，认不出来就别假装成功。
            cookie_path=ManualCookie(platform="qoj",
                                     required=("__Host-UOJSESSID",),
                                     uid_cookie="", needs_uid=True,
                                     verifier=qoj_verify),
        ),
        # 洛谷的 _uid 既是 cookie 也是用户 id，一填两用；
        # C3VK 不列在这里 —— 那是 CDN 挑战 cookie，5 分钟过期，http.py 自己会解
        "luogu": ManualCookie(platform="luogu",
                              required=("__client_id", "_uid"),
                              uid_cookie="_uid", needs_uid=True,
                              verifier=luogu_verify),
    }
