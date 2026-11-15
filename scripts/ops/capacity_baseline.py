#!/usr/bin/env python
"""HTTP capacity baseline for list/dashboard endpoints (OPS08).

This measures API latency and response size on the current hardware; it does
NOT claim a production SLA and does NOT run model inference. Record the host
CPU/RAM and database row counts alongside the output.

    python scripts/ops/capacity_baseline.py --base http://localhost:18001/api \
        --username admin --password admin123 --iterations 30 \
        --out docs/reviews/evidence/capacity-baseline.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import requests


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:18001/api")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    session = requests.Session()
    login = session.post(args.base + "/auth/login", json={
        "username": args.username, "password": args.password}, timeout=15)
    if login.status_code != 200:
        print(f"login failed: {login.status_code} {login.text}", file=sys.stderr)
        return 1
    token = login.json()["data"]["token"]
    session.headers["Authorization"] = f"Bearer {token}"

    endpoints = [
        "/dashboard/summary",
        "/courses/page?page=0&size=10",
        "/sessions/page?page=0&size=10",
        "/reports/page?page=0&size=10",
        "/jobs?limit=20",
        "/dashboard/suggestions",
    ]
    results = {}
    for path in endpoints:
        # Warm-up once so JIT/Hibernate caches are not attributed to the sample.
        session.get(args.base + path, timeout=30)
        latencies = []
        sizes = []
        for _ in range(args.iterations):
            started = time.perf_counter()
            response = session.get(args.base + path, timeout=30)
            latency_ms = (time.perf_counter() - started) * 1000
            if response.status_code >= 500:
                print(f"{path}: HTTP {response.status_code}", file=sys.stderr)
                return 1
            latencies.append(latency_ms)
            sizes.append(len(response.content))
        results[path] = {
            "count": len(latencies),
            "status": response.status_code,
            "p50_ms": round(statistics.median(latencies), 2),
            "p95_ms": round(percentile(latencies, 0.95), 2),
            "max_ms": round(max(latencies), 2),
            "avg_response_bytes": round(statistics.mean(sizes), 1),
        }
        print(f"{path}: p50={results[path]['p50_ms']}ms "
              f"p95={results[path]['p95_ms']}ms bytes={results[path]['avg_response_bytes']}")

    payload = {
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base": args.base,
        "iterations": args.iterations,
        "endpoints": results,
        "note": "List/dashboard API baseline only; model inference capacity is not measured.",
    }
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
