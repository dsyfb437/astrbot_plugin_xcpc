#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步编排：取数 → 落库 → 记断点。

三条硬规则
----------
1. **失败绝不推进断点。** 推进了下次就会从错的位置开始，**静默漏掉一段数据**。
   所以 `save_sync_error` 刻意不动 `last_epoch`（见 store.py）。
2. **各平台互相独立。** AtCoder 的社区 API 挂了不能拖垮 CF 的同步。
   每个平台单独 try，单独记结果。
3. **"没有"和"拿不到"必须区分。** 洛谷 Cookie 过期时如果当成"没有提交"，
   用户会以为自己在洛谷没做过题 —— 这是最危险的一类静默错误。

同一 `(user_id, platform)` 同时只允许一个同步在跑（`sync_lock`），
免得定时任务和手动 `/xcpc 同步` 撞车。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from . import http as httpm
from . import import_platform
from . import log as logm
from .store import PLATFORMS, Store

# 平台适配器（延迟导入，避免没装的依赖影响别的平台）
#
# ⚠️ 这里**不能**写 `from platforms.codeforces import Codeforces`。
# AstrBot 是按包加载的（`data.plugins.astrbot_plugin_xcpc.main`），
# 那种情况下 `platforms` 不是顶层模块，这句会 ModuleNotFoundError —— 真机上炸过。
# `import_platform()` 两种加载方式都认（见 core/__init__.py）。
def _adapter(platform: str):
    if platform == "codeforces":
        return import_platform("codeforces").Codeforces()
    if platform == "atcoder":
        return import_platform("atcoder").AtCoder()
    if platform == "qoj":
        return import_platform("qoj").Qoj()
    if platform == "luogu":
        return import_platform("luogu").Luogu()
    raise ValueError("不认识的平台：%r" % platform)


# 每个平台怎么造 client（洛谷要额外的头）
CLIENT_KWARGS = {
    "codeforces": {"min_interval": 1.0},
    "atcoder": {"min_interval": 1.0},
    "qoj": {"min_interval": 1.0},
    "luogu": {"min_interval": 1.5,
              "extra_headers": {"x-luogu-type": "content-only"}},
}

# 需要登录的平台 —— 没凭据时直接报「需要登录」，不去发注定失败的请求
NEEDS_LOGIN = ("qoj", "luogu")


def parse_platform_proxies(text) -> dict:
    """把配置里一行行的 `平台=代理地址` 解析成 `{平台: 地址}`。

    格式**故意做得宽容**，因为这是给人手打的：换行 / 逗号 / 分号都当分隔符，
    `=` 两边空格无所谓，`#` 开头的整行当注释。所以下面几种写法等价：

        qoj=http://127.0.0.1:7890
        qoj = http://127.0.0.1:7890   # 注释
        qoj=http://127.0.0.1:7890, luogu=http://127.0.0.1:7891

    ⚠️ **认不出来的行一律跳过，绝不抛异常。** 配置里打错一个字就让插件加载失败，
    那是拿用户整个 bot 去赌一行配置 —— 跳过它、让那个平台退回直连，坏处小得多。
    真出问题时的症状是"配了但没生效"，那个靠日志里的 `挑战未过` 就能看出来。
    """
    out: dict[str, str] = {}
    if not text:
        return out
    for chunk in re.split(r"[\n,;]+", str(text)):
        # 先砍行内注释。`#` 在代理地址里没有合法用途（那是 URL 的 fragment 段），
        # 所以砍掉是安全的 —— 而手写配置时"地址后面跟一句说明"太常见了。
        line = chunk.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, addr = line.partition("=")
        if not sep:
            continue
        name = name.strip().lower()
        addr = addr.strip()
        # 平台名要认，地址要非空。地址里带没带协议由 urllib 去较真。
        if name in PLATFORMS and addr:
            out[name] = addr
    return out


@dataclass
class PlatformResult:
    """一个平台的同步结果。"""
    platform: str
    ok: bool
    submissions: int = 0          # 本次**新增**的提交数
    contests: int = 0             # 本次**新增**的比赛数
    total_submissions: int = 0    # 库里这个平台的总数
    error_kind: str = ""
    detail: str = ""
    duration_ms: int = 0

    def line(self) -> str:
        """给人看的一行。"""
        name = {"codeforces": "CF", "atcoder": "AtCoder",
                "qoj": "QOJ", "luogu": "洛谷"}.get(self.platform, self.platform)
        if self.ok:
            extra = "，比赛 +%d" % self.contests if self.contests else ""
            return "%s  ✓  提交 +%d（共 %d）%s  %dms" % (
                name, self.submissions, self.total_submissions, extra, self.duration_ms)
        return "%s  ✗  [%s] %s" % (name, self.error_kind or "内部错误", self.detail[:120])


@dataclass
class SyncReport:
    results: list[PlatformResult] = field(default_factory=list)
    # 题库标注的拉取结果（全局，不按用户分）
    bank: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results) and bool(self.results)

    def text(self) -> str:
        if not self.results:
            return "没有可同步的平台。先在绑定页填 handle。"
        lines = [r.line() for r in self.results]
        # 题库是附属信息，单独一行，不混进平台结果里
        if self.bank:
            if self.bank.get("ok"):
                added = self.bank.get("added") or 0
                if added:
                    lines.append("题库标注  ✓  新增 %d 题" % added)
                else:
                    lines.append("题库标注  ✓  %s" % (self.bank.get("detail") or ""))
            else:
                # **题库失败不算整次同步失败**，但要如实说 ——
                # 没有它的话"难度回避"和"候选题"都用不了。
                lines.append("题库标注  ✗  %s（没有它就没法判难度回避、"
                             "也没题可推）" % (self.bank.get("detail") or "")[:80])
        return "\n".join(lines)

    def failures(self) -> list[PlatformResult]:
        return [r for r in self.results if not r.ok]


class Syncer:
    """同步器。一个 Store 一个实例。"""

    def __init__(self, db, store: Store, recorder: logm.Recorder | None = None,
                 rate_scale: float = 1.0, user_agent: str = "",
                 platform_proxies: dict | None = None,
                 adapter_factory=None) -> None:
        self.db = db
        self.store = store
        self.recorder = recorder
        self.rate_scale = rate_scale
        self.user_agent = user_agent
        # 平台 -> 代理地址。**只对写进来的平台生效**，没写的照旧。
        # 存在的理由是 QOJ：这台机器的 IP 被 Cloudflare 判成数据中心，
        # qoj.ac 一律回 403 挑战页，换 UA 没用，只能换出口 IP。
        self.platform_proxies = dict(platform_proxies or {})
        # 适配器工厂。**做成可注入的**，这样测试能塞假适配器，
        # 不用去 monkeypatch 模块全局变量 —— 那种写法多个测试之间会互相污染
        # （前一个测试打的补丁留在原地，后一个测试就在不知情的情况下用了假的）。
        self._adapter_factory = adapter_factory or _adapter
        # (user_id, platform) -> asyncio.Lock
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock(self, user_id: str, platform: str) -> asyncio.Lock:
        key = (user_id, platform)
        lk = self._locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[key] = lk
        return lk

    def _client_kwargs(self, platform: str) -> dict:
        """造 `HttpClient` 的公共参数：`CLIENT_KWARGS` + 倍率 + 平台代理。

        **两个造 client 的地方都走这里** —— 不然加参数时很容易漏掉一个。
        """
        kw = dict(CLIENT_KWARGS.get(platform, {}))
        kw["rate_scale"] = self.rate_scale
        # 只给配了的平台挂代理。没配的连 `proxy` 这个键都不传，
        # 保证行为跟没这个功能时**逐字节一致**。
        proxy = self.platform_proxies.get(platform) or ""
        if proxy:
            kw["proxy"] = proxy
        return kw

    async def make_client(self, user_id: str, platform: str) -> httpm.HttpClient:
        """造 client 并把该用户的凭据装进去。

        **凭据只在这里被读出来**，读完立刻装进 client，
        不经过任何会打日志的路径。
        """
        kw = self._client_kwargs(platform)
        if self.user_agent:
            kw["extra_headers"] = dict(kw.get("extra_headers") or {})
            kw["extra_headers"]["User-Agent"] = self.user_agent
        c = httpm.HttpClient(platform=platform, recorder=self.recorder, **kw)
        if platform in NEEDS_LOGIN or platform == "atcoder":
            creds = await self.store.get_credentials(user_id, platform)
            if creds:
                # set_cookies 会丢弃含换行的（防 header 注入）
                c.set_cookies(creds)
        return c

    async def sync_one(self, user_id: str, platform: str,
                       with_contests: bool = True) -> PlatformResult:
        """同步单个平台。**永远返回结果对象，不抛异常** ——
        一个平台挂了不该让整次 `/xcpc 同步` 崩掉。"""
        import time
        started = time.monotonic()
        res = PlatformResult(platform=platform, ok=False)

        async with self._lock(user_id, platform):
            handle = await self.store.get_handle(user_id, platform)
            # 洛谷和 QOJ 的"handle"其实是 uid/用户名，逻辑一样
            if not handle and platform == "qoj":
                row = await self.store.get_user(user_id)
                handle = ""
            if not handle:
                res.error_kind = "凭据失效"
                res.detail = "还没绑定 handle —— 在绑定页填一下"
                await self.store.save_sync_error(user_id, platform,
                                                 res.error_kind, res.detail)
                res.duration_ms = int((time.monotonic() - started) * 1000)
                return res

            # 需要登录但没凭据：明确报，不去发注定 401 的请求
            if platform in NEEDS_LOGIN:
                creds = await self.store.get_credentials(user_id, platform)
                if not creds:
                    res.error_kind = "凭据失效"
                    res.detail = "%s 需要登录 —— 在绑定页登录或导入 Cookie" % platform
                    await self.store.save_sync_error(user_id, platform,
                                                     res.error_kind, res.detail)
                    res.duration_ms = int((time.monotonic() - started) * 1000)
                    if self.recorder:
                        self.recorder.event("sync.skip", user_id=user_id,
                                            platform=platform, ok=False,
                                            error_kind=res.error_kind, detail=res.detail)
                    return res

            adapter = self._adapter_factory(platform)
            try:
                client = await self.make_client(user_id, platform)
            except Exception as exc:
                res.error_kind = "内部错误"
                res.detail = "造 client 失败：%s" % exc
                res.duration_ms = int((time.monotonic() - started) * 1000)
                return res

            # 断点：**只要成功过就用它**，不管上次是不是失败。
            #
            # 我第一版写的是 `if state and not state["error_kind"] and ...`，
            # 想着"失败过就保险起见全量重拉"。**那是自相矛盾的**：
            # 我们之所以在失败时不推进 `last_epoch`（见 store.save_sync_error），
            # 就是为了下次能从正确的位置续上。一失败就退化成全量重拉，
            # 等于把那个设计作废，而且每次网络抖一下都要重拉全部历史。
            #
            # 续拉是安全的：`upsert_submissions` 幂等，重复拉不会产生重复数据；
            # 而 `last_epoch` 只会在**整次成功**后才前进，绝不会指向拉了一半的位置。
            state = await self.store.get_sync_state(user_id, platform)
            since = int(state["last_epoch"]) if (state and state["last_epoch"]) else None

            try:
                got = await adapter.fetch_submissions(handle, since, client)
            except Exception as exc:
                got = None
                res.error_kind = getattr(exc, "kind", "内部错误")
                res.detail = "%s: %s" % (type(exc).__name__, exc)

            if got is None or not got.ok:
                res.error_kind = res.error_kind or getattr(got, "error_kind", "内部错误")
                res.detail = res.detail or getattr(got, "detail", "")
                await self.store.save_sync_error(user_id, platform,
                                                 res.error_kind, res.detail)
                res.duration_ms = int((time.monotonic() - started) * 1000)
                if self.recorder:
                    self.recorder.event("sync.fail", user_id=user_id, platform=platform,
                                        ok=False, error_kind=res.error_kind,
                                        detail=res.detail,
                                        duration_ms=res.duration_ms)
                return res

            added = await self.store.upsert_submissions(user_id, platform, got.items)

            # 比赛记录（和提交是两条独立的流，单独抓、单独记）
            #
            # ⚠️ 这里原来有个**静默分支**：`cgot.ok and cgot.items` 不成立、
            # 但 `cgot.ok` 为真（也就是"抓成功了但是空的"）时，
            # 既不计入结果也不记日志 —— 表现是「比赛 0 场」而且**毫无线索**。
            # 端到端测试就是这么抓到的。现在空和非空都要有交代。
            contests_added = 0
            if with_contests and getattr(adapter, "supports_contests", False):
                try:
                    cgot = await adapter.fetch_contests(handle, client)
                    if cgot.ok:
                        if cgot.items:
                            contests_added = await self.store.upsert_contests(
                                user_id, platform, cgot.items)
                        elif self.recorder:
                            # 抓成功但零条 —— 这是**正常情况**（比如从没打过 rated），
                            # 但也要留一行，免得和"没抓"混起来
                            self.recorder.event(
                                "sync.contests_empty", user_id=user_id,
                                platform=platform, detail="抓取成功但该账号没有比赛记录")
                    else:
                        # 比赛抓不到**不算整次失败** —— 提交已经拿到了。
                        # 但要记下来，不能装作没这回事。
                        if self.recorder:
                            self.recorder.event(
                                "sync.contests_fail", user_id=user_id, platform=platform,
                                ok=False,
                                error_kind=getattr(cgot, "error_kind", "解析失败"),
                                detail=cgot.detail)
                except Exception as exc:
                    if self.recorder:
                        self.recorder.event("sync.contests_fail", user_id=user_id,
                                            platform=platform, ok=False,
                                            error_kind="内部错误", detail=str(exc))

            # **成功了才推断点**
            await self.store.save_sync_ok(user_id, platform, last_epoch=got.cursor)
            res.ok = True
            res.submissions = added
            res.contests = contests_added
            res.total_submissions = await self.store.count_submissions(user_id, platform)
            res.duration_ms = int((time.monotonic() - started) * 1000)

            if self.recorder:
                self.recorder.event("sync.ok", user_id=user_id, platform=platform,
                                    duration_ms=res.duration_ms,
                                    added=added, contests=contests_added,
                                    truncated=bool(getattr(got, "truncated", False)))
            return res

    async def ensure_problem_bank(self, platform: str = "codeforces",
                                  force: bool = False) -> tuple[bool, str, int]:
        """确保题库标注可用。**题库是全局的**（同一道题的难度标签对所有人都一样），
        所以只需要拉一次。

        为什么要做这个
        --------------
        `sync_problems` 原来**从来没被任何命令调用过** —— 于是题库永远是空的，
        连带这些全部失效：
          * 按标签的强弱分析
          * 难度回避判定（整个产品的卖点之一）
          * 候选池 → `/xcpc 方案` 没有任何题可推

        也就是说**功能写了但不可达**。这种"实现了但接不出来"的缺口最隐蔽：
        代码是好的、测试是绿的，但用户永远用不到。

        所以接进 `sync()`：**题库空的时候自动拉一次**。
        `force=True` 时无条件重拉（给 `/xcpc 题库` 命令用）。
        """
        if not force:
            try:
                n = await self.store.count_problems(platform)
                if n > 0:
                    return True, "已有 %d 题，跳过" % n, 0
            except Exception:
                pass
        return await self.sync_problems(platform)

    async def sync(self, user_id: str, platforms: list[str] | None = None,
                   with_contests: bool = True) -> SyncReport:
        """同步多个平台。**顺序执行**（不是并发）——
        这些站点都有速率限制，并发只会一起被挡。"""
        if platforms is None:
            handles = await self.store.handles(user_id)
            platforms = [p for p, h in handles.items() if h]
        if not platforms:
            return SyncReport(results=[])

        report = SyncReport()
        for p in platforms:
            try:
                report.results.append(
                    await self.sync_one(user_id, p, with_contests=with_contests))
            except Exception as exc:                 # noqa: BLE001
                # 兜底：即使适配器写了 bug 也不能让整次同步炸掉
                r = PlatformResult(platform=p, ok=False, error_kind="内部错误",
                                   detail="%s: %s" % (type(exc).__name__, exc))
                report.results.append(r)
                if self.recorder:
                    self.recorder.event("sync.crash", user_id=user_id, platform=p,
                                        ok=False, error_kind="内部错误", detail=str(exc))

        # 顺手确保题库标注可用（**只在它是空的时候拉**）。
        #
        # 题库是全局的，不该按用户重复拉；但它决定了整个诊断能不能工作，
        # 所以不能指望用户记得手动触发。放在提交同步之后，
        # 因为**提交是主要目的，题库是附属** —— 题库拉失败不该让整次同步失败。
        #
        # 拉哪些：**给得出标注的**。
        #   * CF      —— 标签 + rating，最全，1 个请求
        #   * AtCoder —— 标题 + IRT 难度（**没有标签**，它的 API 里就没有），
        #                2 个请求。拉进来能把 AtCoder 的难度并进统计
        #   * QOJ     —— 只有题号和标题，判断不了任何事。**不拉**
        #                （见 `platforms/qoj.py` 里 `fetch_problems` 的注释）
        #   * 洛谷     —— 标签 + 1-7 档难度，**两样都给得出来**，但全量是
        #                354 个请求、十几分钟（`perPage` 固定 50）。
        #                放进每次同步的必经路径不可接受 —— 它走
        #                `/xcpc 题库 luogu`（`force=True`）。
        for p in ("codeforces", "atcoder"):
            if p not in platforms:
                continue
            try:
                okb, msgb, addedb = await self.ensure_problem_bank(p)
                prev = report.bank or {}
                report.bank = {"ok": okb, "detail": msgb, "added": addedb,
                               "platform": p,
                               "prev": prev or None}
            except Exception as exc:                 # noqa: BLE001
                report.bank = {"ok": False, "detail": str(exc), "added": 0,
                               "platform": p}
                if self.recorder:
                    self.recorder.event("sync.bank_fail", user_id=user_id, ok=False,
                                        error_kind="内部错误", detail=str(exc))
        return report

    async def sync_problems(self, platform: str) -> tuple[bool, str, int]:
        """拉题库标注（**全局共享**，不属于任何用户）。

        题库标注对所有人一样，所以没必要按用户重复拉。
        """
        import time
        started = time.monotonic()
        adapter = self._adapter_factory(platform)
        if not getattr(adapter, "supports_problems", False):
            return False, "这个平台不支持题库标注", 0
        client = httpm.HttpClient(platform=platform, recorder=self.recorder,
                                  **self._client_kwargs(platform))
        try:
            got = await adapter.fetch_problems(client)
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc), 0
        if not got.ok:
            return False, "%s：%s" % (got.error_kind, got.detail), 0
        added = await self.store.upsert_problems(platform, got.items)
        if self.recorder:
            self.recorder.event("sync.problems", platform=platform,
                                added=added, total=len(got.items),
                                duration_ms=int((time.monotonic() - started) * 1000))
        return True, "ok", added
