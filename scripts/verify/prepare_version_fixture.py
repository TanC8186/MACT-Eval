#!/usr/bin/env python
"""为 U04（版本隔离）准备"同一课堂至少两个已发布版本"的隔离数据。

为什么需要单独一步：``GET /reports/{id}/versions`` 只返回**已发布快照**，
而 rig 每次 seed 只产出一份草稿。于是 U04 的版本切换断言在数据层面根本
无法成立——脚本只能把它记成 requiredSkip，用例判 incomplete。这本来是"数据未就绪"，
但反复出现在每次真实验收里就成了常态化的空白证据。

因此本脚本在**隔离 rig** 上执行真实的业务动作：
    读取当前草稿 → 发布 v1 → 重新生成（产生新草稿）→ 再次发布 v2
产出的两个版本带不同的 ``contentHash`` 与 ``publishedAt``，U04 才能验证
"切到另一版本后分数与证据确实来自那一版"。

安全边界（与其它 rig 脚本一致）：
* 只允许 ``--api-url`` 落在 127.0.0.1，且端口必须在 rig 端口表内（8000）；
* 只允许操作 rig 数据库里的课堂（先探测 ``/reports/page`` 能读到）；
* 不删除任何历史版本，不修改已发布快照；
* 幂等：若目标课堂已有 >= 2 个已发布版本，直接返回，不做任何写入。

用法：
    python -I scripts/verify/prepare_version_fixture.py --api-url http://127.0.0.1:8000/api \\
        --user teacher --password 123456 [--lesson <id>] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

RIG_PORTS = {8000}
DEFAULT_API = "http://127.0.0.1:8000/api"


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token = ""

    def call(self, method: str, path: str, body: dict | None = None,
             token: str | None = None, timeout: int = 120) -> tuple[int, Any]:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        auth = token if token is not None else self.token
        if auth:
            req.add_header("Authorization", f"Bearer {auth}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return exc.code, {"raw": raw[:400]}

    def login(self, user: str, password: str) -> None:
        status, body = self.call("POST", "/auth/login", {"username": user, "password": password})
        if status != 200:
            raise SystemExit(f"登录失败 HTTP {status}: {body}")
        data = body.get("data") or {}
        self.token = data.get("accessToken") or data.get("token") or ""
        if not self.token:
            raise SystemExit(f"登录响应缺少 accessToken: {sorted(data)}")


def assert_isolated(api_url: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(api_url)
    if parts.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit(f"拒绝非本机 API：{api_url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in RIG_PORTS:
        raise SystemExit(
            f"拒绝端口 {port}：只允许隔离 rig 的后端端口 {sorted(RIG_PORTS)}"
            f"（用户自己的 5432/6379/9000 与 8001 一律不碰）")


def versions(api: Api, lesson: str) -> list[dict]:
    status, body = api.call("GET", f"/reports/{lesson}/versions")
    if status != 200:
        return []
    data = body.get("data")
    return data if isinstance(data, list) else []


def pick_lesson(api: Api, explicit: str | None) -> str:
    """选一份**可发布**的报告。

    "已生成"不够：``publish`` 对无有效数据的报告返回 409（"报告无有效数据，不能发布"），
    而 seed 刚创建、分析还没跑完的课堂正好是这种。所以这里必须按
    ``climateScore != null && dataSource != 'EMPTY'`` 过滤，否则脚本会在第一步
    publish 上就 409 退出，看起来像脚本坏了。
    """
    if explicit:
        return explicit
    status, body = api.call("GET", "/reports/page", timeout=60)
    if status != 200:
        raise SystemExit(f"读取报告列表失败 HTTP {status}: {body}")
    items = ((body.get("data") or {}).get("items")) or []
    candidates = [
        str(item["id"]) for item in items
        if item.get("generated")
        and item.get("climateScore") is not None
        and item.get("dataSource") not in (None, "EMPTY")
    ]
    if not candidates:
        raise SystemExit(
            "报告列表里没有『有有效数据且已生成』的报告；"
            "先跑 browser_rig.py seed 并等分析任务结束")
    # 优先选已经有两个及以上版本的（幂等），否则选第一份可发布的。
    for lesson in candidates:
        if len(versions(api, lesson)) >= 2:
            return lesson
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-url", default=DEFAULT_API)
    ap.add_argument("--user", default="teacher")
    ap.add_argument("--password", default="123456")
    ap.add_argument("--lesson", default="")
    ap.add_argument("--second-video", default="",
                    help="第二段媒体路径；提供时两版将来自不同的 analysis job"
                         "（否则同一 job 会让证据隔离断言恒真）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    assert_isolated(args.api_url)
    api = Api(args.api_url)
    api.login(args.user, args.password)

    lesson = pick_lesson(api, args.lesson or None)
    existing = versions(api, lesson)
    report: dict[str, Any] = {
        "lesson": lesson,
        "apiUrl": args.api_url,
        "before": [{"version": v.get("version"), "status": v.get("status"),
                    "publishedAt": v.get("publishedAt")} for v in existing],
        "actions": [],
    }

    # 幂等判定必须看**job 是否不同**，而不是只看版本数：
    # 两个版本共用同一个 analysis job 时，证据隔离断言恒真（独立复核 P0-1）。
    # 因此 `--second-video` 模式下，要求已有 >= 2 个版本且它们的 videoJob 至少有两个不同值。
    distinct_jobs = {(v.get("analysisVideoJobId") or "").strip() for v in existing}
    distinct_jobs.discard("")
    if len(existing) >= 2 and (not args.second_video or len(distinct_jobs) >= 2):
        report["result"] = "already-has-two-versions"
        report["ok"] = True
    else:
        # 1) 先确保当前报告已发布（若是草稿，直接发布为 v1）
        detail_status, detail_body = api.call("GET", f"/reports/{lesson}", timeout=60)
        detail = (detail_body.get("data") or {}) if detail_status == 200 else {}
        if detail_status != 200:
            # 课堂存在但报告还没生成时，后端目前返回 500（见审计 p1-1-unknown-lesson
            # 的同类问题）。这里不把"报告不存在"当成脚本错误：先生成一份草稿，
            # 后面照常 publish → regenerate → publish。
            report["actions"].append({"action": "read-report", "http": detail_status,
                                      "ok": False, "note": "报告不存在，先生成"})
            status, body = api.call("POST", f"/reports/generate/{lesson}", timeout=600)
            report["actions"].append({"action": "generate-first", "http": status,
                                      "ok": status == 200,
                                      "message": (body.get("message") if isinstance(body, dict) else "")})
            if status != 200:
                raise SystemExit(f"首次生成失败 HTTP {status}: {body}")
            detail_status, detail_body = api.call("GET", f"/reports/{lesson}", timeout=60)
            detail = (detail_body.get("data") or {}) if detail_status == 200 else {}
            if detail_status != 200:
                raise SystemExit(f"生成后仍读不到报告 HTTP {detail_status}: {detail_body}")
        if detail.get("status") != "published":
            status, body = api.call("POST", f"/reports/{lesson}/publish", timeout=180)
            report["actions"].append({"action": "publish", "http": status,
                                      "ok": status == 200,
                                      "message": (body.get("message") if isinstance(body, dict) else "")})
            if status != 200:
                raise SystemExit(f"发布失败 HTTP {status}: {body}")

        # 2) 产生第二个版本的数据来源。
        #
        # 默认走"重新生成"：但在同一课堂上重新生成会**复用同一个 analysis job**，
        # 于是两个版本的视频任务号相同、证据帧完全相同——那样"按版本取证据"
        # 的断言恒真，什么也证明不了（独立复核 P0-1 指出的就是这个）。
        # 因此支持 `--second-video`：在**同一个课堂**上传第二段媒体并跑出**新的
        # job**，再生成报告。这样两版的 analysisVideoJobId 必然不同。
        if args.second_video:
            from pathlib import Path as _Path
            video = _Path(args.second_video)
            if not video.exists():
                raise SystemExit(f"第二个视频不存在：{video}")
            upload = api.call("POST", "/upload/media", {
                "mediaType": "video", "fileName": video.name,
                "lessonSessionId": lesson,
                "sizeBytes": video.stat().st_size, "contentType": "video/mp4"},
                timeout=120)
            if upload[0] != 200:
                raise SystemExit(f"上传第二段媒体失败 HTTP {upload[0]}: {upload[1]}")
            data = upload[1]["data"]
            import urllib.request as _u
            with video.open("rb") as handle:
                put = _u.Request(data["uploadUrl"], data=handle.read(), method="PUT")
                put.add_header("Content-Type", "video/mp4")
                with _u.urlopen(put, timeout=1800) as resp:
                    if resp.status not in (200, 201, 204):
                        raise SystemExit(f"MinIO PUT 失败 HTTP {resp.status}")
            confirm = api.call("POST", "/upload/media/confirm", {
                "assetId": data["assetId"], "objectName": data["objectName"],
                "lessonSessionId": lesson,
                "totalSeats": 30, "sampleInterval": 2,
                "windowDuration": 60, "minObservations": 1}, timeout=300)
            report["actions"].append({"action": "upload-second-video", "http": confirm[0],
                                      "ok": confirm[0] == 200,
                                      "jobId": ((confirm[1].get("data") or {}).get("jobId")
                                                if isinstance(confirm[1], dict) else None)})
            if confirm[0] != 200:
                raise SystemExit(f"确认第二段媒体失败 HTTP {confirm[0]}: {confirm[1]}")
        status, body = api.call("POST", f"/reports/generate/{lesson}", timeout=300)
        report["actions"].append({"action": "regenerate", "http": status,
                                  "ok": status == 200,
                                  "message": (body.get("message") if isinstance(body, dict) else "")})
        if status != 200:
            raise SystemExit(f"重新生成失败 HTTP {status}: {body}")

        # 3) 再发布 → v2
        status, body = api.call("POST", f"/reports/{lesson}/publish", timeout=180)
        report["actions"].append({"action": "publish-again", "http": status,
                                  "ok": status == 200,
                                  "message": (body.get("message") if isinstance(body, dict) else "")})
        if status != 200:
            raise SystemExit(f"二次发布失败 HTTP {status}: {body}")

        after = versions(api, lesson)
        report["after"] = [{"version": v.get("version"), "status": v.get("status"),
                            "publishedAt": v.get("publishedAt")} for v in after]
        report["ok"] = len(after) >= 2
        report["result"] = "created-two-versions" if report["ok"] else "still-insufficient"

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"lesson={report['lesson']} result={report['result']}")
        for v in report.get("after") or report["before"]:
            print(f"  v{v['version']} {v['status']} {v['publishedAt']}")
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
