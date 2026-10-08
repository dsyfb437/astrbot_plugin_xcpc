#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QOJ 适配器的离线自测。

**这个文件 2026-10-08 才建起来** —— 在那之前 QOJ 的提交解析器
**一条单元测试都没有**，而它当时把**别人的提交**当成用户的记录写进了库：
31 行里躺着 `ppip`、`ZhaoZiLong`、`C++26` 这些"评测结果"（其实是提交者
用户名和语言）。数据进库了、条数还在涨，所以从外面完全看不出坏。

**夹具必须和现实一样脏。** 下面的 HTML 抄自真实响应结构，不是我自己
编的干净版本。这个文件里最重要的三条脏点：

  1. `Submitter` 列里**装着别人的名字**，而且那些名字长得很像"结果词"
     （纯英文、大小写正常）。旧代码就是在这一列上翻车的 ——
     所以夹具里故意留着一个非空的 `Submitter`。
  2. `Result` 列是 `<a class="uoj-score" data-score=...>` 而不是纯文本；
     **同一行里 `Submitter` 和 `Language` 都是纯文本**，只有认 QOJ
     自己的语义标记才分得清。
  3. 题号链接有两种：`/problem/12371` 和比赛里的
     `/contest/3504/problem/16831`。

跑法：python tests/test_qoj.py
"""

from __future__ import annotations

import asyncio
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from platforms.qoj import (  # noqa: E402
    Qoj, _parse_iso, _qoj_verdict, _has_next_page, _header_columns,
)

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
# 夹具：结构抄自真实响应
# ---------------------------------------------------------------------------

# ⚠️ 脏点：真实表头就长这样，**按名字查列号**才不会在 QOJ 调序时存错数据。
HEADERS = ["ID", "Problem", "Submitter", "Result", "Time", "Memory",
           "Language", "File size", "Submit time"]


def row(sid, pid, verdict="AC", iso="2026-10-08T08:13:48+08:00",
        lang="C++14", submitter="someone", score="100.000000",
        full="100.000000", contest=None):
    """一行真实形状的提交。`submitter` 默认**非空**，因为旧 bug 就在那里。"""
    href = ("/contest/%d/problem/%s" % (contest, pid)) if contest \
        else "/problem/%s" % pid
    return (
        '<tr>'
        '<td><a href="/submission/%s" style="color: orange">#%s</a></td>'
        '<td><a href="%s">#%s. Some Title</a></td>'
        '<td><span class="uoj-username" data-rated="1">%s</span></td>'
        '<td><a href="/submission/%s" class="uoj-score" '
        'data-full="%s" data-score="%s">%s</a></td>'
        '<td>20ms</td><td>4772kb</td>'
        '<td><a href="/submission/%s">%s</a></td><td>436b</td>'
        '<td><small><time class="uoj-time" datetime="%s">%s</time></small></td>'
        '</tr>'
        % (sid, sid, href, pid, submitter, sid, full, score, verdict,
           sid, lang, iso, iso))


def page(rows, next_page=0, headers=True, submitter="UESTC_PepperNoise"):
    """一整页。`next_page=N` 会在页脚放一个 `?page=N` 链接。"""
    th = "".join("<th>%s</th>" % h for h in HEADERS) if headers else ""
    nxt = ('<div class="pagination"><a href="/submissions'
           '?submitter=%s&page=%d">%d</a></div>' % (submitter, next_page, next_page)) \
        if next_page else ""
    return ("<!DOCTYPE html><html><head><title>Submissions - QOJ</title></head>"
            "<body><table><tr>%s</tr>%s</table>%s</body></html>"
            % (th, "".join(rows), nxt))


LOGIN_HTML = '<!DOCTYPE html><html><head><title>Login - QOJ</title></head><body></body></html>'


class Resp:
    def __init__(self, status=200, text="", body=None):
        self.status = status
        self.text = text
        self.body = (body if body is not None
                     else text.encode("utf-8", errors="replace"))
        self.url = ""

    def __bool__(self):
        return 200 <= self.status < 300


class FakeClient:
    """按 URL 关键词返回夹具。**未覆盖的 URL 直接失败，绝不走真网络。**

    额外记下每个请求过的 URL（`self.seen`）—— 「有没有带 `?submitter=`」
    这件事只能从 URL 上验证，而它正是这个适配器最要命的那个 bug。
    """

    def __init__(self, routes):
        self.routes = routes
        self.cookies = {}
        self.seen = []

    def set_cookies(self, c):
        self.cookies = dict(c or {})

    async def get(self, url, **kw):
        self.seen.append(url)
        for key in sorted(self.routes, key=len, reverse=True):
            if key in url:
                v = self.routes[key]
                # 可调用的话把 URL 也递进去 —— 分页夹具要按页码分派。
                return v(url) if callable(v) else v
        raise AssertionError("夹具没覆盖：%s（**不允许走真网络**）" % url)


CK = {"__Host-UOJSESSID": "sess"}
HANDLE = "UESTC_PepperNoise"


# ---------------------------------------------------------------------------
# 1. 时间解析
# ---------------------------------------------------------------------------

def test_parse_iso():
    print("\n[1] _parse_iso（epoch=0 是旧版的第三个 bug）")

    import datetime
    got = _parse_iso("2026-10-08T08:13:48+08:00")
    utc = datetime.datetime.fromtimestamp(got, datetime.timezone.utc)
    check("★ +08:00 的偏移被减掉了（08:13:48 → 00:13:48 UTC）",
          utc.strftime("%Y-%m-%d %H:%M:%S") == "2026-10-08 00:13:48",
          utc.strftime("%Y-%m-%d %H:%M:%S"))
    check("★ 结果不是 0（旧版存进库的全是 1970 年）", got > 0, repr(got))

    same = _parse_iso("2026-10-08T00:13:48Z")
    check("Z 结尾不加偏移", same == got, "%r vs %r" % (same, got))

    check("空格分隔也认（Submit time 那格就是这种）",
          _parse_iso("2026-10-08 08:13:48") > 0, "")
    check("时区写成 +0800 也认",
          _parse_iso("2026-10-08T08:13:48+0800") == got, "")
    check("负偏移（-05:00）也是减",
          _parse_iso("2026-10-08T08:13:48-05:00") == got + 13 * 3600, "")
    check("认不出的返回 0", _parse_iso("刚刚") == 0, repr(_parse_iso("刚刚")))
    check("空输入返回 0", _parse_iso("") == 0 and _parse_iso(None) == 0, "")


# ---------------------------------------------------------------------------
# 2. 结果词表
# ---------------------------------------------------------------------------

def test_verdict():
    print("\n[2] _qoj_verdict")

    check("AC ✓ → AC（对勾是装饰）", _qoj_verdict("AC ✓") == "AC",
          repr(_qoj_verdict("AC ✓")))
    check("WA → WA", _qoj_verdict("WA") == "WA", repr(_qoj_verdict("WA")))
    check("TL → TL", _qoj_verdict("TL") == "TL", repr(_qoj_verdict("TL")))
    check("带空白的也认", _qoj_verdict("  RE \n") == "RE", repr(_qoj_verdict("  RE \n")))
    check("AC 带后缀（如 AC*）也算 AC",
          _qoj_verdict("AC *") == "AC", repr(_qoj_verdict("AC *")))
    check("长的怪词被截到 20 字，不炸库",
          len(_qoj_verdict("X" * 80)) == 20, repr(_qoj_verdict("X" * 80)))

    # 计分题只给分数不给词时才用 data-score
    check("没词 + 满分 → AC", _qoj_verdict("", "100.000000", "100.000000") == "AC",
          repr(_qoj_verdict("", "100", "100")))
    check("没词 + 部分分 → 空（**不能算 AC**）",
          _qoj_verdict("", "40.000000", "100.000000") == "",
          repr(_qoj_verdict("", "40", "100")))
    check("没词 + 0 分 → 空", _qoj_verdict("", "0", "100") == "", "")
    check("没词 + 没有 full → 空（不猜）", _qoj_verdict("", "100", "") == "", "")
    check("全空 → 空", _qoj_verdict("", "", "") == "", "")
    check("分数不是数字 → 空，不抛异常",
          _qoj_verdict("", "abc", "def") == "", "")


# ---------------------------------------------------------------------------
# 3. 按表头定位列
# ---------------------------------------------------------------------------

def test_header_columns():
    print("\n[3] _header_columns：按名字查列号，不按次序猜")

    cols = _header_columns(page([]))
    check("表头解析出来了", bool(cols), repr(cols))
    check("Result 在第 3 列（0 起）", cols.get("result") == 3, repr(cols.get("result")))
    check("Submitter 在第 2 列", cols.get("submitter") == 2, repr(cols.get("submitter")))
    check("Submit time 在第 8 列", cols.get("submit time") == 8,
          repr(cols.get("submit time")))
    check("列名统一小写", "Submitter" not in cols and "submitter" in cols, "")
    check("没有表头 → 空字典", _header_columns("<tr><td>x</td></tr>") == {}, "")
    check("空输入 → 空字典", _header_columns("") == {}, "")


def test_has_next_page():
    print("\n[4] _has_next_page（「不分页」是旧版的第五个错）")

    check("有 page=2 链接 → True",
          _has_next_page('<a href="/submissions?submitter=x&page=2">2</a>', 1), "")
    check("停在 page=19 时没有 page=20 → False",
          not _has_next_page('<a href="/submissions?page=19">19</a>', 19), "")
    check("**不能把 page=1 当成下一页**（当前页是 1，只认 2）",
          not _has_next_page('<a href="/submissions?page=1">1</a>', 1), "")
    check("**page=10 不该被 page=1 误伤**",
          _has_next_page('<a href="/submissions?page=10">10</a>', 9), "")
    check("空输入 → False", not _has_next_page("", 1), "")


# ---------------------------------------------------------------------------
# 5. 行解析（★ 本文件存在的理由）
# ---------------------------------------------------------------------------

def test_parse_rows():
    print("\n[5] _parse_submission_rows —— 旧版在这里把别人的名字当成了评测结果")

    html = page([row(3126880, "12371", "AC ✓", submitter="lrmlrm"),
                 row(3126879, "16831", "WA", submitter="pino",
                     contest=3504, lang="C++26",
                     iso="2026-10-08T08:13:41+08:00")])
    rows = Qoj._parse_submission_rows(html)
    check("解析出 2 行", rows is not None and len(rows) == 2,
          repr(None if rows is None else len(rows)))
    if not rows:
        return

    check("★ verdict 是 AC（来自 uoj-score）", rows[0].verdict == "AC",
          repr(rows[0].verdict))
    check("★ **不是提交者名字 lrmlrm**", rows[0].verdict != "lrmlrm", rows[0].verdict)
    check("★ **也不是语言 C++14**", rows[0].verdict != "C++14", rows[0].verdict)
    check("第二行 verdict 是 WA（不是 pino / C++26）",
          rows[1].verdict == "WA", repr(rows[1].verdict))
    check("★ 三行里没有任何一个 verdict 是人名",
          all(s.verdict not in ("lrmlrm", "pino", "someone") for s in rows),
          repr([s.verdict for s in rows]))

    check("submission_id 抓到了", rows[0].submission_id == "3126880",
          repr(rows[0].submission_id))
    check("平台标 qoj", rows[0].platform == "qoj", repr(rows[0].platform))
    check("普通题号 → QOJ:12371", rows[0].problem_key == "QOJ:12371",
          repr(rows[0].problem_key))
    check("★ **比赛里的题** → QOJ:16831（旧正则认不出来）",
          rows[1].problem_key == "QOJ:16831", repr(rows[1].problem_key))

    check("★ epoch 来自 uoj-time 的 datetime（不是 0）",
          rows[0].epoch == 1791418428, repr(rows[0].epoch))
    check("第二行的时间也不为 0", rows[1].epoch > 0, repr(rows[1].epoch))
    check("★ 两行时间差 7 秒（偏移减对了，不是差 8 小时）",
          rows[0].epoch - rows[1].epoch == 7,
          repr(rows[0].epoch - rows[1].epoch))

    check("language 取 Language 那一列", rows[0].language == "C++14",
          repr(rows[0].language))
    check("第二行 language 是 C++26", rows[1].language == "C++26",
          repr(rows[1].language))
    check("QOJ 不公开难度 → None", rows[0].difficulty is None,
          repr(rows[0].difficulty))
    check("难度来源标 qoj_none", rows[0].difficulty_source == "qoj_none",
          repr(rows[0].difficulty_source))

    # 计分题：Result 格里只有分数
    sc = Qoj._parse_submission_rows(page([row(1, "100", "", score="100.000000",
                                              full="100.000000")]))
    check("计分题满分 → AC", sc and sc[0].verdict == "AC",
          repr(sc and sc[0].verdict))
    sc2 = Qoj._parse_submission_rows(page([row(2, "100", "", score="30",
                                               full="100")]))
    check("计分题部分分 → 空，不算 AC", sc2 and sc2[0].verdict == "",
          repr(sc2 and sc2[0].verdict))

    # ★ None 和 [] 必须分开
    check("★ 没有表头 → None（= 改版了）",
          Qoj._parse_submission_rows("<table><tr><td>x</td></tr></table>") is None, "")
    check("★ 表头在但零行 → []（= 他真的没交过）",
          Qoj._parse_submission_rows(page([])) == [], "")
    check("★ 这两者**不能**混成一个值",
          Qoj._parse_submission_rows(page([]))
          is not Qoj._parse_submission_rows("<p>no table</p>"), "")
    check("空输入 → None", Qoj._parse_submission_rows("") is None, "")
    check("只有表头没有数据行时是 []，不是 None",
          Qoj._parse_submission_rows(
              "<table><tr>%s</tr></table>"
              % "".join("<th>%s</th>" % h for h in HEADERS)) == [], "")
    check("行里没有 submission 链接就跳过",
          Qoj._parse_submission_rows(page(['<tr><td>没链接</td></tr>'])) == [], "")


# ---------------------------------------------------------------------------
# 6. fetch_submissions
# ---------------------------------------------------------------------------

def test_fetch_submissions():
    print("\n[6] fetch_submissions（★ 必须带上 ?submitter=）")

    async def main():
        qj = Qoj()

        # --- 6.1 ★★★ 最重要的一条：URL 上必须有 ?submitter=
        # 不加的话拿到的是**全站最近提交**，也就是别人的数据。
        c = FakeClient({"/submissions": Resp(200, page([row(1, "1")]))})
        c.set_cookies(CK)
        got = await qj.fetch_submissions(HANDLE, None, c)
        check("请求成功了", got.ok, "%s / %s" % (got.error_kind, got.detail[:80]))
        check("★ **请求的 URL 上带着 ?submitter=**",
              any("submitter=" in u for u in c.seen), repr(c.seen))
        check("★ submitter 的值就是传进来的 handle",
              any(("submitter=%s" % HANDLE) in u for u in c.seen), repr(c.seen))
        check("没加 ?submitter= 的请求一条都不该发出去",
              all("submitter=" in u for u in c.seen), repr(c.seen))

        # 用户名里有需要转义的字符时要转义
        c2 = FakeClient({"/submissions": Resp(200, page([row(1, "1")]))})
        c2.set_cookies(CK)
        await qj.fetch_submissions("a b/c", None, c2)
        check("handle 里的空格被转义成 %20",
              any("submitter=a%20b%2Fc" in u for u in c2.seen), repr(c2.seen))

        # --- 6.2 ★ 没有 handle 时必须报错，**不能退化成抓全站**
        c3 = FakeClient({"/submissions": Resp(200, page([row(1, "1")]))})
        c3.set_cookies(CK)
        got3 = await qj.fetch_submissions("", None, c3)
        check("★ 没填用户名 → 失败（宁可报错也不抓全站）",
              not got3.ok and got3.error_kind == "凭据失效",
              "%s / %s" % (got3.error_kind, got3.detail[:80]))
        check("★ 而且**一个请求都没发**（发了就已经抓到别人的数据了）",
              c3.seen == [], repr(c3.seen))
        check("提示里说清楚为什么要填用户名", "submitter" in got3.detail, got3.detail[:90])

        # --- 6.3 没登录
        c4 = FakeClient({"/submissions": Resp(200, page([row(1, "1")]))})
        got4 = await qj.fetch_submissions(HANDLE, None, c4)
        check("没 Cookie → 凭据失效",
              not got4.ok and got4.error_kind == "凭据失效",
              "%s / %s" % (got4.error_kind, got4.detail[:60]))

        # --- 6.4 被重定向到登录页
        c5 = FakeClient({"/submissions": Resp(200, LOGIN_HTML)})
        c5.set_cookies(CK)
        got5 = await qj.fetch_submissions(HANDLE, None, c5)
        check("★ 会话过期认得出登录页（否则会显示成「你没交过题」）",
              not got5.ok and got5.error_kind == "凭据失效",
              "%s / %s" % (got5.error_kind, got5.detail[:60]))

        # --- 6.5 页面改版：没有表头
        c6 = FakeClient({"/submissions": Resp(200, "<p>完全看不懂的页面</p>")})
        c6.set_cookies(CK)
        got6 = await qj.fetch_submissions(HANDLE, None, c6)
        check("★ 改版 → 页面结构变化（**不是**「零提交」）",
              not got6.ok and got6.error_kind == "页面结构变化",
              "%s / %s" % (got6.error_kind, got6.detail[:60]))

        # --- 6.6 表头在、零行 = 真的没交过
        c7 = FakeClient({"/submissions": Resp(200, page([]))})
        c7.set_cookies(CK)
        got7 = await qj.fetch_submissions(HANDLE, None, c7)
        check("★ 表头在但零行 → ok=True / 0 条（没交过题不是错误）",
              got7.ok and got7.items == [],
              "%s / %s" % (got7.error_kind, got7.detail[:60]))

        # --- 6.7 分页
        pages = {1: page([row(1, "1"), row(2, "2")], next_page=2),
                 2: page([row(3, "3"), row(4, "4")], next_page=3),
                 3: page([row(5, "5")])}

        def route(u):
            for n, h in pages.items():
                if "page=%d" % n in u:
                    return Resp(200, h)
            return Resp(200, pages[1])

        c8 = FakeClient({"/submissions": route})
        c8.set_cookies(CK)
        got8 = await qj.fetch_submissions(HANDLE, None, c8)
        check("★ 分页把 3 页 5 条全拉回来",
              got8.ok and len(got8.items) == 5,
              repr(None if not got8.ok else len(got8.items)))
        check("正好请求 3 页（没有 page=4 的链接就收）",
              len(c8.seen) == 3, repr(c8.seen))
        check("没撞上限就不算截断", got8.ok and got8.truncated is False,
              repr(getattr(got8, "truncated", None)))

        # --- 6.8 增量：since 过滤 + 游标
        def route9(u):
            if "page=1" in u:
                return Resp(200, page([row(10, "1", iso="2026-10-08T10:00:00+08:00"),
                                       row(11, "2", iso="2026-10-08T09:00:00+08:00")],
                                      next_page=2))
            return Resp(200, page([row(12, "3", iso="2026-10-08T08:00:00+08:00"),
                                   row(13, "4", iso="2026-10-08T07:00:00+08:00")],
                                  next_page=3))

        c9 = FakeClient({"/submissions": route9})
        c9.set_cookies(CK)
        since = _parse_iso("2026-10-08T08:30:00+08:00")
        got9 = await qj.fetch_submissions(HANDLE, since, c9)
        check("★ 只取新于游标的 2 条",
              got9.ok and len(got9.items) == 2,
              repr(None if not got9.ok else [s.submission_id for s in got9.items]))
        check("★ 整页都旧了就不拉第 3 页（增量省时间）",
              len(c9.seen) == 2, repr(len(c9.seen)))
        check("★ 游标是见过的最新时间（不是过滤后的）",
              got9.ok and got9.cursor == _parse_iso("2026-10-08T10:00:00+08:00"),
              repr(got9.cursor if got9.ok else None))
        check("★ 游标不会倒退（比传进来的 since 小就保住 since）",
              got9.ok and got9.cursor >= since, repr(got9.cursor if got9.ok else None))

        c10 = FakeClient({"/submissions": Resp(
            200, page([row(20, "1", iso="2026-10-08T07:00:00+08:00")]))})
        c10.set_cookies(CK)
        got10 = await qj.fetch_submissions(HANDLE, since, c10)
        check("全都是旧记录时不入队", got10.ok and got10.items == [],
              repr(None if not got10.ok else len(got10.items)))
        check("★ 游标不倒退（还是 since）",
              got10.ok and got10.cursor == since, repr(got10.cursor if got10.ok else None))

        # --- 6.9 撞页数上限
        # 每页都诚实地告诉浏览器"下一页是 N+1"，所以只有 _MAX_PAGES 能拦住它。
        import re as _re

        def inf_route(u):
            n = int(_re.search(r"page=(\d+)", u).group(1))
            return Resp(200, page([row(30 + n, str(n))], next_page=n + 1))

        old_cap = Qoj._MAX_PAGES
        Qoj._MAX_PAGES = 3
        try:
            c11 = FakeClient({"/submissions": inf_route})
            c11.set_cookies(CK)
            got11 = await qj.fetch_submissions(HANDLE, None, c11)
            check("撞到 _MAX_PAGES 上限时 truncated=True",
                  got11.ok and got11.truncated is True,
                  repr(got11.truncated if got11.ok else None))
            check("也确实是拉满了 _MAX_PAGES 页",
                  len(c11.seen) == 3, repr(len(c11.seen)))
        finally:
            Qoj._MAX_PAGES = old_cap

        # --- 6.10 HTTP 错误
        c12 = FakeClient({"/submissions": Resp(500, "boom")})
        c12.set_cookies(CK)
        got12 = await qj.fetch_submissions(HANDLE, None, c12)
        check("HTTP 500 → 网络不可达（不会当成零提交）",
              not got12.ok and got12.error_kind == "网络不可达",
              "%s / %s" % (got12.error_kind, got12.detail[:60]))

        # --- 6.11 没有 client
        got13 = await qj.fetch_submissions(HANDLE, None, None)
        check("没传 client → 内部错误", not got13.ok, repr(got13.error_kind))

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 7. 端到端钉死：库里绝不能再出现人名当 verdict
# ---------------------------------------------------------------------------

def test_no_username_as_verdict():
    print("\n[7] 端到端：别人的用户名绝不能变成 verdict")

    async def main():
        # 一页里塞满"像人名的提交者"，正是旧版抓回来的那种页面
        names = ["lrmlrm", "pino", "Crazyouth", "Tbat", "blackslex",
                 "yunz_qiao", "MaMengQi", "123456xwd"]
        html = page([row(100 + i, str(1000 + i),
                         verdict=["AC ✓", "WA", "TL", "RE", "CE", "AC ✓", "WA", "AC ✓"][i],
                         submitter=n)
                     for i, n in enumerate(names)])
        c = FakeClient({"/submissions": Resp(200, html)})
        c.set_cookies(CK)
        got = await Qoj().fetch_submissions(HANDLE, None, c)

        check("8 条全解析出来", got.ok and len(got.items) == 8,
              repr(None if not got.ok else len(got.items)))
        verdicts = [s.verdict for s in got.items] if got.ok else []
        check("★ 没有一条 verdict 是人名",
              all(v not in names for v in verdicts), repr(verdicts))
        check("★ 也没有一条是语言",
              all(not v.startswith("C++") for v in verdicts), repr(verdicts))
        check("★ 全是真结果词",
              set(verdicts) <= {"AC", "WA", "TL", "RE", "CE"}, repr(verdicts))
        check("AC 有 3 条", verdicts.count("AC") == 3, repr(verdicts.count("AC")))
        check("★ 每条都有真时间（旧版全是 0）",
              got.ok and all(s.epoch > 0 for s in got.items),
              repr([s.epoch for s in got.items] if got.ok else []))

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("QOJ 适配器离线自测")
    print("=" * 62)
    test_parse_iso()
    test_verdict()
    test_header_columns()
    test_has_next_page()
    test_parse_rows()
    test_fetch_submissions()
    test_no_username_as_verdict()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
