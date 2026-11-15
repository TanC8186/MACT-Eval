#!/usr/bin/env python
"""在隔离 rig 上造一份"当前版本丰富、历史版本来自不同 job"的课堂，供 U04 版本隔离验收。

为什么需要这个特定顺序：``ReportService`` 生成报告时取**最新的成功任务**，
而 U04 要在同一个课堂里切换两个**视频任务不同**的版本（否则两版证据帧完全相同，
"按版本取证据"的断言恒真）。同时 U02/U03 需要当前版本的证据帧足够多（有检测框）。

如果先传 120 分钟视频再传 90 秒视频，90 秒那个会成为"最新任务"，当前版本只有 4 帧，
U02 就会因为"没有检测框"而 skip —— 本轮实测踩过这个坑（e2e-20261028T061317.json）。

因此顺序必须是：
    1. 建课堂 → 传 90 秒 → 等成功 → 生成+发布 v1（小 job，作为**历史**版本）
    2. 传 120 分钟 → 等成功 → 生成+发布 v2（大 job，成为**当前**版本）
这样 v1 与 v2 的 analysisVideoJobId 必然不同，且当前版本有 295 帧。

用法：
    python -I scripts/verify/prepare_version_classroom.py \\
        --api-url http://127.0.0.1:8000/api --user <teacher> --password <pass> \\
        --small data/tmp/capacity-2026-10-28/inputs/90s-480p.mp4 \\
        --large data/tmp/capacity-2026-10-28/inputs/120min-480p.mp4 [--json]

**注意 --large 的取值**（SEC-03 之后）：服务端单文件上限是 2 GiB，而
`data/tmp/ui-rig-2026-10-27/classroom-120min.mp4` 是 **2.47 GiB**
（2,656,976,824 B），init 会直接 413。用同源的 480p 版本
`data/tmp/capacity-2026-10-28/inputs/120min-480p.mp4`（276 MB，7426.9 s，
时长完全相同）——课时长度不变，只是码率降到 480p。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RIG_PORTS = {8000}
DEFAULT_API = "http://127.0.0.1:8000/api"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token = ""

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: int = 300) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, (json.loads(raw) if raw.strip() else {})
            except json.JSONDecodeError:
                return exc.code, {"raw": raw[:300]}

    def login(self, user: str, password: str) -> None:
        status, body = self.call("POST", "/auth/login",
                                 {"username": user, "password": password}, timeout=60)
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
    port = parts.port or 80
    if port not in RIG_PORTS:
        raise SystemExit(f"拒绝端口 {port}：只允许隔离 rig 端口 {sorted(RIG_PORTS)}")


def upload_and_wait(api: Api, lesson: str, video: Path, steps: list) -> str:
    status, body = api.call("POST", "/upload/media", {
        "mediaType": "video", "fileName": video.name, "lessonSessionId": lesson,
        # SEC-03：init 需声明大小与 MIME；confirm 会用实测值对照。
        "sizeBytes": video.stat().st_size, "contentType": "video/mp4"}, timeout=120)
    if status != 200:
        raise SystemExit(f"上传初始化失败 HTTP {status}: {str(body)[:300]}")
    data = body["data"]
    # 必须流式上传：2.5 GB 的视频一次性 read_bytes() 再塞给 urllib 会
    # 在 MinIO 端被断开（实测 RemoteDisconnected）。用 requests + 文件句柄，
    # 与 scripts/ops/capacity_common.py 的做法一致。
    try:
        import requests
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"缺少 requests：{exc}（pip install requests）")
    with video.open("rb") as handle:
        resp = requests.put(data["uploadUrl"], data=handle, timeout=3600,
                            headers={"Content-Type": "video/mp4"})
    if resp.status_code not in (200, 201, 204):
        raise SystemExit(f"MinIO PUT 失败 HTTP {resp.status_code} {resp.text[:300]}")
    status, body = api.call("POST", "/upload/media/confirm", {
        "assetId": data["assetId"], "objectName": data["objectName"],
        "lessonSessionId": lesson, "totalSeats": 30, "sampleInterval": 2,
        "windowDuration": 60, "minObservations": 1}, timeout=300)
    if status != 200:
        raise SystemExit(f"确认媒体失败 HTTP {status}: {str(body)[:300]}")
    job_id = body["data"]["jobId"]
    steps.append({"step": "upload", "video": video.name, "jobId": job_id})
    print(f"  job {job_id} 已入队（{video.name}）…", file=sys.stderr)

    deadline = time.time() + 3600
    while time.time() < deadline:
        status, body = api.call("GET", f"/jobs/{job_id}", timeout=60)
        state = ((body.get("data") or {}).get("status") or "").upper() if status == 200 else "?"
        if state in ("SUCCEEDED", "FAILED"):
            if state != "SUCCEEDED":
                raise SystemExit(f"任务 {job_id} 失败：{state} {str(body)[:300]}")
            steps.append({"step": "job", "jobId": job_id, "status": state})
            return job_id
        time.sleep(10)
    raise SystemExit(f"任务 {job_id} 超时（60 分钟）")


def publish_version(api: Api, lesson: str, steps: list) -> int:
    status, body = api.call("POST", f"/reports/generate/{lesson}", timeout=600)
    steps.append({"step": "generate", "http": status,
                  "message": body.get("message") if isinstance(body, dict) else ""})
    if status != 200:
        raise SystemExit(f"生成失败 HTTP {status}: {str(body)[:300]}")
    status, body = api.call("POST", f"/reports/{lesson}/publish", timeout=300)
    steps.append({"step": "publish", "http": status,
                  "message": body.get("message") if isinstance(body, dict) else ""})
    if status != 200:
        raise SystemExit(f"发布失败 HTTP {status}: {str(body)[:300]}")
    status, body = api.call("GET", f"/reports/{lesson}/versions", timeout=60)
    versions = body.get("data") or []
    return max((int(v["version"]) for v in versions), default=0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-url", default=DEFAULT_API)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--small", required=True, help="历史版本用的短视频（先传）")
    ap.add_argument("--large", required=True, help="当前版本用的长视频（后传，成为最新任务）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    assert_isolated(args.api_url)
    small, large = Path(args.small), Path(args.large)
    for path in (small, large):
        if not path.exists():
            raise SystemExit(f"视频不存在：{path}")

    api = Api(args.api_url)
    api.login(args.user, args.password)

    steps: list = []
    status, body = api.call("GET", "/courses", timeout=60)
    courses = body.get("data") or []
    if not courses:
        raise SystemExit("没有可用课程；先跑 browser_rig.py seed")
    course_id = courses[0]["id"]
    status, body = api.call("POST", "/sessions", {
        "courseId": course_id, "classId": "class-001",
        "title": f"版本隔离验收课堂-{int(time.time()) % 100000}"}, timeout=60)
    if status != 200:
        raise SystemExit(f"建课堂失败 HTTP {status}: {str(body)[:300]}")
    lesson = body["data"]["id"]
    print(f"lesson={lesson}", file=sys.stderr)

    # 1) 先传短视频 → 发布为 v1（历史版本，job 与当前不同）
    upload_and_wait(api, lesson, small, steps)
    v1 = publish_version(api, lesson, steps)
    print(f"  v{v1} 已发布（小 job）", file=sys.stderr)

    # 2) 再传长视频 → 成为最新成功任务 → 发布为 v2（当前版本，帧数充足）
    upload_and_wait(api, lesson, large, steps)
    v2 = publish_version(api, lesson, steps)
    print(f"  v{v2} 已发布（大 job，当前版本）", file=sys.stderr)

    status, body = api.call("GET", f"/reports/{lesson}/versions", timeout=60)
    versions = body.get("data") or []
    jobs = {v.get("version"): v.get("analysisVideoJobId") for v in versions}
    out = {"lesson": lesson, "versions": versions, "jobByVersion": jobs,
           "steps": steps,
           "distinctJobs": len({j for j in jobs.values() if j}) >= 2,
           "ok": len(versions) >= 2 and len({j for j in jobs.values() if j}) >= 2}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"lesson={lesson} ok={out['ok']} distinctJobs={out['distinctJobs']}")
        for v in versions:
            print(f"  v{v['version']} job={v.get('analysisVideoJobId')} "
                  f"published={v.get('publishedAt')}")
    return 0 if out["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
