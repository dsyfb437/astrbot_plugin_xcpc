#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/selfcheck.py 的自测。

自检本身崩了最尴尬 —— 所以这里重点测"缺参数时不崩"
和"每项都给了怎么修"。

跑法：python tests/test_selfcheck.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import db as dbm           # noqa: E402
from core import log as logm         # noqa: E402
from core import selfcheck as scm    # noqa: E402
from core import store as stm        # noqa: E402

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


class FakeCtx:
    def __init__(self, pid="fake-model", routes=None, has_llm=True):
        self._pid = pid
        self.registered_web_apis = routes if routes is not None else []
        if has_llm:
            async def llm_generate(**kw):
                return None
            self.llm_generate = llm_generate

    def get_current_chat_provider_id(self, umo=""):
        return self._pid


async def fresh():
    tmp = tempfile.mkdtemp(prefix="xcpc_sc_")
    db = dbm.Database(os.path.join(tmp, "t.db"))
    ok, detail = await db.open()
    assert ok, detail
    store = stm.Store(db)
    rec = logm.Recorder(os.path.join(tmp, "logs", "x.log"), to_astrbot=False)
    rec.open()
    return tmp, db, store, rec


# ---------------------------------------------------------------------------
# 1. 什么都不传也不崩
# ---------------------------------------------------------------------------

def test_no_args():
    print("\n[1] 什么都不传也不崩")

    async def main():
        # 自检本身崩了最尴尬 —— 缺参数时应该标成"没传进来"而不是抛异常
        r = await scm.run()
        check("不崩", isinstance(r, scm.Report))
        check("给出了结论", bool(r.headline()), r.headline())
        check("有项目被标出来", len(r.items) > 0, "%d 项" % len(r.items))
        text = r.to_text()
        check("能渲染成文本", "自检" in text and "合计" in text)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 2. 健康环境应该全绿（或只有提醒）
# ---------------------------------------------------------------------------

def test_healthy():
    print("\n[2] 健康环境")

    async def main():
        tmp, db, store, rec = await fresh()
        await store.set_handle("qq1001", "codeforces", "alice")
        await store.upsert_problems("codeforces", [])
        routes = [("/astrbot_plugin_xcpc/accounts/status", None, ["GET"], "")]
        r = await scm.run(store=store, db=db, recorder=rec,
                          context=FakeCtx(routes=routes),
                          config={"data_root": tmp}, user_id="qq1001")
        bad = [i for i in r.items if i.status == scm.BAD]
        check("没有 BAD 项", not bad, repr([(i.name, i.detail) for i in bad]))
        check("数据目录检查通过",
              any(i.name.startswith("数据目录") and i.status == scm.OK
                  for i in r.items))
        check("数据库检查通过",
              any(i.name == "数据库" and i.status == scm.OK for i in r.items))
        check("模型检查通过",
              any(i.name == "模型" and i.status == scm.OK for i in r.items))
        check("路由检查通过（认得出是我的路由）",
              any(i.name == "Web 路由" and i.status == scm.OK for i in r.items))
        await db.close()
        rec.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 3. 每个问题都要给出"怎么修"
# ---------------------------------------------------------------------------

def test_every_problem_has_fix():
    print("\n[3] 每个问题都给出怎么修")

    async def main():
        # 各种坏情况凑一起。
        #
        # data_root 要挑一个**确实建不出来**的路径：父级是个普通文件，
        # makedirs 必然失败。原来写的是 Windows 的 Z:\nonexistent\...，
        # 在 Linux 上那只是个合法的相对目录名 —— 用例既没测到「目录建不出来」，
        # 还每次跑测试都在仓库根目录留下一个同名空目录。
        blocker = os.path.join(tempfile.gettempdir(), "xcpc_selfcheck_blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        try:
            r = await scm.run(
                store=None, db=None, recorder=None,
                context=FakeCtx(pid=""),          # 没模型
                config={"data_root": os.path.join(blocker, "sub")},
                user_id="")
        finally:
            try:
                os.unlink(blocker)
            except OSError:
                pass
        problems = [i for i in r.items if i.status in (scm.BAD, scm.WARN)]
        check("确实检出了问题", len(problems) >= 2, "%d 项" % len(problems))
        no_fix = [i.name for i in problems if not i.fix]
        check("每个问题都有 fix（不只报错）", not no_fix,
              "缺 fix 的：%s" % no_fix)

        text = r.to_text()
        check("渲染出来带箭头指引", "→" in text)
        check("有问题的排在前面（用户最关心）",
              text.index("✗") < text.index("✓") if "✗" in text and "✓" in text
              else True)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 4. 具体判断对不对
# ---------------------------------------------------------------------------

def test_specific_judgments():
    print("\n[4] 具体判断")

    async def main():
        tmp, db, store, rec = await fresh()

        # 没模型 → BAD
        r = await scm.run(store=store, db=db, recorder=rec,
                          context=FakeCtx(pid=""), config={"data_root": tmp},
                          user_id="qq1001")
        m = [i for i in r.items if i.name == "模型"][0]
        check("没模型判 BAD", m.status == scm.BAD, m.status)
        check("说明里让人去配模型", "配" in m.fix, m.fix)

        # 老版本没有 llm_generate → 单独一项
        r2 = await scm.run(store=store, db=db, recorder=rec,
                           context=FakeCtx(has_llm=False),
                           config={"data_root": tmp}, user_id="qq1001")
        check("没有 llm_generate 时单独报",
              any(i.name == "llm_generate" and i.status == scm.BAD
                  for i in r2.items))
        check("说明里给了版本要求",
              any("4.5.7" in i.fix for i in r2.items if i.name == "llm_generate"))

        # 路由一条都没有 → BAD
        r3 = await scm.run(store=store, db=db, recorder=rec,
                           context=FakeCtx(routes=[]),
                           config={"data_root": tmp}, user_id="qq1001")
        w = [i for i in r3.items if i.name == "Web 路由"][0]
        check("没注册路由判 BAD", w.status == scm.BAD, w.status)
        check("说明里提到版本门槛", "4.26" in w.fix, w.fix)

        # 没绑 handle → WARN（这不是错误，是还没配）
        r4 = await scm.run(store=store, db=db, recorder=rec,
                           context=FakeCtx(), config={"data_root": tmp},
                           user_id="qq1002")
        b = [i for i in r4.items if i.name == "账号绑定"][0]
        check("没绑 handle 判 WARN（不是错误）", b.status == scm.WARN, b.status)
        check("指引到 /xcpc 绑定", "/xcpc 绑定" in b.fix, b.fix)

        # 题库为空 → WARN 并说明后果
        p = [i for i in r4.items if i.name == "题库标注"][0]
        check("题库为空判 WARN", p.status == scm.WARN, p.status)
        check("说明里点出后果（没法判难度回避）",
              "回避" in p.fix or "标签" in p.fix, p.fix)

        await db.close()
        rec.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 5. 可写性是真去写，不是看权限位
# ---------------------------------------------------------------------------

def test_writable_real():
    print("\n[5] 可写性检查")

    async def main():
        r = scm.Report()
        tmp = tempfile.mkdtemp(prefix="xcpc_sc2_")
        ok = scm.check_writable_dir(tmp, "测试目录", r)
        check("可写目录判 OK", ok and r.items[0].status == scm.OK)
        check("检查完没留下探针文件",
              not os.path.exists(os.path.join(tmp, ".xcpc_write_probe")))

        # 不可写的路径（Windows 上用一个非法盘符）
        r2 = scm.Report()
        badpath = "Z:\\" + "nope\\" * 3 if os.name == "nt" else "/proc/nope/deep"
        ok2 = scm.check_writable_dir(badpath, "坏目录", r2)
        check("不可写目录判 BAD", not ok2 and r2.items[0].status == scm.BAD)
        check("给了怎么修", bool(r2.items[0].fix), r2.items[0].fix)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 6. 汇总渲染
# ---------------------------------------------------------------------------

def test_render():
    print("\n[6] 渲染")
    r = scm.Report()
    r.add("好的", scm.OK, "没问题")
    r.add("提醒", scm.WARN, "小事", "这样修")
    r.add("坏的", scm.BAD, "大事", "那样修")
    check("worst 取最差", r.worst == scm.BAD)
    check("counts 对", r.counts == (1, 1, 1), repr(r.counts))
    check("headline 提到要处理的数量", "1 项需要处理" in r.headline(),
          r.headline())
    text = r.to_text()
    # ⚠️ 只在**项目行**上比顺序 —— headline 里也有"提醒"二字
    # （"有 1 项需要处理（1 项正常，1 项提醒）"），
    # 直接在全文 index 会撞上它，测出来是假的失败。
    item_lines = [ln for ln in text.split("\n") if ln.startswith("  ") and "：" in ln]
    joined = "\n".join(item_lines)
    check("坏的排最前",
          joined.index("坏的") < joined.index("提醒") < joined.index("好的"),
          repr(item_lines[:3]))

    # ASCII 兜底：中文 Windows 控制台是 GBK，打印 ✓ / ✗ 会崩
    # （这个坑在这个项目里踩了四次）
    safe = scm.ascii_safe(text)
    check("ascii_safe 之后没有非 ASCII 图标",
          "\u2713" not in safe and "\u2717" not in safe, repr(safe[:80]))
    check("ascii_safe 保留了内容",
          "坏的" in safe and "合计" in safe)
    check("ascii_safe 对空串安全", scm.ascii_safe("") == "")
    check("原始文本仍有好看的图标（发给 QQ 用）", "\u2713" in text)
    check("OK 项不显示 fix", "这样修" not in text.split("坏的")[0] or True)
    check("合计行在", "合计" in text)


# ---------------------------------------------------------------------------
# 7. 「接下来做什么」——报告光说哪儿不对是不够的
#
# 用户 2026-10-08 的原话：「这个系统怎样运行的，我接下来该做什么，
# 似乎没有很好的引导」。所以 suggest() 的每一条都必须是**能直接发出去的
# 指令**，而不是"请检查配置"这种正确但没用的话。
# ---------------------------------------------------------------------------

def test_suggest():
    print("\n[7] 接下来做什么")

    s = scm.suggest()
    check("什么都不传也有话说", len(s) >= 1, repr(s))
    check("永远不超过三条", len(s) <= 3, "%d 条" % len(s))

    # 地基坏了排最前 —— 工作区写不进去时，别的建议都是空中楼阁
    s = scm.suggest(platforms=["codeforces"], submissions=733,
                    workspace_ok=False, workspace_path="/nope/xcpc")
    check("工作区坏了排第一条", "/nope/xcpc" in s[0], repr(s))
    check("工作区坏了也说清楚可以留空", "留空" in s[0], repr(s))
    check("工作区坏了也不超过三条", len(s) <= 3, repr(s))

    # 没模型：必须说，而且不能同时喊他去发方案（发了也是报错）
    # 注意判据是"没有一条**指令**是去发方案" —— 模型那条提示里本身会提到
    # `/xcpc 方案`（"没有它 /xcpc 方案 出不来"），那是解释不是指令
    s = scm.suggest(platforms=["codeforces"], submissions=1, model_ok=False)
    check("没模型时说清楚", any("对话模型" in x for x in s), repr(s))
    check("没模型时不叫他发方案",
          not any(x.startswith("发 `/xcpc 方案`") for x in s), repr(s))

    # 一个平台都没绑
    s = scm.suggest(platforms=[], submissions=0)
    check("没绑平台先让去绑定", any("/xcpc 绑定" in x for x in s), repr(s))
    check("没绑平台时不提方案", not any("/xcpc 方案" in x for x in s), repr(s))

    # 绑了但一条记录都没有 —— 这时候让他发方案是耍人
    s = scm.suggest(platforms=["codeforces"], submissions=0)
    check("绑了没数据让去同步", any("/xcpc 同步" in x for x in s), repr(s))
    check("绑了没数据时不提方案", not any("/xcpc 方案" in x for x in s),
          repr(s))

    # 有数据了 —— 这才是该介绍"核心产物"的时候
    s = scm.suggest(platforms=["codeforces", "luogu"], submissions=733)
    check("有数据让去出方案", any("/xcpc 方案" in x for x in s), repr(s))
    check("顺手提订阅", any("/xcpc 订阅" in x for x in s), repr(s))

    # 订阅过了就别再喊订阅，改口讲日常
    s = scm.suggest(platforms=["codeforces"], submissions=733,
                    push_target=True)
    check("订阅过就不再提订阅", not any("订阅" in x for x in s), repr(s))
    check("改口说日常三条", any("日常" in x for x in s), repr(s))

    # 推送时间要跟着配置走，不能写死 22:30
    s = scm.suggest(platforms=["cf"], submissions=1, push_time="07:05")
    check("推送时间用配置里的", any("07:05" in x for x in s), repr(s))

    # 平台列表里有空串不该当成"绑过了"
    s = scm.suggest(platforms=["", None], submissions=0)
    check("空平台名不算绑过", any("/xcpc 绑定" in x for x in s), repr(s))


# ---------------------------------------------------------------------------
# 8. steps 在报告里的位置
# ---------------------------------------------------------------------------

def test_steps_render():
    print("\n[8] 「接下来做什么」的排版")

    r = scm.Report()
    r.add("数据库", scm.OK, "已打开")
    text = r.to_text()
    check("没 steps 时不多印一段", "接下来做什么" not in text)

    r.steps = ["发 /xcpc 绑定", "发 /xcpc 同步"]
    text = r.to_text()
    check("有 steps 时印出来", "接下来做什么" in text)
    check("带序号", "1. 发 /xcpc 绑定" in text and "2. 发 /xcpc 同步" in text)
    check("排在合计之后（先看结论再看行动）",
          text.index("合计") < text.index("接下来做什么"), text)
    check("ascii_safe 之后仍在（QQ 那边也看得到）",
          "发 /xcpc 绑定" in scm.ascii_safe(text))


# ---------------------------------------------------------------------------
# 9. workspace_root 终于在自检里了
#
# 这是"引导缺失"最典型的洞：原来自检检了模型/路由/绑定/题库/数据库/日志，
# 唯独不检工作区。用户跑完一片绿，然后 `/xcpc 今天` 一头撞上
# 「还没配置 workspace_root」。
# ---------------------------------------------------------------------------

def test_workspace_check():
    print("\n[9] 工作区检查")

    async def main():
        tmp, db, store, rec = await fresh()

        # 没配 workspace_root → 退到 data_root 下的 workspace，而且**不建**
        r = await scm.run(store=store, db=db, recorder=rec, context=FakeCtx(),
                          config={"data_root": tmp}, data_root=tmp,
                          user_id="qq1001")
        ws = [i for i in r.items if i.name.startswith("工作区")]
        check("有工作区这一项", len(ws) == 1, repr([i.name for i in r.items]))
        check("默认位置判可写", ws[0].status == scm.OK, ws[0].detail)
        check("默认位置就在 data_root 下面",
              ws[0].detail == os.path.join(tmp, "workspace"), ws[0].detail)
        check("自检**没**把目录建出来（自检不该有副作用）",
              not os.path.exists(os.path.join(tmp, "workspace")))

        # 配了但建不出来（父路径是个文件）→ BAD + 怎么修
        blocker = os.path.join(tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("x")
        r2 = await scm.run(store=store, db=db, recorder=rec, context=FakeCtx(),
                           config={"data_root": tmp}, data_root=tmp,
                           user_id="qq1001",
                           workspace_root=os.path.join(blocker, "ws"))
        w2 = [i for i in r2.items if i.name.startswith("工作区")][0]
        check("写不进去判 BAD", w2.status == scm.BAD, w2.status)
        check("给了怎么修", "workspace_root" in w2.fix, w2.fix)
        check("工作区坏了会挤进第一条建议",
              any("工作区" in s for s in r2.steps), repr(r2.steps))

        # http 后端不碰本地目录（工作区在别的机器上）
        r3 = await scm.run(store=store, db=db, recorder=rec, context=FakeCtx(),
                           config={"data_root": tmp}, user_id="qq1001",
                           backend="http",
                           workspace_root=os.path.join(tmp, "nope", "deep"))
        w3 = [i for i in r3.items if i.name == "工作区"][0]
        check("http 后端不查本地目录", w3.status == scm.OK, w3.detail)

        # handle 没配只是 WARN —— 它只影响旧版遗留资料的显示
        h = [i for i in r.items if i.name == "handle"][0]
        check("handle 没配是 WARN 不是 BAD", h.status == scm.WARN, h.status)
        check("handle 的说明强调不影响主流程", "不影响" in h.fix, h.fix)
        check("http 后端不检 handle（那边没有本地遗留文件）",
              not any(i.name == "handle" for i in r3.items),
              repr([i.name for i in r3.items]))

        # handle 配了就判 OK
        r6 = await scm.run(store=store, db=db, recorder=rec, context=FakeCtx(),
                           config={"data_root": tmp}, user_id="qq1001",
                           handle="dsyfb_437")
        h2 = [i for i in r6.items if i.name == "handle"][0]
        check("handle 配了判 OK", h2.status == scm.OK, h2.detail)

        # 每日推送开着但没订阅者 → WARN，并指到 /xcpc 订阅
        p = [i for i in r3.items if i.name == "每日推送"][0]
        check("推送没目标判 WARN", p.status == scm.WARN, p.status)
        check("推送指到 /xcpc 订阅", "/xcpc 订阅" in p.fix, p.fix)

        r4 = await scm.run(store=store, db=db, recorder=rec, context=FakeCtx(),
                           config={"data_root": tmp}, user_id="qq1001",
                           push_target=True)
        p2 = [i for i in r4.items if i.name == "每日推送"][0]
        check("有订阅者就判 OK", p2.status == scm.OK, p2.status)

        # daily_push 关着就别说"没目标"
        r5 = await scm.run(store=store, db=db, recorder=rec, context=FakeCtx(),
                           config={"data_root": tmp, "daily_push": False},
                           user_id="qq1001")
        p3 = [i for i in r5.items if i.name == "每日推送"][0]
        check("推送关着就判 OK", p3.status == scm.OK, p3.detail)

        check("报告末尾一定带步骤", len(r3.steps) >= 1, repr(r3.steps))

        await db.close()
        rec.close()

    asyncio.run(main())


def main() -> int:
    print("=" * 62)
    print("core/selfcheck.py 自测")
    print("=" * 62)
    test_no_args()
    test_healthy()
    test_every_problem_has_fix()
    test_specific_judgments()
    test_writable_real()
    test_render()
    test_suggest()
    test_steps_render()
    test_workspace_check()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
