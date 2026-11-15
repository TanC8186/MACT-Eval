#!/usr/bin/env python
"""教师端只读接口的延迟测量（P1-6 的"API p50/p95/p99"部分）。

为什么要单独一个脚本：容量文档里的 p50/p95 过去来自"某次跑了一遍"的口述，
既没有采样间隔，也没有失败率，读的人无法判断它是不是 SLA。这里把测量本身
固化成可复跑的动作：

* 只打 **GET**，只打报告列表/详情/证据三个教师端接口，不写任何数据；
* 每个接口固定 ``--samples`` 次、固定 ``--interval`` 秒采样间隔，
  记录 p50/p95/p99、最大值、失败率与失败状态码分布；
* 隔离守卫与其它 rig 脚本一致（127.0.0.1 + rig 端口表）；
* 输出 JSON，**并明确写入 ``isSla: false``**——单机、单客户端串行请求的
  数字不是并发 SLA，禁止被当作 SLA 引用。

用法：
    python -I scripts/ops/api_latency_probe.py --api-url http://127.0.0.1:8000/api \\
        --user 13800002026 --password UiRigPass2026 --samples 30 --interval 0.2 \\
        --out data/tmp/capacity-2026-10-28/api-latency.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RIG_PORTS = {8000, 18001, 18081}
DEFAULT_API = "http://127.0.0.1:8000/api"


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token = ""

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: int = 60) -> tuple[int, Any, float]:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                elapsed = (time.perf_counter() - started) * 1000
                try:
                    return resp.status, json.loads(raw) if raw.strip() else {}, elapsed
                except json.JSONDecodeError:
                    return resp.status, {"raw": raw[:200]}, elapsed
        except urllib.error.HTTPError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw) if raw.strip() else {}, elapsed
            except json.JSONDecodeError:
                return exc.code, {"raw": raw[:200]}, elapsed
        except Exception as exc:  # noqa: BLE001 - 连接失败也要计入失败率
            return 0, {"error": f"{type(exc).__name__}: {exc}"}, (time.perf_counter() - started) * 1000

    def login(self, user: str, password: str) -> None:
        status, body, _ = self.call("POST", "/auth/login", {"username": user, "password": password})
        if status != 200:
            raise SystemExit(f"登录失败 HTTP {status}: {str(body)[:200]}")
        self.token = (body.get("data") or {}).get("token", "")
        if not self.token:
            raise SystemExit("登录响应缺少 token")


def assert_isolated(api_url: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(api_url)
    if parts.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit(f"拒绝非本机 API：{api_url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in RIG_PORTS:
        raise SystemExit(f"拒绝端口 {port}：只允许隔离 rig 的端口 {sorted(RIG_PORTS)}")


def percentile(values: list[float], pct: float) -> float:
    """线性插值分位数（与 numpy 默认一致），空列表返回 0。"""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def summarize(samples: list[dict]) -> dict:
    ok = [s for s in samples if s["status"] == 200]
    latencies = [s["ms"] for s in ok]
    failures = [s for s in samples if s["status"] != 200]
    return {
        "samples": len(samples),
        "ok": len(ok),
        "failureRate": round(len(failures) / len(samples), 4) if samples else 0.0,
        "failureStatuses": sorted({s["status"] for s in failures}),
        "p50": round(percentile(latencies, 50), 1),
        "p95": round(percentile(latencies, 95), 1),
        "p99": round(percentile(latencies, 99), 1),
        "max": round(max(latencies), 1) if latencies else 0.0,
        "min": round(min(latencies), 1) if latencies else 0.0,
        "mean": round(statistics.fmean(latencies), 1) if latencies else 0.0,
    }


def pick_targets(api: Api) -> dict[str, str]:
    status, body, _ = api.call("GET", "/reports/page")
    if status != 200:
        raise SystemExit(f"读取报告列表失败 HTTP {status}: {str(body)[:200]}")
    items = ((body.get("data") or {}).get("items")) or []
    if not items:
        raise SystemExit("报告列表为空；先跑 browser_rig.py seed")
    # 必须筛已生成的报告：报告列表按课堂列出，**没有报告**的课堂也会出现
    # （generated=false），对它请求 /reports/{id} 会得到 500「报告不存在」。
    # 曾经就是这里没筛，导致 detail/evidence 两个端点 20/20 全失败、
    # 看起来像接口挂了，其实是采样目标选错了。
    generated = [r for r in items if r.get("generated")]
    if not generated:
        raise SystemExit(
            "报告列表里没有已生成的报告（全部 generated=false）；"
            "先跑 browser_rig.py seed 或 prepare_version_fixture.py")
    best = max(generated, key=lambda r: r.get("evidence") or {}.get("windowCount") or 0)
    return {"list": "/reports/page", "detail": f"/reports/{best['id']}",
            "evidence": f"/reports/{best['id']}/behavior-evidence?v=0",
            "lesson": str(best["id"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--user", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--interval", type=float, default=0.2, help="采样间隔（秒）")
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    assert_isolated(args.api_url)
    if not args.user or not args.password:
        raise SystemExit("需要 --user/--password（rig 验收账号）")

    api = Api(args.api_url)
    api.login(args.user, args.password)
    targets = pick_targets(api)

    report: dict[str, Any] = {
        "probe": "api-latency",
        "apiUrl": args.api_url,
        "samplesPerEndpoint": args.samples,
        "samplingIntervalSeconds": args.interval,
        "concurrency": 1,
        "client": "single-threaded urllib (serial)",
        # 这句不是免责声明，是事实边界：单机、单客户端、串行请求的数字
        # 不能当 SLA。并发与多客户端下的 p95/p99 必须另测。
        "isSla": False,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "endpoints": {},
    }

    for name in ("list", "detail", "evidence"):
        path = targets[name]
        samples = []
        for _ in range(args.samples):
            status, _body, ms = api.call("GET", path)
            samples.append({"status": status, "ms": ms})
            if args.interval:
                time.sleep(args.interval)
        report["endpoints"][name] = {"path": path, **summarize(samples)}
        print(f"{name:<9} {path:<58} "
              f"p50={report['endpoints'][name]['p50']}ms "
              f"p95={report['endpoints'][name]['p95']}ms "
              f"p99={report['endpoints'][name]['p99']}ms "
              f"fail={report['endpoints'][name]['failureRate']}",
              file=sys.stderr)

    report["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {out}", file=sys.stderr)
    if args.json or not args.out:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    # 失败率 > 0 时退出码 2：有失败就不能当作"测完了"。
    worst = max(ep["failureRate"] for ep in report["endpoints"].values())
    return 0 if worst == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
