#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/llm.py 的自测 —— 用**假 provider**，不调真模型。

重点：
  1. **编造的题号必须被拦下**（最常见的失效模式，而且编出来的题号看着很合理）
  2. 输出不是 JSON 时明确报错，**不从自由文本里猜结构**
  3. 任务类型是开放的（不能把"学算法"硬塞成"做题"）
  4. **失败要抛错，不能返回一个凑合的方案**
  5. 认不出响应结构时报错要有用（能让人定位到版本差异）

跑法：python tests/test_llm.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import llm as llmm  # noqa: E402

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


CANDIDATES = [
    {"problem_key": "CF:1900D", "title": "D - X", "tags": ["dp"],
     "difficulty": 2100, "difficulty_source": "cf_rating"},
    {"problem_key": "ATC:abc380_d", "title": "D - Y", "tags": [],
     "difficulty": 812, "difficulty_source": "atcoder_irt"},
]


class FakeResp:
    def __init__(self, text):
        self.completion_text = text


class FakeContext:
    """假 context。可以精确控制返回什么 / 抛什么。"""

    def __init__(self, text=None, exc=None, provider="fake-provider",
                 raw_obj=None):
        self._text = text
        self._exc = exc
        self._provider = provider
        self._raw_obj = raw_obj
        self.calls = []

    # ⚠️ 官方就是 async（astrbot/core/star/context.py:329）。替身写成同步的话，
    # 我们少 await 一次也照样全绿 —— 2026-10-08 真机报
    # `Provider <coroutine object ...> not found` 就是这么漏掉的。
    async def get_current_chat_provider_id(self, umo=""):
        return self._provider

    async def llm_generate(self, **kw):
        self.calls.append(kw)
        if self._exc:
            raise self._exc
        if self._raw_obj is not None:
            return self._raw_obj
        return FakeResp(self._text)


def run(ctx, **kw):
    kw.setdefault("summary_text", "# 汇总\n随便")
    kw.setdefault("candidates", CANDIDATES)
    return asyncio.run(llmm.generate(ctx, **kw))


# ---------------------------------------------------------------------------
# 1. 正常路径
# ---------------------------------------------------------------------------

def test_happy():
    print("\n[1] 正常路径")
    payload = {
        "assessment": "你 dp 做过 9 题，通过率比整体低 11 个点。",
        "tasks": [
            {"kind": "practice", "title": "做一道 dp", "problem": "CF:1900D",
             "minutes": 45, "why": "补短板"},
            {"kind": "learn", "title": "看单调队列优化", "problem": "",
             "minutes": 30, "why": "dp 的常用优化"},
        ],
        "watch": "别一上来就写代码",
    }
    ctx = FakeContext(text=json.dumps(payload, ensure_ascii=False))
    plan = run(ctx, date="2026-10-07")
    check("解析出 2 个任务", len(plan.tasks) == 2, "实际 %d" % len(plan.tasks))
    check("assessment 保留", "dp" in plan.assessment)
    check("题号保留", plan.tasks[0].problem == "CF:1900D")
    check("learn 类型没被改成 practice", plan.tasks[1].kind == "learn",
          plan.tasks[1].kind)
    check("没有可疑标记", not plan.problems, repr(plan.problems))
    check("模型 id 记下来了", plan.model_id == "fake-provider")
    check("传了 system_prompt", "system_prompt" in ctx.calls[0])
    check("传了 provider id", ctx.calls[0].get("chat_provider_id") == "fake-provider")

    text = plan.to_text()
    check("文本里有人话标签", "学新算法" in text and "做题" in text, text[:200])

    # ---- to_markdown：图片版排版（v0.5.16）----
    #
    # 为什么单独立一组断言：to_text() 里 why 靠 6 个空格缩进挂在上一行下面，
    # markdown 会把那行当成**同一段的续行**，渲染时几条任务并成一大坨。
    # 这里钉死 markdown 版必须用"- "列表项 + 空行 + 缩进正文"的写法。
    md = plan.to_markdown()
    check("markdown 版是列表项", md.count("- **[") == 2,
          "有 %d 个列表项" % md.count("- **["))
    check("markdown 版有人话标签", "**[做题]**" in md and "**[学新算法]**" in md,
          md[:200])
    check("markdown 版有小标题", "## 今天的任务" in md and "## 下次注意" in md,
          md[:200])
    check("why 前面有空行（不然会被当成续行并成一段）",
          "\n\n  " in md, repr(md[:400]))
    check("没有 to_text() 那种 6 空格续行",
          "\n      " not in md, repr(md[:400]))
    check("题号和分钟都在", "CF:1900D" in md and "（45 分钟）" in md, md[:300])
    check("纯文本版一个字没变（还是老写法）",
          "\n      " in text and "- **[" not in text, text[:200])
    check("两个版本正文一致（都含 assessment 和 watch）",
          plan.assessment in md and plan.watch in md and plan.watch in text)
    check("markdown 版结尾没有多余空行", md == md.rstrip(), repr(md[-30:]))

    # ---- Task.short()：推送用的一行紧凑版（v0.5.23）----
    #
    # 每日推送里的任务是"一眼扫完"的，不能把 why 也塞进去 —— 那是方案正文
    # 的事。line() 必须逐字节不变（老 file 后端和复盘都还在用它）。
    t0, t1 = plan.tasks[0], plan.tasks[1]
    s0 = t0.short()
    check("short() 带人话标签、标题、题号、时长",
          s0 == "[做题] 做一道 dp → CF:1900D （45 分钟）", repr(s0))
    check("short() 里没有 why", "补短板" not in s0, repr(s0))
    check("short() 是**一行**（没有换行）", "\n" not in s0, repr(s0))
    check("line() = 两个空格 + short() + 换行 + 六空格 + why（逐字节不变）",
          t0.line() == "  " + s0 + "\n      补短板", repr(t0.line()))
    check("没有题号的任务不出现空箭头",
          "→" not in t1.short() and t1.short() == "[学新算法] 看单调队列优化 （30 分钟）",
          repr(t1.short()))
    bare = llmm.Task(kind="practice", title="随便做做")
    check("题号 / 时长缺省时不留多余空格或括号",
          bare.short() == "[做题] 随便做做", repr(bare.short()))
    check("没有 why 时 line() 就只有缩进那一行",
          bare.line() == "  [做题] 随便做做", repr(bare.line()))


# ---------------------------------------------------------------------------
# 2. 编造题号
# ---------------------------------------------------------------------------

def test_fabricated_problem():
    print("\n[2] 编造的题号必须被拦下")
    payload = {
        "assessment": "x",
        "tasks": [
            # 看起来完全合理的题号，但不在候选池里 —— 这正是最危险的情况
            {"kind": "practice", "title": "做这道", "problem": "CF:1234D",
             "minutes": 40, "why": "y"},
            {"kind": "practice", "title": "这道是真的", "problem": "CF:1900D",
             "minutes": 40, "why": "y"},
        ],
    }
    ctx = FakeContext(text=json.dumps(payload, ensure_ascii=False))
    plan = run(ctx)
    check("编的题号被剔除", plan.tasks[0].problem == "",
          repr(plan.tasks[0].problem))
    check("真题号保留", plan.tasks[1].problem == "CF:1900D")
    check("明确告诉用户拦了什么",
          any("CF:1234D" in p for p in plan.problems), repr(plan.problems))
    check("说明里点出「可能是编的」",
          any("编" in p for p in plan.problems), repr(plan.problems))
    check("任务本身没被丢掉（只是题号剔了）", len(plan.tasks) == 2)
    check("用户能看到这个问题", "⚠" in plan.to_text())

    # 大小写/空格差异应该被容忍，并且**回填成候选池里的规范 key**
    payload2 = {"assessment": "x", "tasks": [
        {"kind": "practice", "title": "t", "problem": "cf:1900d", "minutes": 10}]}
    plan2 = run(FakeContext(text=json.dumps(payload2)))
    check("大小写差异被容忍（不算编造）", plan2.tasks[0].problem == "CF:1900D",
          repr(plan2.tasks[0].problem))
    check("补全成规范 key，不是原样存模型写的那串",
          plan2.tasks[0].problem in {"CF:1900D"}, repr(plan2.tasks[0].problem))
    check("容忍时不该报「编造」", not any("编" in p for p in plan2.problems),
          repr(plan2.problems))

    # ★ v0.5.21：模型**把整行候选抄进来**（题号 + 标题）—— 它没编，是我们认不出
    #   真机现场：三个任务全被剔，而那三个题号一个不差地都在候选池里
    for raw, want in [
        ("CF:1900D  Some Title", "CF:1900D"),
        ("CF:1900D Some Title", "CF:1900D"),
        ("  CF:1900D  ", "CF:1900D"),
        ("cf:1900d  some title", "CF:1900D"),
    ]:
        p3 = run(FakeContext(text=json.dumps({"assessment": "x", "tasks": [
            {"kind": "practice", "title": "t", "problem": raw, "minutes": 10}]})))
        check("抄了标题也认得出：%r" % raw, p3.tasks[0].problem == want,
              repr(p3.tasks[0].problem))
        check("这时**不能**说人家编题号：%r" % raw,
              not any("编" in x for x in p3.problems), repr(p3.problems))

    # 但真的不在池子里的，还是要拦（别把容忍做成放行）
    p4 = run(FakeContext(text=json.dumps({"assessment": "x", "tasks": [
        {"kind": "practice", "title": "t",
         "problem": "CF:9999Z  Totally Made Up", "minutes": 10}]})))
    check("★ 容忍抄标题 ≠ 放行编造：池子里没有的还是剔除",
          p4.tasks[0].problem == "" and any("编" in x for x in p4.problems),
          repr((p4.tasks[0].problem, p4.problems)))


# ---------------------------------------------------------------------------
# 3. 输出不是 JSON
# ---------------------------------------------------------------------------

def test_not_json():
    print("\n[3] 输出不是 JSON")
    ctx = FakeContext(text="我觉得你今天应该先做一道 dp 题，然后再复习一下图论。")
    try:
        run(ctx)
        check("非 JSON 应该抛错", False, "居然没抛")
    except llmm.LLMError as exc:
        check("非 JSON 抛 LLMError", True)
        check("分类是解析失败", exc.kind == "解析失败", exc.kind)
        check("错误里带了原文（便于定位）", "dp" in str(exc), str(exc)[:120])

    # 空输出：错误信息必须指向"模型吐了空"，而不是"认不出结构"
    try:
        run(FakeContext(text=""))
        check("空输出抛错", False)
    except llmm.LLMError as exc:
        check("空输出抛错", "空内容" in str(exc), str(exc)[:100])
        check("空输出不会误报成「版本差异」",
              "版本差异" not in str(exc), str(exc)[:120])


# ---------------------------------------------------------------------------
# 4. 代码块包裹
# ---------------------------------------------------------------------------

def test_code_fence():
    print("\n[4] ```json 包裹")
    payload = {"assessment": "a", "tasks": [
        {"kind": "review", "title": "复习", "problem": "", "minutes": 20, "why": "w"}]}
    text = "好的，这是方案：\n```json\n%s\n```\n希望有用。" % json.dumps(
        payload, ensure_ascii=False)
    plan = run(FakeContext(text=text))
    check("能从代码块里抠出 JSON", len(plan.tasks) == 1)
    check("类型解析对", plan.tasks[0].kind == "review")

    # 前后有废话但没有代码块
    text2 = "方案如下：%s 就这样。" % json.dumps(payload, ensure_ascii=False)
    plan2 = run(FakeContext(text=text2))
    check("能从废话里抠出花括号", len(plan2.tasks) == 1)


# ---------------------------------------------------------------------------
# 5. 失败不降级
# ---------------------------------------------------------------------------

def test_no_degradation():
    print("\n[5] 失败不降级")
    ctx = FakeContext(exc=RuntimeError("模型服务 500"))
    try:
        run(ctx)
        check("调用失败应该抛错（不能返回凑合的方案）", False, "居然返回了")
    except llmm.LLMError as exc:
        check("调用失败抛 LLMError", True)
        check("错误里带原因", "500" in str(exc), str(exc)[:120])

    # 没有模型
    ctx2 = FakeContext(provider="")
    try:
        run(ctx2)
        check("没有模型时抛错", False)
    except llmm.LLMError as exc:
        check("没有模型时抛错", "模型" in str(exc), str(exc)[:120])
        check("分类是凭据失效（让用户去配模型）", exc.kind == "凭据失效", exc.kind)


# ---------------------------------------------------------------------------
# 6. 认不出响应结构
# ---------------------------------------------------------------------------

def test_unknown_response():
    print("\n[6] 认不出的响应结构")
    class Weird:
        pass
    ctx = FakeContext(raw_obj=Weird())
    try:
        run(ctx)
        check("认不出结构时抛错", False)
    except llmm.LLMError as exc:
        check("认不出结构时抛错", True)
        check("错误信息里有类型和字段（方便定位版本差异）",
              "Weird" in str(exc), str(exc)[:200])

    # 常见字段名都要能认出来
    class R1:
        text = "{}"
    check("能认 text 字段",
          llmm._response_text(R1()) == "{}")
    class R2:
        content = '{"a":1}'
    check("能认 content 字段", llmm._response_text(R2()) == '{"a":1}')
    check("字符串直接可用", llmm._response_text("hello") == "hello")
    class R3:
        data = {"completion_text": "nested"}
    check("能认嵌套的 data.completion_text",
          llmm._response_text(R3()) == "nested")


# ---------------------------------------------------------------------------
# 7. 脏数据的处理
# ---------------------------------------------------------------------------

def test_dirty_input():
    print("\n[7] 脏数据")
    payload = {
        "assessment": "x",
        "tasks": [
            {"kind": "瞎写的类型", "title": "t", "minutes": 10},   # 类型不认识
            {"kind": "practice", "title": "t2", "minutes": "abc"},  # 时长不是数字
            {"kind": "practice", "title": "t3", "minutes": 99999},  # 时长离谱
            "这不是对象",                                            # 根本不是 dict
        ],
    }
    plan = run(FakeContext(text=json.dumps(payload, ensure_ascii=False)))
    check("不认识的类型被归一成 practice", plan.tasks[0].kind == "practice",
          plan.tasks[0].kind)
    check("归一这件事被记下来了（不是静默）",
          any("不认识" in p for p in plan.problems), repr(plan.problems))
    check("非数字时长按 0", plan.tasks[1].minutes == 0, repr(plan.tasks[1].minutes))
    check("离谱时长被记下来", any("不合理" in p for p in plan.problems))
    check("非对象任务被跳过并记下",
          any("不是对象" in p for p in plan.problems), repr(plan.problems))
    check("有效任务保留 3 个", len(plan.tasks) == 3, "%d" % len(plan.tasks))

    # 没有 tasks 字段
    plan2 = run(FakeContext(text=json.dumps({"assessment": "只有一句话"})))
    check("没给 tasks 时记下来", any("tasks" in p for p in plan2.problems),
          repr(plan2.problems))
    check("不崩", plan2.assessment == "只有一句话")

    # 总时长超限（**要多个任务加起来超**，单个任务 500 分钟会先撞上单任务上限）
    payload3 = {"assessment": "x", "tasks": [
        {"kind": "practice", "title": "t1", "minutes": 90},
        {"kind": "practice", "title": "t2", "minutes": 90},
        {"kind": "practice", "title": "t3", "minutes": 90}]}
    plan3 = run(FakeContext(text=json.dumps(payload3)))
    check("总时长超限会提醒（而不是静默接受）",
          any("超过上限" in p for p in plan3.problems), repr(plan3.problems))
    check("超限时任务仍然保留（由用户决定砍哪个）", len(plan3.tasks) == 3)


# ---------------------------------------------------------------------------
# 8. 提示词组装
# ---------------------------------------------------------------------------

def test_prompt():
    print("\n[8] 提示词")
    p = llmm.build_prompt("# 汇总内容", "今天有点累", "这周别安排 VP")
    check("含汇总", "# 汇总内容" in p)
    check("含用户的话", "今天有点累" in p)
    check("含额外要求", "别安排 VP" in p)
    check("额外要求放在最后（近因效应）",
          p.rindex("别安排 VP") > p.rindex("# 汇总内容"))
    check("要求只输出 JSON", "只输出 JSON" in p)

    check("系统提示里写了不能编题号",
          "绝对不要自己编题号" in llmm.SYSTEM_PROMPT)
    check("系统提示里写了难度不跨平台比",
          "不要跨平台比较" in llmm.SYSTEM_PROMPT)
    check("系统提示承认 learn/syntax/read 不是做题",
          "不要**硬塞成" in llmm.SYSTEM_PROMPT or "硬塞成" in llmm.SYSTEM_PROMPT)
    check("系统提示里写了「拿不到」不等于「零」",
          "不要当成" in llmm.SYSTEM_PROMPT)


# ---------------------------------------------------------------------------
# 9. provider id —— 官方那个方法是 async，别同步调
# ---------------------------------------------------------------------------

def test_provider_id():
    """2026-10-08 真机踩的坑：`get_current_chat_provider_id` 是**协程**。

    同步调它拿回来的是 coroutine 对象。coroutine 是**真值**，`or ""` 兜不住，
    于是它被当成 provider id 传给了 `llm_generate`，用户看到的是

        ProviderNotFoundError: Provider <coroutine object
        Context.get_current_chat_provider_id at 0x7f7d4008a9b0> not found

    为什么 958 项测试全绿也没拦住：**替身照着我们的调用方式写的，不是照着
    官方签名写的**，替身和真身一起错。现在替身一律 async，这里再把
    调用点本身钉死 —— async 的必须 await 出真值，同步的（万一哪天改回去）
    也得能用，而且**绝不能**把 coroutine 当 id 传出去。
    """
    print("\n[9] provider id —— 官方那个方法是 async")

    async def pid_of(ctx):
        return await llmm.current_provider_id(ctx, "")

    class AsyncGetter:
        async def get_current_chat_provider_id(self, umo=""):
            return "  async-model  "

    class SyncGetter:
        # 老版本 / 将来改回同步 —— 两种都得认
        def get_current_chat_provider_id(self, umo=""):
            return "sync-model"

    class NoneGetter:
        async def get_current_chat_provider_id(self, umo=""):
            return None

    class NoGetter:
        pass

    class BoomGetter:
        async def get_current_chat_provider_id(self, umo=""):
            raise RuntimeError("炸了")

    check("async getter 被 await 出真值",
          asyncio.run(pid_of(AsyncGetter())) == "async-model",
          repr(asyncio.run(pid_of(AsyncGetter()))))
    check("同步 getter 也认（inspect.isawaitable 兜底）",
          asyncio.run(pid_of(SyncGetter())) == "sync-model")
    check("返回 None → 空串，不是字符串 'None'",
          asyncio.run(pid_of(NoneGetter())) == "")
    check("没有这个方法 → 空串（老 AstrBot）",
          asyncio.run(pid_of(NoGetter())) == "")

    try:
        asyncio.run(pid_of(BoomGetter()))
        check("getter 抛错时不吞（交给 generate 兜）", False, "居然没抛")
    except RuntimeError:
        check("getter 抛错时不吞（交给 generate 兜）", True)

    # ★ 端到端：传出去的必须是普通字符串，不能是 coroutine
    ok_payload = json.dumps({"assessment": "x", "tasks": [], "watch": ""})
    ctx = FakeContext(text=ok_payload)
    run(ctx)
    got = ctx.calls[0].get("chat_provider_id")
    check("过了一遍 generate 之后仍是字符串", isinstance(got, str),
          repr(type(got)))
    check("过了一遍 generate 之后不是 coroutine",
          not asyncio.iscoroutine(got), repr(got))
    check("就是 provider 名字", got == "fake-provider", repr(got))

    # getter 炸了 → 应该是那句人话的「没模型」，不是把整个方案带崩
    class BoomCtx(FakeContext):
        async def get_current_chat_provider_id(self, umo=""):
            raise RuntimeError("context 炸了")

    try:
        run(BoomCtx(text=ok_payload))
        check("getter 炸了 → 报「没模型」而不是崩", False, "居然没抛 LLMError")
    except llmm.LLMError as exc:
        check("getter 炸了 → 报「没模型」而不是崩",
              "llm_provider_id" in str(exc), str(exc))

    # 静态守卫：生产代码里除 core/llm.py 自己，谁都不准直接碰这个方法。
    # （测试文件里那一堆是**替身定义**，不在此列。）
    root = os.path.dirname(HERE)
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git", "data", "tests",
                                    "pages")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel == "core/llm.py":
                continue
            with open(path, encoding="utf-8") as fh:
                if "get_current_chat_provider_id(" in fh.read():
                    hits.append(rel)
    check("生产代码里除 core/llm.py 外没人直接调它", not hits, ", ".join(hits))

    # 我们自己那份声明也别写错 —— 文档里写错了下一个人就照着错
    with open(os.path.join(root, "core", "llm.py"), encoding="utf-8") as fh:
        src = fh.read()
    check("llm.py 自己的文档里标了它是 async", "async！" in src or "协程" in src)
    check("llm.py 里确实 await 了它", "await current_provider_id(" in src
          or "provider_id = await current_provider_id" in src)


# ---------------------------------------------------------------------------
# 10. 时长：标题里别写、VP 得给够（用户问「vp 真是 1.5h 吗」）
# ---------------------------------------------------------------------------

def test_vp_minutes():
    """2026-10-08 真机反馈。

    推送里那条任务写的是「轻量虚拟赛：挑一场最近的 Div.2/3 模拟 **1.5 小时**」，
    而模型自己填的 `minutes` 是 **60** —— 用户一眼看出对不上，来问
    「vp 真是 1.5h 吗」。这里其实是两个问题叠在一起：

      1. 标题里的时长和 `minutes` 打架（他看到的和真排进日程的不是一回事）
      2. **60 分钟根本不是一场 VP**（CF Div.2 是 135 分钟、ABC 是 100 分钟）
    """
    print("\n[10] 标题里的时长 / VP 时长")

    check("「1.5 小时」→ 90 分钟",
          llmm._title_minutes("模拟 1.5 小时") == [90],
          repr(llmm._title_minutes("模拟 1.5 小时")))
    check("「90 分钟」→ 90",
          llmm._title_minutes("限时 90 分钟") == [90],
          repr(llmm._title_minutes("限时 90 分钟")))
    check("「2h」→ 120", llmm._title_minutes("vp 2h") == [120],
          repr(llmm._title_minutes("vp 2h")))
    check("没写时长 → 空", llmm._title_minutes("做一道 dp") == [],
          repr(llmm._title_minutes("做一道 dp")))
    check("题号不会被误当成时长（1326D1）",
          llmm._title_minutes("CF:1326D1 前缀后缀回文") == [],
          repr(llmm._title_minutes("CF:1326D1 前缀后缀回文")))
    check("Div.2 里的 2 也不算时长",
          llmm._title_minutes("CF Div.2 虚拟赛") == [],
          repr(llmm._title_minutes("CF Div.2 虚拟赛")))

    # ---- 用户实际拿到的那一条 ----
    payload = {"assessment": "x", "tasks": [
        {"kind": "vp", "title": "轻量虚拟赛：模拟 1.5 小时", "problem": "",
         "minutes": 60, "why": "找手感"}]}
    plan = run(FakeContext(text=json.dumps(payload, ensure_ascii=False)))
    check("★ 标题写 1.5 小时、minutes 填 60 → 被抓出来",
          any("1.5 小时" in p for p in plan.problems), repr(plan.problems))
    check("说明里点出「以 minutes 为准」",
          any("以 minutes 为准" in p for p in plan.problems), repr(plan.problems))
    check("★ minutes 原样保留（不偷偷改成 90 —— 那是他自己的预算）",
          plan.tasks[0].minutes == 60, repr(plan.tasks[0].minutes))
    check("用户能在输出里看到这个问题", "⚠" in plan.to_text())

    # ---- 60 分钟打不了 VP ----
    payload2 = {"assessment": "x", "tasks": [
        {"kind": "vp", "title": "打一场 VP", "problem": "",
         "minutes": 60, "why": "找手感"}]}
    plan2 = run(FakeContext(text=json.dumps(payload2, ensure_ascii=False)))
    check("★ 60 分钟的 VP 被点名（给出真实赛制长度）",
          any("135" in p and "100" in p for p in plan2.problems),
          repr(plan2.problems))
    check("提示里给了出路（想短练就叫限时练）",
          any("限时练" in p for p in plan2.problems), repr(plan2.problems))

    # ---- 正常情况不误报 ----
    payload3 = {"assessment": "x", "tasks": [
        {"kind": "vp", "title": "CF Div.2 虚拟赛", "problem": "",
         "minutes": 135, "why": "找手感"}]}
    plan3 = run(FakeContext(text=json.dumps(payload3, ensure_ascii=False)))
    check("★ 135 分钟、标题不带时长的 VP → 一条问题都不报",
          plan3.problems == [], repr(plan3.problems))

    payload4 = {"assessment": "x", "tasks": [
        {"kind": "learn", "title": "过一遍 hash（25 分钟）", "problem": "",
         "minutes": 25, "why": "打基础"}]}
    plan4 = run(FakeContext(text=json.dumps(payload4, ensure_ascii=False)))
    check("标题写的 25 分钟和 minutes 一致 → 不报",
          plan4.problems == [], repr(plan4.problems))

    # ---- 提示词里加的两条规则 ----
    check("系统提示禁掉「标题里写时长」",
          "一个字都不要提" in llmm.SYSTEM_PROMPT, "")
    check("系统提示写明真实赛制长度",
          "135" in llmm.SYSTEM_PROMPT and "100" in llmm.SYSTEM_PROMPT, "")
    check("系统提示写明短于 90 分钟不叫 VP",
          "90 分钟的不叫 VP" in llmm.SYSTEM_PROMPT, "")


def main() -> int:
    print("=" * 62)
    print("core/llm.py 自测（假 provider，不调真模型）")
    print("=" * 62)
    test_happy()
    test_fabricated_problem()
    test_not_json()
    test_code_fence()
    test_no_degradation()
    test_unknown_response()
    test_dirty_input()
    test_prompt()
    test_provider_id()
    test_vp_minutes()
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
