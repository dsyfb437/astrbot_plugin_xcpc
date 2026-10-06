#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pages/ 的静态检查 —— **不需要 AstrBot、不需要浏览器**。

为什么要有这个
--------------
写 `pages/accounts/` 时我一口气踩了三个坑，全都是"打开页面才发现白屏"的类型：

  1. bridge API 凭印象写成了 `bridge.request({path, method, body})` —— 实际是
     `apiGet` / `apiPost`，页面会直接不可用。
  2. 改的时候留下**重复的 `function call()`** —— JS 里后定义的覆盖先定义的，
     等于我的修复被旧代码悄悄盖掉（这种最阴，肉眼看 diff 都不一定发现）。
  3. `var BASE` 声明了两次，路径一个带插件名前缀一个不带。

这三类错误都可以静态查出来。所以写成测试，而不是靠记性。

跑法：python tests/test_pages.py
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PAGES = os.path.join(PLUGIN, "pages")

sys.path.insert(0, HERE)
import _paths  # noqa: E402

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


# bridge 上真实存在的方法（对照官方文档 + 参考插件核实）
KNOWN_BRIDGE_METHODS = {
    "ready", "apiGet", "apiPost", "subscribeSSE", "unsubscribeSSE",
    # 官方 demo 里出现过的其他成员
    "apiPut", "apiDelete", "download", "upload",
}


# ---------------------------------------------------------------------------
# 剥注释
#
# **必须做这一步**：我在 app.js 的注释里写了「我第一版误用成 bridge.request」
# 和「不是 /astrbot_plugin_xcpc/accounts/status」，结果检查器把**注释当成代码**，
# 报了 3 个假阳性。
#
# 教训：静态检查要在"代码"上做，不是在"文件内容"上做 ——
# 否则你越认真地记录踩过的坑，检查器就越容易误报。
# ---------------------------------------------------------------------------

def strip_js_comments(src: str) -> str:
    """去掉 // 行注释和 /* */ 块注释，**保留字符串字面量**（别把 URL 里的 // 干掉）。"""
    out = []
    i, n = 0, len(src)
    in_s = None          # 当前字符串引号
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if in_s:
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(nxt)
                    i += 2
                    continue
            elif c == in_s:
                in_s = None
            i += 1
            continue
        if c in "\"'`":
            in_s = c
            out.append(c)
            i += 1
            continue
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def strip_html_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def js_files() -> list[str]:
    out = []
    for root, _dirs, files in os.walk(PAGES):
        for f in files:
            if f.endswith(".js"):
                out.append(os.path.join(root, f))
    return sorted(out)


def html_files() -> list[str]:
    out = []
    for root, _dirs, files in os.walk(PAGES):
        for f in files:
            if f.endswith(".html"):
                out.append(os.path.join(root, f))
    return sorted(out)


# ---------------------------------------------------------------------------
# 1. JS 语法（用 node 真解析，不靠正则猜）
# ---------------------------------------------------------------------------

def test_syntax() -> None:
    print("\n[1] JS 语法")
    files = js_files()
    check("找得到 js 文件", bool(files), "pages/ 下有 %d 个" % len(files))

    node = _paths.find_node()
    if not node:
        check("node 可用（跳过语法检查）", True, "找不到 node，已跳过")
        return

    for f in files:
        rel = os.path.relpath(f, PLUGIN)
        # ⚠️ 走 `_paths.node_check`，它会处理 WSL 路径 → Windows 路径的翻译。
        # 直接在 WSL 的 /tmp 下跑这个测试时，node.exe（Windows 程序）
        # 看不懂 `/tmp/...`，会崩在 module 加载阶段 —— 表现像"JS 有语法错误"，
        # 实际是路径问题。同理见 selftest.py 的 `_translate_wsl_path`。
        ok, detail = _paths.node_check(node, f)
        check("语法：%s" % rel, ok, detail[:300])


# ---------------------------------------------------------------------------
# 2. 重复声明（我自己踩过：后定义的 function 会静默覆盖前面的）
# ---------------------------------------------------------------------------

def test_no_duplicate_decl() -> None:
    print("\n[2] 重复声明")
    for f in js_files():
        rel = os.path.relpath(f, PLUGIN)
        src = io.open(f, encoding="utf-8").read()

        # 顶层（两空格缩进）的 var / let / const / function
        names: dict[str, list[int]] = {}
        for i, line in enumerate(src.split("\n"), 1):
            m = re.match(r"^  (?:var|let|const)\s+([A-Za-z_$][\w$]*)", line)
            if m:
                names.setdefault(m.group(1), []).append(i)
                continue
            m = re.match(r"^  (?:async\s+)?function\s+([A-Za-z_$][\w$]*)", line)
            if m:
                names.setdefault(m.group(1), []).append(i)

        dups = {k: v for k, v in names.items() if len(v) > 1}
        check("%s 无重复声明" % rel, not dups,
              "重复：%s" % {k: v for k, v in dups.items()})


# ---------------------------------------------------------------------------
# 3. bridge API 用对了没
# ---------------------------------------------------------------------------

def test_bridge_usage() -> None:
    print("\n[3] bridge API")
    for f in js_files():
        rel = os.path.relpath(f, PLUGIN)
        code = strip_js_comments(io.open(f, encoding="utf-8").read())

        used = set(re.findall(r"\bbridge\.([A-Za-z_$][\w$]*)", code))
        unknown = {u for u in used if u not in KNOWN_BRIDGE_METHODS}
        check("%s 只用已知 bridge 方法" % rel, not unknown,
              "未知：%s（已知：%s）" % (sorted(unknown), sorted(KNOWN_BRIDGE_METHODS)))

        # 明确禁止我踩过的那个错 API
        check("%s 没有误用 bridge.request" % rel, "bridge.request(" not in code)

        # 必须显式检查 bridge 不存在的情况（否则白屏无提示）
        if "window.AstrBotPluginPage" in code:
            has_guard = ("apiGet" in code and
                         re.search(r"typeof\s+bridge\.apiGet", code) is not None)
            check("%s 对 bridge 缺失有兜底提示" % rel, has_guard,
                  "应检查 typeof bridge.apiGet 并给出人话错误")


# ---------------------------------------------------------------------------
# 4. HTML：SDK 加载顺序 + 资源引用
# ---------------------------------------------------------------------------

def test_html() -> None:
    print("\n[4] HTML")
    for f in html_files():
        rel = os.path.relpath(f, PLUGIN)
        raw = io.open(f, encoding="utf-8").read()
        src = strip_html_comments(raw)          # 注释里提到 app.js 不算数

        has_sdk = "/api/plugin/page/bridge-sdk.js" in src
        check("%s 加载了 bridge-sdk.js" % rel, has_sdk)

        # 只看真正的 <script src=...> 标签，且按出现顺序比
        tags = re.findall(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"']", src)
        check("%s 有 script 标签" % rel, bool(tags), "实际 %s" % tags)
        if has_sdk and "app.js" in " ".join(tags):
            try:
                i_sdk = next(i for i, t in enumerate(tags) if "bridge-sdk.js" in t)
                i_app = next(i for i, t in enumerate(tags) if t.endswith("app.js"))
                check("%s SDK 在 app.js 之前" % rel, i_sdk < i_app,
                      "顺序 %s" % tags)
            except StopIteration:
                check("%s SDK 与 app.js 都在" % rel, False, "%s" % tags)

        check("%s 有 charset=utf-8" % rel, "charset=\"utf-8\"" in src or "charset=utf-8" in src)
        check("%s 有 viewport（手机也能看）" % rel, "viewport" in src)


# ---------------------------------------------------------------------------
# 5. 路由路径：必须是相对的（不要带插件名前缀）
# ---------------------------------------------------------------------------

def test_route_paths() -> None:
    print("\n[5] 路由路径")
    for f in js_files():
        rel = os.path.relpath(f, PLUGIN)
        code = strip_js_comments(io.open(f, encoding="utf-8").read())

        # bridge 的 path 是相对插件的；带上前缀会 404
        bad = re.findall(r'["\'](/astrbot_plugin_[\w]*/[^"\']*)["\']', code)
        check("%s 路径不带插件名前缀" % rel, not bad, "发现：%s" % bad)

        # 找 BASE 的定义
        m = re.search(r'var\s+BASE\s*=\s*["\']([^"\']*)["\']', code)
        if m:
            base = m.group(1)
            check("%s BASE 是相对路径" % rel, not base.startswith("/"),
                  "实际 %r（会 404）" % base)


# ---------------------------------------------------------------------------
# 6. 页面目录结构符合官方约定
# ---------------------------------------------------------------------------

def test_layout() -> None:
    print("\n[6] 目录结构")
    if not os.path.isdir(PAGES):
        check("pages/ 存在", False)
        return
    check("pages/ 存在", True)

    entries = [d for d in os.listdir(PAGES)
               if os.path.isdir(os.path.join(PAGES, d))]
    check("至少有一个 page 目录", bool(entries), "实际 %s" % entries)

    for d in entries:
        idx = os.path.join(PAGES, d, "index.html")
        check("pages/%s/index.html 存在（官方只扫这个文件名）" % d, os.path.exists(idx))
        # 官方明确禁止的目录名
        check("pages/%s 名字合法" % d,
              d not in ("", ".", "..") and not d.startswith(".") and
              "/" not in d and "\\" not in d, "非法目录名")


# ---------------------------------------------------------------------------
# 7. 页面调的路径 vs 后端注册的路由
#
# 这一项补的是最要命的一类"一装就白屏"：
# 页面写着 callGet("status")，后端却把路由注册成了别的名字 ——
# 请求 404，页面什么都不显示，而且**控制台未必有明显报错**。
#
# 静态检查能提前抓住它，不用等装上才发现。
# ---------------------------------------------------------------------------

def _page_paths() -> dict:
    """从 app.js 里抠出页面实际会请求的**完整路径**。

    页面有三种写法，都要认：
      1. `callGet("status")`        → helper 内部拼 BASE，所以是 `accounts/status`
      2. `callPost("login", {...})` → 同上
      3. `call(BASE + "/link", ...)` → 显式拼，也是 `accounts/link`

    ⚠️ 我第一版只认字面量 `"accounts/xxx"`，而页面用的是前两种写法，
    于是抠出来全是裸名、一条都对不上，检查形同虚设。
    **检查器自己写错，比没有检查更糟** —— 它会给你一个"通过"的假象。
    """
    found: dict[str, list[str]] = {}
    for f in js_files():
        code = strip_js_comments(io.open(f, encoding="utf-8").read())
        rel = os.path.relpath(f, PLUGIN)

        # ① helper 形式：callGet / callPost，参数是**相对名**
        for m in re.finditer(r'\bcall(?:Get|Post)\(\s*"([^"]+)"', code):
            found.setdefault("accounts/" + m.group(1).lstrip("/"), []).append(rel)
        # ② 显式形式：call(BASE + "/xxx", ...)
        for m in re.finditer(r'\bcall\(\s*BASE\s*\+\s*"([^"]+)"', code):
            seg = m.group(1)
            # ⚠️ 排除 helper 定义本身 `call(BASE + "/" + path, ...)` ——
            # 那种写法捕获到的是单个 "/"，会变成一条不存在的路径 "accounts/"。
            # 误报会让人去查一个根本不存在的问题。
            if seg.strip("/"):
                found.setdefault("accounts" + seg, []).append(rel)
        # ③ 已是完整路径的字面量
        for m in re.finditer(r'\bcall\(\s*"(accounts/[^"]+)"', code):
            found.setdefault(m.group(1), []).append(rel)
    return found


def _registered_routes() -> list:
    """从 main.py 里抠出注册的账号路由。"""
    src = io.open(os.path.join(PLUGIN, "main.py"), encoding="utf-8").read()
    # 只取账号那一组（_register_account_routes 里的）
    m = re.search(r"def _register_account_routes.*?(?=\n    (?:async )?def )",
                  src, re.S)
    if not m:
        return []
    block = m.group(0)
    prefix = re.search(r'prefix = "/%s/accounts" % _ROUTE_PREFIX', block)
    if not prefix:
        # 退一步：找 route 表里的字面量
        return re.findall(r'\("(/[^"]*)"', block)
    return re.findall(r'\("(/[^"]*)"', block)


def test_page_paths_match_routes() -> None:
    print("\n[7] 页面路径 vs 后端路由")

    routes = _registered_routes()
    check("读到了后端路由表", bool(routes), repr(routes))
    if not routes:
        return

    # 路由是 "/status"、"/login" 这种（前面会拼 "/astrbot_plugin_xcpc/accounts"）
    # 页面里 BASE = "accounts"，所以调的是 "accounts/status"
    route_names = {r.lstrip("/").split("/")[0] for r in routes}
    calls = _page_paths()
    check("读到了页面调的路径", bool(calls), repr(list(calls)))

    missing = []
    for path in calls:
        # 页面路径形如 "accounts/status" / "accounts/login/2fa"
        parts = path.split("/")
        first = parts[0]
        if first != "accounts":
            continue          # 不在这组里的先不管
        # 第二段应该对应一条路由的第一段
        second = parts[1] if len(parts) > 1 else ""
        if second not in route_names:
            missing.append("%s（%s）" % (path, calls[path]))
    check("页面调的每个路径后端都注册了", not missing,
          "对不上的：%s；后端有：%s" % (missing, sorted(route_names)))

    # 反过来：注册了但页面从没调过 —— 不算错，但要能看见
    page_seconds = {p.split("/")[1] for p in calls
                    if p.startswith("accounts/") and len(p.split("/")) > 1}
    unused = sorted(route_names - page_seconds)
    check("（信息）后端 %d 条，页面用到 %d 条；没被页面用到的：%s"
          % (len(route_names), len(page_seconds), unused or "无"),
          True)

    # 保险丝：如果一条都对不上，说明**检查器自己**坏了（不是代码坏了）
    # —— 这正是我第一版的处境：抠出来全是裸名，检查却"通过"了。
    check("交叉检查本身是有效的（至少对上 3 条）",
          len(page_seconds) >= 3,
          "只对上 %d 条 —— 检查器的路径提取可能写错了" % len(page_seconds))


def main() -> int:
    print("=" * 60)
    print("pages/ 静态检查")
    print("=" * 60)
    test_syntax()
    test_no_duplicate_decl()
    test_bridge_usage()
    test_html()
    test_route_paths()
    test_layout()
    test_page_paths_match_routes()
    print("\n" + "=" * 60)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
