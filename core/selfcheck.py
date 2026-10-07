#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：一次把"装好了没、哪儿不对"说清楚。

为什么值得单独做
----------------
用户装完插件第一件事是"试试能不能用"。如果没有自检，他要逐个试：
`/xcpc 同步` 失败是网络问题还是没绑定？`/xcpc 方案` 失败是没模型还是没数据？
页面打不开是版本不对还是路由没注册？—— 每个都要猜。

自检把这些**一次性检查完并给出可操作的下一步**。

设计原则
--------
1. **每项都给出"怎么修"，不只是"哪里错"** —— 只报错等于把问题丢回给用户
2. **区分"没配"和"配错了"** —— 前者是正常状态，后者才要处理
3. **不联网**（除了明确标注的那一项）—— 自检本身不该因为网络慢而卡住
4. 纯逻辑，**可离线测**
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# 检查结果的三档：好 / 提醒 / 有问题
OK = "ok"
WARN = "warn"
BAD = "bad"

_ICON = {OK: "✓", WARN: "!", BAD: "✗"}
# 纯 ASCII 的兜底图标。
#
# **为什么要有兜底**：中文 Windows 的控制台是 GBK 码页，
# 打印 ✓ / ✗ 会抛 `UnicodeEncodeError` 把整个脚本搞崩 ——
# 这个坑我在这个项目里踩了**四次**（.bat 里的中文、Get-Content 读 UTF-8、
# 插件自测打印 ✓、以及这里）。
#
# 发给 QQ 的消息用好看的图标（那边是 UTF-8）；
# 但任何**可能被打印到控制台**的地方都应该能切成 ASCII。
_ICON_ASCII = {OK: "[OK]", WARN: "[!]", BAD: "[X]"}


def ascii_safe(text: str) -> str:
    """把图标换成纯 ASCII。控制台输出前调它。"""
    out = text
    for k in _ICON:
        out = out.replace(_ICON[k] + " ", _ICON_ASCII[k] + " ")
    return out


@dataclass
class Item:
    name: str
    status: str
    detail: str = ""
    fix: str = ""          # **怎么修** —— 只报错等于把问题丢回用户

    def line(self) -> str:
        s = "  %s %s" % (_ICON.get(self.status, "?"), self.name)
        if self.detail:
            s += "：%s" % self.detail
        if self.fix and self.status != OK:
            s += "\n      → %s" % self.fix
        return s


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    #: 「接下来做什么」—— 1~3 条能照着做的指令，见 :func:`suggest`。
    #:
    #: 为什么和 items 分开：items 回答"哪儿不对"，steps 回答"那我现在干嘛"。
    #: 只给前者的话，报告全绿时用户反而最迷茫（2026-10-08 用户原话：
    #: 「我接下来该做什么，似乎没有很好的引导」）。
    steps: list[str] = field(default_factory=list)

    def add(self, name, status, detail="", fix=""):
        self.items.append(Item(name, status, detail, fix))
        return self

    @property
    def worst(self) -> str:
        if any(i.status == BAD for i in self.items):
            return BAD
        if any(i.status == WARN for i in self.items):
            return WARN
        return OK

    @property
    def counts(self) -> tuple:
        return (sum(1 for i in self.items if i.status == OK),
                sum(1 for i in self.items if i.status == WARN),
                sum(1 for i in self.items if i.status == BAD))

    def headline(self) -> str:
        ok, warn, bad = self.counts
        if bad:
            return "有 %d 项需要处理（%d 项正常，%d 项提醒）" % (bad, ok, warn)
        if warn:
            return "基本能用（%d 项正常，%d 项提醒）" % (ok, warn)
        return "全部正常（%d 项）" % ok

    def to_text(self, title: str = "自检") -> str:
        lines = ["【%s】%s" % (title, self.headline()), ""]
        # 有问题的排前面 —— 用户最关心的是"哪里不对"
        order = {BAD: 0, WARN: 1, OK: 2}
        for it in sorted(self.items, key=lambda i: order.get(i.status, 3)):
            lines.append(it.line())
        ok, warn, bad = self.counts
        lines.append("")
        lines.append("合计：%d 正常 ｜ %d 提醒 ｜ %d 需要处理" % (ok, warn, bad))
        if self.steps:
            lines.append("")
            lines.append("接下来做什么")
            for n, s in enumerate(self.steps, 1):
                lines.append("  %d. %s" % (n, s))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------

def check_writable_dir(path: str, name: str, report: Report,
                       fix_hint: str = "", create: bool = True) -> bool:
    """检查目录可写。**真去写一个文件** —— 只看权限位不可靠
    （容器里 root 挂载只读卷时，权限位看着是好的）。

    ``create=False`` 时**不建目录**，退到最近的已存在祖先上试写 ——
    用来回答"这个目录将来建不建得出来"。自检走的是这条路：它只是**看**，
    不该因为跑一次 `/xcpc 自检` 就在用户机器上留下一个空目录。
    """
    try:
        target = path
        if create:
            os.makedirs(path, exist_ok=True)
        else:
            while target and not os.path.isdir(target):
                parent = os.path.dirname(target)
                if parent == target:
                    break
                target = parent
            if not os.path.isdir(target):
                raise OSError("从 %s 往上都找不到一个已存在的目录" % path)
        probe = os.path.join(target, ".xcpc_write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.unlink(probe)
        report.add(name, OK, path)
        return True
    except OSError as exc:
        report.add(name, BAD, "%s（%s）" % (path, exc),
                   fix_hint or "检查这个目录的权限，或在配置里把 data_root 改到可写的位置")
        return False


def suggest(*, platforms=(), submissions: int = 0, model_ok: bool = True,
            workspace_ok: bool = True, workspace_path: str = "",
            push_target: bool = False, push_time: str = "22:30") -> list[str]:
    """按当前状态给 1~3 条**能照着做的**下一步。

    为什么要单独做这一段
    --------------------
    自检原来只回答"哪儿不对"。看完全绿的报告，用户还是不知道下一步干嘛 ——
    2026-10-08 用户的原话：

        ok了，这部分似乎跑通了，然后这个系统怎样运行的，
        我接下来该做什么，似乎没有很好的引导

    所以这里的原则是：**每一条都得是一条能直接发出去的指令**，或者一句
    能照做的操作。不写"请检查配置"这种正确但没用的话。

    参数都是"已经检出来的事实"，这个函数**纯函数、不碰磁盘、不联网** ——
    方便直接测。
    """
    steps: list[str] = []

    if not workspace_ok:
        steps.append(
            "工作区写不进去（%s）。在 WebUI 的插件配置里把 workspace_root "
            "换成可写的位置，**或者干脆留空** —— 留空时插件会在自己的数据"
            "目录下建一个。" % (workspace_path or "默认位置"))
    if not model_ok:
        steps.append("在 AstrBot 里配一个对话模型 —— 没有它 `/xcpc 方案` "
                     "出不来（其余命令不受影响）。")

    bound = [p for p in platforms if p]
    if not bound:
        steps.append("发 `/xcpc 绑定` 拿绑定码，在网页上至少绑一个平台"
                     "（Codeforces / AtCoder / 洛谷 / QOJ）。")
    elif not submissions:
        steps.append("发 `/xcpc 同步` 把做题记录拉下来（第一次要一两分钟）。")
    else:
        if model_ok:
            steps.append("发 `/xcpc 方案` —— 插件的核心产物，按你的做题记录"
                         "排下一步练什么。觉得不对就 `/xcpc 总结` 看它到底"
                         "看到了什么。")
        if not push_target:
            steps.append("发 `/xcpc 订阅`，之后每天 %s 自动推一条今日安排。"
                         % push_time)
        elif model_ok:
            steps.append("日常就三条：看 `/xcpc 今天`、练完发 `/xcpc 方案`、"
                         "收工 `/xcpc 打卡` 或 `/xcpc 复盘`。")
    return steps[:3]


async def run(*, store=None, db=None, recorder=None, context=None,
              config=None, umo: str = "", user_id: str = "",
              routes_probe=None, syncer=None, data_root: str = "",
              workspace_root: str = "", backend: str = "file",
              handle: str = "", push_target: bool = False) -> Report:
    """跑一轮自检。

    参数都可有可无 —— 缺哪个就把那项标成"没传进来"，
    **而不是崩掉**。自检本身崩了最尴尬。

    报告末尾的「接下来做什么」（:attr:`Report.steps`）是**重点**，不是附赠：
    只回答"哪儿不对"是不够的 —— 用户看完还是不知道下一步干嘛，全绿的时候
    尤其迷茫。见 :func:`suggest`。
    """
    config = config or {}
    r = Report()
    # 边检边攒的事实，最后喂给 suggest()。默认值 = "没传进来就不冤枉它"。
    facts = {"platforms": [], "submissions": 0, "model_ok": True,
             "workspace_ok": True, "workspace_path": "",
             "push_target": bool(push_target)}

    # ---- 1. 数据目录 --------------------------------------------------
    # data_root 由调用方算好传进来（它知道 AstrBot 的数据目录在哪）。
    # 没传就退回配置 / 插件自带目录 —— 自检是最后一道诊断，不能因为
    # 少一个参数就整段不跑。
    resolved = str(data_root or config.get("data_root") or "").strip()
    if resolved:
        check_writable_dir(os.path.abspath(os.path.expanduser(resolved)),
                           "数据目录可写", r)
    else:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        check_writable_dir(os.path.join(here, "data"), "数据目录可写（默认位置）", r)

    # ---- 1b. 工作区（决定 复盘 / 随手记 / 今天 / 题单 能不能用）----------
    #
    # ⚠️ 这一项是**后补的**，而它的缺席正是"引导缺失"最典型的样子：
    # 原来自检检了模型、路由、绑定、题库、数据库、日志，唯独没检工作区。
    # 用户跑完一片绿，然后 `/xcpc 今天` 一头撞上「还没配置 workspace_root」。
    mode = str(backend or "file").lower()
    if mode == "http":
        r.add("工作区", OK, "用 http 后端（%s）"
              % (config.get("http_base") or "http://127.0.0.1:8787"))
        facts["workspace_path"] = str(config.get("http_base") or "")
    else:
        root = str(workspace_root or "").strip()
        is_default = not root
        if is_default:
            base = str(data_root or config.get("data_root") or "").strip()
            base = (os.path.abspath(os.path.expanduser(base)) if base
                    else os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data"))
            root = os.path.join(base, "workspace")
        root = os.path.abspath(os.path.expanduser(root))
        facts["workspace_path"] = root
        # create=False：自检只**看**，不建。目录会在插件拿到 backend 时
        # 由 WorkspaceFS.ensure() 建好；自检不该有副作用。
        facts["workspace_ok"] = check_writable_dir(
            root, "工作区可写（默认位置）" if is_default else "工作区可写", r,
            fix_hint="在 WebUI 的插件配置里把 workspace_root 换成可写的位置，"
                     "或者**留空** —— 留空时插件会在自己的数据目录下建一个",
            create=not is_default)

    # ---- 2. 数据库 ----------------------------------------------------
    if db is None:
        r.add("数据库", BAD, "没打开",
              "看 /xcpc 日志 30；多半是 data_root 不可写或库文件损坏")
    elif db._conn is None:
        r.add("数据库", BAD, "连接是空的",
              "看 /xcpc 日志 30 里 db.open 那条，那里有具体原因")
    else:
        try:
            row = await db.query_one("SELECT COUNT(*) AS n FROM users")
            n = row["n"] if row else 0
            r.add("数据库", OK, "已打开，%d 个用户" % n)
        except Exception as exc:
            r.add("数据库", BAD, "查询失败：%s" % exc, "库可能损坏了")

    # ---- 3. 日志 ------------------------------------------------------
    if recorder is None:
        r.add("日志", WARN, "没初始化", "日志写不出来时插件仍能跑，但出问题没法查")
    elif recorder._fh is None:
        r.add("日志", BAD, "文件 handler 没挂上",
              "看启动日志里「日志打不开」那条；多半是目录权限")
    else:
        r.add("日志", OK, recorder.log_path)

    # ---- 4. 模型（这一项决定 /xcpc 方案 能不能用）---------------------------
    if context is None:
        r.add("模型", WARN, "没传 context（脱离 AstrBot 跑的）")
    else:
        pid = ""
        try:
            pid = str(config.get("llm_provider_id") or "").strip()
            if not pid:
                getter = getattr(context, "get_current_chat_provider_id", None)
                if getter:
                    pid = str(getter(umo) or "").strip()
        except Exception as exc:
            r.add("模型", BAD, "取模型 id 时出错：%s" % exc)
        if pid:
            r.add("模型", OK, pid)
        else:
            r.add("模型", BAD, "没找到可用的对话模型",
                  "在 AstrBot 里配一个对话模型；或在插件配置里填 llm_provider_id")
            facts["model_ok"] = False
        if not hasattr(context, "llm_generate"):
            r.add("llm_generate", BAD, "这个 AstrBot 版本没有它",
                  "需要 AstrBot >= 4.5.7；低于这个版本 /xcpc 方案 用不了")

    # ---- 5. Web 路由（决定绑定页能不能用）-----------------------------
    if routes_probe is None and context is not None:
        try:
            routes = getattr(context, "registered_web_apis", None)
            if routes is None:
                routes_probe = None
            else:
                mine = [x for x in routes
                        if str(x[0]).startswith("/astrbot_plugin_xcpc/")]
                routes_probe = mine
        except Exception:
            routes_probe = None
    if routes_probe is None:
        r.add("Web 路由", WARN, "查不到",
              "老版本 AstrBot 可能没有 registered_web_apis；"
              "不影响聊天命令，只影响网页绑定页")
    elif len(routes_probe) == 0:
        r.add("Web 路由", BAD, "一条都没注册上",
              "确认 AstrBot >= 4.26（插件 Pages 能力需要它），然后重载插件")
    else:
        r.add("Web 路由", OK, "注册了 %d 条" % len(routes_probe))

    # ---- 6. 用户与绑定 ------------------------------------------------
    if store is not None and user_id:
        try:
            handles = await store.handles(user_id)
            bound = [p for p, h in handles.items() if h]
            facts["platforms"] = bound
            if bound:
                r.add("账号绑定", OK,
                      "、".join("%s=%s" % (p, handles[p]) for p in bound))
            else:
                r.add("账号绑定", WARN, "一个平台都没绑",
                      "先 /xcpc 绑定 拿绑定码，在网页上关联，然后填 handle")
            subs = await store.count_submissions(user_id)
            facts["submissions"] = subs
            if subs:
                r.add("已同步数据", OK, "%d 条提交" % subs)
            else:
                r.add("已同步数据", WARN, "还没有数据", "绑好 handle 后发 /xcpc 同步")
            states = await store.all_sync_states(user_id)
            errs = [p for p, st in states.items() if st.get("error_kind")]
            if errs:
                r.add("同步健康", WARN,
                      "这些平台上次同步失败：%s" % "、".join(errs),
                      "看 /xcpc 日志 30 里的失败分类；凭据失效就去重新绑定")
        except Exception as exc:
            r.add("账号绑定", BAD, "读取失败：%s" % exc)
    elif store is not None:
        r.add("账号绑定", WARN, "没传 user_id（脱离聊天环境跑的）")

    # ---- 7. 题库标注（决定"难度回避"能不能算）-------------------------
    if store is not None:
        try:
            n = await store.count_problems()
            if n == 0:
                r.add("题库标注", WARN, "还没拉",
                      "没有题库就没法按标签分析、也没法判难度回避；"
                      "配好 CF handle 后重跑同步会自动拉")
            else:
                cf = await store.count_problems("codeforces")
                r.add("题库标注", OK, "%d 题（CF %d）" % (n, cf))
        except Exception as exc:
            r.add("题库标注", BAD, "读取失败：%s" % exc)

    # ---- 8. handle（旧 file 后端找遗留资料用的，缺了不影响主流程）--------
    if mode == "http":
        pass                              # http 后端不读本地文件，无所谓
    elif str(handle or "").strip():
        r.add("handle", OK, str(handle).strip())
    else:
        r.add("handle", WARN, "没配",
              "这是**旧版遗留**：file 后端拿它去找 data/<handle>_info.json "
              "那份资料（CF 分数、rating 那些），没有也只是 /xcpc 状态 少显示"
              "一点。**不影响同步、方案、打卡。**"
              "要补就在插件配置的 handle 里填 Codeforces 用户名。")

    # ---- 9. 每日推送的目标 ---------------------------------------------
    if not config.get("daily_push", True):
        r.add("每日推送", OK, "关着（daily_push=false）")
    elif facts["push_target"]:
        r.add("每日推送", OK, "开着，每天 %s"
              % str(config.get("push_time") or "22:30"))
    else:
        r.add("每日推送", WARN, "开着，但没有任何收件人",
              "发 /xcpc 订阅（在哪个会话发就推到哪），"
              "或在插件配置里填 push_umo")

    # ---- 10. AstrBot 版本门槛 ------------------------------------------
    r.add("版本要求", OK, "需要 AstrBot >= 4.26（网页绑定页）/ >= 4.5.7（调模型）",
          "")

    # 报告的落点：用户看完"哪儿不对"，还得知道"那我现在干嘛"。
    r.steps = suggest(
        platforms=facts["platforms"],
        submissions=facts["submissions"],
        model_ok=facts["model_ok"],
        workspace_ok=facts["workspace_ok"],
        workspace_path=facts["workspace_path"],
        push_target=facts["push_target"],
        push_time=str(config.get("push_time") or "22:30"))
    return r
