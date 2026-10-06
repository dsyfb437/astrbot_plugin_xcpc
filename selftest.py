#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""astrbot_plugin_xcpc 的自测 —— **不需要 AstrBot**。

测的是 ``xcpc_core.py``（纯逻辑：复盘解析、文件读写、指标计算）。
``main.py`` 依赖 astrbot 包，只能放进真的 AstrBot 里跑。

用法：
    python selftest.py                  # 用临时目录造一份假工作区
    python selftest.py --root <工作区>   # 顺便拿真实工作区做只读检查（不会写）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

# 输出必须是 UTF-8 —— 否则在中文 Windows 的 GBK 控制台里，
# 下面那些 ✓ / ✗ 会直接抛 UnicodeEncodeError 把自测**整场搞崩**。
#
# 这和 `.bat` 必须纯 ASCII、`Get-Content` 会把行合并是**同一类问题**：
# Windows 默认按系统码页处理文本，而项目里全是 UTF-8。
# `errors="replace"` 兜底：万一某个字符还是编不出来，退化成 ? 而不是崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 老 Python 或被重定向
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import xcpc_core as core  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  \u2713 %s" % label)
    else:
        FAIL += 1
        print("  \u2717 %s %s" % (label, ("-> " + detail) if detail else ""))


# ==========================================================================
# 1. 复盘解析
# ==========================================================================
def test_parse() -> None:
    print("\n[1] 复盘文本解析")

    text = """复盘
比赛: CF Round 1024
类型: VP
过题: 3
罚时: 145
排名: 123
顺序: A>C>B
贡献: 想出 A、C
想歪: 看到区间修改就反射性上线段树，其实是差分+前缀和
模式: 看到 n≤2e5 就想数据结构
套路: 反悔贪心用堆维护
补题: E 已 AC；D 还没补
A 思路 想了一个小时没往图论上想
E 实现 独立想出 P1 边界写挂了
"""
    d = core.parse_review_text(text, default_date="2026-10-06")
    check("比赛名", d.name == "CF Round 1024", d.name)
    check("类型", d.kind == "VP", d.kind)
    check("过题", d.solved == "3", d.solved)
    check("罚时", d.penalty == "145", d.penalty)
    check("排名", d.rank == "123", d.rank)
    check("顺序", d.order == "A>C>B", d.order)
    check("贡献", d.mine == "想出 A、C", d.mine)
    check("想歪", "差分" in d.wrong, d.wrong)
    check("模式", "数据结构" in d.pattern, d.pattern)
    check("套路", "反悔贪心" in d.tricks, d.tricks)
    check("补题", "已 AC" in d.upsolve, d.upsolve)
    check("逐题 2 条", len(d.problems) == 2, str(len(d.problems)))
    if len(d.problems) == 2:
        a, e = d.problems
        check("A 题号", a.label == "A", a.label)
        check("A 卡点=思路", a.kind == "思路", a.kind)
        check("A 说明保留", "图论" in a.note, a.note)
        check("E 卡点=实现", e.kind == "实现", e.kind)
        check("E 识别出独立想出", e.contrib == core.CONTRIB_INDEP, e.contrib)
        check("E 识别出 P1", e.upsolve == "P1", e.upsolve)
    check("独立想出计数 = 1", d.independent_count == 1, str(d.independent_count))

    # 自由文本：一个字都不能丢
    raw = "今天这把打得很难受，B 题读错了题意，C 题知道是网络流但不会建图。"
    d2 = core.parse_review_text(raw, default_date="2026-10-06")
    check("自由文本不丢内容", raw.replace(" ", "") in
          (d2.wrong + d2.raw).replace(" ", "").replace("- ", ""),
          repr((d2.wrong + d2.raw)[:80]))
    check("自由文本有兜底名字", bool(d2.name), d2.name)

    # 空输入不崩
    d3 = core.parse_review_text("", default_date="2026-10-06")
    check("空输入不崩", d3.name == "" and d3.problems == [])

    # 各种分隔符
    d4 = core.parse_review_text("比赛=ABC\n过题：5\n罚时 = 200", default_date="2026-10-06")
    check("= 分隔", d4.name == "ABC", d4.name)
    check("全角冒号", d4.solved == "5", d4.solved)
    check("带空格 = ", d4.penalty == "200", d4.penalty)

    # 不该被误判成题号
    d5 = core.parse_review_text("2026-10-06 打了一场 VP，感觉很累",
                                default_date="2026-10-06")
    check("日期行不被当成题号", d5.problems == [], str(d5.problems))


# ==========================================================================
# 2. 渲染：必须能被 train_stats.py 反解回来
# ==========================================================================
def test_render_roundtrip() -> None:
    print("\n[2] 渲染格式能被 train_stats.py 解析回来")

    text = """比赛: CF Round 1024
类型: VP
过题: 3
罚时: 145
排名: 123
贡献: 想出 A、C
A 思路 想了一小时没往图论上想
E 实现 独立想出 P1 边界写挂了
"""
    d = core.parse_review_text(text, default_date="2026-10-06")
    md = core.render_review_md(d)

    check("首行符合 TITLE_RE",
          bool(core.TITLE_RE.match(md.split("\n")[0])), md.split("\n")[0])
    check("有「类型」行", "| 类型 | VP |" in md)
    check("有「结果」行", "| 结果 | 排名 123 ｜ 过题 3 ｜ 罚时 145 |" in md)
    check("有逐题表头", "| 题 | 难度 | 卡点类型 | 我的贡献 | 补题 | 说明 |" in md)

    # 写进临时目录，再用自己的解析器读回来（逻辑与 train_stats 一致）
    tmp = tempfile.mkdtemp(prefix="xcpc-selftest-")
    try:
        path = os.path.join(tmp, "2026-10-06-CF-Round-1024.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        rec = core.parse_review_file(path)
        check("能被解析回来", rec is not None)
        if rec:
            check("回读 name", rec["name"] == "CF Round 1024", rec["name"])
            check("回读 date", rec["date"] == "2026-10-06", rec["date"])
            check("回读 kind", rec["kind"] == "VP", rec["kind"])
            check("回读 solved", rec["solved"] == 3, str(rec["solved"]))
            check("回读 penalty", rec["penalty"] == 145, str(rec["penalty"]))
            check("回读 rank", rec["rank"] == 123, str(rec["rank"]))
            check("回读 2 道题", len(rec["problems"]) == 2, str(len(rec["problems"])))
            check("回读 independent=1", rec["independent"] == 1, str(rec["independent"]))
            check("回读 P0/P1 待补 1 条",
                  len(rec["upsolve_p0p1"]) == 1, str(rec["upsolve_p0p1"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# 3. 指标计算
# ==========================================================================
def test_metrics() -> None:
    print("\n[3] 指标计算")

    base = int(datetime(2026, 10, 1, 12, 0, 0).timestamp()) - core.CN_OFFSET
    day = 86400
    subs = [
        # 连续 3 天：10/01 10/02 10/03，然后断掉，再 10/06 单独一天
        {"creationTimeSeconds": base + 0 * day, "verdict": "OK",
         "problem": {"contestId": 1, "index": "A"}},
        {"creationTimeSeconds": base + 0 * day, "verdict": "WRONG_ANSWER",
         "problem": {"contestId": 1, "index": "B"}},
        {"creationTimeSeconds": base + 1 * day, "verdict": "OK",
         "problem": {"contestId": 1, "index": "B"}},
        {"creationTimeSeconds": base + 2 * day, "verdict": "OK",
         "problem": {"contestId": 1, "index": "C"}},
        {"creationTimeSeconds": base + 5 * day, "verdict": "OK",
         "problem": {"contestId": 1, "index": "A"}},   # 重复题，不该重复计数
    ]
    st = core.activity_streak(subs)
    check("活跃天数 = 4", st["days_active"] == 4, str(st["days_active"]))
    check("最长连续 = 3", st["max_streak"] == 3, str(st["max_streak"]))
    check("去重 AC = 3", core.count_solved(subs) == 3, str(core.count_solved(subs)))
    check("空输入不崩", core.activity_streak([])["max_streak"] == 0)


# ==========================================================================
# 4. 工作区后端（造一份假工作区，真写一遍）
# ==========================================================================
def test_workspace_fs() -> None:
    print("\n[4] 文件后端读写")

    root = tempfile.mkdtemp(prefix="xcpc-fs-")
    try:
        os.makedirs(os.path.join(root, "data"))
        os.makedirs(os.path.join(root, "config"))
        os.makedirs(os.path.join(root, "03-log"))
        os.makedirs(os.path.join(root, "00-plan", "lists"))

        with open(os.path.join(root, "data", "test_handle_info.json"), "w",
                  encoding="utf-8") as fh:
            import json
            json.dump({"handle": "test_handle", "rating": 1713,
                       "maxRating": 1826, "rank": "expert"}, fh)
        with open(os.path.join(root, "data", "test_handle_status.json"), "w",
                  encoding="utf-8") as fh:
            json.dump([{"creationTimeSeconds": int(datetime.now().timestamp()),
                        "verdict": "OK",
                        "problem": {"contestId": 9, "index": "A"}}], fh)
        with open(os.path.join(root, "config", "cf_profile.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"solved": {"all_time": 227}}, fh)
        with open(os.path.join(root, "config", "schedule.json"), "w",
                  encoding="utf-8") as fh:
            future = (datetime.strptime(core.today_cn(), "%Y-%m-%d")
                      + timedelta(days=12)).strftime("%Y-%m-%d")
            json.dump({"contests": [{"name": "CCPC 长春站", "date": future}]}, fh)
        with open(os.path.join(root, "00-plan", "sprint.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("## Day 1 ｜ %s（周二）\n- [ ] VP 一场\n- [x] 已经做完的\n"
                     "- [ ] 补题到 AC\n" % core.today_cn()[5:].replace("-", "/"))

        fs = core.WorkspaceFS(root=root, handle="test_handle")

        st = fs.status()
        check("status rating", st["rating"] == 1713, str(st["rating"]))
        check("status max_rating", st["max_rating"] == 1826, str(st["max_rating"]))
        check("status solved_all_time", st["solved_all_time"] == 227,
              str(st["solved_all_time"]))
        check("status 倒计时 12 天",
              st["next_contest"] and st["next_contest"]["days"] == 12,
              str(st["next_contest"]))
        check("status 连续天数有值",
              st["streak"]["days_since_last"] == 0,
              str(st["streak"]["days_since_last"]))

        today = fs.today_tasks()
        check("今天任务命中日期组", today["title"].startswith("Day 1"), today["title"])
        check("今天任务 3 条", len(today["items"]) == 3, str(len(today["items"])))
        check("今天任务已完成 1 条", today["done"] == 1, str(today["done"]))

        stamp = fs.append_inbox("看到区间修改先想差分")
        check("收件箱写入时间戳", bool(stamp), stamp)
        inbox = fs.read_inbox()
        check("收件箱读回 1 条", len(inbox) == 1, str(len(inbox)))
        check("收件箱内容正确", "差分" in inbox[0]["text"], inbox[0]["text"])

        d = core.parse_review_text(
            "比赛: 自测场\n过题: 2\n贡献: 独立想出 A\nA 思路 没想出\nB 实现 独立想出 已AC",
            default_date=core.today_cn())
        check("还没写复盘时 today_reviewed = False", fs.today_reviewed() is False)
        name = fs.save_review(d)
        check("复盘文件名以日期开头",
              name.startswith(core.today_cn()), name)
        check("复盘文件真的存在",
              os.path.exists(os.path.join(root, "04-review", name)), name)
        check("contests.csv 被追加",
              os.path.exists(os.path.join(root, "03-log", "contests.csv")))
        check("写完复盘后 today_reviewed = True", fs.today_reviewed() is True)
        check("传别的日期就是 False",
              fs.today_reviewed("1999-01-01") is False)

        # 同名再存一次不能覆盖
        name2 = fs.save_review(core.parse_review_text(
            "比赛: 自测场\n过题: 1", default_date=core.today_cn()))
        check("同名不覆盖（加了时间后缀）", name2 != name, "%s vs %s" % (name, name2))

        # KPI 汇总
        kpi = core.review_kpis(os.path.join(root, "04-review"))
        check("KPI sessions = 2", kpi["sessions"] == 2, str(kpi.get("sessions")))
        check("KPI 独立想出 >= 1", kpi.get("max_independent", 0) >= 1,
              str(kpi.get("max_independent")))

        # 题单
        with open(os.path.join(root, "00-plan", "lists", "t.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# dp 1600-1900\n\n|   | # | 题 | 名称 | 难度 | 标签 |\n"
                     "|---|---|---|---|---|---|\n"
                     "| [ ] | **1** | [1A](https://codeforces.com/1/A) "
                     "| Theatre Square | 1000 | math |\n"
                     "| [x] | **2** | [2B](https://codeforces.com/2/B) "
                     "| Lorry | 1900 | dp |\n")
        lists = fs.problem_lists()
        check("题单解析 1 份", len(lists) == 1, str(len(lists)))
        if lists:
            check("题单标题", lists[0]["title"] == "dp 1600-1900", lists[0]["title"])
            check("题单 2 道", len(lists[0]["items"]) == 2, str(len(lists[0]["items"])))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ==========================================================================
# 5. 「今天记过复盘没有」—— 推送里那句催促的依据
# ==========================================================================
def test_reviewed_today() -> None:
    """http 后端没有文件系统，只能靠 /api/data 里的 contests 表判断。

    这条以前是缺口：main.py 只会 glob 本地 04-review/，而 http 后端没有 root，
    于是**每晚都误报"今天还没记复盘"**。
    """
    print("\n[5] 「今天记过复盘没有」判定")

    today = core.today_cn()
    rows = [
        {"date": today, "kind": "VP", "contest": "今天这场"},
        {"date": "1999-01-01", "kind": "VP", "contest": "很久以前"},
    ]
    check("表里有今天 → True", core.reviewed_today(rows) is True)
    check("显式传今天也是 True", core.reviewed_today(rows, today) is True)
    check("只查旧日期 → True（那条在表里）",
          core.reviewed_today(rows, "1999-01-01") is True)
    check("表里没有的日期 → False",
          core.reviewed_today(rows, "2001-02-03") is False)
    check("空表 → False", core.reviewed_today([]) is False)
    check("None → False（不崩）", core.reviewed_today(None) is False)
    check("脏数据不崩",
          core.reviewed_today([None, "x", 42, {}, {"date": None}]) is False)
    check("date 带时间后缀也认",
          core.reviewed_today([{"date": today + " 21:03"}]) is True)
    check("date 带空格也认",
          core.reviewed_today([{"date": "  " + today + "  "}]) is True)

    # http 后端：把 _request 换掉，这样不需要 aiohttp、也不需要真的有服务器在跑。
    # 这一步才是真正守住"跨机器时不再误报"的那条。
    calls = []
    box = {"data": {}}

    def fake_request(self, method, path, payload=None):
        # 注意第一个参数是 self —— 这是**类属性**上的普通函数，
        # 赋值后就成了方法（第一次写漏了 self，被这条断言当场抓出来）。
        calls.append((method, path))

        async def _coro():
            return box["data"]
        return _coro()

    real = core.XcpcHttp._request
    core.XcpcHttp._request = fake_request
    try:
        box["data"] = {"contests": [{"date": today, "contest": "今天这场"}]}
        check("http 后端：表里有今天 → True",
              asyncio.run(core.XcpcHttp("http://x").today_reviewed()) is True)
        box["data"] = {"contests": [{"date": "1999-01-01"}]}
        check("http 后端：表里没有今天 → False",
              asyncio.run(core.XcpcHttp("http://x").today_reviewed()) is False)
        box["data"] = {}
        check("http 后端：/api/data 没有 contests 字段 → False（不崩）",
              asyncio.run(core.XcpcHttp("http://x").today_reviewed()) is False)
        check("http 后端：走的是 GET /api/data",
              calls and calls[-1] == ("GET", "/api/data"), str(calls[-1:]))
    finally:
        core.XcpcHttp._request = real


# ==========================================================================
# 7. 粘贴榜单 / 提交记录 → 自动补全复盘
# ==========================================================================
# 这一节守的是这次新增的能力：他打完 VP 把 QOJ 的榜单/提交记录粘进 QQ，
# 插件替他补上 过题 / AC 顺序 / 罚时（带算式）和逐题题号。
#
# 解析规则本身在 02-tools/standings.py（那边有 50+ 项自测），这里测的是
# **接线**：借没借到、两个后端给的是不是同一种结构、补的时候有没有守住
# "手打的字段优先"和"时间不足就不瞎填"。

SUB_PASTE = """0:12\tA\tAccepted
0:45\tB\tWrong Answer
0:52\tB\tWrong Answer
1:10\tB\tAccepted
1:30\tC\tTime Limit Exceeded"""

ROW_PASTE = "A\t+2\t1:45\nB\t-3\nC\t+ 0:23\nD"

#: 期望值（对着 standings.py 自己的自测手算过一遍）：
#:   A 0:12 一次过 → 0 分 0 罚；B 1:10 AC，前面 2 次失败 → 1 + 20×2 = 41；
#:   C 没过 → 不计。合计 41 分钟，AC 顺序 A>B。
SUB_PENALTY = 41


def _fake_parsed(kind="submissions"):
    """没有真实工作区时用的假解析结果。

    形状**故意照抄 serve.py /api/standings 的真实返回** —— 包括
    "榜单不给 timeline、提交记录不给 rows、罚时只有提交记录才算得出来"这几条，
    这样这一节在"插件被拷到 AstrBot 的 data/plugins 下、旁边没有工作区"
    的环境里也能跑，守住的仍然是合并逻辑（合并逻辑才是这次新写的代码）。
    """
    if kind == "standings":
        return core.normalize_contest_result({
            "ok": True, "kind": "standings",
            "rows": [{"label": "A", "result": "AC", "attempts": 3, "time": 105,
                      "note": ""},
                     {"label": "B", "result": "FAIL", "attempts": 3, "time": None,
                      "note": ""},
                     {"label": "C", "result": "AC", "attempts": 1, "time": 23,
                      "note": ""},
                     {"label": "D", "result": "NONE", "attempts": 0, "time": None,
                      "note": ""}],
            "timeline": [], "summary": "", "warnings": [], "ac_order": "C>A",
            "penalty": None, "penalty_breakdown": "", "per_fail": 20,
            "submission_count": 0,
        })
    return core.normalize_contest_result({
        "ok": True, "kind": "submissions", "rows": [],
        "timeline": [{"at": 12, "label": "A", "verdict": "AC", "fails": 0},
                     {"at": 45, "label": "B", "verdict": "WA", "fails": 2},
                     {"at": 70, "label": "B", "verdict": "AC", "fails": 0},
                     {"at": 90, "label": "C", "verdict": "TLE", "fails": 1}],
        "summary": "A 一次过；B 交了 3 次后过；C 交了 1 次没过（TLE）",
        "warnings": [], "ac_order": "A>B", "penalty": SUB_PENALTY,
        "penalty_breakdown": "A 0  +  B 1+20×2", "per_fail": 20,
        "submission_count": 5,
    })


def test_paste_merge() -> None:
    print("\n[7] 粘贴榜单/提交记录 → 补全复盘")

    # ---- 提交记录：过题 / 罚时 / AC 顺序 / 逐题题号 ----
    draft = core.parse_review_text(
        "比赛: QOJ VP 自测\n" + SUB_PASTE, default_date="2026-10-06")
    report = core.merge_contest_into_draft(draft, _fake_parsed())
    check("提交记录：认出来了", report["applied"] is True)
    check("提交记录：过题自动填 2", draft.solved == "2", draft.solved)
    check("提交记录：罚时自动填 41", draft.penalty == str(SUB_PENALTY), draft.penalty)
    check("提交记录：AC 顺序自动填 A>B", draft.order == "A>B", draft.order)
    check("提交记录：逐题题号 = A/B/C",
          [p.label for p in draft.problems] == ["A", "B", "C"],
          str([p.label for p in draft.problems]))
    check("提交记录：贡献/补题留空（不猜主观信息）",
          all(p.contrib == "—" and p.upsolve == "—" and p.kind == "—"
              for p in draft.problems), str([(p.contrib, p.upsolve) for p in draft.problems]))
    check("提交记录：回报里有算式",
          any("B 1+20×2" in x for x in core.format_contest_report(report)))
    check("提交记录：回报里提醒 CF 是 10 分钟/次",
          any("Codeforces 是 10" in x for x in core.format_contest_report(report)))
    check("提交记录：回报里有『比赛的形状』",
          any("B 交了 3 次后过" in x for x in core.format_contest_report(report)),
          str(core.format_contest_report(report)))
    check("提交记录：机器格子没重复进「想歪的地方」",
          "Wrong Answer" not in draft.wrong and "0:12" not in draft.wrong,
          repr(draft.wrong[:60]))
    check("提交记录：比赛名没被覆盖", draft.name == "QOJ VP 自测", draft.name)

    # 粘贴行本来会被 parse_review_text 当成"逐题记录"（说明栏是 +2 1:45 这种格子）
    d2 = core.parse_review_text(ROW_PASTE, default_date="2026-10-06")
    check("粘贴前：格子确实被误当成逐题说明（说明这里真的需要清理）",
          any("1:45" in p.note for p in d2.problems),
          str([p.note for p in d2.problems]))

    # ---- 榜单：过题 / AC 顺序 / 逐题题号（罚时它给不出来，见下）----
    d3 = core.parse_review_text("比赛: QOJ VP\n" + ROW_PASTE,
                                default_date="2026-10-06")
    rep3 = core.merge_contest_into_draft(d3, _fake_parsed("standings"))
    check("榜单：认出来了", rep3["applied"] is True)
    check("榜单：过题自动填 2", d3.solved == "2", d3.solved)
    check("榜单：AC 顺序自动填 C>A", d3.order == "C>A", d3.order)
    check("榜单：逐题题号含没提交的 D（A/B/C/D）",
          [p.label for p in d3.problems] == ["A", "B", "C", "D"],
          str([p.label for p in d3.problems]))
    check("榜单：说明栏里的格子被清掉了",
          all("1:45" not in p.note for p in d3.problems),
          str([p.note for p in d3.problems]))
    check("榜单：罚时没自动填（只有最终状态，时间信息不够）",
          d3.penalty == "" and rep3["penalty_skipped"] is True,
          repr((d3.penalty, rep3["penalty_skipped"])))

    # ---- 手打的字段优先（这次改动最容易踩的坑）----
    body = ("比赛: 手打优先\n过题: 3\n罚时: 145\n顺序: A>C>B\n想歪: 看到区间就上线段树\n"
            + SUB_PASTE)
    d4 = core.parse_review_text(body, default_date="2026-10-06")
    rep4 = core.merge_contest_into_draft(d4, _fake_parsed())
    check("过题没被 5 题的粘贴内容覆盖（仍是 3）", d4.solved == "3", d4.solved)
    check("罚时没被估算覆盖（仍是 145）", d4.penalty == "145", d4.penalty)
    check("AC 顺序没被覆盖", d4.order == "A>C>B", d4.order)
    kept = dict(rep4["kept_manual"])
    check("回报里说明了哪些手打字段被保住",
          kept.get("过题") == "3" and kept.get("罚时") == "145"
          and kept.get("AC 顺序") == "A>C>B", str(rep4["kept_manual"]))
    check("手打的『想歪』还在",
          "看到区间就上线段树" in d4.wrong, repr(d4.wrong[:60]))
    check("手打的罚时没被算进 auto",
          all(k != "罚时" for k, _ in rep4["auto"]), str(rep4["auto"]))

    # ---- 手打的逐题说明要留住，同时补上粘贴里有、他没写的题号 ----
    d5 = core.parse_review_text(
        "A 思路 想了一小时没往图论上想\nE 实现 独立想出 P1 边界挂了\n" + SUB_PASTE,
        default_date="2026-10-06")
    rep5 = core.merge_contest_into_draft(d5, _fake_parsed())
    labels5 = [p.label for p in d5.problems]
    check("手打的逐题说明保留", "想了一小时" in d5.problems[0].note, str(labels5))
    check("逐题合并后是 A/E/B/C（手打在前，粘贴补在后）",
          labels5 == ["A", "E", "B", "C"], str(labels5))
    check("E 的独立想出没被影响",
          [p for p in d5.problems if p.label == "E"][0].contrib == core.CONTRIB_INDEP)
    check("回报里报的是**真正补的**题号（A 已手打，所以只报 B/C）",
          rep5["added_labels"] == ["B", "C"] and rep5["problems_added"] == 2,
          str((rep5["problems_added"], rep5["added_labels"])))
    check("回报里的『补了 2 条题号（B/C）』",
          any("补了 2 条题号（B/C）" in x
              for x in core.format_contest_report(rep5)),
          str(core.format_contest_report(rep5)))

    # ---- 时间信息不足 → 罚时一个数字都不许填 ----
    no_time = core.normalize_contest_result({
        "ok": True, "kind": "submissions", "submission_count": 3,
        "rows": [], "timeline": [{"at": None, "label": "A", "verdict": "AC", "fails": 0},
                                {"at": None, "label": "B", "verdict": "WA", "fails": 1},
                                {"at": None, "label": "B", "verdict": "AC", "fails": 0}],
        "summary": "A 一次过；B 交了 2 次后过", "ac_order": "A>B",
        "penalty": None, "penalty_breakdown": "", "per_fail": 20, "warnings": [],
    })
    d6 = core.parse_review_text("比赛: 没时间的记录", default_date="2026-10-06")
    rep6 = core.merge_contest_into_draft(d6, no_time)
    check("时间不足：罚时保持空", d6.penalty == "", repr(d6.penalty))
    check("时间不足：但过题照填", d6.solved == "2", d6.solved)
    check("时间不足：回报里说明为什么没填", rep6["penalty_skipped"] is True)
    check("时间不足：回报里没有算式",
          not any("算式" in x for x in core.format_contest_report(rep6)),
          str(core.format_contest_report(rep6)))

    # ---- 认不出的文本 → 完全退回 parse_review_text 的行为 ----
    junk = "今天天气不错\n随便写点什么\nB 题读错题意了"
    plain = core.parse_review_text(junk, default_date="2026-10-06")
    d7 = core.parse_review_text(junk, default_date="2026-10-06")
    rep7 = core.merge_contest_into_draft(d7, core.empty_contest_result("没有 02-tools"))
    check("认不出：applied=False", rep7["applied"] is False)
    check("认不出：一个字都没改（渲染结果逐字节相同）",
          core.render_review_md(plain) == core.render_review_md(d7),
          repr(core.render_review_md(d7)[:60]))
    check("认不出：回报里没有多余的话", core.format_contest_report(rep7) == [])
    check("认不出：既没填过题也没填罚时（逐题记录原样不动）",
          d7.solved == "" and d7.penalty == "" and
          [p.label for p in d7.problems] == [p.label for p in plain.problems],
          str((d7.solved, d7.penalty, [p.label for p in d7.problems])))
    check("认不出：null / 空 dict 也不崩",
          core.merge_contest_into_draft(
              core.ReviewDraft(), None)["applied"] is False)

    # ---- 单条提交不算"认出来"（≥2 条才算，见 contest_recognized）----
    one = core.normalize_contest_result(
        {"ok": True, "kind": "submissions", "submission_count": 1,
         "timeline": [{"at": 12, "label": "A", "verdict": "AC", "fails": 0}],
         "rows": [], "penalty": 0, "ac_order": "A"})
    check("单条提交不算认出", core.contest_recognized(one) is False)
    check("单条提交不填任何字段",
          core.merge_contest_into_draft(
              core.ReviewDraft(), one)["applied"] is False)

    # ---- /解析 的回报：内容要全（类型/题数/顺序/罚时/形状/没看懂的行）----
    reply = core.format_parse_reply(_fake_parsed())
    check("/解析 说类型", "提交记录" in reply, reply[:120])
    check("/解析 说提交条数", "5 条" in reply, reply[:200])
    check("/解析 说过题数", "过题：2 道" in reply, reply[:200])
    check("/解析 说 AC 顺序", "AC 顺序：A>B" in reply, reply[:200])
    check("/解析 给估算罚时", "估算罚时：41 分钟" in reply, reply[:400])
    check("/解析 给算式", "A 0  +  B 1+20×2" in reply, reply[:400])
    check("/解析 提醒平台规则", "Codeforces 是 10" in reply, reply[:400])
    check("/解析 给『比赛的形状』", "比赛的形状" in reply and "TLE" in reply, reply[-400:])
    check("/解析 给时间线", "时间线" in reply, reply[-400:])
    check("/解析 明说没落盘", "没有写任何文件" in reply, reply[:40])

    # 没能理解的行必须**原样**列出来（不能静默丢）
    parsed_warn = core.normalize_contest_result({
        "ok": True, "kind": "standings",
        "rows": [{"label": "A", "result": "AC", "attempts": 1, "time": 23, "note": ""}],
        "timeline": [], "summary": "", "ac_order": "A", "penalty": None,
        "penalty_breakdown": "", "submission_count": 0,
        "warnings": ["有 1 行没能理解（原样列出）：", "  Rank 1234 someone 1713"],
    })
    reply2 = core.format_parse_reply(parsed_warn)
    check("/解析 列出没理解的行", "Rank 1234 someone 1713" in reply2, reply2)
    check("/解析 自己那句统计头不重复出现",
          "有 1 行没能理解" not in reply2, reply2)
    check("榜单没提交时罚时明说算不出来",
          "算不出来" in reply2 and "缺时间信息" in reply2, reply2)

    # 完全认不出 + 解析器加载失败，两种都要有可读的回复（不能抛异常）
    none_reply = core.format_parse_reply(core.normalize_contest_result(
        {"ok": True, "kind": "none", "rows": [], "timeline": [],
         "submission_count": 0, "warnings": ["没能从这段文本里认出任何题目。"]}))
    check("/解析：认不出时给提示", "没认出榜单或提交记录" in none_reply, none_reply)
    err_reply = core.format_parse_reply(core.empty_contest_result("没有 02-tools"))
    check("/解析：解析器缺失时报错不报栈", "解析不了" in err_reply
          and "02-tools" in err_reply, err_reply)

    # ---- 两个后端必须是同一种结构（上层才不用管用的是哪个）----
    check("统一结构：字段齐",
          set(core.empty_contest_result()) == {
              "ok", "error", "backend", "per_fail", "kind", "rows", "timeline",
              "submission_count", "summary", "ac_order", "penalty",
              "penalty_breakdown", "warnings"},
          str(sorted(core.empty_contest_result())))
    check("统一结构：http 后端返回的也是这些字段",
          set(core.normalize_contest_result({"ok": True})) ==
          set(core.empty_contest_result()))
    check("统一结构：坏返回值不崩（字符串）",
          core.normalize_contest_result("boom")["ok"] is False)
    check("统一结构：error 字段被翻成 ok=False",
          core.normalize_contest_result({"error": "解析失败：xx"})["ok"] is False)

    # ---- 平台提示词（cf → 10 分钟/次）----
    pf, rest = core.split_per_fail_directive("cf\n0:12 A Accepted", default=None)
    check("正文首行 cf → 按 10 分钟/次", pf == core.CF_PER_FAIL, str(pf))
    check("提示词那一行被摘掉", rest == "0:12 A Accepted", repr(rest))
    pf2, rest2 = core.split_per_fail_directive("qoj\nA +2 1:45", default=None)
    check("正文首行 qoj → 按 20", pf2 == core.DEFAULT_PER_FAIL, str(pf2))
    pf3, rest3 = core.split_per_fail_directive("比赛: CF Round 1024",
                                               default=core.DEFAULT_PER_FAIL)
    check("不是提示词就不动正文", pf3 == core.DEFAULT_PER_FAIL and
          rest3 == "比赛: CF Round 1024", repr((pf3, rest3)))
    pf4, rest4 = core.split_per_fail_directive("\n\ncf\nA + 1:00", default=None)
    check("前面有空行也认", pf4 == core.CF_PER_FAIL and rest4 == "A + 1:00",
          repr((pf4, rest4)))

    # ---- "像不像粘贴"的启发式（只用来决定要不要多嘴提醒）----
    check("像粘贴：提交记录", core.looks_like_paste(SUB_PASTE) is True)
    check("像粘贴：榜单格子", core.looks_like_paste(ROW_PASTE) is True)
    check("不像粘贴：自由文本",
          core.looks_like_paste("今天这把很难受，B 题读错了题意") is False)
    check("不像粘贴：单行日期的复盘开头",
          core.looks_like_paste("比赛: CF Round 1024\n过题: 3") is False)


def test_paste_backend(root: str | None) -> None:
    """真·端到端：借工作区里的 02-tools/standings.py 解析一遍。"""
    print("\n[8] 粘贴解析的两个后端")

    # ---- 不存在的工作区：绝不能抛异常（老版本 workspace / 配错路径）----
    empty = os.path.join(tempfile.gettempdir(), "xcpc-不存在的工作区")
    fs_bad = core.WorkspaceFS(root=empty)
    try:
        bad = fs_bad.parse_contest_text(SUB_PASTE)
        check("找不到 02-tools 时返回 ok=False（不抛异常）", bad["ok"] is False)
        check("找不到 02-tools 时给出可读原因",
              "02-tools" in (bad.get("error") or ""), str(bad.get("error")))
        check("找不到 02-tools 时结构仍然完整",
              set(bad) == set(core.empty_contest_result()), str(sorted(bad)))
    except Exception as exc:  # noqa: BLE001
        check("找不到 02-tools 时返回 ok=False（不抛异常）", False, repr(exc))

    # ---- http 后端：把 _request 换掉，不需要 aiohttp、也不需要真服务器 ----
    calls = []
    box = {"data": {}}

    def fake_request(self, method, path, payload=None):
        calls.append((method, path, payload))
        box["last_payload"] = payload

        async def _coro():
            if isinstance(box["data"], Exception):
                raise box["data"]
            return box["data"]
        return _coro()

    real = core.XcpcHttp._request
    core.XcpcHttp._request = fake_request
    try:
        box["data"] = {"ok": True, "kind": "submissions", "rows": [],
                       "timeline": [{"at": 12, "label": "A", "verdict": "AC"}],
                       "summary": "A 一次过", "warnings": [], "ac_order": "A",
                       "penalty": 0, "penalty_breakdown": "A 0",
                       "per_fail": 10, "submission_count": 2}
        got = asyncio.run(core.XcpcHttp("http://x").parse_contest_text(
            SUB_PASTE, per_fail=10))
        check("http 后端：走的是 POST /api/standings",
              calls and calls[-1][:2] == ("POST", "/api/standings"), str(calls[-1:]))
        check("http 后端：把正文和罚时规则都发过去了",
              box["last_payload"].get("text") == SUB_PASTE
              and box["last_payload"].get("per_fail") == 10,
              str(box["last_payload"])[:80])
        check("http 后端：返回值和 file 后端同构",
              set(got) == set(core.empty_contest_result()), str(sorted(got)))
        check("http 后端：字段解析正确",
              got["ok"] and got["kind"] == "submissions"
              and got["ac_order"] == "A" and got["penalty"] == 0,
              str({k: got[k] for k in ("kind", "ac_order", "penalty")}))

        # 那边没开机 / token 过期 / 旧版本没这个端点 → 只该"没解析成"
        box["data"] = RuntimeError("HTTP 502: bad gateway")
        down = asyncio.run(core.XcpcHttp("http://x").parse_contest_text(SUB_PASTE))
        check("http 后端：连不上也不抛，返回 ok=False", down["ok"] is False)
        check("http 后端：连不上时给出可读原因",
              "standings" in (down.get("error") or ""), str(down.get("error")))

        box["data"] = {"error": "解析失败：boom"}
        err = asyncio.run(core.XcpcHttp("http://x").parse_contest_text("x"))
        check("http 后端：服务端报错原样传上来", err["ok"] is False
              and "boom" in err["error"], str(err.get("error")))
    finally:
        core.XcpcHttp._request = real

    # ---- 真·端到端：借真实工作区的 standings.py ----
    if not root:
        print("      （跳过：没找到工作区里的 02-tools/standings.py —— "
              "这份自测是单独跑的？）")
        return
    fs = core.WorkspaceFS(root=root)
    got = fs.parse_contest_text(SUB_PASTE)
    check("真实工作区：提交记录解析成功", got["ok"] is True, str(got.get("error")))
    check("真实工作区：认出 5 条提交", got["submission_count"] == 5,
          str(got["submission_count"]))
    check("真实工作区：AC 顺序 A>B", got["ac_order"] == "A>B", got["ac_order"])
    check("真实工作区：罚时 41（20 分钟/次）", got["penalty"] == SUB_PENALTY,
          str(got["penalty"]))
    check("真实工作区：算式可读",
          "B 1+20×2" in (got["penalty_breakdown"] or ""), got["penalty_breakdown"])
    check("真实工作区：CF 规则按 10 算出来是 21",
          fs.parse_contest_text(SUB_PASTE, per_fail=core.CF_PER_FAIL)["penalty"] == 21,
          str(fs.parse_contest_text(SUB_PASTE, per_fail=10)["penalty"]))
    check("真实工作区：比赛的形状",
          "B 交了 3 次后过" in (got["summary"] or ""), got["summary"])
    check("真实工作区：榜单也认（kind=standings）",
          fs.parse_contest_text(ROW_PASTE)["kind"] == "standings")
    check("真实工作区：榜单认不出罚时（没有提交时间，不瞎填）",
          fs.parse_contest_text(ROW_PASTE)["penalty"] is None)
    check("真实工作区：AC 顺序从榜单推 C>A",
          fs.parse_contest_text(ROW_PASTE)["ac_order"] == "C>A",
          fs.parse_contest_text(ROW_PASTE)["ac_order"])
    check("真实工作区：乱文本 kind=none",
          fs.parse_contest_text("今天天气不错\n随便写点什么")["kind"] == "none")
    check("真实工作区：空文本不崩",
          fs.parse_contest_text("")["ok"] is True)
    check("真实工作区：连着解析两次结果一样（模块缓存没串味）",
          fs.parse_contest_text(SUB_PASTE)["penalty"] == got["penalty"])

    # ---- 端到端一遍：粘贴 → draft ----
    draft = core.parse_review_text("比赛: QOJ VP\n" + SUB_PASTE,
                                  default_date="2026-10-06")
    report = core.merge_contest_into_draft(draft, got)
    check("端到端：过题 2 / 罚时 41 / 顺序 A>B",
          (draft.solved, draft.penalty, draft.order) == ("2", "41", "A>B"),
          str((draft.solved, draft.penalty, draft.order)))
    check("端到端：逐题题号 A/B/C",
          [p.label for p in draft.problems] == ["A", "B", "C"],
          str([p.label for p in draft.problems]))
    md = core.render_review_md(draft)
    check("端到端：渲染里带着补好的结果",
          "| 结果 | 排名 - ｜ 过题 2 ｜ 罚时 41 |" in md and "A>B" in md,
          repr([l for l in md.splitlines() if "结果" in l]))
    # 写出去的文件必须还能被 train_stats.py 那套规则读回来（补字段不能破格式）
    tmp = tempfile.mkdtemp(prefix="xcpc-paste-")
    try:
        path = os.path.join(tmp, "2026-10-06-QOJ-VP.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        rec = core.parse_review_file(path)
        check("端到端：落盘后能被解析回来（KPI 不会算错）",
              rec is not None and rec["solved"] == 2 and rec["penalty"] == 41
              and len(rec["problems"]) == 3,
              str(rec and (rec["solved"], rec["penalty"], len(rec["problems"]))))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# 9. main.py 的接线（用一个 astrbot 外壳驱动 /复盘 和 /解析）
# ==========================================================================
# main.py 依赖 astrbot，平时只能放进真的 AstrBot 里跑。但这次改动的**接线**
# 全在 main.py 里（什么时候调后端、怎么把结果并进 draft、回报什么），
# 不跑一遍就等于没测。所以这里给它套一个最小外壳 ——
#
# ⚠️ 这个外壳**不校验 AstrBot 的 API**（那部分用法和改动前一模一样），
#    它只回答一个问题："把消息喂进去，插件会不会炸、写出来的东西对不对"。

SUB_PASTE_E2E = """比赛: QOJ VP 自测
类型: VP
0:12\tA\tAccepted
0:45\tB\tWrong Answer
0:52\tB\tWrong Answer
1:10\tB\tAccepted
1:30\tC\tTime Limit Exceeded"""


def _install_astrbot_stub():
    """把最小的 astrbot 外壳塞进 sys.modules（让 ``import main`` 能过）。"""
    import logging
    import types

    if "astrbot" in sys.modules:
        return sys.modules["astrbot"]

    quiet = logging.getLogger("xcpc-selftest")
    quiet.addHandler(logging.NullHandler())
    quiet.setLevel(logging.CRITICAL)

    class AstrMessageEvent:            # noqa: D401 - 只是占位
        pass

    class _Filter:
        def command(self, *args, **kwargs):
            def deco(fn):
                return fn
            return deco

        def regex(self, *args, **kwargs):
            def deco(fn):
                return fn
            return deco

    class Star:
        def __init__(self, context=None):
            self.context = context

        async def get_kv_data(self, key, default=None):
            return default

        async def put_kv_data(self, key, value):
            return None

        async def text_to_image(self, text, **kwargs):
            raise RuntimeError("自测里没有文转图")

    class Context:
        pass

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = quiet
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    event_mod.MessageChain = lambda *a, **kw: None
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Star = Star
    star_mod.Context = Context

    astrbot.api = api
    api.event = event_mod
    api.star = star_mod
    sys.modules.update({"astrbot": astrbot, "astrbot.api": api,
                        "astrbot.api.event": event_mod, "astrbot.api.star": star_mod})
    return astrbot


class _FakeEvent:
    """假消息事件 —— 只实现插件真的用到的那几个方法。"""

    def __init__(self, text):
        self.message_str = text
        self.replies = []
        self.unified_msg_origin = "aiocqhttp:FriendMessage:10001"
        self.stopped = False

    def plain_result(self, text):
        self.replies.append(text)
        return text

    def image_result(self, url):
        self.replies.append("(image)")
        return "(image)"

    def should_call_llm(self, flag):
        pass

    def stop_event(self):
        self.stopped = True

    def get_sender_id(self):
        return "10001"

    def is_admin(self):
        return True


def _run_handlers(agen):
    """把 async generator 跑完，返回它 yield 出来的所有结果。"""
    async def _collect():
        return [item async for item in agen]
    return asyncio.run(_collect())


def _review_files(root):
    review_dir = os.path.join(root, "04-review")
    if not os.path.isdir(review_dir):
        return []
    return sorted(os.listdir(review_dir))


def test_main_handlers(root: str | None) -> None:
    print("\n[9] main.py 接线（/复盘 与 /解析，用 astrbot 外壳驱动）")
    if not root:
        print("      （跳过：没找到带 02-tools/standings.py 的工作区）")
        return

    # 造一份**临时工作区**：只把 standings.py 拷进去 —— 这样能验证
    # "插件在别的地方也能借到解析器"，而且绝不碰真工作区的任何文件。
    tmp = tempfile.mkdtemp(prefix="xcpc-plugin-")
    try:
        os.makedirs(os.path.join(tmp, "02-tools"))
        shutil.copy(os.path.join(root, "02-tools", "standings.py"),
                    os.path.join(tmp, "02-tools", "standings.py"))

        _install_astrbot_stub()
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import main as plugin_main                     # noqa: PLC0415

        plugin = plugin_main.XcpcPlugin(
            None, {"workspace_root": tmp, "admin_only": False,
                   "handle": "test_handle"})
        plugin._backend = core.WorkspaceFS(root=tmp, handle="test_handle")

        check("插件能实例化（新 import 的名字都在）",
              plugin._per_fail() == core.DEFAULT_PER_FAIL, str(plugin._per_fail()))
        plugin.config["penalty_per_fail"] = 10
        check("罚时规则能配（CF=10）", plugin._per_fail() == 10,
              str(plugin._per_fail()))
        plugin.config["penalty_per_fail"] = "乱七八糟"
        check("罚时规则配错了退回 20（不崩）",
              plugin._per_fail() == core.DEFAULT_PER_FAIL, str(plugin._per_fail()))
        plugin.config["penalty_per_fail"] = 20

        # ---- /复盘 + 粘贴 ----
        event = _FakeEvent("/复盘 " + SUB_PASTE_E2E)
        _run_handlers(plugin.cmd_review(event))
        reply = "\n".join(event.replies)
        check("/复盘：有回复", bool(reply), reply[:80])
        check("/复盘：回报里说自动补了哪些", "自动补上：过题 2、罚时 41、AC 顺序 A>B" in reply,
              reply)
        check("/复盘：回报里有罚时算式", "B 1+20×2" in reply, reply)
        check("/复盘：回报里提醒 CF 是 10 分钟/次", "Codeforces 是 10" in reply, reply)
        check("/复盘：回报里有『比赛的形状』", "B 交了 3 次后过" in reply, reply)
        check("/复盘：回报里有逐题题号", "逐题记录补了 3 条题号" in reply, reply)

        files = _review_files(tmp)
        check("/复盘：文件落了盘", len(files) == 1, str(files))
        written = ""
        if files:
            with open(os.path.join(tmp, "04-review", files[0]),
                      encoding="utf-8") as fh:
                written = fh.read()
        check("/复盘：文件里有补好的过题/罚时",
              "| 结果 | 排名 - ｜ 过题 2 ｜ 罚时 41 |" in written,
              repr([x for x in written.splitlines() if "结果" in x]))
        check("/复盘：文件里有 AC 顺序", "| AC 顺序 | A>B |" in written, written[:400])
        check("/复盘：逐题表 3 行",
              written.count("\n| A |") + written.count("\n| B |")
              + written.count("\n| C |") == 3, written[-500:])
        check("/复盘：粘贴的流水账没进『想歪的地方』",
              "Wrong Answer" not in written.split("## 2.")[-1].split("## 3.")[0],
              written.split("## 2.")[-1][:120])
        check("/复盘：contests.csv 追加了一行",
              os.path.exists(os.path.join(tmp, "03-log", "contests.csv")))

        # ---- /复盘：手打的字段不被覆盖 ----
        event2 = _FakeEvent("/复盘 比赛: 手打优先\n过题: 3\n罚时: 145\n罚时算式无所谓\n"
                            + SUB_PASTE)
        _run_handlers(plugin.cmd_review(event2))
        reply2 = "\n".join(event2.replies)
        check("/复盘：手打的过题/罚时没被覆盖",
              "过题 3 ｜ 罚时 145" in reply2, reply2)
        check("/复盘：回报里说明手打的优先", "优先，没被粘贴里的估算顶掉" in reply2, reply2)
        files2 = _review_files(tmp)
        newest = ""
        with open(os.path.join(tmp, "04-review", files2[-1]), encoding="utf-8") as fh:
            newest = fh.read()
        check("/复盘：落盘文件里也是手打的值",
              "过题 3 ｜ 罚时 145" in newest, newest[:400])

        # ---- /解析：只回报，不落盘 ----
        before = _review_files(tmp)
        csv_before = os.path.getmtime(os.path.join(tmp, "03-log", "contests.csv"))
        event3 = _FakeEvent("/解析 " + SUB_PASTE_E2E)
        _run_handlers(plugin.cmd_parse(event3))
        reply3 = "\n".join(event3.replies)
        check("/解析：有回复", bool(reply3), reply3[:80])
        check("/解析：说类型和条数", "提交记录" in reply3 and "5 条" in reply3, reply3)
        check("/解析：给罚时和算式",
              "估算罚时：41 分钟" in reply3 and "A 0  +  B 1+20×2" in reply3, reply3)
        check("/解析：说 AC 顺序", "AC 顺序：A>B" in reply3, reply3)
        check("/解析：给『比赛的形状』", "比赛的形状" in reply3, reply3)
        check("/解析：明说没落盘", "没有写任何文件" in reply3, reply3[:60])
        check("/解析：真的没写文件", _review_files(tmp) == before,
              str(_review_files(tmp)))
        check("/解析：也没动 contests.csv",
              os.path.getmtime(os.path.join(tmp, "03-log", "contests.csv")) == csv_before)
        check("/解析：提醒怎么落盘", "发 /复盘" in reply3, reply3[-120:])
        print("      /解析 的实际回复：")
        print("      " + reply3.replace("\n", "\n      "))

        # ---- /解析：正文首行 cf → 按 10 分钟/次 ----
        event4 = _FakeEvent("/解析 cf\n" + SUB_PASTE_E2E)
        _run_handlers(plugin.cmd_parse(event4))
        reply4 = "\n".join(event4.replies)
        check("/解析：cf 提示词按 10 分钟/次算（罚时 21）",
              "估算罚时：21 分钟" in reply4 and "B 1+10×2" in reply4, reply4)

        # ---- 认不出的内容：退回老行为，不报错 ----
        event5 = _FakeEvent("/解析 今天天气不错，随便写点什么")
        _run_handlers(plugin.cmd_parse(event5))
        check("/解析：认不出也不炸",
              event5.replies and "没认出榜单或提交记录" in "\n".join(event5.replies),
              str(event5.replies)[:200])

        event6 = _FakeEvent("/复盘 今天这把很难受，B 题读错了题意")
        _run_handlers(plugin.cmd_review(event6))
        reply6 = "\n".join(event6.replies)
        check("/复盘：纯自由文本照旧能记", "已记录" in reply6, reply6[:80])
        check("/复盘：自由文本不会触发粘贴解析那套话",
              "解析了你粘的" not in reply6, reply6)

        # ---- 后端给不出解析（老工作区）时，/复盘 仍然要能记 ----
        plugin._backend = core.WorkspaceFS(root=os.path.join(tmp, "没有02-tools"))
        event7 = _FakeEvent("/复盘 比赛: 老工作区\n过题: 1\n" + SUB_PASTE)
        _run_handlers(plugin.cmd_review(event7))
        reply7 = "\n".join(event7.replies)
        check("/复盘：解析器缺失也照记（只是没自动补）",
              "已记录" in reply7 and "解析了你粘的" not in reply7, reply7)
        check("/复盘：解析器缺失时给出提示（这段确实像粘的）",
              "自动解析没成" in reply7 and "02-tools" in reply7, reply7)

        # 同一个后端下 /解析 要老实报错（不静默）
        event8 = _FakeEvent("/解析 " + SUB_PASTE)
        _run_handlers(plugin.cmd_parse(event8))
        check("/解析：后端不可用时报可读原因",
              "02-tools" in "\n".join(event8.replies), str(event8.replies)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # 清掉临时工作区那份被加载的 standings 模块，免得影响后面的测试
        core._STANDINGS_CACHE.clear()


# ==========================================================================
# 10. 拿真实工作区做只读检查
# ==========================================================================
def _detect_handle(root: str) -> str:
    """工作区里 ``data/`` 是按 handle 命名的，别写死 —— 从 ``*_info.json`` 认出来。

    这里原来写的是一个固定的用户名。清个人标识那次把它换成了一个占位符，
    结果是：自测拿**工作区里根本不存在**的 handle 去读，``status()`` 老老实实
    返回 rating=None，这一节从此永远红着。永远红的检查比没有检查更糟 ——
    看两次之后人就不看它了。

    认不出来（工作区新建的、还没同步过）就返回空串，本节自己跳过。
    """
    try:
        names = sorted(os.listdir(os.path.join(root, "data")))
    except OSError:
        return ""
    for name in names:
        if name.endswith("_info.json"):
            return name[: -len("_info.json")]
    return ""


def test_real_workspace(root: str) -> None:
    print("\n[10] 真实工作区只读检查：%s" % root)
    if not os.path.isdir(root):
        check("工作区存在", False, root)
        return
    handle = _detect_handle(root)
    if not handle:
        print("      data/ 里没有 *_info.json（这工作区还没同步过？），本节跳过")
        return
    fs = core.WorkspaceFS(root=root, handle=handle)
    try:
        st = fs.status()
        print("      rating=%s  AC(API)=%s  AC(主页)=%s  连续=%s  倒计时=%s"
              % (st.get("rating"), st.get("solved_api"), st.get("solved_all_time"),
                 (st.get("streak") or {}).get("current_streak"),
                 (st.get("next_contest") or {}).get("days")))
        check("status 能读出来", st.get("rating") is not None, str(st.get("rating")))
        today = fs.today_tasks()
        print("      今天: %s（%d/%d）"
              % (today.get("title"), today.get("done", 0), today.get("total", 0)))
        lists = fs.problem_lists()
        print("      题单 %d 份" % len(lists))
        kpi = st.get("kpi") or {}
        print("      复盘 %s 场" % kpi.get("sessions"))
        print("      渲染预览：")
        print("      " + format_status_preview(st, handle).replace("\n", "\n      "))
    except Exception as exc:
        check("读真实工作区不报错", False, repr(exc))


def format_status_preview(st: dict, handle: str) -> str:
    """简单预览，避免为了自测去 import main.py（那个要 astrbot）。"""
    return "rating %s / AC %s / 连续 %s 天 / 下一场 %s 天后" % (
        st.get("rating"), st.get("solved_api"),
        (st.get("streak") or {}).get("current_streak"),
        (st.get("next_contest") or {}).get("days"))


# ==========================================================================
def _detect_workspace(cli_root: str | None) -> str | None:
    """找一个**真的**带着 02-tools/standings.py 的工作区（没有就返回 None）。

    顺序：命令行给的 > 插件目录往上两层（插件就住在 ``xcpc/integrations/`` 里，
    这是开发时的常见情况）。插件被拷到 AstrBot 的 ``data/plugins/`` 下时两个
    都不成立 —— 那时"粘贴解析端到端"这一节会自己跳过，不报红。
    """
    for cand in (cli_root, os.path.dirname(os.path.dirname(HERE))):
        if cand and os.path.isfile(os.path.join(cand, "02-tools", "standings.py")):
            return os.path.abspath(cand)
    return None


def _translate_wsl_path(path: str) -> str:
    """把 WSL 路径转成 Windows 路径。

    **为什么需要**：开发时习惯在 WSL 里敲命令，但跑的是 Windows Python
    （`python.exe`）—— 它看不懂 `/mnt/c/...`，`os.path.isdir()` 直接为假。
    表现是 `[10] 真实工作区只读检查` 报"工作区存在 ✗"，
    看起来像插件坏了，其实只是路径没翻译（我自己就被绕进去过一次）。

    只在 Windows 上且路径以 `/mnt/<盘>/` 开头时才转。
    """
    if os.name != "nt":
        return path
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path)
    if not m:
        return path
    return "%s:\\%s" % (m.group(1).upper(), m.group(2).replace("/", "\\"))


def main() -> int:
    parser = argparse.ArgumentParser(description="astrbot_plugin_xcpc 自测")
    parser.add_argument("--root", default=None,
                        help="真实 XCPC 工作区根目录（只读检查）")
    args = parser.parse_args()

    root = _translate_wsl_path(args.root) if args.root else None
    if root and root != args.root:
        print("（路径已从 WSL 转成 Windows：%s）" % root)

    print("=" * 62)
    print(" astrbot_plugin_xcpc 自测（不需要 AstrBot）")
    print("=" * 62)

    test_parse()
    test_render_roundtrip()
    test_metrics()
    test_workspace_fs()
    test_reviewed_today()
    test_paste_merge()
    workspace = _detect_workspace(root)
    test_paste_backend(workspace)
    test_main_handlers(workspace)
    if root:
        test_real_workspace(root)

    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
