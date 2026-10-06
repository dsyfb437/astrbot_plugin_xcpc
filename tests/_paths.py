#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨 WSL / Windows 的路径与解释器工具。

**为什么需要这个**
------------------
开发时在 WSL 里敲命令，但跑的往往是 **Windows 的 python.exe / node.exe** ——
它们看不懂 `/mnt/c/...` 或 `/tmp/...` 这种 WSL 路径。

这个坑我踩了两次：
  1. `selftest.py --root /mnt/c/...` → `os.path.isdir()` 假 → 报"工作区不存在"
  2. `test_pages.py` 在 WSL 的 `/tmp` 下调用 `node --check /tmp/...` → node 崩

两次的表现都像"代码坏了"，实际只是路径没翻译。
所以抽到这里，**所有跨进程调用都走它**。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys


def is_windows_python() -> bool:
    return os.name == "nt"


def to_windows_path(path: str) -> str:
    r"""WSL 路径 → Windows 路径。**翻译不了就返回 None。**

        /mnt/c/Users/x                →  C:\Users\x        ✅
        /tmp/x                        →  None              ❌（WSL 虚拟盘）
        \\wsl.localhost\Ubuntu\tmp\x  →  None              ❌（UNC，见下）

    ⚠️ 第二种是踩出来的：在 WSL 里启动 Windows 的 python.exe 时，
    **cwd 会被传成 UNC 路径** `\\wsl.localhost\<发行版>\...`。
    Windows Python 读得动它，但 `node.exe` 读不动 ——
    会崩在 `package_json_reader`，看起来像"JS 有语法错误"。

    所以 UNC 也算"翻译不了"，交给调用方去复制文件。
    """
    if not is_windows_python():
        return path
    s = str(path)
    # UNC 形式：\\wsl.localhost\... 或 \\wsl$\...
    if s.startswith("\\\\"):
        low = s.lower()
        if "wsl.localhost" in low or low.startswith("\\\\wsl$"):
            return None            # type: ignore[return-value]
        return s                   # 普通 UNC 可以用
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", s)
    if m:
        return "%s:\\%s" % (m.group(1).upper(), m.group(2).replace("/", "\\"))
    if s.startswith("/"):
        return None                # type: ignore[return-value]
    return s


def explain_path_problem(path: str) -> str:
    """给一句人话，解释为什么这个路径在 Windows 程序里用不了。"""
    return (
        "路径 %r 是 WSL 路径，但这里要调用 Windows 程序（python.exe / node.exe），"
        "它看不懂。\n"
        "  解决：把仓库放在 /mnt/c/... 下（Windows 盘）再跑，"
        "或者用 Windows 侧的 Python 跑测试。" % path)


def find_node() -> str | None:
    """找可用的 node。找到的路径**已经转成当前解释器能用的形式**。"""
    # 不写死路径 —— 别人机器上没有这个目录。
    # 优先从**当前解释器旁边**推：DSH 的运行时把 node 和 python 放在
    # 同一个 dependencies/ 下，所以 python.exe 的兄弟目录里就有 node。
    cands = []
    env = os.environ.get("XCPC_NODE")
    if env:
        cands.append(env)
    try:
        dep = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        for rel in ("node/bin/node.exe", "node/bin/node",
                    "node/node.exe", "node/node"):
            cands.append(os.path.join(dep, rel.replace("/", os.sep)))
    except Exception:
        pass
    cands += [shutil.which("node"), shutil.which("nodejs")]
    cands = [c for c in cands if c]
    for c in cands:
        if not c:
            continue
        try:
            r = subprocess.run([c, "--version"], capture_output=True, timeout=15)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


def node_check(node: str, js_path: str) -> tuple[bool, str]:
    """用 node 检查一个 JS 文件的语法。

    返回 `(是否通过, 说明)`。

    **路径问题不该判成"语法错误"**。Windows 的 node.exe 看不见 WSL 的
    `/tmp/...`（在虚拟磁盘里，连 `/mnt/c` 都不是），这时：
      1. 先把文件**复制到 Windows 能看见的临时目录**再检查（首选）
      2. 实在不行才如实说明"这个环境检查不了"

    为什么这么绕：我第一版直接报失败，结果在 WSL 的 /tmp 下跑测试时
    永远有一条红的 —— 而那条红**不是代码问题**，会训练人忽略失败。
    一条"永远红但不重要"的检查，比没有检查更糟。
    """
    target = js_path
    tmp_copy = None
    if is_windows_python():
        p = to_windows_path(js_path)
        if p is None:
            # Windows 看不见这个路径 → 复制到 Windows 临时目录
            import tempfile
            try:
                fd, tmp_copy = tempfile.mkstemp(suffix=".js")
                os.close(fd)
                shutil.copyfile(js_path, tmp_copy)
                p = tmp_copy
            except OSError as exc:
                return True, ("跳过：这个位置（%s）Windows 的 node 看不见，"
                              "复制到临时目录也失败（%s）。**不是语法问题**。"
                              % (js_path, exc))
        target = p

    try:
        r = subprocess.run([node, "--check", target], capture_output=True,
                           timeout=30, text=True)
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or "").strip().split("\n")
        return False, err[0] if err else "node --check 失败"
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    finally:
        if tmp_copy:
            try:
                os.unlink(tmp_copy)
            except OSError:
                pass
