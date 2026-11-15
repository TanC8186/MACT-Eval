#!/usr/bin/env python
"""Fault-injection drills on the isolated mact-cap-* environment.

Scenarios (run one or ``all``):
  redis-outage   graceful Redis stop -> job queued in outbox -> Redis restored
  redis-loss     FLUSHALL after publish -> outbox reconciliation republishes
  worker-kill    two consumers -> SIGKILL the claimant -> survivor takes over
  callback-down  backend stopped after RUNNING -> pending callback -> restart
  group-loss     consumer group destroyed while the worker is running

Only containers whose names start with ``mact-cap-`` are stopped or modified.
No user database, media, container or process is touched. Every scenario writes
a JSON timeline and command/log artifacts under
``data/tmp/capacity-2026-10-26/faults/<scenario>-<timestamp>/``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity_common import (  # noqa: E402
    Api, ISO_DIR, JOB_GROUP, JOB_STREAM, PG_CONTAINER, REDIS_CONTAINER,
    count_rows, find_pid_by_port, find_service_pids, job_row, outbox_row,
    pending_callbacks, pending_summary, psql, redis_cli, sql_literal,
    stream_length, write_json,
)

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:18081/api"
AI_SERVICE = ROOT / "ai-service"
LOG_DIR = ISO_DIR / "logs"
FAULT_DIR = ISO_DIR / "faults"
JWT_SECRET = "capacity-only-jwt-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
SERVICE_TOKEN = "capacity-service-token-2026-10-26"
JAR = ROOT / "backend" / "target" / "mact-eval-backend-1.0.0.jar"

BACKEND_ENV = {
    "MACT_JWT_SECRET": JWT_SECRET,
    "MACT_SERVICE_TOKEN": SERVICE_TOKEN,
    "MACT_DB_URL": "jdbc:postgresql://127.0.0.1:55432/macteval_capacity",
    "MACT_DB_USERNAME": "macteval",
    "MACT_DB_PASSWORD": "macteval",
    "SPRING_DATA_REDIS_HOST": "127.0.0.1",
    "SPRING_DATA_REDIS_PORT": "6380",
    "MACT_MINIO_ENDPOINT": "http://127.0.0.1:9100",
    "MACT_MINIO_PUBLIC_ENDPOINT": "http://127.0.0.1:9100",
    "MACT_MINIO_ACCESS_KEY": "capacity",
    "MACT_MINIO_SECRET_KEY": "capacity_secret",
    "MACT_MINIO_ENABLED": "true",
    "MACT_AI_SERVICE_URL": "http://127.0.0.1:18082",
    "MACT_DEMO_ENABLED": "false",
    "SERVER_PORT": "18081",
    "MACT_OUTBOX_STALE_QUEUED_SECONDS": "60",
    "MACT_OUTBOX_RECONCILE_INTERVAL_MS": "15000",
}

AI_ENV = {
    "KMP_DUPLICATE_LIB_OK": "TRUE",
    "MACT_AI_REDIS_URL": "redis://127.0.0.1:6380/0",
    "MACT_AI_MINIO_ENDPOINT": "127.0.0.1:9100",
    "MACT_AI_MINIO_ACCESS_KEY": "capacity",
    "MACT_AI_MINIO_SECRET_KEY": "capacity_secret",
    "MACT_AI_BACKEND_URL": BASE,
    "MACT_AI_SERVICE_TOKEN": SERVICE_TOKEN,
    "MACT_AI_DEVICE": "cuda",
    "MACT_AI_WHISPER_DEVICE": "cuda",
    "MACT_AI_WHISPER_COMPUTE_TYPE": "float16",
    "MACT_AI_DEMO_ENABLED": "false",
    "MACT_AI_EVIDENCE_FRAMES_ENABLED": "true",
    "MACT_AI_EVIDENCE_FRAME_MAX_PER_WINDOW": "4",
    "MACT_AI_BEHAVIOR_WEIGHTS": str(
        ROOT / "docs/experiments/runs/scb5-yolo/train/weights/best.pt"),
    "MACT_AI_BEHAVIOR_CONFIDENCE": "0.4",
    "MACT_AI_VISION_BACKEND": "yolo",
    "MACT_AI_FACE_WEIGHTS": str(ROOT / "models/yolov8n.pt"),
    "PYTHONUNBUFFERED": "1",
}

VIDEO_90S = ROOT / "data/raw/public/classroom/sav-sample90s.mp4"
VIDEO_10MIN = ISO_DIR / "video/fault-10min.mp4"

NOTES = [
    "Evidence-frame counts are selected images, not action events or student counts.",
    "subjectId is frame-local (target-N); it is not a name or a cross-frame identity.",
    "No presigned URL is stored in these results or report snapshots; only bucket/object keys are recorded.",
    "The action checkpoint supports only the six names reported by its own names field.",
]


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_manifest() -> dict:
    try:
        git_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True,
            encoding="utf-8", errors="replace").strip()
    except Exception:
        git_head = None
    return {
        "capturedAt": now_iso(),
        "gitHead": git_head,
        "backendJar": str(JAR),
        "backendJarSha256": sha256_file(JAR),
        "video90sSha256": sha256_file(VIDEO_90S),
        "video10minSha256": sha256_file(VIDEO_10MIN),
        "backendEnv": {k: BACKEND_ENV[k] for k in (
            "MACT_OUTBOX_STALE_QUEUED_SECONDS", "MACT_OUTBOX_RECONCILE_INTERVAL_MS",
            "MACT_AI_SERVICE_URL")},
        "aiEnv": {k: AI_ENV[k] for k in (
            "MACT_AI_DEVICE", "MACT_AI_BEHAVIOR_WEIGHTS",
            "MACT_AI_WHISPER_DEVICE", "MACT_AI_WHISPER_COMPUTE_TYPE")},
    }


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        return requests.get(url, timeout=timeout).status_code == 200
    except Exception:
        return False


def wait_http(url: str, timeout: float, label: str) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if http_ok(url):
            return round(time.monotonic() - started, 2)
        time.sleep(1)
    raise TimeoutError(f"{label} did not become ready within {timeout}s ({url})")


def wait_redis(timeout: float = 60) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if redis_cli("ping", timeout=5) == "PONG":
                return round(time.monotonic() - started, 2)
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError("isolated Redis did not become ready")


def start_backend(log_name: str = "backend.log") -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(BACKEND_ENV)
    log = (LOG_DIR / log_name).open("a", encoding="utf-8")
    proc = subprocess.Popen(
        ["java", "-jar", str(JAR)], cwd=str(ROOT), env=env,
        stdout=log, stderr=subprocess.STDOUT)
    wait_http(BASE + "/health", 120, "backend")
    return proc


def start_ai(port: int, log_name: str) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(AI_ENV)
    log = (LOG_DIR / log_name).open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(AI_SERVICE), env=env, stdout=log, stderr=subprocess.STDOUT)
    wait_http(f"http://127.0.0.1:{port}/ready", 120, f"ai-service:{port}")
    return proc


def stop_pid(pid: int | None, label: str) -> float:
    if not pid:
        raise RuntimeError(f"no pid for {label}")
    started = time.monotonic()
    proc = psutil.Process(pid)
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except psutil.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    return round(time.monotonic() - started, 2)


def stop_port(port: int) -> float | None:
    pid = find_pid_by_port(port)
    if not pid:
        return None
    return stop_pid(pid, f"port {port}")


def stop_backend() -> float:
    pids = find_service_pids()
    if not pids.get("backend"):
        raise RuntimeError("backend pid not found")
    return stop_pid(pids["backend"], "backend")


def ensure_services(need_ai_b: bool = False) -> dict:
    log = {}
    if not http_ok(BASE + "/health"):
        start_backend()
        log["startedBackend"] = True
    if not http_ok("http://127.0.0.1:18082/ready"):
        start_ai(18082, "ai-service.log")
        log["startedAiA"] = True
    if need_ai_b and not http_ok("http://127.0.0.1:18083/ready"):
        start_ai(18083, "ai-service-b.log")
        log["startedAiB"] = True
    wait_redis()
    return log


def new_lesson(api: Api, label: str) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    api.login_or_register(f"fault-teacher-{suffix}", "capacity123")
    course = api.create_course(suffix)
    lesson = api.create_session(course, f"故障演练-{label}-{suffix}")
    return lesson, suffix


def poll_job(api: Api, job_id: str, timeout: float, stop_when=None) -> dict:
    started = time.monotonic()
    events = []
    last = None
    while time.monotonic() - started < timeout:
        try:
            job = api.get_job(job_id)
            state = (job.get("status"), job.get("progress"))
            if state != last:
                events.append({"at": now_iso(), "elapsed": round(time.monotonic() - started, 2),
                               "status": job.get("status"), "progress": job.get("progress")})
                last = state
            if job.get("status") in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
                return {"job": job, "events": events,
                        "elapsed": round(time.monotonic() - started, 2)}
        except Exception as exc:  # backend may be intentionally down
            events.append({"at": now_iso(), "elapsed": round(time.monotonic() - started, 2),
                           "error": str(exc)[:200]})
        if stop_when and stop_when():
            return {"job": None, "events": events,
                    "elapsed": round(time.monotonic() - started, 2), "stopped": True}
        time.sleep(2)
    return {"job": None, "events": events,
            "elapsed": round(time.monotonic() - started, 2), "timeout": True}


def wait_published(job_id: str, timeout: float = 60) -> dict | None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        row = outbox_row(job_id)
        if row and row.get("published_at"):
            return row
        time.sleep(1)
    return outbox_row(job_id)


def wait_outbox_retry(job_id: str, timeout: float = 90,
                      initial_published: str | None = None) -> dict:
    started = time.monotonic()
    seen_reset = False
    reset_at = None
    initial = initial_published
    if initial is None:
        initial = (outbox_row(job_id) or {}).get("published_at") or None
    while time.monotonic() - started < timeout:
        row = outbox_row(job_id)
        if row and not row.get("published_at"):
            seen_reset = True
            reset_at = reset_at or time.monotonic()
        changed = bool(row and row.get("published_at") and initial
                       and row.get("published_at") != initial)
        if (seen_reset or changed) and row and row.get("published_at"):
            return {"seenReset": seen_reset, "changed": changed, "row": row,
                    "resetAt": reset_at, "initialPublished": initial,
                    "elapsed": round(time.monotonic() - started, 2)}
        time.sleep(2)
    return {"seenReset": seen_reset, "changed": False, "row": outbox_row(job_id),
            "resetAt": reset_at, "initialPublished": initial,
            "elapsed": round(time.monotonic() - started, 2), "timeout": True}


def wait_log(log_name: str, needle: str, timeout: float) -> float | None:
    path = LOG_DIR / log_name
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                return round(time.monotonic() - started, 2)
        except OSError:
            pass
        time.sleep(1)
    return None


def log_size(log_name: str) -> int:
    path = LOG_DIR / log_name
    try:
        return path.stat().st_size
    except OSError:
        return 0


def wait_log_after(log_name: str, needle: str, offset: int,
                   timeout: float) -> float | None:
    """Wait for a line appended after an injection offset."""
    path = LOG_DIR / log_name
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                appended = handle.read().decode("utf-8", errors="replace")
            if needle in appended:
                return round(time.monotonic() - started, 2)
        except OSError:
            pass
        time.sleep(1)
    return None


def job_counts(job_id: str, lesson_id: str) -> dict:
    return {
        "jobDb": job_row(job_id),
        "behaviorObservations": count_rows("behavior_observations", f"job_id='{sql_literal(job_id)[1:-1]}'"),
        "evidenceFrames": count_rows("behavior_evidence_frames", f"job_id='{sql_literal(job_id)[1:-1]}'"),
        "windowFeatures": count_rows("lesson_window_features",
                                     f"lesson_session_id='{sql_literal(lesson_id)[1:-1]}'"),
        "streamLength": stream_length(),
        "pending": pending_summary(),
        "pendingCallbacks": pending_callbacks(),
        "outbox": outbox_row(job_id),
    }


def scenario_redis_outage(api: Api, out: Path) -> dict:
    ensure_services()
    lesson, _ = new_lesson(api, "redis-outage")
    result = {"scenario": "redis-outage", "lessonId": lesson, "timeline": []}
    subprocess.run(["docker", "stop", REDIS_CONTAINER], check=True, capture_output=True)
    result["timeline"].append({"at": now_iso(), "event": "redis_stopped"})
    try:
        upload = api.upload_media(lesson, VIDEO_90S, "video", 30, 2, 60, 5)
        job_id = upload["jobId"]
        result["jobId"] = job_id
        # Longer than the default 60s Lettuce command timeout so the outbox
        # publisher actually records a failed attempt and a backoff, rather
        # than sitting inside one blocked XADD.
        time.sleep(75)
        result["duringOutage"] = {
            "job": job_row(job_id),
            "outbox": outbox_row(job_id),
            "aiReady": http_ok("http://127.0.0.1:18082/ready"),
        }
    finally:
        result["timeline"].append({"at": now_iso(), "event": "redis_start_requested"})
        start_requested = time.monotonic()
        subprocess.run(["docker", "start", REDIS_CONTAINER], check=True, capture_output=True)
        wait_redis()
        result["redisReadySeconds"] = round(time.monotonic() - start_requested, 2)
        result["timeline"].append({"at": now_iso(), "event": "redis_ready"})
    published = wait_published(job_id, 90)
    result["published"] = published
    result["timeline"].append({"at": now_iso(), "event": "outbox_published", "row": published})
    waited = poll_job(api, job_id, 180)
    result["wait"] = waited
    result["timeline"].append({"at": now_iso(), "event": "terminal_or_timeout"})
    result["counts"] = job_counts(job_id, lesson)
    outage = result.get("duringOutage", {})
    result["pass"] = (
        outage.get("job", {}).get("status") == "QUEUED"
        and not outage.get("outbox", {}).get("published_at")
        and int(outage.get("outbox", {}).get("attempt") or 0) >= 1
        and bool(outage.get("outbox", {}).get("last_error"))
        and bool(result.get("published", {}).get("published_at"))
        and waited.get("job", {}).get("status") == "SUCCEEDED"
        and result["counts"]["behaviorObservations"] > 0
        and result["counts"]["windowFeatures"] > 0
        and result["counts"]["pending"].startswith("0")
        and not result["counts"]["pendingCallbacks"]
    )
    return result


def scenario_redis_loss(api: Api, out: Path) -> dict:
    ensure_services()
    lesson, _ = new_lesson(api, "redis-loss")
    result = {"scenario": "redis-loss", "lessonId": lesson, "timeline": []}
    # Stop both consumers so the message cannot be claimed before the loss.
    stop_port(18082)
    stop_port(18083)
    result["timeline"].append({"at": now_iso(), "event": "consumers_stopped"})
    upload = api.upload_media(lesson, VIDEO_90S, "video", 30, 2, 60, 5)
    job_id = upload["jobId"]
    result["jobId"] = job_id
    published = wait_published(job_id, 60)
    result["publishedBeforeLoss"] = published
    redis_cli("flushall")
    result["timeline"].append({"at": now_iso(), "event": "redis_flushall"})
    result["afterFlush"] = {"job": job_row(job_id), "outbox": outbox_row(job_id),
                            "streamLength": stream_length()}
    # The reconciler resets the published event after the grace period.
    republished = wait_outbox_retry(
        job_id, 120,
        initial_published=(published or {}).get("published_at"))
    result["republished"] = republished
    result["timeline"].append({"at": now_iso(), "event": "outbox_reconciled",
                               "row": republished.get("row"),
                               "seenReset": republished.get("seenReset"),
                               "elapsed": republished.get("elapsed")})
    start_ai(18082, "ai-service.log")
    waited = poll_job(api, job_id, 180)
    result["wait"] = waited
    result["counts"] = job_counts(job_id, lesson)
    result["pass"] = (
        result.get("afterFlush", {}).get("job", {}).get("status") == "QUEUED"
        and int(result.get("afterFlush", {}).get("streamLength", -1)) == 0
        and bool(republished.get("seenReset") or republished.get("changed"))
        and bool(republished.get("row", {}).get("published_at"))
        and waited.get("job", {}).get("status") == "SUCCEEDED"
        and result["counts"]["behaviorObservations"] > 0
        and result["counts"]["windowFeatures"] > 0
        and not result["counts"]["pendingCallbacks"]
    )
    return result


def scenario_group_loss(api: Api, out: Path) -> dict:
    ensure_services()
    lesson, _ = new_lesson(api, "group-loss")
    result = {"scenario": "group-loss", "lessonId": lesson, "timeline": []}
    log_offset = log_size("ai-service.log")
    destroyed = redis_cli("xgroup", "destroy", JOB_STREAM, JOB_GROUP)
    result["destroyed"] = destroyed
    result["timeline"].append({"at": now_iso(), "event": "consumer_group_destroyed"})
    upload = api.upload_media(lesson, VIDEO_90S, "video", 30, 2, 60, 5)
    job_id = upload["jobId"]
    result["jobId"] = job_id
    waited = poll_job(api, job_id, 180)
    result["wait"] = waited
    result["counts"] = job_counts(job_id, lesson)
    result["rejoinLoggedSeconds"] = wait_log_after(
        "ai-service.log", "Consumer group is missing; recreating it",
        log_offset, 30)
    result["pass"] = (
        destroyed == "1"
        and waited.get("job", {}).get("status") == "SUCCEEDED"
        and result["counts"]["behaviorObservations"] > 0
        and result["counts"]["windowFeatures"] > 0
        and result["counts"]["pending"].startswith("0")
        and result["rejoinLoggedSeconds"] is not None
    )
    return result


def scenario_worker_kill(api: Api, out: Path) -> dict:
    ensure_services(need_ai_b=False)
    stop_port(18083)
    lesson, _ = new_lesson(api, "worker-kill")
    result = {"scenario": "worker-kill", "lessonId": lesson, "timeline": []}
    b_proc = start_ai(18083, "ai-service-b.log")
    result["timeline"].append({"at": now_iso(), "event": "second_consumer_started",
                               "pid": b_proc.pid})
    upload = api.upload_media(lesson, VIDEO_10MIN, "video", 30, 2, 60, 5)
    job_id = upload["jobId"]
    result["jobId"] = job_id
    # Wait for one consumer to log the job, then identify and SIGKILL it.
    claimant = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for log_name, port in (("ai-service.log", 18082), ("ai-service-b.log", 18083)):
            path = LOG_DIR / log_name
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if f"Processing job {job_id}" in text:
                claimant = (log_name, port, find_pid_by_port(port))
                break
        if claimant:
            break
        time.sleep(1)
    if not claimant:
        result["pass"] = False
        result["error"] = "no consumer logged the job before timeout"
        return result
    log_name, port, pid = claimant
    result["claimant"] = {"log": log_name, "port": port, "pid": pid}
    result["timeline"].append({"at": now_iso(), "event": "claimant_identified",
                               "log": log_name, "port": port})
    # Ensure the job is RUNNING and has a recent heartbeat before the kill.
    for _ in range(30):
        row = job_row(job_id)
        if row.get("status") == "RUNNING":
            break
        time.sleep(1)
    result["beforeKill"] = job_row(job_id)
    kill_started = time.monotonic()
    stop_pid(pid, f"claimant on {port}")
    result["timeline"].append({"at": now_iso(), "event": "claimant_killed",
                               "atSeconds": round(time.monotonic() - kill_started, 2)})
    # Poll DB directly because the API may be fine; capture attempt transitions.
    transitions = []
    last_attempt = None
    terminal = None
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        row = job_row(job_id)
        state = (row.get("status"), row.get("attempt"))
        if state != last_attempt:
            transitions.append({"at": now_iso(), "status": row.get("status"),
                                "attempt": row.get("attempt"),
                                "elapsed": round(time.monotonic() - kill_started, 2)})
            last_attempt = state
        if row.get("status") in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
            terminal = row
            break
        time.sleep(2)
    result["transitions"] = transitions
    result["terminal"] = terminal
    attempt2 = next((item for item in transitions
                     if int(item.get("attempt") or 0) >= 2 and item.get("elapsed", 0) > 0),
                    None)
    result["attempt2AtSeconds"] = attempt2.get("elapsed") if attempt2 else None
    result["recoverySeconds"] = round(time.monotonic() - kill_started, 2) if terminal else None
    result["counts"] = job_counts(job_id, lesson)
    # Restore exactly one normal consumer on 18082.
    if port == 18082:
        start_ai(18082, "ai-service.log")
    stop_port(18083)
    result["pass"] = (
        terminal is not None
        and terminal.get("status") == "SUCCEEDED"
        and int(terminal.get("attempt") or 0) >= 2
        and attempt2 is not None
        and result["counts"]["behaviorObservations"] > 0
        and result["counts"]["behaviorObservations"] == result["counts"]["windowFeatures"]
        and result["counts"]["pending"].startswith("0")
        and not result["counts"]["pendingCallbacks"]
    )
    return result


def scenario_callback_down(api: Api, out: Path) -> dict:
    ensure_services()
    lesson, _ = new_lesson(api, "callback-down")
    result = {"scenario": "callback-down", "lessonId": lesson, "timeline": []}
    upload = api.upload_media(lesson, VIDEO_10MIN, "video", 30, 2, 60, 5)
    job_id = upload["jobId"]
    result["jobId"] = job_id
    # Wait until inference has started, then take the backend down.
    started = wait_log("ai-service.log", f"Processing job {job_id}", 30)
    result["workerStartedSeconds"] = started
    result["beforeStop"] = job_row(job_id)
    stop_backend()
    result["timeline"].append({"at": now_iso(), "event": "backend_stopped"})
    # The worker retries three times, then stores the result in Redis and ACKs.
    pending = None
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if job_id in pending_callbacks():
            pending = job_id
            break
        time.sleep(2)
    result["pendingCallbackObserved"] = bool(pending)
    result["timeline"].append({"at": now_iso(), "event": "pending_callback_observed",
                               "found": bool(pending)})
    result["pendingState"] = {"job": job_row(job_id),
                              "pendingSummary": pending_summary(),
                              "streamLength": stream_length()}
    backend_started = time.monotonic()
    start_backend()
    result["backendReadySeconds"] = round(time.monotonic() - backend_started, 2)
    result["timeline"].append({"at": now_iso(), "event": "backend_ready"})
    waited = poll_job(api, job_id, 180)
    result["wait"] = waited
    result["counts"] = job_counts(job_id, lesson)
    log_text = ((LOG_DIR / "ai-service.log").read_text(encoding="utf-8", errors="replace")
                if (LOG_DIR / "ai-service.log").exists() else "")
    result["workerInferenceStarts"] = log_text.count(
        f"Starting video analysis: lesson={lesson}")
    result["pass"] = (
        result["pendingCallbackObserved"]
        and waited.get("job", {}).get("status") == "SUCCEEDED"
        and result["counts"]["behaviorObservations"] > 0
        and result["counts"]["windowFeatures"] > 0
        and result["counts"]["behaviorObservations"] == result["counts"]["windowFeatures"]
        and not result["counts"]["pendingCallbacks"]
        and result["workerInferenceStarts"] == 1
        and int(result["counts"]["jobDb"].get("attempt") or 0) == 1
    )
    return result


SCENARIOS = {
    "redis-outage": scenario_redis_outage,
    "redis-loss": scenario_redis_loss,
    "group-loss": scenario_group_loss,
    "worker-kill": scenario_worker_kill,
    "callback-down": scenario_callback_down,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=[*SCENARIOS, "all"])
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if not JAR.exists():
        raise SystemExit(f"backend jar missing: {JAR}")
    FAULT_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out) if args.out else FAULT_DIR / (
        args.scenario + "-" + time.strftime("%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    api = Api(BASE)
    # Authenticate before any Redis outage so a throttle/refresh lookup cannot
    # turn an authorization failure into a misleading fault result.
    api.login_or_register(f"fault-bootstrap-{str(int(time.time()))[-8:]}", "capacity123")
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    summary = {}
    for name in names:
        print(f"==> {name}", flush=True)
        try:
            result = SCENARIOS[name](api, out_dir)
        except Exception as exc:  # keep later scenarios runnable
            result = {"scenario": name, "pass": False, "error": repr(exc)}
        result.setdefault("notes", NOTES)
        summary[name] = result
        write_json(out_dir / f"{name}.json", result)
        print(json.dumps({"scenario": name, "pass": result.get("pass"),
                          "error": result.get("error"),
                          "recoverySeconds": result.get("recoverySeconds")},
                         ensure_ascii=True), flush=True)
    summary["manifest"] = run_manifest()
    write_json(out_dir / "summary.json", summary)
    print(json.dumps({"out": str(out_dir),
                      "passed": [k for k, v in summary.items() if v.get("pass")]},
                     ensure_ascii=True))
    scenario_results = {k: v for k, v in summary.items() if k != "manifest"}
    return 0 if all(v.get("pass") for v in scenario_results.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
