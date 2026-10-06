#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调 AstrBot 的模型，拿一份"下一步做什么"。

依赖的 AstrBot API（已在源码里核实）
------------------------------------
    context.llm_generate(*, chat_provider_id, system_prompt, prompt,
                         contexts, tools, **kw) -> LLMResponse   # v4.5.7+
    context.get_current_chat_provider_id(umo) -> str

`llm_generate` **不会自动执行工具调用**（官方 docstring 明说），
要工具循环得用 `tool_loop_agent`。我们这里不需要 ——
我们要的是一次"读汇总 → 给方案"的纯生成。

两条硬约束
----------
1. **输出必须是能解析的 JSON**，且符合约定字段。解析不出来就报错，
   不做"从自由文本里猜结构"这种事（猜错了会静默产生错误的任务）。
2. **题号必须真实存在**（在候选池里）。LLM 编题号是最常见的失效模式，
   而它编出来的题号**看起来很合理**（比如 CF:1234D 完全可能是真存在的题），
   不校验就会让用户去搜一道不存在的题。

不降级
------
调用失败 → **抛 `LLMError`**，由上层明确报错 + 记日志 + 保留上一版方案。
**没有"退化成规则计划"这条路。**

为什么：悄悄降级会让你以为在按计划走，实际走的是另一套东西 ——
这比直接报错危险得多。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 允许的任务类型。**开放的** —— 训练不只是做题。
# （上一版方案把"任务"默认成"做题"，是错的：学新算法、学语法糖、
#   读题解这些都没有题号，但对训练同样重要。）
KINDS = ("learn", "practice", "review", "vp", "upsolve", "syntax", "read")

KIND_LABEL = {
    "learn": "学新算法", "practice": "做题", "review": "复习",
    "vp": "打 VP", "upsolve": "补题", "syntax": "学语法/技巧", "read": "读题解",
}


class LLMError(Exception):
    """带失败分类的模型调用错误。"""

    def __init__(self, message: str, kind: str = "内部错误"):
        super().__init__(message)
        # 复用日志层那套固定枚举，保证可统计
        from . import log as logm
        self.kind = kind if kind in logm.ERROR_KINDS else "内部错误"


@dataclass
class Task:
    kind: str
    title: str
    problem: str = ""
    minutes: int = 0
    why: str = ""

    def line(self) -> str:
        label = KIND_LABEL.get(self.kind, self.kind)
        bits = ["[%s] %s" % (label, self.title)]
        if self.problem:
            bits.append("→ %s" % self.problem)
        if self.minutes:
            bits.append("（%d 分钟）" % self.minutes)
        text = " ".join(bits)
        if self.why:
            text += "\n      %s" % self.why
        return "  " + text


@dataclass
class Plan:
    date: str = ""
    assessment: str = ""
    tasks: list[Task] = field(default_factory=list)
    watch: str = ""
    raw: str = ""
    model_id: str = ""
    # 校验时被拦下的问题（**要展示给用户看**，不是内部消化）
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"date": self.date, "assessment": self.assessment,
                "watch": self.watch, "model_id": self.model_id,
                "tasks": [{"kind": t.kind, "title": t.title, "problem": t.problem,
                           "minutes": t.minutes, "why": t.why} for t in self.tasks],
                "problems": self.problems}

    def to_text(self) -> str:
        L = []
        if self.assessment:
            L.append(self.assessment)
            L.append("")
        if self.tasks:
            for t in self.tasks:
                L.append(t.line())
        else:
            L.append("（这次没给出任务）")
        if self.watch:
            L += ["", "下次注意：%s" % self.watch]
        if self.problems:
            L += ["", "⚠ 有几处我没法确认，已标出："]
            L += ["  · %s" % p for p in self.problems]
        return "\n".join(L)


SYSTEM_PROMPT = """你是一个 XCPC（ICPC/CCPC）备赛教练。用户会给你一份他自己训练数据的汇总，
你要给出**今天/明天该做什么**。

## 你的输出必须是 JSON，不能有任何别的文字

```json
{
  "assessment": "两三句话：他现在什么状态。要具体，引用数据。",
  "tasks": [
    {"kind": "learn|practice|review|vp|upsolve|syntax|read",
     "title": "人话任务名",
     "problem": "候选池里的题号，或空字符串",
     "minutes": 30,
     "why": "为什么安排这个，一句话"}
  ],
  "watch": "你想让他下次注意什么（可空）"
}
```

## 硬规则（违反了这份方案就没用）

1. **`problem` 只能从汇总里的「候选题目」一节里挑，逐字复制题号。**
   绝对不要自己编题号，也不要写"类似的题"。没有合适的就留空字符串。
2. **`kind` 只能是那七个之一。** 训练不只是做题 ——
   学新算法用 `learn`、学语言技巧用 `syntax`、读题解用 `read`，
   这些**不要**硬塞成 `practice`。
3. **难度不要跨平台比较。** CF rating、AtCoder 的 IRT 值、洛谷的 1-7 档
   是三套不同的尺子，汇总里已经分开列了。不要说"你在 AtCoder 的 1200
   相当于 CF 的 1600"这种话。
4. **总时长控制在 60-150 分钟。** 他还有课。宁可少而完成，不要多而放弃。
5. **汇总里说「拿不到」「没有数据」的，不要当成「零」或「没有」。**
   比如没登录的平台不等于他没练过。
6. **如果汇总里有「疑似难度回避」**，认真对待：那是最值得处理的问题，
   优先安排一道那个方向的题（用候选池里对应标签的）。
   但也不要一次全塞 —— 一道就够。
7. **`assessment` 要引用具体数字**，不要空泛地说"继续加油"。

## 语气

像个了解他的教练，不是打卡软件。
说人话，不要"根据数据分析，建议您……"这种。"""


def build_prompt(summary_text: str, feedback_hint: str = "",
                 constraints: str = "") -> str:
    """拼提示词。约束放在**最后**（近因效应，模型更容易遵守）。"""
    parts = [summary_text]
    if feedback_hint:
        parts += ["", "## 他刚才说", feedback_hint]
    if constraints:
        parts += ["", "## 这次的额外要求（优先级最高）", constraints]
    parts += ["", "现在给出今天的方案。**只输出 JSON。**"]
    return "\n".join(parts)


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON。

    模型常常会包 ```json 代码块，或者前面加一句话。
    **但只在"能明确抠出来"的情况下才继续** —— 抠不出就报错，
    不做"猜结构"。
    """
    if not text or not text.strip():
        raise LLMError("模型返回了空内容", "解析失败")
    s = text.strip()

    # 去掉 ```json ... ``` 包裹
    m = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
    if m:
        s = m.group(1).strip()

    # 直接解析
    try:
        data = json.loads(s)
    except ValueError:
        # 退一步：找最外层的一对花括号
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            raise LLMError(
                "模型输出里找不到 JSON 对象。原文前 300 字：%s" % text[:300],
                "解析失败") from None
        try:
            data = json.loads(s[i:j + 1])
        except ValueError as exc:
            raise LLMError(
                "模型输出不是合法 JSON（%s）。原文前 300 字：%s" % (exc, text[:300]),
                "解析失败") from None

    if not isinstance(data, dict):
        raise LLMError("模型返回的 JSON 不是对象，而是 %s" % type(data).__name__,
                       "解析失败")
    return data


def validate(data: dict, candidates: list[dict], *,
             max_minutes: int = 200, date: str = "") -> Plan:
    """校验并转成 `Plan`。

    **校验只做一件事：题号必须真实存在。** 其余不校验 ——
    那是用户复核的地方，不是我替他决定的。

    但"不校验"不等于"不检查"：`kind` 不认识、分钟数离谱这些会
    被**修正并记进 `problems`**，而不是静默接受或静默丢弃。
    """
    plan = Plan(date=date, raw=json.dumps(data, ensure_ascii=False)[:4000])
    plan.assessment = str(data.get("assessment") or "").strip()[:1500]
    plan.watch = str(data.get("watch") or "").strip()[:500]

    valid_keys = {c["problem_key"] for c in candidates if c.get("problem_key")}
    # 也接受大小写/空格差异（模型偶尔会写成 "cf:1234d"）
    normalized = {k.upper().replace(" ", ""): k for k in valid_keys}

    raw_tasks = data.get("tasks")
    if raw_tasks is None:
        plan.problems.append("模型没给 tasks 字段")
        raw_tasks = []
    if not isinstance(raw_tasks, list):
        plan.problems.append("tasks 不是数组（是 %s），已忽略" % type(raw_tasks).__name__)
        raw_tasks = []

    total_minutes = 0
    for i, t in enumerate(raw_tasks):
        if not isinstance(t, dict):
            plan.problems.append("第 %d 个任务不是对象，已跳过" % (i + 1))
            continue
        kind = str(t.get("kind") or "").strip().lower()
        if kind not in KINDS:
            plan.problems.append(
                "第 %d 个任务的类型「%s」不认识，按「做题」处理" % (i + 1, kind or "空"))
            kind = "practice"

        problem = str(t.get("problem") or "").strip()
        if problem:
            key = problem.upper().replace(" ", "")
            real = valid_keys and (problem in valid_keys or key in normalized)
            if not real:
                # 这是最重要的一条校验：不能让它编题号
                plan.problems.append(
                    "「%s」不在候选池里，**已剔除**（可能是模型编的题号）" % problem)
                problem = ""
        try:
            minutes = int(t.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes < 0 or minutes > 300:
            plan.problems.append("第 %d 个任务的时长 %s 不合理，按 0 处理"
                                 % (i + 1, t.get("minutes")))
            minutes = 0
        total_minutes += minutes

        plan.tasks.append(Task(
            kind=kind,
            title=str(t.get("title") or "").strip()[:200] or KIND_LABEL.get(kind, kind),
            problem=problem, minutes=minutes,
            why=str(t.get("why") or "").strip()[:500]))

    if total_minutes > max_minutes:
        plan.problems.append(
            "总时长 %d 分钟超过上限 %d —— 计划可能偏满，你自己看着砍"
            % (total_minutes, max_minutes))

    if not plan.tasks and not plan.problems:
        plan.problems.append("模型一个任务都没给")

    return plan


async def generate(context, *, summary_text: str, candidates: list[dict],
                   model_id: str = "", umo: str = "", feedback_hint: str = "",
                   constraints: str = "", max_minutes: int = 200,
                   date: str = "", recorder=None, user_id: str = "") -> Plan:
    """调模型拿方案。

    **失败抛 `LLMError`，不返回一个"凑合的"方案。**
    上层要把它变成一句明确的错误 + 一条日志，并保留上一版方案。
    """
    import time
    started = time.monotonic()

    if context is None:
        raise LLMError("拿不到 AstrBot 的 context，没法调模型")

    # 决定用哪个模型
    provider_id = (model_id or "").strip()
    if not provider_id:
        try:
            provider_id = context.get_current_chat_provider_id(umo) or ""
        except Exception:
            provider_id = ""
    if not provider_id:
        raise LLMError(
            "找不到可用的模型 —— 请在 AstrBot 里配置一个对话模型，"
            "或在插件配置里指定 llm_provider_id", "凭据失效")

    llm_generate = getattr(context, "llm_generate", None)
    if llm_generate is None:
        raise LLMError(
            "这个 AstrBot 版本没有 llm_generate（需要 >= 4.5.7）", "内部错误")

    prompt = build_prompt(summary_text, feedback_hint, constraints)
    try:
        resp = await llm_generate(
            chat_provider_id=provider_id,
            system_prompt=SYSTEM_PROMPT,
            prompt=prompt,
        )
    except Exception as exc:                       # noqa: BLE001
        if recorder:
            recorder.event("llm.call", user_id=user_id, ok=False,
                           error_kind="内部错误",
                           detail="%s: %s" % (type(exc).__name__, exc),
                           duration_ms=int((time.monotonic() - started) * 1000))
        raise LLMError("调用模型失败：%s: %s" % (type(exc).__name__, exc),
                       "内部错误") from exc

    text = _response_text(resp)
    elapsed = int((time.monotonic() - started) * 1000)
    if recorder:
        recorder.event("llm.call", user_id=user_id,
                       duration_ms=elapsed, provider=provider_id,
                       chars=len(text))

    data = extract_json(text)
    plan = validate(data, candidates, max_minutes=max_minutes, date=date)
    plan.model_id = provider_id
    plan.raw = text[:4000]

    if recorder:
        recorder.event("llm.plan", user_id=user_id,
                       tasks=len(plan.tasks), flagged=len(plan.problems))
    return plan


def _response_text(resp: Any) -> str:
    """从 LLMResponse 里取文本。

    不同版本字段名可能不同，按优先级试。

    ⚠️ **"字段在但内容是空"和"认不出字段"必须分开报**。
    我第一版只看 `v.strip()` 是不是空的，于是模型返回空串时会走到
    "认不出结构（可能是版本差异）" —— 那句话会把人引去查 AstrBot 版本，
    而真实原因是**模型吐了个空**。错误信息指错方向比没有错误信息更费时间。
    """
    if resp is None:
        raise LLMError("模型返回了 None", "内部错误")
    if isinstance(resp, str):
        if not resp.strip():
            raise LLMError("模型返回了空内容", "解析失败")
        return resp

    # ① 先找"字段存在"的，不管内容空不空
    for attr in ("completion_text", "text", "content", "message"):
        if not hasattr(resp, attr):
            continue
        v = getattr(resp, attr)
        if isinstance(v, str):
            if not v.strip():
                raise LLMError(
                    "模型返回了空内容（字段 %s 是空字符串）—— "
                    "可能是模型拒答、被截断，或上下文太长" % attr, "解析失败")
            return v
    for attr in ("result", "data"):
        v = getattr(resp, attr, None)
        if isinstance(v, str):
            if not v.strip():
                raise LLMError("模型返回了空内容（字段 %s）" % attr, "解析失败")
            return v
        if isinstance(v, dict):
            for k in ("completion_text", "text", "content"):
                if k in v and isinstance(v[k], str):
                    if not v[k].strip():
                        raise LLMError("模型返回了空内容（%s.%s）" % (attr, k),
                                       "解析失败")
                    return v[k]

    # ② 确实认不出结构 —— 这时才提版本差异
    raise LLMError(
        "认不出模型返回的结构（类型 %s，可用字段 %s）—— "
        "可能是 AstrBot 版本差异，把这条报给我"
        % (type(resp).__name__,
           [a for a in dir(resp) if not a.startswith("_")][:12]),
        "内部错误")
