#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/log.py 的自测 —— 重点在**掩码**（日志泄漏凭据是不可接受的）。

跑法：python tests/test_log.py
"""

from __future__ import annotations

import io
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

from core import log as logm  # noqa: E402

PASS = 0
FAIL = 0
SECRETS = ("s3cr3t-value", "abc123deadbeef", "9713b1", "mypassword")


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  \u2713 %s" % label)
    else:
        FAIL += 1
        print("  \u2717 %s%s" % (label, ("  -> " + detail) if detail else ""))


def no_secret(label: str, text: str) -> None:
    """断言输出里**不含任何**测试用密钥。"""
    hit = [s for s in SECRETS if s in (text or "")]
    check(label, not hit, "泄漏了 %s  原文=%r" % (hit, (text or "")[:200]))


# ---------------------------------------------------------------------------
# 1. 掩码
# ---------------------------------------------------------------------------

def test_mask_text() -> None:
    print("\n[1] 文本掩码")

    cases = [
        ("HTTP 头 Cookie", "Cookie: SESSDATA=s3cr3t-value; Path=/"),
        ("小写 cookie", "cookie=abc123deadbeef"),
        ("Set-Cookie", "Set-Cookie: bili_jct=abc123deadbeef; HttpOnly"),
        ("password", "password=mypassword&username=me"),
        ("_token", '_token: "abc123deadbeef"'),
        ("Authorization", "Authorization: Bearer abc123deadbeef"),
        ("查询串里的 token", "https://x/api?token=abc123deadbeef&page=1"),
        ("洛谷 C3VK", "C3VK=9713b1; path=/; max-age=300"),
    ]
    for name, raw in cases:
        no_secret("掩码：%s" % name, logm.mask_text(raw))

    # 洛谷那种 JS 写 cookie 的形式
    js = 'window.document.cookie="C3VK=9713b1; path=/; max-age=300;"'
    out = logm.mask_text(js)
    no_secret("掩码：JS 里写 cookie（洛谷挑战页）", out)

    # 不能把正常内容也吃掉
    check("不误伤普通文本",
          logm.mask_text("比赛 CF Round 1024 过题 3") == "比赛 CF Round 1024 过题 3")
    check("不误伤数字", "12345" in logm.mask_text("submission 12345 accepted"))
    check("空输入不崩", logm.mask_text(None) == "" and logm.mask_text("") == "")


def test_mask_mapping() -> None:
    print("\n[2] 结构化字段掩码")
    data = {
        "platform": "qoj",
        "Cookie": "SESSDATA=s3cr3t-value",
        "nested": {"password": "mypassword", "keep": "ok"},
        "list": [{"_token": "abc123deadbeef"}, "plain"],
        "note": "header 里有 cookie=abc123deadbeef",
    }
    out = logm.mask_mapping(data)
    no_secret("dict 掩码后无密钥", repr(out))
    check("非敏感字段保留", out["platform"] == "qoj" and out["nested"]["keep"] == "ok")
    check("list 里的也盖掉", out["list"][0]["_token"] == logm.MASK)

    hdrs = logm.headers_for_log({
        "Cookie": "SESSDATA=s3cr3t-value",
        "User-Agent": "Mozilla/5.0",
        "Set-Cookie": "bili_jct=abc123deadbeef",
    })
    no_secret("headers 掩码后无密钥", repr(hdrs))
    check("User-Agent 保留", hdrs.get("User-Agent") == "Mozilla/5.0")
    check("空 headers 不崩", logm.headers_for_log(None) == {})


# ---------------------------------------------------------------------------
# 3. 事件写入与环形缓冲
# ---------------------------------------------------------------------------

def test_recorder() -> None:
    print("\n[3] 事件写入")
    tmp = tempfile.mkdtemp(prefix="xcpc_log_")
    path = os.path.join(tmp, "sub", "xcpc.log")   # 故意多一层不存在的目录

    rec = logm.Recorder(path, level="info", to_astrbot=False)
    ok, detail = rec.open()
    check("日志目录自动创建并打开", ok, detail)
    check("日志文件已生成", os.path.exists(path))

    line = rec.event("sync.start", user_id="qq1001", platform="codeforces")
    check("event 返回写入的那行", "sync.start" in line)
    check("含 user_id", "user=qq1001" in line)
    check("含平台", "pf=codeforces" in line)

    # 失败事件必须带分类
    fail = rec.event("sync.fail", user_id="qq1001", platform="luogu",
                     ok=False, error_kind="凭据失效", detail="cookie expired")
    check("失败事件带分类", "[凭据失效]" in fail)

    # 非法分类要归一化，而不是原样写进去（否则没法统计）
    bad = rec.event("sync.fail", ok=False, error_kind="我自己瞎编的分类")
    check("非法失败分类被归一化", "[内部错误]" in bad and "瞎编" not in bad)

    # HTTP 事件
    h = rec.http("POST", "https://qoj.ac/login?token=abc123deadbeef",
                 status=200, duration_ms=123, resp_bytes=12224,
                 user_id="qq1001", platform="qoj", note="challenge:C3VK")
    no_secret("HTTP 日志掩码了 URL 里的 token", h)
    check("HTTP 日志含状态码与耗时", "-> 200" in h and "123ms" in h)

    # 字段里的密钥也要盖
    ev = rec.event("auth.login", platform="qoj", ok=True,
                   headers={"Cookie": "SESSDATA=s3cr3t-value"})
    no_secret("event 的 fields 也掩码", ev)

    rec.close()

    # 文件内容同样不能有密钥
    raw = io.open(path, encoding="utf-8").read()
    no_secret("落盘内容无密钥", raw)
    check("落盘确实写了东西", len(raw) > 100, "%d 字节" % len(raw))


# ---------------------------------------------------------------------------
# 4. tail() 的按用户过滤
# ---------------------------------------------------------------------------

def test_tail() -> None:
    print("\n[4] /日志 的读取与隔离")
    tmp = tempfile.mkdtemp(prefix="xcpc_log2_")
    rec = logm.Recorder(os.path.join(tmp, "x.log"), to_astrbot=False)
    rec.open()

    rec.event("sync.start", user_id="qq1001", platform="codeforces")
    rec.event("sync.start", user_id="qq2002", platform="atcoder")
    rec.event("boot", detail="全局事件，没有 user")
    rec.event("sync.start", user_id="qq1001", platform="luogu")

    all_lines = rec.tail(50)
    check("不带 user_id 能看全部", len(all_lines) == 4, "实际 %d" % len(all_lines))

    mine = rec.tail(50, user_id="qq1001")
    check("按 user_id 过滤掉别人的", all("qq2002" not in x for x in mine), repr(mine))
    check("自己的还在", any("qq1001" in x for x in mine))
    check("全局事件也保留", any("boot" in x for x in mine))

    check("n 生效", len(rec.tail(2)) == 2)
    check("n 有下限保护", len(rec.tail(0)) >= 1)
    check("n 有上限保护", len(rec.tail(99999)) <= 200)

    rec.close()


# ---------------------------------------------------------------------------
# 5. 时间
# ---------------------------------------------------------------------------

def test_time() -> None:
    print("\n[5] 时间")
    n = logm.now_cn()
    check("带时区", n.tzinfo is not None)
    check("恒定 UTC+8", n.utcoffset().total_seconds() == 8 * 3600)
    import re
    check("stamp 格式", bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", logm.stamp())))
    check("ERROR_KINDS 是固定枚举", len(logm.ERROR_KINDS) >= 6 and
          "凭据失效" in logm.ERROR_KINDS)


def main() -> int:
    print("=" * 60)
    print("core/log.py 自测")
    print("=" * 60)
    test_mask_text()
    test_mask_mapping()
    test_recorder()
    test_tail()
    test_time()
    print("\n" + "=" * 60)
    print(" 通过 %d ｜ 失败 %d" % (PASS, FAIL))
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
