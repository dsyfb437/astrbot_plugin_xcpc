#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py 的静态检查 —— 抓那些"能加载但行为错"的问题。

都是我自己踩过的：
  1. **重复定义的函数/命令** —— Python 里后定义的**静默覆盖**先定义的。
     我加新版 /帮助 时写在旧版前面，结果生效的是旧版，而且不报任何错。
     （同一个坑在 JS 里也踩过：重复的 `function call()`。）
  2. **注册了命令却忘了实现** —— 或者反过来。
  3. **数据访问没带 user_id**。

跑法：python tests/test_main_static.py
"""

from __future__ import annotations

import io
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
MAIN = os.path.join(PLUGIN, "main.py")

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


def load() -> str:
    return io.open(MAIN, encoding="utf-8").read()


def strip_py_comments(src: str) -> str:
    """去注释 —— **必须在代码上做静态检查**，否则注释里提到的东西会误报
    （在 test_pages.py 里吃过这个亏：注释里写了"我误用过 bridge.request"，
     检查器把它当成真的调用了）。"""
    out = []
    in_s = None
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if in_s:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == in_s:
                in_s = None
            i += 1
            continue
        if c in "\"'":
            # 三引号
            if src[i:i + 3] in ('"""', "'''"):
                q = src[i:i + 3]
                j = src.find(q, i + 3)
                i = (j + 3) if j != -1 else n
                out.append(" ")          # docstring 不算代码
                continue
            in_s = c
            out.append(c)
            i += 1
            continue
        if c == "#":
            while i < n and src[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 1. 重复定义
# ---------------------------------------------------------------------------

def test_no_duplicate_defs() -> None:
    print("\n[1] 重复定义")
    code = strip_py_comments(load())

    # 缩进 4 空格的 def（Star 类的方法）
    names: dict[str, list[int]] = {}
    for i, line in enumerate(code.split("\n"), 1):
        m = re.match(r"^    (?:async\s+)?def (\w+)", line)
        if m:
            names.setdefault(m.group(1), []).append(i)
    dups = {k: v for k, v in names.items() if len(v) > 1}
    check("main.py 里没有重复定义的方法", not dups,
          "重复：%s" % dups)

    # 注册的命令名也不能重复
    cmds: dict[str, int] = {}
    for i, line in enumerate(code.split("\n"), 1):
        m = re.search(r'@filter\.command\(\s*"([^"]+)"', line)
        if m:
            cmds[m.group(1)] = cmds.get(m.group(1), 0) + 1
    dup_cmds = {k: v for k, v in cmds.items() if v > 1}
    check("命令名没有重复注册", not dup_cmds, "重复：%s" % dup_cmds)

    # 别名也不能和别的命令名撞
    aliases: dict[str, str] = {}
    clash = []
    for line in code.split("\n"):
        m = re.search(r'@filter\.command\(\s*"([^"]+)"\s*,\s*alias=\{([^}]*)\}', line)
        if not m:
            continue
        cmd = m.group(1)
        for a in re.findall(r'"([^"]+)"', m.group(2)):
            if a in cmds and a != cmd:
                clash.append("%s 是 %s 的别名，但也是一个命令名" % (a, cmd))
            aliases[a] = cmd
    check("别名不和命令名冲突", not clash, "；".join(clash))


# ---------------------------------------------------------------------------
# 2. 命令都有实现
# ---------------------------------------------------------------------------

def test_commands_implemented() -> None:
    print("\n[2] 命令实现")
    code = strip_py_comments(load())

    # 每个 @filter.command 后面紧跟的 def 必须存在
    lines = code.split("\n")
    missing = []
    for i, line in enumerate(lines):
        if "@filter.command(" not in line:
            continue
        # 往下找第一个 def
        for j in range(i + 1, min(i + 6, len(lines))):
            if re.match(r"^\s+(?:async\s+)?def ", lines[j]):
                break
        else:
            missing.append("第 %d 行的 @filter.command 后面没有 def" % (i + 1))
    check("每个 @filter.command 都有对应方法", not missing, "；".join(missing))

    # 我们承诺过的命令必须都在
    for want in ("同步", "绑定", "帮助", "比赛", "日志"):
        check("有 /%s 命令" % want,
              bool(re.search(r'@filter\.command\(\s*"%s"' % re.escape(want), code)))


# ---------------------------------------------------------------------------
# 3. 帮助文本里必须写清平台限制
# ---------------------------------------------------------------------------

def test_help_mentions_limits() -> None:
    print("\n[3] 帮助里写清限制")
    src = load()          # 这里要看**原文**，因为帮助文本在字符串里

    # 这三个限制不写清，用户会被自己坑
    check("帮助提到 QOJ 要登录", "QOJ" in src and "登录" in src)
    check("帮助提到洛谷要导入 Cookie", "洛谷" in src and "Cookie" in src)
    check("帮助提到 AtCoder 没有标签",
          "没有算法标签" in src or "标签是空的" in src)

    # 失败分类要让用户看懂
    for kind in ("凭据失效", "限流", "挑战未过"):
        check("帮助解释了失败分类「%s」" % kind, kind in src)

    # /帮助 要能分主题
    check("帮助支持分主题（/帮助 同步）", "帮助 同步" in src or "topic" in src)


# ---------------------------------------------------------------------------
# 4. 数据访问都带 user_id
# ---------------------------------------------------------------------------

def test_data_access_uses_uid() -> None:
    print("\n[4] 数据访问带 user_id")
    code = strip_py_comments(load())

    # 命令拿到的是 `_store_or_error()` 返回的**局部变量 store**，
    # 所以不能去找 `self.store.` —— 我第一版就是这么写的，永远匹配 0 处。
    calls = re.findall(r"(?:self\.)?store\.(\w+)\(", code)
    check("main.py 确实在调 store", bool(calls), "找到 %d 处" % len(calls))
    check("用的是 _store_or_error 统一取 store", "_store_or_error()" in code)

    # 每个 store 调用附近应该有 uid / user_id
    #
    # 例外：
    #   * **基于令牌的方法**按设计收 token 而不是 user_id ——
    #     token 的作用就是"解析出 user_id"，让它再收一个 user_id 是自相矛盾的。
    #   * **题库相关的方法**是**全局**的 —— 同一道题的难度和标签对所有人都一样，
    #     按用户分表反而是错的。所以它们收 platform 不收 user_id。
    #
    # 加白名单时必须写清理由，否则这条检查会慢慢变成摆设。
    TOKEN_METHODS = {"resolve_web_token", "revoke_web_token"}
    GLOBAL_METHODS = {"count_problems", "upsert_problems", "get_problem",
                      "problems_missing_info", "sync_problems"}

    bad = []
    lines = code.split("\n")
    for i, line in enumerate(lines):
        m = re.search(r"\bstore\.(\w+)\(", line)
        if not m:
            continue
        if "self.store =" in line:          # 赋值不是调用
            continue
        if m.group(1) in TOKEN_METHODS or m.group(1) in GLOBAL_METHODS:
            continue
        window = "\n".join(lines[max(0, i - 3):i + 5])
        if not re.search(r"\buid\b|user_id|_uid\(", window):
            bad.append("第 %d 行：%s" % (i + 1, line.strip()[:70]))
    check("每次 store 调用附近都有 user_id", not bad, "可疑：%s" % bad[:3])
    check("令牌类白名单只有那两个（没被滥用）",
          len(TOKEN_METHODS) == 2, repr(TOKEN_METHODS))
    check("全局方法白名单有理由（题库对所有人一样）",
          GLOBAL_METHODS == {"count_problems", "upsert_problems", "get_problem",
                             "problems_missing_info", "sync_problems"},
          repr(GLOBAL_METHODS))

    # 不允许直接拼 SQL（都要走 store）
    check("main.py 里没有裸 SQL",
          not re.search(r"(?i)\b(SELECT|INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b", code),
          "发现裸 SQL —— 数据访问应该都走 store")


def test_encoding_guards() -> None:
    """每个入口脚本都要有输出编码守卫。

    中文 Windows 控制台是 GBK，打印 `✓` 会抛 `UnicodeEncodeError`
    把整个脚本搞崩。**这个坑在这个项目里踩了五次**：

      1. `.bat` 里有中文 → cmd 按 GBK 解析，命令行错位
      2. `Get-Content` 默认编码读 UTF-8 → 行被合并
      3. 插件自测打印 ✓ → 整场自测崩掉
      4. `core/selfcheck.py` 的 ✓ → 控制台输出崩
      5. 新写的 `tests/live_contests.py` 又忘了

    前四次都是"具体文件出问题才去补"。第五次说明**靠记性不管用** ——
    所以做成自动检查：任何会打印东西的入口脚本都必须有守卫。
    """
    print("\n[5] 输出编码守卫（GBK 控制台）")

    tests_dir = os.path.join(PLUGIN, "tests")
    # 库文件（不直接打印）和空文件跳过
    SKIP = {"__init__.py", "_paths.py"}

    missing = []
    checked = 0
    for fn in sorted(os.listdir(tests_dir)):
        if not fn.endswith(".py") or fn in SKIP:
            continue
        path = os.path.join(tests_dir, fn)
        src = io.open(path, encoding="utf-8").read()
        # 只检查"会打印非 ASCII"的
        if "print(" not in src:
            continue
        checked += 1
        if "reconfigure(encoding" not in src:
            missing.append(fn)

    check("有 %d 个入口脚本要查" % checked, checked >= 10, "实际 %d" % checked)
    check("每个会打印的测试脚本都有编码守卫", not missing,
          "缺守卫：%s" % missing)

    # 也要看 core/ 里有没有会打印非 ASCII 的模块
    #
    # 白名单：`log.py` 有一处 print —— 那是"文件 handler 没挂上、
    # 又是 debug 级别"时的**最后兜底**，不 print 的话消息就静默丢了。
    # 它内部做了显式转码（编不出来用 ? 代替），所以不会踩 GBK 坑。
    # 加白名单必须写理由，否则这条检查会慢慢变成摆设。
    CORE_PRINT_ALLOWED = {"log.py"}
    core_dir = os.path.join(PLUGIN, "core")
    core_missing = []
    for fn in sorted(os.listdir(core_dir)):
        if not fn.endswith(".py") or fn in CORE_PRINT_ALLOWED:
            continue
        src = io.open(os.path.join(core_dir, fn), encoding="utf-8").read()
        # core 里的模块**不该**直接 print（要么返回字符串，要么走 logger）
        if re.search(r"^\s*print\(", src, re.M):
            core_missing.append(fn)
    check("core/ 里没有裸 print（输出统一走 logger 或返回字符串）",
          not core_missing, "有 print 的：%s" % core_missing)
    check("print 白名单只有 log.py（没被滥用）",
          CORE_PRINT_ALLOWED == {"log.py"}, repr(CORE_PRINT_ALLOWED))


def test_uid_is_sender_id() -> None:
    """身份只能来自 `get_sender_id()`，不能退回会话 ID。

    我原来在 `_uid()` 里写了个"退化"：拿不到 sender_id 就用
    `unified_msg_origin` 顶上。**那是错的** ——
    在群里 `unified_msg_origin` 标识的是**群**不是人
    （形如 `aiocqhttp:GroupMessage:123456`，123456 是群号）。
    用它当身份意味着同一个群里所有人共用一个身份，
    绑的账号全部串在一起 —— 这是"会绑错人"那类错误里最严重的一种。

    这个 bug 是命令接线测试抓出来的（sender 为空时 /绑定 照样生成码）。
    这里做成静态检查，防止再犯。
    """
    print("\n[6] 身份只能来自 get_sender_id")
    code = strip_py_comments(load())

    # 找 _uid 的函数体
    m = re.search(r"def _uid\(self[^)]*\)[^:]*:(.*?)(?=\n    (?:async )?def )",
                  code, re.S)
    check("找得到 _uid", m is not None)
    if m:
        body = m.group(1)
        check("_uid 里没有 unified_msg_origin",
              "unified_msg_origin" not in body,
              "又拿会话 ID 当身份了 —— 群聊里那是群号，不是 QQ 号")
        check("_uid 用了 get_sender_id", "get_sender_id" in body)
        check("拿不到时返回空（不猜别的）",
              re.search(r"return\s+sid\b", body) is not None
              or re.search(r"return\s+\"\"", body) is not None,
              "应该直接返回 sender_id（可能为空），不要有 fallback")

    # 全局：不允许把 unified_msg_origin 当身份键传给 store
    bad = []
    for i, line in enumerate(code.split("\n"), 1):
        if "store." in line and "unified_msg_origin" in line:
            bad.append("第 %d 行：%s" % (i, line.strip()[:70]))
    check("没有把会话 ID 传给 store 当 user_id", not bad, "; ".join(bad))


def test_no_unreachable_methods() -> None:
    """core/ 和 platforms/ 里的公开方法都必须有**生产代码**的调用点。

    连续两轮撞到同一类缺口：
      * `sync_problems` —— 实现了、有测试，但从没被调用，
        导致整个「题库标注 → 难度回避 → 候选池」的链路不可达
      * `sync_interval_min` —— 写在配置里、有 hint，但从没被读

    共同特征：**代码是好的、单元测试是绿的、文档还承诺了行为**，
    而用户永远用不到。这种缺口只有把「定义 → 调用点」这条线也测了才能拦住。

    ⚠️ **用 `ast` 解析，不用正则。** 第一版用正则在源码上扫，误报一大堆：
      * **属性**（`@property`）—— 调用时不带括号
      * **函数内嵌套的函数** —— 缩进也是 4 空格，正则分不出它是不是方法
      * **框架回调**（`redirect_request` 由 urllib 调）
    误报多了这条检查就会被人忽略，**等于没有**。
    """
    print("\n[7] 没有「实现了但接不出来」的方法")
    import ast

    sources = {}
    for sub in ("core", "platforms", ""):
        d = os.path.join(PLUGIN, sub) if sub else PLUGIN
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".py"):
                p = os.path.join(d, fn)
                # ⚠️ `os.path.relpath` 在 Windows 上给的是**反斜杠**，
                # 拿它 `startswith("core/")` 匹配不上。统一成正斜杠。
                rel = os.path.relpath(p, PLUGIN).replace("\\", "/")
                sources[rel] = io.open(p, encoding="utf-8").read()

    targets = {k: v for k, v in sources.items()
               if k.startswith(("core/", "platforms/"))}
    check("扫到了源码", len(targets) >= 10,
          "%d 个文件：%s" % (len(targets), sorted(sources)[:6]))
    prod = {k: v for k, v in sources.items() if not k.startswith("tests/")}
    check("生产代码也被扫到了", len(prod) >= 10, "%d 个" % len(prod))

    # 框架回调 —— 由标准库/AstrBot 调用，源码里搜不到调用点
    FRAMEWORK_HOOKS = {"redirect_request", "http_error_302", "log_message"}

    # 已知的「实现了但暂时没人用」—— **每一项都要写清是哪种情况**。
    #
    # 这个名单是 ast 检查器第一次跑出来的结果（8 个）。分三类：
    #   A. 留给将来接的 API 面（逻辑正确、有测试，只是还没有调用点）
    #   B. 被别的实现取代了（真死代码）
    #   C. 只在测试里用的辅助
    #
    # ⚠️ 名单**只能缩小不能扩大** —— 加新项时必须先问：
    #    这是「还没接」，还是「我以为接了」？
    #    后者才是前两轮踩到的坑（题库拉取、自动同步间隔）。
    KNOWN_UNWIRED = {
        # A. 备用 API 面
        "accepted_problems": "store 上的备用查询（汇总目前自己算 AC 集合）",
        "get_problem": "store 上按题号取单题（目前都是批量读题库）",
        "problems_missing_info": "为「按缺口补题库」预留的查询",
        "plan_history": "为「看历史方案」预留的查询",
        "transaction": "db 的事务封装（目前写的都是单条语句）",
        "get_cookies": "AccountService 上取凭据；同步层目前直接读 store",
        # B. 被取代
        "login": "洛谷的自动登录入口；实际走 ManualCookie authenticator",
        # C. 测试辅助
        "worst": "selfcheck.Report 的属性，目前只有测试在用",
    }

    def collect(src: str):
        """只取**类的一级成员**（ast 天然区分嵌套函数与属性）。"""
        out = []
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return out
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name.startswith("_"):
                    continue
                is_prop = any(
                    (isinstance(d, ast.Name) and d.id == "property")
                    for d in item.decorator_list)
                out.append((is_prop, item.name))
        return out

    defs = {}
    for rel, src in targets.items():
        for is_prop, name in collect(src):
            defs.setdefault(name, []).append((rel, is_prop))

    check("解析出足够多的公开方法", len(defs) >= 40,
          "只解析出 %d 个" % len(defs))

    dead = []
    for name, places in sorted(defs.items()):
        if name in FRAMEWORK_HOOKS or name in KNOWN_UNWIRED:
            continue
        hits = 0
        for rel2, src2 in prod.items():
            for line in src2.split("\n"):
                # 调用（带括号）
                if re.search(r"[.\s]%s\s*\(" % re.escape(name), line):
                    if not re.match(r"^\s*(?:async )?def %s\(" % re.escape(name),
                                    line):
                        hits += 1
                # 属性访问 / 被当值用（不带括号）
                elif re.search(r"[.\s]%s\b" % re.escape(name), line):
                    if not re.match(r"^\s*(?:async )?def %s\b" % re.escape(name),
                                    line):
                        hits += 1
        if hits == 0:
            rel, is_prop = places[0]
            dead.append("%s → %s%s" % (rel, name, "（属性）" if is_prop else "()"))

    check("每个公开方法都有生产代码的调用点", not dead,
          "接不出来的：%s" % (dead if len(dead) < 12 else dead[:12]))


def test_api_contract() -> None:
    """插件用到的 AstrBot API，必须和官方签名对得上。

    这些签名不是猜的 —— 是**读官方 wheel（4.28.2）核对过的**：

        class Star(CommandParserMixin, PluginKVStoreMixin):
            def __init__(self, context: Context, config: dict | None = None)

        async def llm_generate(...)                 # core/star/context.py:171
        async def get_current_chat_provider_id(self, umo: str) -> str   # :329
        def register_web_api(self, route: str, view_handler,
                             methods: list[str], desc: str)             # :705

        def register_command(command_name=None, sub_command=None,
                             alias: set | None = None, **kwargs)
        # filter.command 就是 register_command 的别名

        class AstrMessageEvent:
            def get_sender_id(self) -> str      # 返回 sender.user_id
            def is_admin(self) -> bool

    这里把「我依赖的形状」钉住：alias 是 **set 不是 list**、
    __init__ 要收 config、register_web_api 的参数名等等。
    哪天插件改岔了，这个测试会先炸，而不是等装到 AstrBot 上才发现。
    """
    print("\n[8] AstrBot API 契约（对着官方 wheel 核对过的）")
    code = io.open(os.path.join(PLUGIN, "main.py"), encoding="utf-8").read()

    # ① __init__ 必须能收 (context, config)
    m = re.search(r"def __init__\(self, ([^)]*)\)", code)
    check("找得到 __init__", m is not None)
    if m:
        params = m.group(1)
        check("__init__ 收 context", "context" in params, params)
        check("__init__ 收 config（官方签名有它）", "config" in params, params)

    # ② 必须继承 Star
    check("继承了 Star", re.search(r"class \w+\(Star\)", code) is not None,
          "官方是 class Star(...)")

    # ③ filter.command 的 alias 必须是 **set** 字面量
    bad_alias = []
    for m2 in re.finditer(r"@filter\.command\(([^)]*)\)", code):
        args = m2.group(1)
        am = re.search(r"alias\s*=\s*(\S+)", args)
        if am and not am.group(1).startswith("{"):
            bad_alias.append(args[:60])
    check("alias 用的是 set 字面量（官方签名 alias: set）",
          not bad_alias, "这几处不是 set：%s" % bad_alias[:3])

    # ④ register_web_api 的参数
    # ⚠️ 校验**位置和数量**，不校验变量名 ——
    # 第一版要求参数里出现字面量 "route"，但插件传的是
    # `prefix + path`（拼出来的表达式），于是假失败。
    # **检查器比代码更容易写错**，断言要尽量贴合真实形状。
    m3 = re.search(r"register_web_api\(([^)]*)\)", code, re.S)
    check("用了 register_web_api", m3 is not None)
    if m3:
        raw = m3.group(1).replace("\n", " ")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        check("register_web_api 传了 4 个参数（route/handler/methods/desc）",
              len(parts) == 4, "实际 %d 个：%s" % (len(parts), parts))
        check("第 1 个是路由表达式",
              bool(parts) and any(k in parts[0] for k in
                                  ("route", "prefix", "path")),
              parts[0] if parts else "")
        # 第 3 个参数传变量名（`methods`）或字面量列表都合法 ——
        # 我上一版要求必须是 `[...]` 字面量，又一次假失败。
        # **检查器比代码更容易写错**：这已经是同一个测试里第二次了。
        check("第 3 个是方法列表（字面量或变量都行）",
              len(parts) > 2 and bool(parts[2]),
              parts[2] if len(parts) > 2 else "")

    # ⑤ 不该用我没核对过的 event 成员
    UNVERIFIED = ("get_group_id", "get_platform_name", "message_obj_raw",
                  "set_extra", "get_extra")
    used = [u for u in UNVERIFIED if ("event.%s" % u) in code]
    check("没有用未核对过的 event 成员", not used, "用了：%s" % used)


def main() -> int:
    print("=" * 62)
    print("main.py 静态检查")
    print("=" * 62)
    test_no_duplicate_defs()
    test_commands_implemented()
    test_help_mentions_limits()
    test_data_access_uses_uid()
    test_encoding_guards()
    test_uid_is_sender_id()
    test_no_unreachable_methods()
    test_api_contract()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
