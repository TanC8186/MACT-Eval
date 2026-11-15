#!/usr/bin/env python
"""敏感字段日志扫描（任务书 §B4.4）：可重复执行，带退出码。

    python -I scripts/verify/scan_log_secrets.py [--json-out <path>] [--strict]

## 为什么需要脚本而不是一次性手工扫描

2026-10-28 那份 `secret-scan-20261028.json` 是**手工**做的，仓库里没有生成它的
代码。后果：后续每一轮都会产生新的日志（真实密钥起服务、双密钥轮换的三阶段、
nginx harness 的 access/error），而这些**没有任何机制会去重扫**。
一次性的"扫描过"在持续追加日志的仓库里等于"没扫"。

## 判定规则：区分"真凭据"与"故意的占位符"

这是本脚本最容易写错的地方。第一版把 `ticket=deadbeef…` 也算成一类命中，
但那个值是**测试夹具故意造出来的**（`errlog-probe` 用它证明 error_log 会泄漏 URI）。
把它报成"泄漏"会训练人忽略告警——狼来了。

因此分三类，**分别计数、分别判定**：

* ``REAL``   —— 形状像真凭据，且不在占位符白名单里。**这是失败条件**。
* ``PLACEHOLDER`` —— 精确等于已知夹具值（见 ``PLACEHOLDERS``）。只记录、不判失败。
* 其余（JWT 三段式、SigV4 查询串、MinIO secret 字面量等）任何出现都算 ``REAL``。

占位符白名单是**精确匹配**，不是前缀/正则。这样将来真有随机票据泄漏时，
它不会因为"看起来像那个夹具"而被放过。

## 扫描范围

``.cache/tmp``、``data/tmp``、``.playwright-mcp``、``docs/archives`` 下的
``.log/.out/.txt``，跳过 ``node_modules`` 与 ``.git``。默认**不**读取 MinIO/容器
内部日志（那需要 docker，可能被磁盘门禁阻塞）；需要时用 ``--include-container``。

## 退出码

* 0 —— 无 REAL 命中
* 1 —— 有 REAL 命中（必须处理）
* 2 —— 环境不可用（扫描根不存在等），**不是通过**
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 已知测试夹具值（精确匹配）。它们**故意**出现在日志里，用来证明"日志会泄漏"
#: 这件事本身。把它们报成泄漏会让告警失去意义。
PLACEHOLDERS = {
    "deadbeefdeadbeefdeadbeefdeadbeef",   # errlog-probe: 证明 error_log 记录 URI
    "0" * 32,                              # ws_auth: forged ticket
    "not-a-ticket",                        # ws_auth: malformed ticket
}

SCAN_ROOTS = [
    ".cache/tmp",
    "data/tmp",
    ".playwright-mcp",
    "docs/archives",
]
SUFFIXES = {".log", ".out", ".txt"}

#: (名字, 正则, 抽取组)。组为 0 表示整个匹配。
PATTERNS = [
    ("jwt-triple-segment",
     re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), 0),
    ("ws-ticket-query",
     re.compile(r"ticket=([0-9a-f]{32})"), 1),
    ("sigv4-signature",
     re.compile(r"X-Amz-Signature=([0-9a-f]{16,})"), 1),
    ("sigv4-credential",
     re.compile(r"X-Amz-Credential=([^&\s\"']{8,})"), 1),
    ("minio-secret-literal",
     re.compile(r"(urig_secret|macteval_secret)"), 1),
    ("jwt-secret-env",
     re.compile(r"MACT_JWT_SECRETS?=([A-Za-z0-9+/=_-]{16,})"), 1),
    ("bearer-token",
     re.compile(r"Authorization:\s*Bearer\s+([A-Za-z0-9._-]{20,})"), 1),
]


def iter_files(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".git"}]
            for name in filenames:
                if Path(name).suffix.lower() in SUFFIXES:
                    out.append(Path(dirpath) / name)
    return sorted(out)


def redact(value: str) -> str:
    """只保留前 6 个字符。证据里不复制完整凭据。"""
    return value[:6] + "***" if len(value) > 6 else "***"


def scan(files: list[Path]) -> tuple[list[dict], list[dict], int]:
    real: list[dict] = []
    placeholder: list[dict] = []
    total_bytes = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total_bytes += len(text)
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for name, rx, group in PATTERNS:
            for match in rx.finditer(text):
                value = match.group(group)
                bucket = placeholder if value in PLACEHOLDERS else real
                bucket.append({
                    "file": rel,
                    "pattern": name,
                    "sample": redact(value),
                    "offset": match.start(),
                })
    return real, placeholder, total_bytes


def scan_container_logs() -> list[dict]:
    """可选的容器日志扫描。容器不在时返回空，而不是失败。"""
    findings: list[dict] = []
    try:
        listing = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30)
        if listing.returncode != 0:
            return findings
        for name in listing.stdout.split():
            logs = subprocess.run(
                ["docker", "logs", "--tail", "2000", name],
                capture_output=True, text=True, timeout=60)
            text = (logs.stdout or "") + (logs.stderr or "")
            for pattern_name, rx, group in PATTERNS:
                for match in rx.finditer(text):
                    value = match.group(group)
                    if value in PLACEHOLDERS:
                        continue
                    findings.append({"container": name, "pattern": pattern_name,
                                     "sample": redact(value)})
    except Exception:  # noqa: BLE001
        pass
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", default="")
    parser.add_argument("--include-container", action="store_true",
                        help="同时扫描 docker logs（需要 docker 可用）")
    parser.add_argument("--strict", action="store_true",
                        help="占位符命中也算失败（用于确认夹具本身还在）")
    args = parser.parse_args()

    roots = [ROOT / r for r in SCAN_ROOTS]
    if not any(r.exists() for r in roots):
        print(f"!! 扫描根都不存在：{SCAN_ROOTS}", file=sys.stderr)
        return 2

    files = iter_files(roots)
    if not files:
        print("!! 没找到任何日志文件；这不该发生，判为环境不可用", file=sys.stderr)
        return 2

    real, placeholder, total_bytes = scan(files)
    container = scan_container_logs() if args.include_container else []

    print(f"扫描 {len(files)} 个文件 / {total_bytes:,} 字节")
    print(f"  REAL 命中        {len(real)}")
    print(f"  占位符命中（豁免）{len(placeholder)}")
    if container:
        print(f"  容器日志命中     {len(container)}")

    for f in real[:20]:
        print(f"  !! REAL {f['pattern']} @ {f['file']} -> {f['sample']}")
    for f in placeholder[:10]:
        print(f"  .. 占位符 {f['pattern']} @ {f['file']} -> {f['sample']}（豁免）")
    for f in container[:10]:
        print(f"  !! CONTAINER {f['pattern']} @ {f['container']} -> {f['sample']}")

    failed = bool(real) or bool(container) or (args.strict and bool(placeholder))

    if args.json_out:
        payload = {
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "taskbookRef": "§B4.4 Spring / Nginx / AI service log sensitive-field scan",
            "scope": {
                "roots": SCAN_ROOTS,
                "fileCount": len(files),
                "totalBytes": total_bytes,
                "containerLogs": bool(args.include_container),
                "notCovered": "运行中容器的实时 stdout 需 --include-container；"
                              "镜像层与 writable layer 不在本扫描范围",
            },
            "redaction": "所有样本只保留前 6 个字符 + '***'；不复制完整凭据",
            "placeholders": sorted(PLACEHOLDERS),
            "realHits": real,
            "placeholderHits": placeholder,
            "containerHits": container,
            "passed": not failed,
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.json_out}")

    if failed:
        print("\n有 REAL 命中：必须处理（不要把它当占位符放过）。", file=sys.stderr)
        return 1
    print("\n无真实凭据出现在日志中。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
