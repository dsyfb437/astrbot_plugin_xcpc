#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""五环调度：聚合 → 汇总 → 评估 → 给出 → 反馈。

    ┌──────────────────────────────────────────────┐
    │                                              │
    ▼                                              │
  ① 聚合 ──► ② 汇总 ──► ③ LLM 评估 ──► ④ 给出方案
   （同步）    （数学）    （AstrBot 模型）   （存 + 推）
    ▲                                              │
    │                                              ▼
    └────────── ⑤ 你执行 + 反馈 ◄──────────────────┘

规则和 AI 的分工
----------------
| 层 | 谁做 | 为什么 |
|---|---|---|
| 候选池筛选（tag × 难度 × 没做过） | **规则** | 约束满足，不是判断题 |
| 原始数据 → 一页汇总 | **规则** | 确定性、可审计、省 token |
| 从候选池组装今天做什么 + 理由 | **LLM** | 这是"推理" |
| 校验题号是否真实存在 | **规则** | 防编造 |
| 理解口语反馈 | **LLM** | 规则写不完 |

**AI 不直接读原始数据。** 它读的是规则压缩过的一页 ——
这样每次输出有据可查（"它为什么说你 dp 弱"能追到具体数字）。

不降级
------
LLM 失败 → **报错 + 记日志 + 保留上一版方案**，不返回凑合的结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import blocks as blocksm
from . import curriculum as cur
from . import llm as llmm
from . import log as logm
from . import summary as summ


@dataclass
class LoopResult:
    ok: bool
    plan: llmm.Plan | None = None
    summary_text: str = ""
    error_kind: str = ""
    detail: str = ""
    used_previous: bool = False     # 失败时是否回退展示上一版
    candidates: int = 0

    def text(self) -> str:
        if self.ok and self.plan:
            body = self.plan.to_text()
            return body
        msg = "[%s] %s" % (self.error_kind or "内部错误", self.detail)
        if self.used_previous and self.plan:
            return msg + "\n\n（下面是**上一次**的方案，不是新生成的）\n\n" \
                   + self.plan.to_text()
        return msg


# 熟悉度 → 候选难度区间（相对他做过的题的中位难度）
# 这是**规则**：目标难度由数据决定，不由模型拍脑袋
def _target_band(median: float | None, lower: int, upper: int) -> tuple[int, int]:
    """由"他做过的题的中位难度"推出该给他什么难度的题。

    偏难一点是对的（舒适区外才有提升），但**不能一步跨太远** ——
    CF 上跨 400 分基本就是"看得懂题但做不出"，容易挫败。

    `lower` / `upper` 是相对中位的偏移。
    """
    if median is None:
        return (0, 10 ** 9)
    return (int(median) + lower, int(median) + upper)


class Loop:
    """循环的执行体。一个插件实例一个。"""

    def __init__(self, db, store, recorder: logm.Recorder | None = None,
                 syncer=None) -> None:
        self.db = db
        self.store = store
        self.recorder = recorder
        self.syncer = syncer

    async def _load_bank(self, limit_per_platform: int = 0) -> dict:
        """把题库标注读成 `{platform: {key: {...}}}`。

        ⚠️⚠️ 这里**曾经是 `LIMIT 4000`，而且不带 `ORDER BY`** ——
        在 11425 道 CF 题里**任意切了 4000 道**。后果不是"少一点数据"，
        而是**两份数据对不上**：

          · 候选池只从那 4000 道里挑；
          · 更致命的是 `prob_tags` 也只认得那 4000 道 ——
            他 AC 的 90 道 CF 题里只有 **6 道**能在里面找到。

        于是「疑似难度回避」是在 **6 道题**上算出来的：dp 只做过 2 题
        → share = 2/6 = 33%，**远高于 4% 的阈值，dp 直接被判定为"不算回避"**。
        用户问「dp 的权重是不是该大一点」，而 dp 那条**根本没能进列表**。
        任何"在切片上算比例"的统计都会这样静默地烂掉。

        一万多道题的标注撑死十几 MB，**这点内存不值得拿正确性去换**。
        `limit_per_platform=0` 表示不限制。
        """
        bank: dict[str, dict] = {}
        for p in ("codeforces", "atcoder", "luogu", "qoj"):
            sql = ("SELECT problem_key, title, tags_json, difficulty, difficulty_source "
                   "FROM problems WHERE platform=?")
            params: tuple = (p,)
            if int(limit_per_platform) > 0:
                sql += " LIMIT ?"
                params = (p, int(limit_per_platform))
            try:
                rows = await self.db.query(sql, params)
            except Exception:
                continue
            if not rows:
                continue
            bank[p] = {}
            for r in rows:
                import json
                try:
                    tags = json.loads(r["tags_json"]) if r["tags_json"] else None
                except (ValueError, TypeError):
                    tags = None
                bank[p][r["problem_key"]] = {
                    "title": r["title"], "tags": tags,
                    "difficulty": r["difficulty"],
                    "difficulty_source": r["difficulty_source"],
                }
        return bank

    async def prepare(self, user_id: str, *,
                      auto_sync: bool = True,
                      bank: dict | None = None) -> dict:
        """①②：聚合 + 汇总（**不调 LLM**）。

        拆出来是为了让"看看数据"和"要方案"能用同一份汇总，
        也便于单独测试和排查（不用每次都花 token）。
        """
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("user_id 不能为空")

        sync_report = None
        if auto_sync and self.syncer is not None:
            try:
                sync_report = await self.syncer.sync(user_id)
            except Exception as exc:                       # noqa: BLE001
                if self.recorder:
                    self.recorder.event("loop.sync_fail", user_id=user_id, ok=False,
                                        error_kind="内部错误", detail=str(exc))
                sync_report = None

        bank = bank if bank is not None else await self._load_bank()
        subs = await self.store.list_submissions(user_id, limit=100000)

        # 汇总先建一次（为了拿到中位难度），再据此挑候选，再重建
        base = await summ.build(self.store, user_id, bank=bank)

        primary = "cf_rating" if "cf_rating" in base.by_source else None
        median = None
        if primary:
            median = base.by_source[primary].get("median")
        lo, hi = _target_band(median, -200, 400)

        # ---- 训练块：一段连续的日子里只吃一个子专题（v0.6.0）----
        #
        # 这一段取代了 v0.5.x 的"回避方向注入"。那个做法本身没错
        # （数据告诉我们 dp/rutures 弱 → 推几道 dp），但它**解决不了用户的问题**：
        #
        #     像这样子推荐一个两个题练一下我感觉根本没效果啊，也没有针对性，
        #     每次似乎都是从整体做题情况出发
        #
        # 他说得对 —— 回避注入每天仍在换方向：今天 trees 明天 graphs，
        # 每个方向一天半道题。**方向对了，量不对，等于没练。**
        # 训练块把"方向"钉住一段时间，量才攒得起来。
        #
        # 块的候选池排在**最前面** —— 候选池的排列顺序就是给模型的暗示，
        # 这比在提示词里多写一句"要有针对性"管用得多（v0.5.14 的教训）。
        blk = await self.store.get_block(user_id)
        cov = blocksm.coverage(subs, bank)
        if not blk:
            mod = blocksm.pick_module(subs, bank, cov)
            tkey = blocksm.choose(mod, cov)
            t = cur.BY_KEY.get(tkey)
            if t is not None:
                await self.store.set_block(
                    user_id, mod, tkey, target=t.count, band_lo=t.lo, band_hi=t.hi,
                    note=blocksm.reason(tkey, cov))
                blk = await self.store.get_block(user_id)
        blk = dict(blk) if blk else {}
        topic = cur.BY_KEY.get(str(blk.get("topic") or ""))
        prog = blocksm.progress(subs, bank, blk) if topic is not None else {}

        topic_cands = (summ.pick_topic_candidates(subs, bank, topic, limit=30)
                       if topic is not None else [])

        # 兜底候选：冷门子专题 + 难度带窄 + 他已经做过一部分时，
        # 这一块可能只剩几道题。那时还得有别的题可挑。
        # **排在训练块候选后面** —— 位置本身就是"优先挑前面的"的暗示。
        normal_cands = summ.pick_candidates(
            subs, bank, limit=12,
            min_difficulty=lo if median is not None else None,
            max_difficulty=hi if median is not None else None,
            source=primary or "cf_rating")

        # 训练块的排前面，然后接兜底候选；按 key 去重
        seen: set[str] = set()
        candidates: list[dict] = []
        for c in topic_cands + normal_cands:
            k = c["problem_key"]
            if k in seen:
                continue
            seen.add(k)
            candidates.append(c)
        candidates = candidates[:42]

        info = await summ.build(self.store, user_id, bank=bank,
                                candidates=candidates,
                                block=blk,
                                block_progress=prog)
        if topic is not None:
            info.notes.append(
                "候选池前 %d 条是**当前训练块**「%s」的题（难度带 %s，"
                "两个平台各自的尺子分开翻）。后面的 %d 条是兜底，"
                "**优先从前面的里挑**。" % (len(topic_cands), topic.name,
                                        cur.band_for(topic, "codeforces"),
                                        len(normal_cands)))
        if median is not None:
            info.notes.append(
                "兜底候选的难度区间是 %s~%s，依据是你做过的题的中位难度（%s）。"
                "这是按「比舒适区略难」算的，不是随便定的。" % (lo, hi, median))
        else:
            info.notes.append(
                "题目还没有难度信息，候选池**没做难度筛选** —— "
                "先同步一次题库（CF 的 problemset 带难度和标签）。")
        return {"summary": info, "sync_report": sync_report,
                "candidates": candidates}

    async def run(self, context, user_id: str, *, umo: str = "",
                  model_id: str = "", feedback_hint: str = "",
                  constraints: str = "", auto_sync: bool = True,
                  date: str = "", max_minutes: int = 200,
                  prep: dict | None = None) -> LoopResult:
        """跑完整的一轮。**失败不降级** —— 返回的 result.ok=False 且带分类。"""
        prep = prep or await self.prepare(user_id, auto_sync=auto_sync)
        info = prep["summary"]
        candidates = prep["candidates"]

        try:
            plan = await llmm.generate(
                context,
                summary_text=info.to_text(),
                candidates=candidates,
                model_id=model_id, umo=umo,
                feedback_hint=feedback_hint, constraints=constraints,
                max_minutes=max_minutes, date=date or _today(),
                recorder=self.recorder, user_id=user_id)
        except llmm.LLMError as exc:
            # **不降级**：报错 + 记日志 + 把上一版方案取出来给用户看
            if self.recorder:
                self.recorder.event("loop.llm_fail", user_id=user_id, ok=False,
                                    error_kind=exc.kind, detail=str(exc))
            prev = None
            try:
                prev = await self.get_latest_plan(user_id)
            except Exception:
                prev = None
            return LoopResult(ok=False, plan=prev, error_kind=exc.kind,
                              detail=str(exc), used_previous=prev is not None,
                              summary_text=info.to_text(),
                              candidates=len(candidates))
        except Exception as exc:                            # noqa: BLE001
            if self.recorder:
                self.recorder.event("loop.crash", user_id=user_id, ok=False,
                                    error_kind="内部错误", detail=str(exc))
            return LoopResult(ok=False, error_kind="内部错误",
                              detail="%s: %s" % (type(exc).__name__, exc),
                              summary_text=info.to_text())

        # ④ 存下来
        try:
            await self.store.save_plan(user_id, plan.date or _today(), plan.to_dict())
        except Exception as exc:                            # noqa: BLE001
            if self.recorder:
                self.recorder.event("loop.save_fail", user_id=user_id, ok=False,
                                    error_kind="数据库错误", detail=str(exc))
            # 存不下不影响用户看到方案，但要如实说
            plan.problems.append("方案没能存进数据库（%s）—— 下次可能看不到它"
                                 % exc)

        if self.recorder:
            self.recorder.event("loop.done", user_id=user_id,
                                tasks=len(plan.tasks), flagged=len(plan.problems))
        return LoopResult(ok=True, plan=plan, summary_text=info.to_text(),
                          candidates=len(candidates))

    async def get_latest_plan(self, user_id: str, date: str = "") -> llmm.Plan | None:
        """取最近一版方案。用于"失败时展示上一版"。

        `date` 不为空时只要那一天的（推送用 —— 它要的是**今天**那版，
        而不是"最近一版"，两者在跨天的时候不是一回事）。
        """
        import json
        row = await self.store.latest_plan(user_id, date)
        if not row:
            return None
        try:
            data = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            return None
        plan = llmm.Plan(
            date=row["date"], assessment=str(data.get("assessment") or ""),
            watch=str(data.get("watch") or ""), model_id=str(data.get("model_id") or ""),
            problems=list(data.get("problems") or []))
        for t in (data.get("tasks") or []):
            if not isinstance(t, dict):
                continue
            plan.tasks.append(llmm.Task(
                kind=str(t.get("kind") or "practice"),
                title=str(t.get("title") or ""),
                problem=str(t.get("problem") or ""),
                minutes=int(t.get("minutes") or 0),
                why=str(t.get("why") or "")))
        return plan

    async def feedback(self, user_id: str, text: str, date: str = "") -> int:
        """⑤ 记一条反馈。下一轮会带进提示词。"""
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("user_id 不能为空")
        n = await self.store.add_feedback(user_id, date or _today(), text)
        if self.recorder:
            self.recorder.event("loop.feedback", user_id=user_id, chars=len(text or ""))
        return n


def _today() -> str:
    from . import log as logm
    return logm.now_cn().strftime("%Y-%m-%d")
