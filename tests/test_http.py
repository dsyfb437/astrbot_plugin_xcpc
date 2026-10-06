#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/http.py + platforms/ 的离线自测。

**完全不碰外网** —— 起一个本地 HTTP 服务器，把实测到的真实行为都模拟出来：
CF 的「200 但 FAILED」、洛谷的 C3VK 挑战、AtCoder 的 302 到登录页、
Cloudflare 的 403 挑战页、限速、cookie 注入。

为什么坚持离线
--------------
之前吃过亏：拿真实站点做测试，网络一抖就"失败"，或者对面改版就全红，
**分不清是代码坏了还是环境坏了**。夹具化之后每次失败都能定位到具体代码。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import http as httpm  # noqa: E402
from core import log as logm    # noqa: E402
from platforms import atcoder as atc      # noqa: E402
from platforms import codeforces as cf    # noqa: E402

PASS = 0
FAIL = 0
HITS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  \u2713 %s" % label)
    else:
        FAIL += 1
        print("  \u2717 %s%s" % (label, ("  -> " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# 本地 stub 服务器
# ---------------------------------------------------------------------------

class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):        # 静音
        pass

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        HITS.append(self.path)
        p = self.path

        # 1. 洛谷式 C3VK 挑战：第一次返回 JS，带上 cookie 之后给真内容
        #
        # ⚠️ 夹具必须用**洛谷真实发的混淆形式**，不能用没混淆的 `document.cookie`。
        # 我第一版夹具写的是干净形式，结果代码里按 `document.cookie` 字面量做的
        # 判断在夹具上能过、在真站点上解不出来 —— **夹具比现实宽松就会漏真 bug**。
        # 下面这段是从真实响应里抄的（属性名混淆 + \x 转义的 "document"）。
        if p.startswith("/c3vk"):
            if "C3VK=" not in (self.headers.get("Cookie") or ""):
                js = (b'<script>var _$daewqwskl=["\\x64\\x6f\\x63\\x75\\x6d\\x65\\x6e\\x74"];'
                      b'var _$oopopdwskl=["\\x6f\\x6e\\x4d\\x6f\\x75\\x73\\x65\\x4d\\x6f\\x76\\x65"];'
                      b'window.open("/", "_self");'
                      b'window[_$daewqwskl[0]].cookie="C3VK=9713b1; path=/; max-age=300;"'
                      b'</script>')
                return self._send(200, js, "text/html")
            return self._send(200, b'{"ok":true,"page":"real"}')

        # 1b. 302 指向自身（洛谷 CDN 的行为）—— 不拦会一直重定向到超限
        if p.startswith("/self-redirect"):
            return self._send(302, b"", extra={"Location": p})

        # 2. Cloudflare 挑战页（403）
        if p.startswith("/cf-block"):
            return self._send(403, b"<html><title>Just a moment...</title>"
                                   b"<div>cloudflare</div></html>", "text/html")

        # 3. 302 到登录页（AtCoder 的行为）
        if p.startswith("/need-login"):
            return self._send(302, b"", extra={"Location": "/login"})
        if p.startswith("/login"):
            return self._send(200, b"<title>Sign In</title>", "text/html")

        # 4. CF：HTTP 200 但 body 里 status=FAILED
        if p.startswith("/cf-failed"):
            return self._send(200, json.dumps(
                {"status": "FAILED", "comment": "handle not found"}).encode())

        if p.startswith("/cf-ok"):
            return self._send(200, json.dumps({
                "status": "OK",
                "result": [
                    {"id": 3, "creationTimeSeconds": 3000, "verdict": "OK",
                     "programmingLanguage": "GNU C++17",
                     "problem": {"contestId": 1900, "index": "D", "rating": 2100}},
                    {"id": 2, "creationTimeSeconds": 2000, "verdict": "WRONG_ANSWER",
                     "programmingLanguage": "GNU C++17",
                     "problem": {"contestId": 1900, "index": "D", "rating": 2100}},
                    # 没有 contestId 的题：必须跳过，不能造假 key
                    {"id": 1, "creationTimeSeconds": 1000, "verdict": "OK",
                     "problem": {"index": "Z"}},
                ]}).encode())

        if p.startswith("/cf-rating"):
            return self._send(200, json.dumps({
                "status": "OK",
                "result": [{"contestId": 1900, "contestName": "Codeforces Round 1024",
                            "rank": 300, "oldRating": 1700, "newRating": 1760,
                            "ratingUpdateTimeSeconds": 3000}]}).encode())

        # 字段名变了（模拟 CF 改版）：必须报「页面结构变化」，
        # 不能返回一堆 contest_id 为空的壳子
        if p.startswith("/cf-rating-broken"):
            return self._send(200, json.dumps({
                "status": "OK",
                "result": [{"someRenamedId": 1900, "someRenamedName": "X",
                            "rank": 300}]}).encode())

        # CF 的错误也可能以 HTTP 400 + JSON body 的形式出现（不是 200）
        if p.startswith("/cf-bad-handle"):
            return self._send(400, json.dumps({
                "status": "FAILED",
                "comment": "handle: Field should contain between 3 and 24 characters"
            }).encode())

        # 5. AtCoder Problems 式
        if p.startswith("/atc-subs"):
            return self._send(200, json.dumps([
                {"id": 1, "epoch_second": 1000, "problem_id": "abc380_d",
                 "contest_id": "abc380", "result": "AC", "language": "C++ 20"},
                {"id": 2, "epoch_second": 2000, "problem_id": "abc380_e",
                 "contest_id": "abc380", "result": "WA", "language": "C++ 20"},
            ]).encode())

        if p.startswith("/atc-notlist"):
            # 用户不存在时这个服务也可能回 200 但给对象而不是数组
            return self._send(200, json.dumps({"error": "not found"}).encode())

        if p.startswith("/atc-problems"):
            return self._send(200, json.dumps([
                {"id": "abc380_d", "contest_id": "abc380", "problem_index": "D",
                 "name": "D - Strange Mirroring", "title": "D - Strange Mirroring"},
            ]).encode())

        if p.startswith("/atc-models"):
            return self._send(200, json.dumps({
                "abc380_d": {"difficulty": 812, "discrimination": 0.5},
                "abc380_e": {"difficulty": None},
            }).encode())

        # 6. 慢响应（测限速/超时用不到，但保留）
        if p.startswith("/slow"):
            time.sleep(0.05)
            return self._send(200, b"{}")

        self._send(404, b"{}")


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def test_c3vk(base: str) -> None:
    print("\n[1] 洛谷 C3VK 挑战（本次实测到的真实形式）")
    rec = logm.Recorder(os.path.join(tempfile.mkdtemp(), "t.log"), to_astrbot=False)
    rec.open()
    c = httpm.HttpClient(platform="luogu", recorder=rec, min_interval=0.0)
    r = asyncio.run(c.get(base + "/c3vk"))
    check("自动解出 C3VK 并重试成功", r.status == 200 and b'"ok":true' in r.body,
          "status=%d body=%r" % (r.status, r.body[:120]))
    check("C3VK 已存进 cookie jar", c.cookies.get("C3VK") == "9713b1",
          repr(c.cookies))
    rec.close()

    lines = io.open(rec.log_path, encoding="utf-8").read()
    check("日志里记了挑战", "C3VK" in lines)

    # 302 指向自身：必须报「挑战未过」，不能报「重定向次数过多」
    c2 = httpm.HttpClient(platform="luogu", min_interval=0.0)
    try:
        asyncio.run(c2.get(base + "/self-redirect"))
        check("302 指向自身应抛 HttpError", False, "居然没抛")
    except httpm.HttpError as exc:
        check("302 指向自身被识别（不是笼统的重定向超限）",
              exc.kind == "挑战未过", "%s / %s" % (exc.kind, exc))


def test_cloudflare(base: str) -> None:
    print("\n[2] Cloudflare 挑战（QOJ 用 curl UA 时就是这样）")
    c = httpm.HttpClient(platform="qoj", min_interval=0.0)
    try:
        asyncio.run(c.get(base + "/cf-block"))
        check("403 挑战页应抛 HttpError", False, "居然没抛")
    except httpm.HttpError as exc:
        check("403 挑战页抛 HttpError", True)
        check("分类是「挑战未过」而不是笼统的 403", exc.kind == "挑战未过", exc.kind)
        check("错误信息提到 Cloudflare", "Cloudflare" in str(exc), str(exc))


def test_redirect(base: str) -> None:
    print("\n[3] 302 到登录页（AtCoder 的行为）")
    c = httpm.HttpClient(platform="atcoder", min_interval=0.0)
    r = asyncio.run(c.get(base + "/need-login"))
    check("跟随重定向后拿到登录页", r.status == 200 and b"Sign In" in r.body,
          "status=%d" % r.status)

    c2 = httpm.HttpClient(platform="atcoder", min_interval=0.0)
    r2 = asyncio.run(c2.get(base + "/need-login", allow_redirect=False))
    check("可以关掉跟随，看见真实的 302", r2.status == 302, "status=%d" % r2.status)


def _patch_client(client: httpm.HttpClient, base: str, routes: dict) -> httpm.HttpClient:
    """把 client 的网络出口换成 stub。

    ⚠️ **必须同时拦 `get` 和 `get_json`**。

    这里有个教训：我最初只替换了 `get_json`，后来把适配器改成用 `get`
    （为了读 CF 的 HTTP 400 body），补丁就失效了 ——
    **"离线测试"开始偷偷打真实网络**，而且因为真实 API 也会返回数据，
    测试看起来还在跑，只是结果变得莫名其妙
    （比如 `handle: User someone not found` 这种真实响应混进了断言）。

    正确做法是在**最低层**拦：把 `_do_request` 换掉，
    这样无论上层用 `get` / `get_json` / `post_form`，都出不了网。
    """
    def fake_do_request(req):
        # 在整个 URL 里找键，**不是只比 path** ——
        # CF 的路径是 `/api/user.rating`，AtCoder 的带查询串
        # （`.../user/submissions?user=x`），只比 path 会全部匹配不上。
        url = req.full_url
        for key, fixture in routes.items():
            if key in url:
                status, body, _ctype, extra = fixture
                return status, body, extra.get("Location", "")
        # 没覆盖到的路径 → 明确失败，而不是悄悄走真网络
        raise AssertionError(
            "测试夹具没覆盖这个请求：%s\n"
            "**绝不允许回落到真实网络** —— 否则测试结果不可信。" % url)

    client._do_request = fake_do_request       # type: ignore[assignment]
    return client


def _json(body) -> tuple:
    return (200, json.dumps(body).encode(), "application/json", {})


def _html(body: bytes, status: int = 200, extra: dict | None = None) -> tuple:
    return (status, body, "text/html", extra or {})


def test_no_real_network() -> None:
    """元检查：确认上面的补丁真的拦住了网络。

    这是**防止"离线测试"退化成联网测试**的保险丝 ——
    我自己就踩过一次，而且因为真实 API 也回数据，很难发现。
    """
    print("\n[0] 确认测试不出网")
    c = httpm.HttpClient(platform="x", min_interval=0.0)
    base = "https://example.invalid"
    _patch_client(c, base, {"/ok": _json({"hi": 1})})

    r = asyncio.run(c.get(base + "/ok"))
    check("夹具路径能正常工作", r.status == 200 and r.json()["hi"] == 1)

    try:
        asyncio.run(c.get(base + "/not-covered"))
        check("未覆盖的路径会明确失败（不会走真网络）", False, "居然成功返回了")
    except AssertionError as exc:
        check("未覆盖的路径会明确失败（不会走真网络）",
              "不允许回落到真实网络" in str(exc))

    # 真网络确实不可达（用不存在的域名，正常会抛 URLError）
    c2 = httpm.HttpClient(platform="x", min_interval=0.0, timeout=3.0)
    try:
        asyncio.run(c2.get("https://this-host-does-not-exist-xyz.invalid/"))
        check("对照组：真请求会失败（说明上面拦的是真的）", False, "居然连上了")
    except Exception:
        check("对照组：真请求会失败（说明上面拦的是真的）", True)


def test_cf(base: str) -> None:
    print("\n[4] Codeforces 适配器")
    adapter = cf.Codeforces()

    def mk(routes):
        return _patch_client(
            httpm.HttpClient(platform="codeforces", min_interval=0.0), base, routes)

    CF_OK = {"status": "OK", "result": [
        {"id": 3, "creationTimeSeconds": 3000, "verdict": "OK",
         "programmingLanguage": "GNU C++17",
         "problem": {"contestId": 1900, "index": "D", "rating": 2100}},
        {"id": 2, "creationTimeSeconds": 2000, "verdict": "WRONG_ANSWER",
         "programmingLanguage": "GNU C++17",
         "problem": {"contestId": 1900, "index": "D", "rating": 2100}},
        {"id": 1, "creationTimeSeconds": 1000, "verdict": "OK",
         "problem": {"index": "Z"}},
    ]}
    CF_RATING = {"status": "OK", "result": [
        {"contestId": 1900, "contestName": "Codeforces Round 1024",
         "rank": 300, "oldRating": 1700, "newRating": 1760,
         "ratingUpdateTimeSeconds": 3000}]}
    SUB = {"/user.status": _json(CF_OK)}

    # 4.1 「200 但 FAILED」必须被识别成失败
    got = asyncio.run(adapter.fetch_submissions(
        "nobody", client=mk({"/user.status": _json(
            {"status": "FAILED", "comment": "handle not found"})})))
    check("HTTP 200 + status=FAILED 被识别为失败", got.ok is False, repr(got)[:200])
    check("handle 不存在归到「凭据失效」", got.error_kind == "凭据失效", got.error_kind)

    # 4.1b **HTTP 400 + JSON body** 也是 CF 的错误形式（实测出来的）
    got = asyncio.run(adapter.fetch_submissions(
        "ab", client=mk({"/user.status": _json(
            {"status": "FAILED",
             "comment": "handle: Field should contain between 3 and 24 characters"})})))
    check("HTTP 400 的错误也被识别（不是笼统的解析失败）",
          got.ok is False and got.error_kind == "凭据失效",
          "ok=%s kind=%s" % (got.ok, got.error_kind))

    # 4.2 正常解析
    got = asyncio.run(adapter.fetch_submissions("someone", client=mk(SUB)))
    check("解析成功", got.ok and len(got.items) == 2,
          "拿到 %d 条（应为 2：没 contestId 的那条应被跳过）" % len(got.items))
    if got.items:
        s = got.items[0]
        check("题目 key 形如 CF:1900D", s.problem_key == "CF:1900D", s.problem_key)
        check("难度标了来源 cf_rating", s.difficulty_source == "cf_rating")
        # 夹具顺序：id=3 是 OK，id=2 是 WRONG_ANSWER（结果按时间倒序）
        check("AC 判定：第一条是 OK", got.items[0].accepted is True)
        check("AC 判定：第二条是 WRONG_ANSWER",
              len(got.items) > 1 and got.items[1].accepted is False)
    check("游标是最新时间", got.cursor == 3000, repr(got.cursor))

    # 4.3 增量：since 之后的不重复
    got = asyncio.run(adapter.fetch_submissions(
        "someone", since_epoch=2500, client=mk(SUB)))
    check("增量只拿 since 之后的", len(got.items) == 1,
          "拿到 %d 条（应为 1）" % len(got.items))

    # 4.4 比赛记录 —— **CF 用的是扁平字段**（contestId / contestName）
    got = asyncio.run(adapter.fetch_contests(
        "someone", client=mk({"/user.rating": _json(CF_RATING)})))
    check("比赛记录解析成功", got.ok and len(got.items) == 1, repr(got)[:200])
    if got.items:
        r = got.items[0]
        # ⚠️ **必须断言内容，不能只断言条数**。
        #
        # 这里有过一次教训：夹具用的就是 CF 真实的**扁平字段**
        # （`contestId` / `contestName`），代码却按嵌套的 `contest.id` 解析。
        # 结果每条记录的 `contest_id` 都是空串 —— 而测试**是绿的**，
        # 因为我只断言了"有 1 条"和 `rank`，没看 id 是不是空的。
        #
        # 真实症状更隐蔽：落库时"没有 contest_id 就跳过"把 19 条全丢了，
        # 但同步报告显示"成功"，只是比赛 0 场。
        # **数量对、状态对、内容全空** —— 最危险的一类。
        check("contest_id 非空（CF 用的是扁平字段）",
              bool(r.contest_id), "contest_id=%r" % r.contest_id)
        check("比赛名非空", bool(r.name), "name=%r" % r.name)
        check("开始时间非零", bool(r.start_epoch), "start=%r" % r.start_epoch)
        check("contest_id 解析正确", r.contest_id == "1900", r.contest_id)
        check("比赛名解析正确", r.name == "Codeforces Round 1024", r.name)
        check("rating 变化算对（1760-1700=60）", r.rating_delta == 60, repr(r.rating_delta))
        check("排名解析", r.rank == 300)

    # 4.5 结构变了要报错，不能返回一堆空壳
    got = asyncio.run(adapter.fetch_contests(
        "someone", client=mk({"/user.rating": _json(
            {"status": "OK", "result": [{"someRenamedId": 1, "rank": 3}]})})))
    check("解析不出 contestId 时明确报错（而不是返回空壳）",
          got.ok is False and got.error_kind == "页面结构变化",
          "ok=%s kind=%s" % (got.ok, got.error_kind))


def test_atcoder(base: str) -> None:
    print("\n[5] AtCoder 适配器")
    adapter = atc.AtCoder()

    ATC_SUBS = [
        {"id": 1, "epoch_second": 1000, "problem_id": "abc380_d",
         "contest_id": "abc380", "result": "AC", "language": "C++ 20"},
        {"id": 2, "epoch_second": 2000, "problem_id": "abc380_e",
         "contest_id": "abc380", "result": "WA", "language": "C++ 20"},
    ]
    SUBS = {"/user/submissions": _json(ATC_SUBS)}

    def mk(routes):
        return _patch_client(
            httpm.HttpClient(platform="atcoder", min_interval=0.0), base, routes)

    got = asyncio.run(adapter.fetch_submissions("someone", client=mk(SUBS)))
    check("提交解析成功", got.ok and len(got.items) == 2, repr(got))
    if got.items:
        check("题目 key 形如 ATC:abc380_d",
              got.items[0].problem_key == "ATC:abc380_d", got.items[0].problem_key)
        check("难度留空（提交接口不给难度，要另查题库）",
              got.items[0].difficulty is None)

    # 比赛记录：走**官方 rating 历史接口**
    # （不是从提交反推 —— 反推没有排名和 rating 变化，
    #   而"压力下的表现"恰恰要看这两个）
    ATC_HISTORY = [
        {"IsRated": True, "Place": 59, "OldRating": 0, "NewRating": 1255,
         "Performance": 2455, "ContestScreenName": "arc061.contest.atcoder.jp",
         "ContestName": "AtCoder Regular Contest 061",
         "EndTime": "2016-09-11T22:40:00+09:00"},
        {"IsRated": False, "Place": 300, "OldRating": 1255, "NewRating": 1255,
         "ContestScreenName": "abc380.contest.atcoder.jp",
         "ContestName": "AtCoder Beginner Contest 380",
         "EndTime": "2024-11-16T23:00:00+09:00"},
    ]
    got = asyncio.run(adapter.fetch_contests("someone", client=mk({
        "/history/json": _json(ATC_HISTORY)})))
    check("比赛记录解析成功", got.ok and len(got.items) == 2, repr(got)[:200])
    if got.items:
        r = got.items[0]
        check("标题是真实名字（不是 contest_id）",
              r.name == "AtCoder Regular Contest 061", r.name)
        check("contest_id 从 ContestScreenName 取出来",
              r.contest_id == "arc061", r.contest_id)
        check("有排名", r.rank == 59, repr(r.rank))
        check("有 rating 变化（1255-0=1255）", r.rating_delta == 1255,
              repr(r.rating_delta))
        check("时间从 ISO8601 解析出来（不是 0）",
              r.start_epoch > 1_400_000_000, repr(r.start_epoch))
        check("IsRated=true 标成 rated", r.kind == "rated", r.kind)
        check("IsRated=false 标成 unrated（不是当成 rated）",
              got.items[1].kind == "unrated", got.items[1].kind)

    # 空数组有两种完全不同的原因，必须分清
    # ① 用户不存在 → 凭据失效
    got = asyncio.run(adapter.fetch_contests("nobody", client=mk({
        "/history/json": _json([]),
        "/users/nobody": _html(b"<html>404</html>", status=404)})))
    check("用户不存在时报凭据失效（不是「没打过比赛」）",
          not got.ok and got.error_kind == "凭据失效",
          "%s / %s" % (got.ok, got.error_kind))
    check("说明里点了 handle", "nobody" in got.detail, got.detail[:80])

    # ② 用户存在但确实没打过 → 成功 + 零条
    got = asyncio.run(adapter.fetch_contests("newbie", client=mk({
        "/history/json": _json([]),
        "/users/newbie": _html(b"<html>ok</html>", status=200)})))
    check("确实没打过时返回成功 + 零条",
          got.ok and not got.items, "%s / %d" % (got.ok, len(got.items)))

    # ③ 结构变了要报错
    got = asyncio.run(adapter.fetch_contests("someone", client=mk({
        "/history/json": _json([{"Weird": 1}, {"AlsoWeird": 2}])})))
    check("解析不出任何一条时明确报错（不返回空壳）",
          not got.ok and got.error_kind == "页面结构变化",
          "%s / %s" % (got.ok, got.error_kind))

    # ④ 非数组
    got = asyncio.run(adapter.fetch_contests("someone", client=mk({
        "/history/json": _json({"error": "x"})})))
    check("非数组时报页面结构变化",
          not got.ok and got.error_kind == "页面结构变化", got.error_kind)

    # 题库：难度合并 + tags 必须是 None
    got = asyncio.run(adapter.fetch_problems(client=mk({
        "/problem-models": _json({
            "abc380_d": {"difficulty": 812, "discrimination": 0.5},
            "abc380_e": {"difficulty": None}}),
        "/problems": _json([
            {"id": "abc380_d", "contest_id": "abc380", "problem_index": "D",
             "name": "D - Strange Mirroring", "title": "D - Strange Mirroring"}]),
    })))
    check("题库解析成功", got.ok and len(got.items) == 1, repr(got))
    if got.items:
        p = got.items[0]
        check("难度从 problem-models 合并进来", p.difficulty == 812, repr(p.difficulty))
        check("难度来源标 atcoder_irt（不是 cf_rating）",
              p.difficulty_source == "atcoder_irt", p.difficulty_source)
        check("tags 是 None 而不是空列表（这个平台给不出标签）",
              p.tags is None, repr(p.tags))

    # 用户不存在：回对象而不是数组
    got = asyncio.run(adapter.fetch_submissions(
        "nobody", client=mk({"/user/submissions": _json({"error": "not found"})})))
    check("返回非数组时明确报错（不当成零提交）", got.ok is False, repr(got)[:200])
    check("分类是解析失败", got.error_kind == "解析失败", got.error_kind)


def test_cookie_safety() -> None:
    print("\n[6] cookie 安全")
    c = httpm.HttpClient(platform="qoj")
    c.set_cookies({"SESSDATA": "good", "evil": "abc\r\nX-Injected: 1"})
    check("含换行符的 cookie 被丢弃", "evil" not in c.cookies, repr(c.cookies))
    check("正常的 cookie 保留", c.cookies.get("SESSDATA") == "good")
    check("cookie_header 不含换行", "\r" not in c.cookie_header()
          and "\n" not in c.cookie_header())


def test_rate_limit() -> None:
    print("\n[7] 限速")
    c = httpm.HttpClient(platform="x", min_interval=0.25)
    check("硬下限生效（调成 0 也不低于 RATE_FLOOR）",
          c._effective_interval() >= c.RATE_FLOOR, repr(c._effective_interval()))
    c.rate_scale = 0.001
    check("倍率再小也守住下限", c._effective_interval() >= c.RATE_FLOOR,
          repr(c._effective_interval()))
    c.rate_scale = 999
    check("倍率上限被夹住", c._effective_interval() <= 10 * c.min_interval + 1e-6,
          repr(c._effective_interval()))


def test_ua() -> None:
    print("\n[8] User-Agent（QOJ 的 Cloudflare 靠它放行）")
    check("默认 UA 是完整浏览器 UA", "Mozilla/5.0" in httpm.BROWSER_UA
          and "Chrome/" in httpm.BROWSER_UA)
    check("默认 UA 不是 curl（会被 Cloudflare 拦）",
          "curl" not in httpm.BROWSER_UA.lower())
    check("默认头里带 UA", httpm.DEFAULT_HEADERS.get("User-Agent") == httpm.BROWSER_UA)


def main() -> int:
    print("=" * 62)
    print("core/http.py + platforms/ 离线自测（不碰外网）")
    print("=" * 62)
    srv, base = start_stub()
    try:
        test_c3vk(base)
        test_cloudflare(base)
        test_redirect(base)
        test_cf(base)
        test_atcoder(base)
    finally:
        srv.shutdown()
    test_cookie_safety()
    test_rate_limit()
    test_ua()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
