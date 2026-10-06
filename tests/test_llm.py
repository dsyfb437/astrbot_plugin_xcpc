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

    def get_current_chat_provider_id(self, umo=""):
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

    # 大小写/空格差异应该被容忍
    payload2 = {"assessment": "x", "tasks": [
        {"kind": "practice", "title": "t", "problem": "cf:1900d", "minutes": 10}]}
    plan2 = run(FakeContext(text=json.dumps(payload2)))
    check("大小写差异被容忍（不算编造）", plan2.tasks[0].problem == "cf:1900d",
          repr(plan2.tasks[0].problem))


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
    print("\n" + "=" * 62)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
