#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加载方式的自测 —— 抓"测试全绿、真机全炸"的那一类。

**起因**：真机上账号绑定页显示

    读状态失败：读状态失败：No module named 'platforms'

而本地 728 项测试全绿。原因不是逻辑错，是**加载方式不同**：

  * AstrBot 按**包**加载插件。`astrbot/core/star/star_manager.py` 里
    拼出 `path = "data.plugins." + 插件目录名 + "." + "main"`，再
    `__import__(path, fromlist=["main"])`。所以 `core/sync.py` 的模块名是
    `data.plugins.astrbot_plugin_xcpc.core.sync`，**`platforms` 不是顶层模块**，
    `from platforms.codeforces import Codeforces` 直接 ModuleNotFoundError。
  * `tests/` 和 `selftest.py` 按**顶层模块**加载：把插件目录塞进 `sys.path`，
    再 `from core import accounts`。这时上面那句**恰好能用**。

于是测试跑的是"恰好能用"的那种方式，真机跑的是另一种。这个文件把两种都跑一遍。

跑法：python tests/test_imports.py
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import textwrap

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  \u2713 %s" % label)
    else:
        FAIL += 1
        print("  \u2717 %s%s" % (label, ("  -> " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# 1. 静态：本地模块不许用裸的顶层导入
# ---------------------------------------------------------------------------

# `core/` 和 `platforms/` 都是插件的子包。在包加载方式下它们**没有**顶层名字，
# 所以任何"裸"的 `from platforms.x import Y` / `import core` 都是定时炸弹。
_BARE = re.compile(
    r"^[ \t]*(?:from[ \t]+(?P<from>platforms|core|xcpc_core)[\s.]"
    r"|import[ \t]+(?P<imp>platforms|core|xcpc_core)\b)",
    re.M)


def _py_files():
    out = [os.path.join(PLUGIN, "main.py"), os.path.join(PLUGIN, "xcpc_core.py")]
    for sub in ("core", "platforms"):
        d = os.path.join(PLUGIN, sub)
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                out.append(os.path.join(d, name))
    return [p for p in out if os.path.isfile(p)]


def test_no_bare_local_imports() -> None:
    """core/ 和 platforms/ 里不许出现裸的 `from platforms.x import Y`。"""
    offenders = []
    for path in _py_files():
        rel = os.path.relpath(path, PLUGIN)
        if rel.startswith("main.py") or rel.startswith("xcpc_core.py"):
            continue          # main.py 自己就有 try/except 双份导入，交给第 3 项查
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for m in _BARE.finditer(src):
            line_no = src[:m.start()].count("\n") + 1
            offenders.append("%s:%d  %s" % (rel, line_no, m.group(0).strip()))
    check("core/ 和 platforms/ 里没有裸的顶层本地导入（%d 个文件）"
          % len(_py_files()), not offenders,
          "；".join(offenders[:4]) + ("…" if len(offenders) > 4 else ""))


# ---------------------------------------------------------------------------
# 2. 动态：真的按 AstrBot 的方式（当包）加载一遍
# ---------------------------------------------------------------------------

# 在子进程里跑的脚本。复刻 star_manager.py 的目录形状：
#   <tmp>/data/plugins/astrbot_plugin_xcpc  ->  插件目录（软链）
# 然后 `import data.plugins.astrbot_plugin_xcpc.core.accounts` —— 和真机一模一样。
#
# 里面那个 astrbot 桩是**为了能 import main.py**（真机入口就是它）。
# 桩必须自己搭，不能去 import tests/test_routes.py 的 install_stub ——
# 那个会往 sys.path 里插插件目录，`platforms` 就变成顶层模块了，复刻立刻失效。
_PKG_SCRIPT = textwrap.dedent('''
    import logging
    import sys
    import types

    sys.path.insert(0, {tmp!r})

    quiet = logging.getLogger("xcpc-pkgmode")
    quiet.addHandler(logging.NullHandler())
    quiet.setLevel(logging.CRITICAL)

    class _Group:
        def command(self, *a, **kw):
            def deco(fn):
                return fn
            return deco
        regex = command

    class _Filter:
        def command(self, *a, **kw):
            def deco(fn):
                return fn
            return deco
        regex = command
        def command_group(self, group_name, *a, **kw):
            def deco(fn):
                return _Group()
            return deco

    class AstrMessageEvent:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

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
    sys.modules.update({{"astrbot": astrbot, "astrbot.api": api,
                         "astrbot.api.event": ev, "astrbot.api.star": star,
                         "astrbot.api.web": web}})

    # 1) 真机的入口：main.py 自己也必须能在包加载下 import 成功
    import data.plugins.astrbot_plugin_xcpc.main as m
    assert m.__package__ == "data.plugins.astrbot_plugin_xcpc", m.__package__

    import data.plugins.astrbot_plugin_xcpc.core.accounts as acc
    import data.plugins.astrbot_plugin_xcpc.core.sync as sync

    assert acc.__package__ == "data.plugins.astrbot_plugin_xcpc.core", acc.__package__

    # 2) 绑定页读状态走的正是这一条（AccountService.__init__ 调的）
    got = sorted(acc.default_authenticators())
    assert got == ["atcoder", "codeforces", "luogu", "qoj"], got

    # 3) AccountService 建得起来 —— 真机上就是这一步炸的
    #    （`main.py` 的 `_acct()` 会 `AccountService(store, recorder, ...)`）
    svc = acc.AccountService(store=object(), recorder=None)
    assert sorted(svc._auth) == ["atcoder", "codeforces", "luogu", "qoj"], svc._auth

    # 4) 同步 / 登录走的这一条
    names = {{}}
    for p in ("codeforces", "atcoder", "qoj", "luogu"):
        names[p] = type(sync._adapter(p)).__name__
    assert names == {{"codeforces": "Codeforces", "atcoder": "AtCoder",
                     "qoj": "Qoj", "luogu": "Luogu"}}, names

    # 5) 对照组：裸导入在这里必须失败，否则说明这个复刻没复刻到点子上
    try:
        from platforms.atcoder import AtCoder  # noqa: F401
    except ModuleNotFoundError:
        print("PACKAGE_MODE_OK", "main=" + m.__package__,
              "auth=" + ",".join(got),
              "adapters=" + ",".join(sorted(names.values())))
    else:
        print("REPRO_NOT_FAITHFUL")
''')


def test_imports_work_in_package_mode() -> None:
    """按 AstrBot 的包加载方式跑一遍：认证器和四个平台适配器都要能导出来。"""
    tmp = tempfile.mkdtemp(prefix="xcpc_pkgmode_")
    link = os.path.join(tmp, "data", "plugins", "astrbot_plugin_xcpc")
    os.makedirs(os.path.dirname(link), exist_ok=True)
    try:
        os.symlink(PLUGIN, link)
    except (OSError, NotImplementedError) as exc:       # pragma: no cover
        check("包加载方式下导入 platforms（软链失败：%s）" % exc, False,
              "这个检查需要能建软链")
        return
    try:
        # cwd 必须是**中立**目录：`python -c` 会把 cwd 塞进 sys.path，
        # 在插件目录里跑等于把 platforms 变成顶层模块，那就测不出问题了。
        proc = subprocess.run(
            [sys.executable, "-c", _PKG_SCRIPT.format(tmp=tmp)],
            capture_output=True, text=True, cwd=tmp, timeout=120)
        out = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0 and "PACKAGE_MODE_OK" in out
        tail = out.strip().splitlines()[-3:]
        detail = " / ".join(t.strip() for t in tail) if not ok else ""
        if proc.returncode == 0 and "REPRO_NOT_FAITHFUL" in out:
            detail = "复刻不准：包加载下裸导入竟然成功了"
            ok = False
        if ok:
            # 把证据打出来，省得"绿了但不知道绿在哪"
            for line in out.splitlines():
                if line.startswith("PACKAGE_MODE_OK"):
                    print("      %s" % line[len("PACKAGE_MODE_OK"):].strip())
                    break
        check("按 AstrBot 的方式（当包）加载 main.py 和 platforms", ok, detail)
    finally:
        try:
            os.unlink(link)
            os.rmdir(os.path.join(tmp, "data", "plugins"))
            os.rmdir(os.path.join(tmp, "data"))
            os.rmdir(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 3. main.py 的双份导入仍然成对
# ---------------------------------------------------------------------------

def test_main_has_both_import_paths() -> None:
    """main.py 的相对导入和顶层导入必须成对出现 —— 只有一份就说明退化过。"""
    with open(os.path.join(PLUGIN, "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    rel, top = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 1 and mod in ("core", "xcpc_core"):
                rel.append(mod)
            elif node.level == 0 and mod in ("core", "xcpc_core"):
                top.append(mod)
            elif node.level == 1 and mod == "":
                for a in node.names:
                    if a.name in ("core", "xcpc_core"):
                        rel.append(a.name)
    check("main.py 有相对导入（包加载）", bool(rel), "一个都没找到")
    check("main.py 有顶层导入兜底（自测 / 手工调试）", bool(top), "一个都没找到")


# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 62)
    print("导入 / 加载方式检查")
    print("=" * 62)
    test_no_bare_local_imports()
    test_imports_work_in_package_mode()
    test_main_has_both_import_paths()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
