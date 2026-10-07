#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区目录自动初始化的自测（xcpc_core.WorkspaceFS.ensure）。

**起因** —— 用户 2026-10-08 的原话：

    这首先是个插件，插件应该建好这个目录吧，然后引导也修一下

原来的做法是 ``workspace_root`` 留空就直接抛「还没配置 workspace_root ——
去 WebUI 的插件配置里填上 xcpc 工作区的绝对路径」：用户得先猜出插件内部
约定的 ``00-plan`` / ``03-log`` / ``04-review`` 这几个名字，自己 mkdir
一遍，再回来填。现在改成插件自己建。

跑法：python tests/test_workspace.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import xcpc_core as xc    # noqa: E402

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


def _raises_value_error(fs):
    try:
        fs.append_inbox("   ")
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------------------
# 1. 骨架
# ---------------------------------------------------------------------------

def test_ensure_dirs():
    print("\n[1] 建目录")

    root = tempfile.mkdtemp(prefix="xcpc_ws_")
    shutil.rmtree(root)                    # 连根都不存在，逼它自己建
    fs = xc.WorkspaceFS(root=root)
    try:
        ret = fs.ensure()
        check("ensure 返回根目录", ret == fs.root, ret)
        check("根目录真被建出来了", os.path.isdir(root))
        for rel in ("00-plan", "00-plan/lists", "03-log", "04-review",
                    "data", "config"):
            check("有 %s/" % rel,
                  os.path.isdir(os.path.join(root, *rel.split("/"))))

        check("路径属性和 root 对得上",
              fs.review_dir == os.path.join(root, "04-review")
              and fs.inbox_path == os.path.join(root, "03-log", "inbox.md")
              and fs.csv_path == os.path.join(root, "03-log", "contests.csv")
              and fs.sprint_path == os.path.join(root, "00-plan", "sprint.md")
              and fs.lists_dir == os.path.join(root, "00-plan", "lists"))

        # 幂等：插件每次拿 backend 都会 ensure 一遍，不能第二次就出事
        before = sorted(os.listdir(root))
        fs.ensure()
        check("第二次 ensure 不炸", True)
        check("第二次 ensure 不多出东西",
              sorted(os.listdir(root)) == before, repr(sorted(os.listdir(root))))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. 种子文件
# ---------------------------------------------------------------------------

def test_seeds():
    print("\n[2] 种子文件")

    root = tempfile.mkdtemp(prefix="xcpc_ws_")
    fs = xc.WorkspaceFS(root=root)
    try:
        fs.ensure()
        for rel in ("README.md", "03-log/inbox.md", "00-plan/sprint.md"):
            p = os.path.join(root, *rel.split("/"))
            check("有 %s" % rel, os.path.isfile(p))
            check("%s 不是空文件" % rel,
                  os.path.isfile(p) and os.path.getsize(p) > 0)

        with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        check("README 讲了目录结构",
              "00-plan" in readme and "03-log" in readme
              and "04-review" in readme)
        check("README 讲了 workspace_root 留空就是默认位置",
              "workspace_root" in readme and "留空" in readme)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. 已存在的文件一个字都不动
# ---------------------------------------------------------------------------

def test_no_clobber():
    print("\n[3] 不覆盖已有文件")

    root = tempfile.mkdtemp(prefix="xcpc_ws_")
    fs = xc.WorkspaceFS(root=root)
    try:
        os.makedirs(os.path.join(root, "00-plan"))
        mine = os.path.join(root, "00-plan", "sprint.md")
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write("# 我自己写的\n\n## 10/08 周三\n\n- [ ] 我的任务\n")

        fs.ensure()

        with open(mine, encoding="utf-8") as fh:
            after = fh.read()
        check("用户自己的 sprint.md 没被动过",
              after.startswith("# 我自己写的"), after[:40])
        check("用户自己的任务还在", "我的任务" in after, after[:80])
        check("缺的 inbox.md 照样补上",
              os.path.isfile(os.path.join(root, "03-log", "inbox.md")))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. 种子不能自己长出任务来 ← 这条最要紧
#
# 种子示例里必然要写 `- [ ] 一场 VP` 这种格式说明。但
#     CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.+?)\s*$")
#     HEADING_RE  = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# 都是 ^ 锚定的 —— 示例要是顶格写，就等于凭空给用户排出两条"今天的待办"，
# 而且 /xcpc 今天 会一本正经地把它念出来。所以示例一律用 `>` 引用块包住。
# ---------------------------------------------------------------------------

def test_seed_is_not_tasks():
    print("\n[4] 种子不能长出假任务")

    root = tempfile.mkdtemp(prefix="xcpc_ws_")
    fs = xc.WorkspaceFS(root=root)
    try:
        fs.ensure()
        with open(fs.sprint_path, encoding="utf-8") as fh:
            raw = fh.read()
        check("示例确实写在文件里（不然测了个寂寞）",
              "[ ] 一场 VP" in raw, raw[:200])

        groups = xc.parse_checklist(fs.sprint_path, "冲刺打卡")
        check("parse_checklist 读不到任何任务", groups == [], repr(groups))

        t = fs.today_tasks(day="2026-10-08")
        check("today_tasks 老实说没找到安排",
              t["items"] == [] and "没找到" in t["title"], repr(t))
        check("today_tasks 的结构仍然完整",
              {"date", "title", "items"} <= set(t), repr(sorted(t)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. 空工作区上的只读方法要干净退化（不能因为没数据就崩）
# ---------------------------------------------------------------------------

def test_readonly_on_empty():
    print("\n[5] 空工作区上不崩")

    root = tempfile.mkdtemp(prefix="xcpc_ws_")
    fs = xc.WorkspaceFS(root=root, handle="dsyfb_437")
    try:
        fs.ensure()
        st = fs.status()
        check("status 不崩", isinstance(st, dict))
        check("status 认得出 handle",
              st.get("handle") == "dsyfb_437", repr(st.get("handle")))
        check("status 的提交数是 0",
              st.get("submissions") == 0, repr(st.get("submissions")))
        check("题单是空的", fs.problem_lists() == [])
        check("收件箱是空的", fs.read_inbox() == [])
        check("今天没写过复盘", not fs.today_reviewed(day="2026-10-08"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. 真的能往里写
# ---------------------------------------------------------------------------

def test_write_paths():
    print("\n[6] 写进去能读回来")

    root = tempfile.mkdtemp(prefix="xcpc_ws_")
    fs = xc.WorkspaceFS(root=root)
    try:
        fs.ensure()
        stamp = fs.append_inbox("今天写了三道 1800 的题")
        check("append_inbox 返回时间戳", bool(stamp), stamp)
        items = fs.read_inbox()
        check("read_inbox 读得到", len(items) == 1, repr(items))
        check("内容是刚写的那条",
              items and "三道 1800" in items[0]["text"], repr(items[:1]))
        check("空内容会被拒", _raises_value_error(fs))
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. 建不出来要抛，不能装作没事
# ---------------------------------------------------------------------------

def test_ensure_raises():
    print("\n[7] 建不出来要抛")

    tmp = tempfile.mkdtemp(prefix="xcpc_ws_")
    blocker = os.path.join(tmp, "blocker")
    with open(blocker, "w", encoding="utf-8") as fh:
        fh.write("x")
    fs = xc.WorkspaceFS(root=os.path.join(blocker, "ws"))
    try:
        try:
            fs.ensure()
        except OSError as exc:
            check("父路径是文件时抛 OSError", True, type(exc).__name__)
        else:
            check("父路径是文件时抛 OSError", False, "居然没抛")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 62)
    print("WorkspaceFS.ensure 自测（工作区自动创建）")
    print("=" * 62)
    test_ensure_dirs()
    test_seeds()
    test_no_clobber()
    test_seed_is_not_tasks()
    test_readonly_on_empty()
    test_write_paths()
    test_ensure_raises()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
