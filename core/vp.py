#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VP 场次候选（v0.6.1）。

为什么需要这个模块
------------------
v0.6.0 的 prompt 里，VP 那条规则只能写泛指（「打一场 CF Div.2 的 VP」），
并且**明令禁止模型写具体场次**。原因是库里 `contests` 表只存
**已经参加过**的比赛 —— 没有任何「还没打、但可以打」的候选。
模型没有这个数据，写具体场次就一定是编的。

用户原话：「我认为把量拉起来的方法应该是 vp，而不是这样刷题，
刷题应该更具针对性，针对算法专题思维啥的」—— VP 现在是量的主力，
所以「打哪一场」必须由数据回答，不能靠模型猜。

怎么做的
--------
两个公开数据源，都不需要登录：

  · CF      `codeforces.com/api/contest.list?gym=false`（实测 410KB / 2155 场）
  · AtCoder `kenkoooo.com/atcoder/resources/contests.json`（实测 1.0MB）

另外各抓一次**他自己的 rating**，因为「哪一场合适」完全取决于这个：

  · CF      `user.info?handles=<handle>` → `rating` / `maxRating`
  · AtCoder `atcoder.jp/users/<handle>/history/json` → 最后一场的 `NewRating`

实测（2026-10-10）：他 CF **1713**（最高 1826，expert）、AtCoder **1225**。
这两个数决定了推荐 —— CF 该打 Div.2 / Educational（Div.3 偏简单、
Div.1 打不动），AtCoder 该打 ABC（ARC 的 1200+ 他刚好在门槛上）。

★ 四条设计决定：

1. **缓存进库，不是每次现拉。** 1.4MB / 两次外部请求不该挂在
   `/xcpc` 的关键路径上，而且 CF 限速 1 req/s。TTL 24 小时。
2. **失败也要记下时间。** 只记成功的话，抓不到就会**每次**重试，
   把一个"偶尔失败"变成"每次都慢三秒"。所以 `vp_cache` 一行的
   `fetched_at` 是**尝试**时间，`ok` 才是结果。
3. **整场都被做过的要排除。** 他平时就在刷题，很可能**没参加某场
   比赛却已经 AC 了里面三四道题** —— 拿这种场次做 VP 是自欺欺人。
   规则：这场里他已经 AC ≥ 2 道就跳过（`SPOIL_LIMIT`）。
4. **推荐失败绝不能让方案失败。** 本模块对外的方法**永不抛异常**，
   抓不到就返回空表 / `ok=False`，方案照常出，只是少一段。
   VP 是锦上添花，不是主流程。
"""

from __future__ import annotations

import time

CF_LIST_URL = "https://codeforces.com/api/contest.list?gym=false"
AT_LIST_URL = "https://kenkoooo.com/atcoder/resources/contests.json"

CF_INFO_URL = "https://codeforces.com/api/user.info?handles=%s"
AT_HISTORY_URL = "https://atcoder.jp/users/%s/history/json"

CF = "codeforces"
AT = "atcoder"

#: 缓存多久算过期（秒）。CF 每天都有新场次，一天一刷够了。
CACHE_TTL = 24 * 3600

#: 这场里他 AC 了几道以上就算「已经被剧透」→ 不推荐做 VP。
SPOIL_LIMIT = 2

#: 一场 VP 最多留多少条候选（只留合适赛制的，实际远小于这个数）。
MAX_KEEP = 300

_AC = ("OK", "AC", "ACCEPTED")


# ======================================================================
# 赛制识别
# ======================================================================
#: 顺序**有讲究**：先匹配到的赢。Educational 必须排在 Div. 2 前面，
#: 因为它的名字长这样 ——「Educational Codeforces Round 170 (Rated for Div. 2)」，
#: 反过来的话每一场 Educational 都会被认成 Div. 2。
_CF_RULES = (
    ("April Fools", "April Fools"),
    ("Educational", "Educational"),
    ("Global Round", "Global"),
    ("CodeTON", "CodeTON"),
    ("Good Bye", "Good Bye"),
    ("Hello ", "Hello"),
    ("Div. 1 + Div. 2", "Div. 1+2"),
    ("Div. 1", "Div. 1"),
    ("Div. 2", "Div. 2"),
    ("Div. 3", "Div. 3"),
    ("Div. 4", "Div. 4"),
)


def cf_division(name: str) -> str:
    """从 CF 的比赛名认出赛制。认不出返回「其它」。"""
    n = name or ""
    for needle, div in _CF_RULES:
        if needle in n:
            return div
    return "其它"


def at_division(contest_id: str) -> str:
    """从 AtCoder 的 contest id 认出系列。

    id 是 `abc470` / `arc230` / `agc067` 这种。
    ⚠️ 不能只看 `startswith("abc")` —— `abs`（AtCoder Beginner Contest
    的练习版）和 `practice` 系列 id 短、前缀会撞。用**数字**当分隔。
    """
    cid = (contest_id or "").strip().lower()
    for pref, name in (("abc", "ABC"), ("arc", "ARC"), ("agc", "AGC")):
        tail = cid[len(pref):]
        if cid.startswith(pref) and tail.isdigit():
            return name
    return "其它"


def _int(v, default=0) -> int:
    """能转就转，转不了给 default。

    ★ 必须容错：`rating` 是从外部 JSON 里挖出来的，
    CF 的字段在某些情况下会是 null、AtCoder 的 history 里混着
    `IsRated: false` 的场次。第一版直接 `int(rating or 0)`，
    遇到 `"abc"` 就 `ValueError: invalid literal for int()`。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


#: 按「离他的水平多远」排出来的优先级。
#: 越靠前越先取。**不是**难度排序，是「这一场对他有多少训练价值」。
def target_order(platform: str, rating) -> tuple:
    """给定他的 rating，返回该优先打的赛制顺序。"""
    r = _int(rating, 0)
    # ★ rating 抓不到时是 0 —— **不能让它落进最高那一档**。
    # 第一次写成 `if r < 1400 / if r < 1700 / else 2000+`，于是
    # rating=0 直接拿到「Div. 1+2 / Global / CodeTON」——
    # 抓取失败会把最难的一档推荐给他，比不推荐还糟。
    # 未知时走保守档：Div. 3 / Div. 2。
    if not r:
        return ("Div. 3", "Div. 2") if platform == CF else ("ABC",)
    if platform == CF:
        if r and r < 1400:
            return ("Div. 4", "Div. 3", "Div. 2")
        if r and r < 1700:
            return ("Div. 3", "Div. 2", "Educational", "Div. 4")
        if r and r < 2000:
            # 1713 落在这里：Div.2 正合适，Educational 同档，
            # Div.1+2 / Global / CodeTON 的 A-C 也是他能做的，Div.3 只用来练手速
            return ("Div. 2", "Educational", "Div. 1+2", "Global",
                    "CodeTON", "Div. 3")
        return ("Div. 1+2", "Global", "CodeTON", "Div. 2", "Educational")
    if platform == AT:
        # ★ AtCoder 的分段按**赛事的 rated 下界**切，不是等距切。
        # ABC 是 0-1999 人人能打；ARC 名义上 0-2799，但它的 A 题
        # 就相当于 CF 1600 上下 —— 900 分的人进去大概率只做得出一道，
        # 那不叫 VP，叫坐牢。所以 ARC 只在 1200+ 才推。
        if r and r < 1200:
            return ("ABC",)
        if r and r < 1600:
            # 1225 落在这里：ABC 是主场，ARC 刚好在门槛上
            return ("ABC", "ARC")
        return ("ABC", "ARC", "AGC")
    return ()


def suitable(platform: str, division: str, order: tuple) -> bool:
    return division in order


# ======================================================================
# 解析
# ======================================================================
def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def parse_cf(payload) -> list[dict]:
    """把 `contest.list` 的 result 变成我们要的那几列。

    ⚠️ **不能按 `type` 过滤**：CF 把 Div. 3 的场次 `type` 标成 `ICPC`，
    而 Div. 2 标成 `CF` —— 这一列跟赛制没关系。真正的 ICPC/VK Cup
    靠名字认（会落到「其它」，`target_order` 里没有，自然被排除）。
    """
    out = []
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("phase") or "") != "FINISHED":
            continue
        cid = row.get("id")
        if cid in (None, ""):
            continue
        dur = _int(row.get("durationSeconds"))
        start = _int(row.get("startTimeSeconds"))
        # 时长为 0 或离谱的（gym 里有些挂名场次）不要
        if not (600 <= dur <= 6 * 3600) or start <= 0:
            continue
        out.append({
            "contest_id": str(cid),
            "name": str(row.get("name") or "").strip(),
            "division": cf_division(str(row.get("name") or "")),
            "start_epoch": start,
            "duration_sec": dur,
        })
    return out


def parse_at(payload) -> list[dict]:
    """把 kenkoooo 的 contests.json 变成我们要的那几列。

    ⚠️ 那份数据里混着**练习用**的假比赛：`APG4b` 的时长是
    `3153600000` 秒（一百年），`start_epoch_second` 是 0。
    所以时长和开始时间必须一起卡。
    """
    out = []
    for row in payload or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        div = at_division(cid)
        if div == "其它":
            continue
        dur = _int(row.get("duration_second"))
        start = _int(row.get("start_epoch_second"))
        if not (600 <= dur <= 6 * 3600) or start <= 0:
            continue
        out.append({
            "contest_id": cid,
            "name": str(row.get("title") or cid).strip(),
            "division": div,
            "start_epoch": start,
            "duration_sec": dur,
        })
    return out


# ======================================================================
# 挑
# ======================================================================
def solved_per_contest(platform: str, problem_keys) -> dict:
    """`{contest_id: 他 AC 了几道}` —— 用来排除已经被剧透的场次。

    ★ **两个平台的 key 格式不一样，而且都不能用前缀匹配**：

      · CF      `CF:1123A`  → 去掉 `CF:` 后的**前导数字**才是 contest id。
        用 `startswith("CF:1123")` 会把 `CF:11230A`（如果存在）算进来。
      · AtCoder `ATC:abc470_a` → contest id 是**最后一个下划线之前**那段。

    两个都拆得出来才算，拆不出来的忽略（宁可不排除，也不要误排除）。
    """
    out: dict[str, int] = {}
    for key in problem_keys or []:
        k = str(key or "")
        cid = ""
        if platform == CF and k.startswith("CF:"):
            tail = k[3:]
            digits = ""
            for ch in tail:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            cid = digits
        elif platform == AT and k.startswith("ATC:"):
            tail = k[4:]
            if "_" in tail:
                cid = tail.rsplit("_", 1)[0]
        if cid:
            out[cid] = out.get(cid, 0) + 1
    return out


def pick(rows, order, participated, solved, *,
         per_division: int = 2, limit: int = 8) -> list[dict]:
    """按赛制轮流取最近的几场。

    参数
    ----
    rows        `store.list_vp()` 出来的行（dict 或 sqlite3.Row 都行）
    order       `target_order()` 的结果，同时当白名单用
    participated 他参加过的 contest_id 集合（**参加过的不能当 VP**）
    solved      `solved_per_contest()` 的结果，用于排除已被剧透的场次

    ★ 为什么按赛制**轮流**取，而不是一把按时间倒序捞完：
    只按时间倒序的话，最近三个月如果连着办了 5 场 Div.3，
    出来的候选就全是 Div.3 —— 而候选的排列顺序本身就是给模型的暗示
    （v0.5.14 血泪：排列顺序错了，模型会照着排）。
    """
    def field(r, name, default=None):
        if isinstance(r, dict):
            return r.get(name, default)
        try:
            return r[name]
        except (KeyError, IndexError, TypeError):
            return getattr(r, name, default)

    order = tuple(order or ())
    if not order:
        return []
    part = {str(x) for x in (participated or ())}
    spoilt = solved or {}

    buckets: dict[str, list] = {}
    for r in rows or []:
        cid = str(field(r, "contest_id", "") or "")
        if not cid or cid in part:
            continue
        div = str(field(r, "division", "") or "")
        if div not in order:
            continue
        if int(spoilt.get(cid, 0)) >= SPOIL_LIMIT:
            continue
        buckets.setdefault(div, []).append(r)

    def norm(r) -> dict:
        return {
            "platform": str(field(r, "platform", "") or ""),
            "contest_id": str(field(r, "contest_id", "") or ""),
            "name": str(field(r, "name", "") or ""),
            "division": str(field(r, "division", "") or ""),
            "start_epoch": _int(field(r, "start_epoch", 0)),
            "duration_sec": _int(field(r, "duration_sec", 0)),
        }

    # 每个桶里按开始时间倒序（新的先）
    for div in buckets:
        buckets[div].sort(key=lambda r: -_int(field(r, "start_epoch", 0)))

    out: list[dict] = []
    for div in order:
        out.extend(norm(r) for r in buckets.get(div, [])[:per_division])

    # 不够就从剩下的补（同样是新的先）
    if len(out) < limit:
        seen = {x["contest_id"] for x in out}
        rest = []
        for div in order:
            for r in buckets.get(div, [])[per_division:]:
                cid = str(field(r, "contest_id", "") or "")
                if cid in seen:
                    continue
                seen.add(cid)
                rest.append(r)
        rest.sort(key=lambda r: -_int(field(r, "start_epoch", 0)))
        out.extend(norm(r) for r in rest[:limit - len(out)])

    return out[:limit]


def contest_url(platform: str, contest_id: str) -> str:
    if platform == CF:
        return "https://codeforces.com/contest/%s" % contest_id
    if platform == AT:
        return "https://atcoder.jp/contests/%s/tasks" % contest_id
    return ""


def day(epoch) -> str:
    """`2026-09-02`。本地时区 —— 这些日期只是给人看的。"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(_int(epoch)))
    except (ValueError, OSError):
        return "?"


def describe(items) -> str:
    """给人也给模型看的一段。每场两行，第二行是链接。"""
    lines = []
    for it in items or []:
        minutes = max(1, _int(it.get("duration_sec")) // 60)
        lines.append("  · %s（%s）—— %s，时长 %d 分钟"
                     % (it.get("name") or "", it.get("division") or "",
                        day(it.get("start_epoch")), minutes))
        url = contest_url(str(it.get("platform") or ""),
                          str(it.get("contest_id") or ""))
        if url:
            lines.append("    %s" % url)
    return "\n".join(lines)


# ======================================================================
# 抓（全部永不抛）
# ======================================================================
def _client(platform: str, recorder=None):
    try:
        from . import http as httpm
        return httpm.HttpClient(platform=platform, recorder=recorder)
    except Exception:                                       # noqa: BLE001
        return None


async def _get_json(client, url: str):
    """返回 `(payload, err)`。**不抛。**"""
    if client is None:
        return None, "拿不到 HTTP 客户端"
    try:
        resp = await client.get(url)
        if resp.status == 429:
            return None, "被限流了（HTTP 429）"
        if resp.status >= 400:
            return None, "HTTP %d" % resp.status
        return resp.json(), ""
    except Exception as exc:                                # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)


async def _cf_rating(client, handle: str) -> int:
    """他 CF 的当前 rating。抓不到返回 0（`target_order` 会退回保守档）。"""
    if not handle:
        return 0
    payload, err = await _get_json(client, CF_INFO_URL % handle)
    if err or not isinstance(payload, dict):
        return 0
    rows = payload.get("result")
    if not isinstance(rows, list) or not rows:
        return 0
    return _int(rows[0].get("rating"))


async def _at_rating(client, handle: str) -> int:
    """他 AtCoder 的当前 rating。取**最后一场 rated**的 NewRating。

    ⚠️ 不能取 `d[-1]`：history 里混着 `IsRated: false` 的场次，
    那些的 `NewRating` 是没变的旧值 —— 大部分时候运气好也一样，
    但只要最后一场恰好是 unrated 就会拿到过期数字。
    """
    if not handle:
        return 0
    payload, err = await _get_json(client, AT_HISTORY_URL % handle)
    if err or not isinstance(payload, list):
        return 0
    rated = [x for x in payload
             if isinstance(x, dict) and x.get("IsRated")]
    if not rated:
        return 0
    return _int(rated[-1].get("NewRating"))


async def refresh(store, platform: str, *, handle: str = "",
                  recorder=None, client=None) -> tuple:
    """抓一个平台并落库。返回 `(ok, detail, count)`。**不抛。**"""
    url = CF_LIST_URL if platform == CF else AT_LIST_URL
    parser = parse_cf if platform == CF else parse_at
    if client is None:
        client = _client(platform, recorder)

    payload, err = await _get_json(client, url)
    if err:
        await store.save_vp(platform, [], ok=False,
                            detail="抓比赛列表失败：%s" % err)
        return False, "抓比赛列表失败：%s" % err, 0

    items = parser(payload)
    if not items:
        # ★ 抓到了但解析出 0 条 —— 这是**页面结构变化**，不是"没有比赛"。
        # 当成成功会把空表缓存 24 小时，然后一整天都推荐不出东西。
        await store.save_vp(platform, [], ok=False,
                            detail="比赛列表解析出 0 条（对方可能改版了）")
        return False, "比赛列表解析出 0 条（对方可能改版了）", 0

    rating = 0
    try:
        if platform == CF:
            rating = await _cf_rating(client, handle)
        else:
            rating = await _at_rating(client, handle)
    except Exception:                                       # noqa: BLE001
        rating = 0

    # 只留合适赛制的，别把 2155 场全塞进库
    order = target_order(platform, rating)
    keep = [x for x in items if x["division"] in order]
    keep.sort(key=lambda x: -x["start_epoch"])
    keep = keep[:MAX_KEEP]

    await store.save_vp(platform, keep, ok=True, detail="", rating=rating)
    return True, "", len(keep)


async def ensure(store, *, handles=None, ttl: int = CACHE_TTL,
                 force: bool = False, recorder=None) -> dict:
    """缓存过期就刷。返回 `{platform: {"ok","detail","count","rating"}}`。**不抛。**

    ⚠️ **无论成功失败都写 `fetched_at`**（见 `store.save_vp`）——
    只在成功时写的话，抓一次失败就会让之后**每一次**调用都重试，
    把一个"偶尔慢"变成"每次都慢三秒"。
    """
    handles = handles or {}
    out = {}
    now = int(time.time())
    for platform in (CF, AT):
        state = None
        try:
            state = await store.vp_state(platform)
        except Exception:                                   # noqa: BLE001
            state = None
        fresh = (state is not None
                 and not force
                 and (now - _int(state.get("fetched_at"))) < int(ttl))
        if fresh:
            out[platform] = {
                "ok": bool(state.get("ok")),
                "detail": str(state.get("detail") or ""),
                "rating": _int(state.get("rating")),
                "count": -1,        # 没重抓，不知道条数
                "cached": True,
            }
            continue
        # ★ `refresh` 自己已经把网络错误都吃掉了，但这里**再包一层**：
        # 这个方法对外承诺「永不抛」，而它跑在 `/xcpc` 和 22:30 推送的
        # 关键路径上 —— 解析代码里任何一个手误都不该让今天的方案发不出去。
        try:
            ok, detail, count = await refresh(store, platform,
                                              handle=str(handles.get(platform) or ""),
                                              recorder=recorder)
        except Exception as exc:                            # noqa: BLE001
            ok, detail, count = False, "抓 VP 场次时出意外：%s" % exc, 0
            try:
                await store.save_vp(platform, [], ok=False, detail=detail)
            except Exception:                               # noqa: BLE001
                pass
        after = None
        try:
            after = await store.vp_state(platform)
        except Exception:                                   # noqa: BLE001
            after = None
        out[platform] = {
            "ok": ok, "detail": detail, "count": count,
            "rating": _int((after or {}).get("rating")) if after else 0,
            "cached": False,
        }
    return out


async def candidates(store, user_id: str, *, limit: int = 8) -> list[dict]:
    """给他挑几场还没打、也没被剧透的。**不抛**，失败返回 `[]`。

    每个平台各取一半左右，CF 在前（他 CF 场次多、rating 也更有参考价值）。
    """
    out: list[dict] = []
    for platform in (CF, AT):
        try:
            state = await store.vp_state(platform) or {}
            if not state.get("ok"):
                continue
            rows = await store.list_vp(platform)
            if not rows:
                continue
            cons = await store.list_contests(user_id, platform, limit=500)
            part = {str((c["contest_id"] if not isinstance(c, dict)
                         else c.get("contest_id")) or "") for c in cons}
            subs = await store.list_submissions(user_id, platform=platform,
                                                limit=100000)
            keys = [s["problem_key"] for s in subs
                    if str(s["verdict"] or "").upper() in _AC]
            order = target_order(platform, state.get("rating"))
            got = pick(rows, order, part, solved_per_contest(platform, keys),
                       per_division=2, limit=limit)
            for g in got:
                g["platform"] = platform
            out.extend(got)
        except Exception:                                   # noqa: BLE001
            continue
    return out[:limit]
