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
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------

def check_writable_dir(path: str, name: str, report: Report,
                       fix_hint: str = "") -> bool:
    """检查目录可写。**真去写一个文件** —— 只看权限位不可靠
    （容器里 root 挂载只读卷时，权限位看着是好的）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".xcpc_write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.unlink(probe)
        report.add(name, OK, path)
        return True
    except OSError as exc:
        report.add(name, BAD, "%s（%s）" % (path, exc),
                   fix_hint or "检查这个目录的权限，或在配置里把 data_root 改到可写的位置")
        return False


async def run(*, store=None, db=None, recorder=None, context=None,
              config=None, umo: str = "", user_id: str = "",
              routes_probe=None, syncer=None) -> Report:
    """跑一轮自检。

    参数都可有可无 —— 缺哪个就把那项标成"没传进来"，
    **而不是崩掉**。自检本身崩了最尴尬。
    """
    config = config or {}
    r = Report()

    # ---- 1. 数据目录 --------------------------------------------------
    data_root = str(config.get("data_root") or "").strip()
    if data_root:
        check_writable_dir(os.path.abspath(os.path.expanduser(data_root)),
                           "数据目录可写", r)
    else:
        # 没配就是用插件自带目录，那里应该永远可写
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        check_writable_dir(os.path.join(here, "data"), "数据目录可写（默认位置）", r)

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
            if bound:
                r.add("账号绑定", OK,
                      "、".join("%s=%s" % (p, handles[p]) for p in bound))
            else:
                r.add("账号绑定", WARN, "一个平台都没绑",
                      "先 /xcpc 绑定 拿绑定码，在网页上关联，然后填 handle")
            subs = await store.count_submissions(user_id)
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

    # ---- 8. AstrBot 版本门槛 ------------------------------------------
    r.add("版本要求", OK, "需要 AstrBot >= 4.26（网页绑定页）/ >= 4.5.7（调模型）",
          "")

    return r
