#!/usr/bin/env python
"""实测后端 API 的安全响应头，写成证据 JSON（SEC-01，2026-10-28）。

    python -I scripts/verify/capture_security_headers.py \
        --base http://127.0.0.1:8000/api \
        --out docs/reviews/evidence/security-headers-20261028.json

审计当时对"Spring Security 默认头是否真的下发"的判断来自框架语义，
不是实测响应。这个脚本把那条判断变成实测：对三类响应各抓一次头
（200、401、404），逐项断言。

为什么必须抓 401：`headers.add_header`（nginx 侧）与 Spring 的
`HeaderWriterFilter` 在错误响应上的行为不同——nginx 不带 `always` 时
4xx 就没有安全头，而错误页恰恰是最容易被嵌进 iframe 的页面。
只测 200 会漏掉这一类。

**范围声明**：本脚本只测 **API 层**（Spring Security）。nginx 静态层的
头需要跑容器才能抓到，而当前 Docker 数据根在 C 盘、任务书要求阻塞
docker build/up。静态层的配置由 `test_security_headers.py` 做静态契约
检查，运行时证据**本轮没有**——不得据此声称"网页已带 CSP"。
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 每一项：(header 名, 断言方式, 期望)
#: 断言方式 "equals" 精确匹配；"contains" 子串。
EXPECTED = [
    ("Content-Security-Policy", "contains", "default-src 'none'"),
    ("Content-Security-Policy", "contains", "frame-ancestors 'none'"),
    ("X-Content-Type-Options", "equals", "nosniff"),
    ("X-Frame-Options", "equals", "DENY"),
    ("Referrer-Policy", "equals", "no-referrer"),
    ("Permissions-Policy", "contains", "camera=()"),
    ("Permissions-Policy", "contains", "microphone=()"),
    ("Cross-Origin-Opener-Policy", "equals", "same-origin"),
    ("Cross-Origin-Resource-Policy", "equals", "same-origin"),
]

#: 反向断言：API 的 CSP 里不该出现的（出现说明有人把页面 CSP 抄了过来）
FORBIDDEN = [
    ("Content-Security-Policy", "unsafe-inline"),
    ("Content-Security-Policy", "unsafe-eval"),
]

PROBES = [
    ("health-200", "GET", "/health", 200),
    ("not-found-401", "GET", "/definitely-not-a-route", 401),
    ("preflight-retired-port", "OPTIONS", "/health", None),
]


def capture(base: str, method: str, path: str, origin: str | None = None):
    headers = {"Origin": origin} if origin else {}
    if method == "OPTIONS":
        headers["Access-Control-Request-Method"] = "GET"
    return requests.request(method, base.rstrip("/") + path, headers=headers, timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000/api")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    records = []
    failures = []

    for name, method, path, expected_status in PROBES:
        origin = "http://localhost:5173" if method == "OPTIONS" else None
        try:
            resp = capture(args.base, method, path, origin)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: 请求失败 {type(exc).__name__}: {exc}")
            continue

        got = {k.lower(): v for k, v in resp.headers.items()}
        checks = []
        for header, mode, want in EXPECTED:
            actual = got.get(header.lower())
            if mode == "equals":
                ok = actual == want
            else:
                ok = actual is not None and want in actual
            checks.append({"header": header, "expect": want, "actual": actual, "ok": ok})
            if not ok:
                failures.append(f"{name}: {header} 期望 {mode} {want!r}，实际 {actual!r}")
        for header, forbidden in FORBIDDEN:
            actual = got.get(header.lower()) or ""
            ok = forbidden not in actual
            checks.append({"header": header, "forbid": forbidden,
                           "actual": actual, "ok": ok})
            if not ok:
                failures.append(f"{name}: {header} 含禁止项 {forbidden!r}")

        if expected_status is not None and resp.status_code != expected_status:
            failures.append(f"{name}: 状态码期望 {expected_status}，实际 {resp.status_code}")

        records.append({
            "probe": name,
            "method": method,
            "path": path,
            "origin": origin,
            "status": resp.status_code,
            "headers": {k: v for k, v in got.items()
                        # 只保留安全相关头，避免把 Date/Server 之类噪声写进证据
                        if k.startswith(("content-security", "x-", "referrer",
                                         "permissions", "cross-origin", "vary"))},
            "checks": checks,
            "passed": all(c["ok"] for c in checks),
        })

    # CORS 拒绝：退役端口 3001 的预检必须被拒
    try:
        rejected = capture(args.base, "OPTIONS", "/health", "http://localhost:3001")
        cors_ok = rejected.status_code == 403
        records.append({
            "probe": "preflight-retired-port-3001",
            "method": "OPTIONS",
            "path": "/health",
            "origin": "http://localhost:3001",
            "status": rejected.status_code,
            "passed": cors_ok,
            "checks": [{"header": "CORS", "expect": "403 (retired origin rejected)",
                        "actual": rejected.status_code, "ok": cors_ok}],
        })
        if not cors_ok:
            failures.append(
                f"preflight-retired-port-3001: 期望 403，实际 {rejected.status_code}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"preflight-retired-port-3001: {type(exc).__name__}: {exc}")

    payload = {
        "base": args.base,
        "scope": "API layer only (Spring Security). nginx static layer NOT captured: "
                 "requires a container run, blocked by the disk gate.",
        "records": records,
        "failures": failures,
        "passed": not failures,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    print(f"==> 抓取 {len(records)} 个响应")
    for record in records:
        print(f"  {'PASS' if record['passed'] else 'FAIL'} {record['probe']} "
              f"HTTP {record['status']}")
    if failures:
        print(f"\n{len(failures)} 项不满足：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        print(f"\n结果已写入 {args.out}")
        return 1
    print(f"\n全部通过；结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
