#!/usr/bin/env python
"""Recovery-protocol fault drills for the round-2 isolated environment.

Acceptance harness for ``docs/operations/result-delivery-and-recovery-protocol.md``
(frozen 2026-10-26); covers the F01-F13 rows of the verification matrix in
``docs/reviews/2026-10-26-claude-code-next-optimization-plan.md`` §6.

Safety model
------------
Every destructive action goes through a helper that first calls
:func:`assert_isolated_target`. It compares the configured containers, host
ports and database name against frozen allow-lists and aborts with the *actual*
values on any mismatch. The user's ``personal_design-*`` Compose project and the
previous round's ``mact-cap-postgres`` / ``mact-cap-minio`` can therefore never
be reached by name, port or database. No ``FLUSHALL`` is ever issued: F04 wipes
``analysis:jobs`` on ``mact-cap-redis`` (the only instance-wide step, and only
while that instance is reserved for this drill), everything else is scoped to
one job id.

Scenarios (``--list``)::

    result-before-redis-loss   F04  persisted result survives full Redis loss
    requeue-after-worker-exit  F05  expired RUNNING without result is requeued
    crash-after-put            F06  object saved, worker killed before index/ACK
    response-lost              F07  duplicate completion is idempotent
    minio-unavailable          F09  store outage defers, never requeues
    f01-f03                    F01/F02/F03 lease + conditional-update fencing
    f10-f12                    F10/F11/F12 stale attempt/heartbeat/exhaustion
    readiness                  F13  /live and /ready across four states
    ttl-boundary               F08  undelivered results never expire (accelerated)

Every scenario writes one JSON file containing a timeline, the observed attempt
sequence, the terminal state, business-row counts before/after, a duplicate
check and a list of assertions. ``pass`` is derived from the assertions only; a
skipped scenario records ``notRun`` and is never reported as a pass.

This file is written to be run against the round-2 rig. Each scenario probes at
runtime whether the code it needs is present (result store wiring, recovery
service, V28 columns, the F06 fault hook) and skips with an explicit reason when
it is not; a skip is never reported as a pass. ``--self-check`` performs only
read-only validation (targets, containers, ports, database, files, jar
freshness) and starts no inference.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import psutil
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity_common import (  # noqa: E402
    Api,
    JOB_GROUP,
    JOB_STREAM,
    find_pid_by_port,
    redis_cli,
    write_json,
)

ROOT = Path(__file__).resolve().parents[2]
ISO_DIR = ROOT / "data" / "tmp" / "round2-2026-10-26"
FAULT_DIR = ISO_DIR / "faults"
LOG_DIR = ISO_DIR / "logs"

# --- frozen isolation targets (round 2) -----------------------------------
PG_CONTAINER = "mact-r2-postgres"
PG_PORT = 15532          # 55432 is inside the Windows excluded range 55392-55491
PG_DB = "macteval_round2"
PG_USER = "macteval"
PG_PASSWORD = "macteval"
REDIS_CONTAINER = "mact-cap-redis"   # reused from the previous round, AOF everysec
REDIS_PORT = 6380
MINIO_CONTAINER = "mact-r2-minio"
MINIO_PORT = 9100
MINIO_CONSOLE_PORT = 9101
MINIO_ACCESS_KEY = "capacity"
MINIO_SECRET_KEY = "capacity_secret"
MINIO_BUCKET = "analysis-artifacts"
RESULT_PREFIX = "analysis-results"

BACKEND_PORT = 18081
AI_A_PORT = 18082
AI_B_PORT = 18083

ALLOWED_PORTS = frozenset({15532, 6380, 9100, 18081, 18082, 18083})
ALLOWED_CONTAINERS = frozenset({PG_CONTAINER, REDIS_CONTAINER, MINIO_CONTAINER})
ALLOWED_DATABASES = frozenset({PG_DB})
#: Names that must never be targeted, even if a caller passes them explicitly.
FORBIDDEN_CONTAINER_PREFIXES = ("personal_design-",)
FORBIDDEN_CONTAINERS = frozenset({"mact-cap-postgres", "mact-cap-minio"})
FORBIDDEN_PORTS = frozenset({5432, 6379, 9000, 9001, 8000, 8001, 55432})

BASE = f"http://127.0.0.1:{BACKEND_PORT}/api"
AI_BASE = f"http://127.0.0.1:{AI_A_PORT}"
JWT_SECRET = "round2-only-jwt-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
SERVICE_TOKEN = "round2-service-token-2026-10-26"

JAR = ROOT / "backend" / "target" / "mact-eval-backend-1.0.0.jar"
AI_SERVICE = ROOT / "ai-service"
CHECKPOINT = ROOT / "docs" / "experiments" / "runs" / "scb5-yolo" / "train" / "weights" / "best.pt"
VIDEO_90S = ROOT / "data" / "raw" / "public" / "classroom" / "sav-sample90s.mp4"
VIDEO_10MIN = ROOT / "data" / "tmp" / "capacity-2026-10-26" / "video" / "fault-10min.mp4"

TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}

NOTES = [
    "Evidence-frame counts are selected images, not action events or student counts.",
    "subjectId is frame-local (target-N); it is not a name or a cross-frame identity.",
    "No presigned URL is stored in these results or report snapshots; only bucket/object keys.",
    "The action checkpoint supports only the six names reported by its own names field.",
    "This script targets only mact-r2-postgres / mact-cap-redis / mact-r2-minio and ports "
    "15532, 6380, 9100, 18081, 18082, 18083.",
]

BACKEND_ENV = {
    "MACT_JWT_SECRET": JWT_SECRET,
    "MACT_SERVICE_TOKEN": SERVICE_TOKEN,
    "MACT_DB_URL": f"jdbc:postgresql://127.0.0.1:{PG_PORT}/{PG_DB}",
    "MACT_DB_USERNAME": PG_USER,
    "MACT_DB_PASSWORD": PG_PASSWORD,
    "SPRING_DATA_REDIS_HOST": "127.0.0.1",
    "SPRING_DATA_REDIS_PORT": str(REDIS_PORT),
    "MACT_MINIO_ENDPOINT": f"http://127.0.0.1:{MINIO_PORT}",
    "MACT_MINIO_PUBLIC_ENDPOINT": f"http://127.0.0.1:{MINIO_PORT}",
    "MACT_MINIO_ACCESS_KEY": MINIO_ACCESS_KEY,
    "MACT_MINIO_SECRET_KEY": MINIO_SECRET_KEY,
    "MACT_MINIO_ENABLED": "true",
    "MACT_AI_SERVICE_URL": f"http://127.0.0.1:{AI_A_PORT}",
    "MACT_DEMO_ENABLED": "false",
    "SERVER_PORT": str(BACKEND_PORT),
    "MACT_OUTBOX_STALE_QUEUED_SECONDS": "60",
    "MACT_OUTBOX_RECONCILE_INTERVAL_MS": "15000",
    # Short recovery cycle so a drill finishes in minutes, not tens of minutes.
    "MACT_RECOVERY_INTERVAL_MS": "5000",
    "MACT_RECOVERY_LEASE_GRACE_SECONDS": "5",
    "MACT_RECOVERY_RETRY_BACKOFF_SECONDS": "10",
    "MACT_RECOVERY_MAX_ATTEMPTS": "3",
}

AI_ENV = {
    "KMP_DUPLICATE_LIB_OK": "TRUE",
    "MACT_AI_REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
    "MACT_AI_MINIO_ENDPOINT": f"127.0.0.1:{MINIO_PORT}",
    "MACT_AI_MINIO_ACCESS_KEY": MINIO_ACCESS_KEY,
    "MACT_AI_MINIO_SECRET_KEY": MINIO_SECRET_KEY,
    "MACT_AI_BACKEND_URL": BASE,
    "MACT_AI_SERVICE_TOKEN": SERVICE_TOKEN,
    "MACT_AI_DEVICE": "cuda",
    "MACT_AI_WHISPER_DEVICE": "cuda",
    "MACT_AI_WHISPER_COMPUTE_TYPE": "float16",
    "MACT_AI_DEMO_ENABLED": "false",
    "MACT_AI_EVIDENCE_FRAMES_ENABLED": "true",
    "MACT_AI_EVIDENCE_FRAME_MAX_PER_WINDOW": "4",
    "MACT_AI_BEHAVIOR_WEIGHTS": str(CHECKPOINT),
    "MACT_AI_BEHAVIOR_CONFIDENCE": "0.4",
    "MACT_AI_VISION_BACKEND": "yolo",
    "MACT_AI_FACE_WEIGHTS": str(ROOT / "models" / "yolov8n.pt"),
    "MACT_AI_RESULT_BUCKET": MINIO_BUCKET,
    "MACT_AI_RESULT_PREFIX": RESULT_PREFIX,
    "MACT_AI_READY_STALE_SECONDS": "15",
    "MACT_AI_READY_PING_TIMEOUT_SECONDS": "2",
    "PYTHONUNBUFFERED": "1",
}


class IsolationError(RuntimeError):
    """Raised when a target is outside the frozen round-2 allow-list."""


def assert_isolated_target(target: Mapping[str, Any] | None = None,
                           **overrides: Any) -> dict:
    """Return the normalised target dict or raise :class:`IsolationError`.

    Any container, port or database outside the frozen allow-list aborts the
    run. The error message carries the *actual* offending values so a caller
    cannot mistake which target was rejected.
    """
    merged: dict[str, Any] = {
        "pgContainer": PG_CONTAINER,
        "redisContainer": REDIS_CONTAINER,
        "minioContainer": MINIO_CONTAINER,
        "pgPort": PG_PORT,
        "redisPort": REDIS_PORT,
        "minioPort": MINIO_PORT,
        "backendPort": BACKEND_PORT,
        "aiPorts": [AI_A_PORT, AI_B_PORT],
        "database": PG_DB,
    }
    if target:
        merged.update(dict(target))
    merged.update(overrides)

    problems: list[str] = []
    for key in ("pgContainer", "redisContainer", "minioContainer"):
        value = merged.get(key)
        if not isinstance(value, str) or value not in ALLOWED_CONTAINERS:
            problems.append(f"{key}={value!r} is not an allowed container "
                            f"{sorted(ALLOWED_CONTAINERS)}")
        elif value.startswith(FORBIDDEN_CONTAINER_PREFIXES) or value in FORBIDDEN_CONTAINERS:
            problems.append(f"{key}={value!r} is a forbidden target")

    ports: list[tuple[str, Any]] = [
        ("pgPort", merged.get("pgPort")),
        ("redisPort", merged.get("redisPort")),
        ("minioPort", merged.get("minioPort")),
        ("backendPort", merged.get("backendPort")),
    ]
    ai_ports = merged.get("aiPorts") or []
    if not isinstance(ai_ports, Iterable) or isinstance(ai_ports, (str, bytes)):
        problems.append(f"aiPorts={ai_ports!r} is not a list of ports")
        ai_ports = []
    ports.extend((f"aiPorts[{i}]", port) for i, port in enumerate(ai_ports))
    for label, port in ports:
        if not isinstance(port, int) or isinstance(port, bool):
            problems.append(f"{label}={port!r} is not an integer port")
        elif port in FORBIDDEN_PORTS:
            problems.append(f"{label}={port} is a forbidden port")
        elif port not in ALLOWED_PORTS:
            problems.append(f"{label}={port} is outside the allowed ports "
                            f"{sorted(ALLOWED_PORTS)}")

    database = merged.get("database")
    if database not in ALLOWED_DATABASES:
        problems.append(f"database={database!r} is not an allowed database "
                        f"{sorted(ALLOWED_DATABASES)}")

    if problems:
        raise IsolationError(
            "refusing to touch a non-isolated target: " + "; ".join(problems)
            + f" | actual={merged}")
    return merged


# --- database helpers (round-2 instance) ----------------------------------
def psql(sql: str, timeout: float = 60.0) -> list[list[str]]:
    """Run SQL on the isolated round-2 database.

    ``capacity_common.psql`` is bound to the previous round's container and
    database constants and is owned by another agent, so this module carries a
    copy that targets ``mact-r2-postgres`` / ``macteval_round2`` only.
    """
    assert_isolated_target()
    result = subprocess.run(
        ["docker", "exec", "-e", "PGCLIENTENCODING=UTF8", PG_CONTAINER,
         "psql", "-U", PG_USER, "-d", PG_DB,
         "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.strip()[:500]}")
    return [line.split("|") for line in result.stdout.strip().splitlines() if line]


def psql_one(sql: str) -> list[str] | None:
    rows = psql(sql)
    return rows[0] if rows else None


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


JOB_COLUMNS = [
    "status", "attempt", "lease_until", "heartbeat_at", "created_at",
    "completed_at", "recovery_count", "next_recovery_at", "last_result_ref",
    "error_message", "result_metadata",
]


V28_COLUMNS = {"recovery_count", "next_recovery_at", "last_result_ref"}


def job_row(job_id: str) -> dict:
    """Read one job row, tolerating a database that has not run V28 yet.

    Before V28 the recovery columns do not exist; selecting them would raise and
    hide the real state. The fallback returns the same keys as NULL so every
    assertion reports before/after values instead of crashing.
    """
    if V28_COLUMNS <= schema_columns("analysis_jobs"):
        recovery_cols = (
            "coalesce(recovery_count,0), "
            "coalesce(to_char(next_recovery_at,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
            "coalesce(last_result_ref,''), ")
    else:
        recovery_cols = "null, null, null, "
    row = psql_one(
        "select status, attempt, "
        "coalesce(to_char(lease_until,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
        "coalesce(to_char(heartbeat_at,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
        "coalesce(to_char(created_at,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
        "coalesce(to_char(completed_at,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
        + recovery_cols +
        "coalesce(error_message,''), coalesce(result_metadata,'') "
        f"from analysis_jobs where id={sql_literal(job_id)}")
    if row is None:
        raise RuntimeError(f"job not found in DB: {job_id}")
    data = dict(zip(JOB_COLUMNS, row))
    for key in ("attempt", "recovery_count"):
        try:
            data[key] = int(data[key])
        except (TypeError, ValueError):
            data[key] = None
    return data


def job_exists(job_id: str) -> bool:
    return psql_one(
        f"select 1 from analysis_jobs where id={sql_literal(job_id)}") is not None


def count_rows(table: str, where: str) -> int:
    row = psql_one(f"select count(*) from {table} where {where}")
    return int(row[0]) if row else 0


def business_counts(lesson_id: str, job_id: str) -> dict:
    """Row counts used for the "no duplicate business data" assertions."""
    return {
        "observations": count_rows("behavior_observations",
                                   f"job_id={sql_literal(job_id)}"),
        "distinctObservationWindows": _distinct_windows(job_id),
        "windowFeatures": count_rows("lesson_window_features",
                                     f"lesson_session_id={sql_literal(lesson_id)}"),
        "suggestions": count_rows("suggestions",
                                  f"lesson_session_id={sql_literal(lesson_id)}"),
        "evidenceFrames": count_rows("behavior_evidence_frames",
                                     f"job_id={sql_literal(job_id)}"),
        "transcripts": count_rows("lesson_transcripts",
                                  f"job_id={sql_literal(job_id)}"),
    }


def _distinct_windows(job_id: str) -> int:
    row = psql_one(
        "select count(distinct window_index) from behavior_observations "
        f"where job_id={sql_literal(job_id)}")
    return int(row[0]) if row else 0


def outbox_row(job_id: str) -> dict | None:
    rows = psql(
        "select coalesce(to_char(published_at,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
        "attempt, coalesce(last_error,'') "
        f"from task_outbox where aggregate_id={sql_literal(job_id)} "
        "order by created_at desc limit 1")
    if not rows:
        return None
    return dict(zip(["published_at", "attempt", "last_error"], rows[0]))


def force_expired_lease(job_id: str, seconds_ago: int = 30) -> None:
    """Make a RUNNING lease look expired (isolated DB only, recorded as accelerated)."""
    assert_isolated_target()
    psql("update analysis_jobs set lease_until = now() - interval "
         f"'{int(seconds_ago)} seconds' where id={sql_literal(job_id)}")


# --- MinIO / HTTP helpers --------------------------------------------------
def minio_client():
    from minio import Minio
    return Minio(f"127.0.0.1:{MINIO_PORT}", access_key=MINIO_ACCESS_KEY,
                 secret_key=MINIO_SECRET_KEY, secure=False)


def result_objects(job_id: str, attempt: int) -> dict:
    """List ``analysis-results/{job}/{attempt}/`` and report reachability.

    A store error is reported as ``{"error": ...}`` and is never conflated with
    "the object does not exist".
    """
    prefix = f"{RESULT_PREFIX}/{job_id}/{attempt}/"
    try:
        client = minio_client()
        keys = [obj.object_name for obj in client.list_objects(
            MINIO_BUCKET, prefix=prefix, recursive=True)]
        return {"error": None, "keys": keys,
                "hasResult": any(k.endswith("/result.json") for k in keys),
                "hasEnvelope": any(k.endswith("/envelope.json") for k in keys)}
    except Exception as exc:  # noqa: BLE001 - reachability must stay observable
        return {"error": f"{type(exc).__name__}: {exc}", "keys": [],
                "hasResult": False, "hasEnvelope": False}


def http_get(url: str, timeout: float = 3.0) -> dict:
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:300]}
        return {"status": response.status_code, "body": body,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001 - connection refused is a result
        return {"status": None, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}


def http_ok(url: str, timeout: float = 2.0) -> bool:
    return http_get(url, timeout=timeout).get("status") == 200


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    if port not in ALLOWED_PORTS:
        raise IsolationError(f"port {port} is outside the allowed ports")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


# --- scenario bookkeeping ------------------------------------------------
class ScenarioSkip(RuntimeError):
    """A precondition for the scenario is absent; recorded as ``notRun``."""


class Timeline:
    """Monotonic event log; ``at_s`` is guaranteed non-decreasing."""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self.events: list[dict] = []

    def mark(self, event: str, **fields: Any) -> dict:
        entry: dict[str, Any] = {"at_s": round(time.monotonic() - self._started, 3),
                                 "event": event}
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.events.append(entry)
        return entry

    def elapsed(self) -> float:
        return round(time.monotonic() - self._started, 3)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def add_assertion(result: dict, name: str, ok: bool, expected: Any, actual: Any) -> bool:
    result["assertions"].append({"name": name, "ok": bool(ok),
                                 "expected": expected, "actual": actual})
    return bool(ok)


def derive_pass(result: dict) -> bool:
    """``pass`` is true only when there is at least one assertion and all pass.

    A skipped scenario records ``notRun`` and can never be a pass.
    """
    if result.get("notRun"):
        return False
    assertions = result.get("assertions") or []
    return bool(assertions) and all(bool(item.get("ok")) for item in assertions)


def new_result(scenario: str) -> dict:
    return {
        "scenario": scenario,
        "startedAt": now_iso(),
        "timeline": [],
        "jobId": None,
        "attempts": [],
        "terminal": None,
        "counts": {"before": {}, "after": {}, "delta": {}},
        "duplicateCheck": {"ok": False, "detail": "not evaluated"},
        "pass": False,
        "assertions": [],
        "notes": list(NOTES),
        "notRun": None,
    }


def count_delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict:
    keys = sorted(set(before) | set(after))
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in keys}


def wait_until(fn: Callable[[], Any], timeout: float, interval: float = 1.0) -> Any:
    """Poll ``fn`` until it returns truthy; returns the truthy value or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


# --- process / service rig ------------------------------------------------
def find_backend_pid() -> int | None:
    """PID of the *round-2* backend: jar name and a listener on 18081.

    Matching only the jar name would also match another instance of the same
    artifact; requiring the listening socket on the isolated port keeps these
    stop/start helpers from touching a process this drill does not own.
    """
    if BACKEND_PORT not in ALLOWED_PORTS:
        raise IsolationError(f"backend port {BACKEND_PORT} is not allowed")
    listeners: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (conn.status == psutil.CONN_LISTEN and conn.laddr
                    and conn.laddr.port == BACKEND_PORT and conn.pid):
                listeners.add(conn.pid)
    except (psutil.AccessDenied, OSError):
        listeners = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if proc.info["name"] not in ("java.exe", "java"):
            continue
        if "mact-eval-backend" not in cmd:
            continue
        if listeners and proc.info["pid"] not in listeners:
            continue
        return proc.info["pid"]
    return None


def find_ai_pid(port: int) -> int | None:
    if port not in (AI_A_PORT, AI_B_PORT):
        raise IsolationError(f"AI port {port} is not an allowed test port")
    return find_pid_by_port(port)


def terminate_pid(pid: int, hard: bool = False, wait_s: float = 15.0) -> float:
    started = time.monotonic()
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0.0
    if hard:
        proc.kill()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=wait_s)
    except psutil.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            pass
    return round(time.monotonic() - started, 2)


class Rig:
    """Owns the isolated round-2 backend and AI consumer processes."""

    def __init__(self, api: Api, out_dir: Path, timeline: Timeline,
                 result: dict | None = None) -> None:
        self.api = api
        self.out_dir = out_dir
        self.timeline = timeline
        self.result = result
        self.backend: subprocess.Popen | None = None
        self.ai: dict[int, subprocess.Popen] = {}

    # -- lifecycle
    def wait_http(self, url: str, timeout: float, label: str) -> float:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            if http_ok(url):
                return round(time.monotonic() - started, 2)
            time.sleep(1)
        raise TimeoutError(f"{label} did not become ready within {timeout}s ({url})")

    def wait_redis(self, timeout: float = 60) -> float:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            try:
                if redis_cli("ping", timeout=5) == "PONG":
                    return round(time.monotonic() - started, 2)
            except Exception:  # noqa: BLE001 - container may be mid-restart
                pass
            time.sleep(1)
        raise TimeoutError(f"isolated Redis {REDIS_CONTAINER} did not become ready")

    def start_backend(self, log_name: str = "backend.log") -> subprocess.Popen:
        assert_isolated_target()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not JAR.exists():
            raise ScenarioSkip(f"backend jar missing: {JAR} (run mvn package first)")
        env = os.environ.copy()
        env.update(BACKEND_ENV)
        log = (LOG_DIR / log_name).open("a", encoding="utf-8")
        proc = subprocess.Popen(["java", "-jar", str(JAR)], cwd=str(ROOT),
                                env=env, stdout=log, stderr=subprocess.STDOUT)
        self.wait_http(BASE + "/health", 240, "backend")
        self.backend = proc
        clear_schema_cache()
        self.timeline.mark("backend_ready", port=BACKEND_PORT, pid=proc.pid)
        return proc

    def ensure_backend(self) -> bool:
        if http_ok(BASE + "/health"):
            clear_schema_cache()
            return False
        self.start_backend()
        return True

    def stop_backend(self) -> float:
        pid = find_backend_pid()
        if not pid:
            return 0.0
        elapsed = terminate_pid(pid)
        self.timeline.mark("backend_stopped", elapsed_s=elapsed)
        self.backend = None
        return elapsed

    def start_ai(self, port: int, log_name: str,
                 extra_env: Mapping[str, str] | None = None) -> subprocess.Popen:
        assert_isolated_target()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(AI_ENV)
        if extra_env:
            env.update(dict(extra_env))
        log = (LOG_DIR / log_name).open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(AI_SERVICE), env=env, stdout=log, stderr=subprocess.STDOUT)
        self.wait_http(f"http://127.0.0.1:{port}/ready", 300, f"ai-service:{port}")
        self.ai[port] = proc
        self.timeline.mark("ai_started", port=port, pid=proc.pid)
        return proc

    def ensure_ai(self, port: int = AI_A_PORT, log_name: str = "ai-service.log") -> bool:
        if http_ok(f"http://127.0.0.1:{port}/ready"):
            return False
        self.start_ai(port, log_name)
        return True

    def stop_ai(self, port: int, hard: bool = False) -> float | None:
        pid = find_ai_pid(port)
        if not pid:
            return None
        elapsed = terminate_pid(pid, hard=hard)
        self.ai.pop(port, None)
        self.timeline.mark("ai_killed" if hard else "ai_stopped",
                           port=port, pid=pid, elapsed_s=elapsed)
        return elapsed

    def ensure_services(self) -> None:
        self.ensure_backend()
        self.wait_redis()
        self.ensure_ai()

    # -- domain helpers
    def new_lesson(self, label: str) -> tuple[str, str]:
        suffix = uuid.uuid4().hex[:8]
        self.api.login_or_register(f"recovery-teacher-{suffix}", "capacity123")
        course = self.api.create_course(suffix)
        lesson = self.api.create_session(course, f"恢复演练-{label}-{suffix}")
        return lesson, suffix

    def submit(self, lesson: str, video: Path = VIDEO_90S, **kwargs: Any) -> str:
        if not video.exists():
            raise ScenarioSkip(f"video fixture missing: {video}")
        options = {"total_seats": 30, "sample_interval": 2, "window_duration": 60,
                   "min_observations": 5}
        options.update(kwargs)
        upload = self.api.upload_media(lesson, video, "video", **options)
        job_id = upload["jobId"]
        if self.result is not None:
            self.result["jobId"] = job_id
        self.timeline.mark("job_submitted", lesson=lesson, jobId=job_id,
                           status=upload.get("status"))
        return job_id

    def counts(self, lesson: str, job_id: str) -> dict:
        return business_counts(lesson, job_id)

    # -- log helpers
    def log_path(self, log_name: str) -> Path:
        return LOG_DIR / log_name

    def log_size(self, log_name: str) -> int:
        try:
            return self.log_path(log_name).stat().st_size
        except OSError:
            return 0

    def log_text(self, log_name: str) -> str:
        try:
            return self.log_path(log_name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def count_log(self, log_name: str, needle: str) -> int:
        return self.log_text(log_name).count(needle)

    def wait_log_after(self, log_name: str, needle: str, offset: int,
                       timeout: float) -> float | None:
        started = time.monotonic()
        path = self.log_path(log_name)
        while time.monotonic() - started < timeout:
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    appended = handle.read().decode("utf-8", errors="replace")
                if needle in appended:
                    return round(time.monotonic() - started, 2)
            except OSError:
                pass
            time.sleep(0.5)
        return None


# --- capability probes (observed at runtime, never inferred from intent) --
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def fault_hook_supported(name: str = "MACT_AI_FAULT_STOP_AFTER_RESULT_PUT") -> bool:
    return any(name in _read_text(path) for path in (AI_SERVICE / "app").rglob("*.py"))


def capability_matrix() -> dict:
    """Report which protocol pieces the current source tree actually exposes.

    The values are facts about files and migration columns; they are printed in
    every summary so a skip reason is auditable and cannot be hand-waved.
    """
    columns = schema_columns("analysis_jobs")
    return {
        "analysisJobColumns": sorted(columns),
        "hasRecoveryColumns": {"recovery_count", "last_result_ref",
                               "next_recovery_at"} <= columns,
        "resultStoreModule": (AI_SERVICE / "app" / "workers" / "result_store.py").exists(),
        "readinessModule": (AI_SERVICE / "app" / "core" / "readiness.py").exists(),
        "aiWorkerUsesResultStore": "result_store" in _read_text(
            AI_SERVICE / "app" / "workers" / "base.py"),
        "backendRecoveryService": any(
            path.name == "JobRecoveryService.java"
            for path in (ROOT / "backend" / "src" / "main" / "java").rglob("*.java")),
        "faultHookResultPut": fault_hook_supported(),
        "jarMtime": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                   time.localtime(JAR.stat().st_mtime))
                     if JAR.exists() else None),
    }


_SCHEMA_CACHE: dict[str, set[str]] = {}


def clear_schema_cache() -> None:
    _SCHEMA_CACHE.clear()


def schema_columns(table: str) -> set[str]:
    if table not in _SCHEMA_CACHE:
        try:
            rows = psql("select column_name from information_schema.columns "
                        f"where table_name={sql_literal(table)}")
            _SCHEMA_CACHE[table] = {row[0] for row in rows}
        except Exception:  # noqa: BLE001 - unreachable DB means "no columns known"
            return set()
    return _SCHEMA_CACHE[table]


def require(result: dict, caps: Mapping[str, Any], key: str, reason: str) -> None:
    """Raise :class:`ScenarioSkip` when a needed capability is missing."""
    if not caps.get(key):
        raise ScenarioSkip(f"{reason} [capability {key}={caps.get(key)!r}]")


# --- service-token HTTP (claim / heartbeat / complete) --------------------
def _svc_post(path: str, body: Mapping[str, Any] | None = None,
              timeout: float = 30.0) -> dict:
    payload = dict(body or {})
    try:
        response = requests.post(BASE + path, json=payload,
                                 headers={"X-Service-Token": SERVICE_TOKEN},
                                 timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - reachability is a result
        return {"httpStatus": None, "data": {},
                "error": f"{type(exc).__name__}: {exc}"}
    try:
        parsed = response.json()
    except ValueError:
        parsed = {"raw": response.text[:500]}
    data = parsed.get("data") if isinstance(parsed, dict) else None
    return {"httpStatus": response.status_code, "data": data or {}, "raw": parsed}


def svc_claim(job_id: str, lease_seconds: int = 120) -> dict:
    return _svc_post(f"/jobs/{job_id}/claim", {"leaseSeconds": lease_seconds})


def svc_heartbeat(job_id: str, attempt: int | None = None,
                  lease_seconds: int = 120) -> dict:
    body: dict[str, Any] = {"leaseSeconds": lease_seconds}
    if attempt is not None:
        body["attempt"] = attempt
    return _svc_post(f"/jobs/{job_id}/heartbeat", body)


def svc_complete(job_id: str, result: Mapping[str, Any]) -> dict:
    return _svc_post(f"/jobs/{job_id}/complete", result)


class JobWatch:
    """DB-side polling: the backend may be intentionally down or unresponsive."""

    def __init__(self, job_id: str, result: dict, timeline: Timeline,
                 interval: float = 1.5) -> None:
        self.job_id = job_id
        self.result = result
        self.timeline = timeline
        self.interval = interval
        self._last_key: tuple | None = None
        self.last: dict | None = None

    def poll(self) -> dict:
        row = job_row(self.job_id)
        self.last = row
        key = (row.get("status"), row.get("attempt"), row.get("recovery_count"))
        if key != self._last_key:
            self._last_key = key
            entry = {"at_s": self.timeline.elapsed(), "status": row.get("status"),
                     "attempt": row.get("attempt"),
                     "recoveryCount": row.get("recovery_count"),
                     "leaseUntil": row.get("lease_until")}
            self.result.setdefault("attempts", []).append(entry)
            self.timeline.mark("job_state", **{k: entry[k] for k in
                                               ("status", "attempt", "recoveryCount")})
        return row

    def until(self, predicate: Callable[[dict], bool], timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.poll()
            if predicate(row):
                return row
            time.sleep(self.interval)
        return None

    def until_terminal(self, timeout: float) -> dict | None:
        return self.until(lambda row: row.get("status") in TERMINAL, timeout)

    def status(self) -> str:
        return str(self.poll().get("status"))


def recipe_result(lesson: str, job_id: str, attempt: int | None = None) -> dict:
    """Minimal but structurally valid completion payload for HTTP-level drills."""
    payload: dict[str, Any] = {
        "status": "succeeded",
        "observations": [],
        "frames": [],
        "window_count": 0,
        "model_digest": "fault-drill",
    }
    if attempt is not None:
        payload["attempt"] = attempt
    payload["drill"] = {"lessonSessionId": lesson, "jobId": job_id}
    return payload


def result_index_exists(job_id: str, attempt: int) -> bool:
    try:
        out = redis_cli("exists", f"analysis:result:{job_id}:{attempt}")
        return out.strip() == "1"
    except Exception:  # noqa: BLE001 - Redis may be down on purpose
        return False


def result_index_ttl(job_id: str, attempt: int) -> int | None:
    try:
        return int(redis_cli("ttl", f"analysis:result:{job_id}:{attempt}").strip())
    except Exception:  # noqa: BLE001
        return None


def redis_message_pending(job_id: str) -> int | None:
    """Total PEL length for the job group (``XPENDING`` summary, best effort).

    ``job_id`` is kept so callers read as intent-revealing; the count is the
    group-wide pending total, which is what "the message is still un-ACKed"
    means on an instance reserved for this drill.
    """
    try:
        out = redis_cli("xpending", JOB_STREAM, JOB_GROUP)
        first = out.splitlines()[0].strip() if out else ""
        return int(first) if first.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def drop_job_redis_state(job_id: str, attempt: int) -> dict:
    """Simulate Redis stream/PEL loss (never FLUSHALL).

    F04 needs the job's notification path to disappear completely, so the
    consumer group and the whole stream key are destroyed. That wipes every
    message in ``analysis:jobs`` on ``mact-cap-redis``, which is why the drill
    requires an exclusive instance and why this is the only place the stream key
    is deleted. Everything else is job-scoped: the result index, the pending
    set member and the stored callback for ``job_id`` only.
    """
    assert_isolated_target()
    removed: dict[str, Any] = {}
    removed["group"] = redis_cli("xgroup", "destroy", JOB_STREAM, JOB_GROUP)
    removed["stream"] = redis_cli("del", JOB_STREAM)
    removed["index"] = redis_cli("del", f"analysis:result:{job_id}:{attempt}")
    removed["pendingSet"] = redis_cli("srem", "analysis:results:pending", job_id)
    removed["callback"] = redis_cli("del", f"analysis:pending_callback:{job_id}")
    removed["pendingCallbacks"] = redis_cli("srem", "analysis:pending_callbacks", job_id)
    return removed


def minio_drop_result(job_id: str, attempt: int) -> dict:
    """Delete only ``analysis-results/{job}/{attempt}/`` objects (scoped)."""
    assert_isolated_target()
    prefix = f"{RESULT_PREFIX}/{job_id}/{attempt}/"
    removed = []
    try:
        client = minio_client()
        for obj in client.list_objects(MINIO_BUCKET, prefix=prefix, recursive=True):
            client.remove_object(MINIO_BUCKET, obj.object_name)
            removed.append(obj.object_name)
        return {"error": None, "removed": removed}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "removed": removed}


def container_action(action: str, container: str) -> dict:
    """Start/stop one allowed container; anything else aborts first."""
    assert_isolated_target()
    if container not in ALLOWED_CONTAINERS:
        raise IsolationError(f"container {container!r} is not an allowed target")
    if action not in ("start", "stop"):
        raise IsolationError(f"container action {action!r} is not allowed")
    result = subprocess.run(["docker", action, container], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120)
    return {"action": action, "container": container,
            "returncode": result.returncode,
            "stderr": (result.stderr or "").strip()[:300]}


# --- F04 ------------------------------------------------------------------
def scenario_result_before_redis_loss(ctx: dict) -> dict:
    """F04: RUNNING with a persisted result survives a full Redis loss."""
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    rig: Rig = ctx["rig"]
    caps = ctx["caps"]
    require(result, caps, "aiWorkerUsesResultStore",
            "worker does not persist results to MinIO yet (A5 result_store wiring)")
    if not caps.get("backendRecoveryService"):
        raise ScenarioSkip("backend JobRecoveryService is missing (A7)")
    rig.ensure_backend()
    rig.wait_redis()
    rig.ensure_ai(AI_A_PORT, "ai-service.log")
    rig.stop_ai(AI_B_PORT)          # one claimant keeps the timeline unambiguous
    timeline.mark("second_consumer_absent")

    lesson, _ = rig.new_lesson("f04")
    log_name = "ai-service.log"
    marker = f"Starting video analysis: lesson={lesson}"
    starts_before = rig.count_log(log_name, marker)
    job_id = rig.submit(lesson)
    watch = JobWatch(job_id, result, timeline)

    running = watch.until(lambda row: row.get("status") == "RUNNING", 120)
    if running is None:
        raise ScenarioSkip("job never became RUNNING")
    attempt = int(running.get("attempt") or 0)
    result["attempts"].append({"at_s": timeline.elapsed(), "status": "RUNNING",
                               "attempt": attempt,
                               "recoveryCount": running.get("recovery_count"),
                               "leaseUntil": running.get("lease_until"),
                               "event": "claimed_by_worker"})
    # Block the callback while inference is still running: this leaves the job
    # RUNNING with a persisted result instead of racing a successful callback.
    rig.stop_backend()
    timeline.mark("backend_stopped_before_callback")
    counts_before = rig.counts(lesson, job_id)

    objects = wait_until(
        lambda: (lambda probe: probe if probe.get("hasEnvelope") else None)(
            result_objects(job_id, attempt)), 300, 1.0)
    if not objects:
        probe = result_objects(job_id, attempt)
        raise ScenarioSkip(
            "no envelope.json was written for this job; result store is not "
            f"active in the worker (probe={probe})")

    timeline.mark("result_envelope_present", keys=objects.get("keys"))
    rig.stop_ai(AI_A_PORT, hard=True)
    redis_state = drop_job_redis_state(job_id, attempt)
    timeline.mark("redis_state_dropped", **{k: v for k, v in redis_state.items()})
    force_expired_lease(job_id)
    timeline.mark("lease_expired", accelerated=True,
                  method="UPDATE lease_until = now() - 30s (no real wait)")

    rig.start_backend()
    watch.poll()
    terminal = watch.until_terminal(300)
    counts_after = rig.counts(lesson, job_id)
    result["counts"] = {"before": counts_before, "after": counts_after,
                        "delta": count_delta(counts_before, counts_after)}
    starts_after = rig.count_log(log_name, marker)

    row = watch.last or {}
    add_assertion(result, "job_reached_terminal", terminal is not None, True,
                  terminal is not None)
    add_assertion(result, "terminal_succeeded",
                  (row.get("status") == "SUCCEEDED"), "SUCCEEDED", row.get("status"))
    add_assertion(result, "attempt_unchanged", row.get("attempt") == attempt,
                  attempt, row.get("attempt"))
    add_assertion(result, "not_requeued", int(row.get("recovery_count") or 0) == 0,
                  0, row.get("recovery_count"))
    add_assertion(result, "no_reinference", starts_after - starts_before == 1,
                  1, starts_after - starts_before)
    add_assertion(result, "observations_written",
                  result["counts"]["after"].get("observations", 0) > 0, ">0",
                  result["counts"]["after"].get("observations"))
    add_assertion(result, "window_features_written",
                  result["counts"]["after"].get("windowFeatures", 0) > 0, ">0",
                  result["counts"]["after"].get("windowFeatures"))
    duplicate_ok = (result["counts"]["after"].get("observations")
                    == result["counts"]["after"].get("distinctObservationWindows"))
    result["duplicateCheck"] = {
        "ok": duplicate_ok,
        "detail": "observations == distinct window_index, single inference start",
        "observations": result["counts"]["after"].get("observations"),
        "distinctWindows": result["counts"]["after"].get("distinctObservationWindows"),
        "inferenceStarts": starts_after - starts_before,
    }
    add_assertion(result, "no_duplicate_business_rows", duplicate_ok, True,
                  result["duplicateCheck"])
    result["notes"].append(
        "Redis loss is job-scoped: XGROUP DESTROY + DEL of this job's index keys, "
        "not FLUSHALL. Lease expiry was forced with SQL and is marked accelerated.")
    return result


# --- F05 ------------------------------------------------------------------
def scenario_requeue_after_worker_exit(ctx: dict) -> dict:
    """F05: RUNNING without a result + worker SIGKILL -> requeue -> attempt+1."""
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    rig: Rig = ctx["rig"]
    caps = ctx["caps"]
    require(result, caps, "backendRecoveryService",
            "backend JobRecoveryService is missing (A7)")
    rig.ensure_backend()
    rig.wait_redis()
    rig.ensure_ai(AI_A_PORT, "ai-service.log")
    rig.stop_ai(AI_B_PORT)          # a second consumer could reclaim before we look
    timeline.mark("second_consumer_absent")

    lesson, _ = rig.new_lesson("f05")
    log_name = "ai-service.log"
    marker = f"Starting video analysis: lesson={lesson}"
    # A 90s clip finishes inference in ~2s here, which is shorter than the time
    # needed to locate the process and SIGKILL it: the job completed before the
    # fault could land on the first execution of this drill. The 10-minute clip
    # keeps the worker inside inference for ~30s, so the kill lands mid-attempt.
    job_id = rig.submit(lesson, VIDEO_10MIN)
    watch = JobWatch(job_id, result, timeline)
    running = watch.until(lambda row: row.get("status") == "RUNNING", 120)
    if running is None:
        raise ScenarioSkip("job never became RUNNING")
    attempt = int(running.get("attempt") or 0)
    started_at = rig.wait_log_after(log_name, marker, 0, 120)
    add_assertion(result, "inference_started", started_at is not None, True, started_at)

    rig.stop_ai(AI_A_PORT, hard=True)
    kill_row = watch.poll()
    if kill_row.get("status") in TERMINAL:
        raise ScenarioSkip(
            f"the job reached {kill_row.get('status')} before the SIGKILL landed; "
            "F05 needs a live RUNNING attempt that has stored no result")
    existing = result_objects(job_id, attempt)
    if existing.get("error"):
        raise ScenarioSkip(
            f"result store unreachable during F05 setup: {existing['error']}")
    add_assertion(result, "no_result_stored_before_kill",
                  existing.get("hasEnvelope") is False, False, existing)
    force_expired_lease(job_id)
    timeline.mark("lease_expired", accelerated=True,
                  method="UPDATE lease_until = now() - 30s")

    pre_claim = svc_claim(job_id, lease_seconds=120)
    timeline.mark("claim_on_expired_running", **{
        "claimed": pre_claim["data"].get("claimed"),
        "reason": pre_claim["data"].get("reason"),
        "attempt": pre_claim["data"].get("attempt")})
    claim_data = pre_claim["data"]
    add_assertion(result, "expired_running_claim_refused",
                  claim_data.get("claimed") is False, False, claim_data.get("claimed"))
    add_assertion(result, "expired_running_reason_recovery_pending",
                  claim_data.get("reason") == "recovery_pending",
                  "recovery_pending", claim_data.get("reason"))
    add_assertion(result, "expired_running_claim_did_not_increment_attempt",
                  claim_data.get("attempt") == attempt, attempt,
                  claim_data.get("attempt"))
    post_claim_row = watch.poll()
    add_assertion(result, "claim_left_job_running",
                  post_claim_row.get("status") == kill_row.get("status"),
                  kill_row.get("status"), post_claim_row.get("status"))

    # Observe the requeue *before* restarting the consumer: once a live worker is
    # waiting, it claims the QUEUED row within ~1s and a 2s poll would only ever
    # see RUNNING/attempt+1. Requiring the transient QUEUED sample with the worker
    # already running made this drill flaky, not the implementation wrong.
    requeued = watch.until(
        lambda row: int(row.get("recovery_count") or 0) >= 1
        or row.get("status") in TERMINAL, 180)
    add_assertion(result, "recovery_requeued_job", requeued is not None,
                  "recovery_count>=1", requeued and requeued.get("recovery_count"))
    if requeued and requeued.get("status") not in TERMINAL:
        add_assertion(result, "requeue_keeps_attempt",
                      requeued.get("attempt") == attempt, attempt,
                      requeued.get("attempt"))
        add_assertion(result, "requeue_sets_queued",
                      requeued.get("status") == "QUEUED", "QUEUED",
                      requeued.get("status"))
    rig.start_ai(AI_A_PORT, "ai-service.log")

    terminal = watch.until_terminal(420)
    row = watch.last or {}
    counts = rig.counts(lesson, job_id)
    result["counts"] = {"before": {}, "after": counts,
                        "delta": count_delta({}, counts)}
    add_assertion(result, "job_reached_terminal", terminal is not None, True,
                  terminal is not None)
    add_assertion(result, "terminal_succeeded", row.get("status") == "SUCCEEDED",
                  "SUCCEEDED", row.get("status"))
    add_assertion(result, "new_attempt_took_over",
                  int(row.get("attempt") or 0) == attempt + 1, attempt + 1,
                  row.get("attempt"))
    add_assertion(result, "observations_written", counts.get("observations", 0) > 0,
                  ">0", counts.get("observations"))
    duplicate_ok = (counts.get("observations") == counts.get("distinctObservationWindows")
                    and counts.get("windowFeatures", 0) > 0)
    result["duplicateCheck"] = {
        "ok": duplicate_ok,
        "detail": "single set of windows after takeover",
        "observations": counts.get("observations"),
        "distinctWindows": counts.get("distinctObservationWindows"),
        "windowFeatures": counts.get("windowFeatures"),
    }
    add_assertion(result, "no_duplicate_business_rows", duplicate_ok, True,
                  result["duplicateCheck"])
    result["notes"].append(
        "Lease expiry was forced with SQL (accelerated); the AI process was SIGKILLed "
        "mid-inference so no result object could exist.")
    return result


# --- F06 ------------------------------------------------------------------
def scenario_crash_after_put(ctx: dict) -> dict:
    """F06: result object written, worker dies before index/ACK."""
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    rig: Rig = ctx["rig"]
    caps = ctx["caps"]
    require(result, caps, "faultHookResultPut",
            "worker lacks the MACT_AI_FAULT_STOP_AFTER_RESULT_PUT test hook; "
            "the crash window between result PUT and index/ACK cannot be injected "
            "reliably from outside the process")
    require(result, caps, "backendRecoveryService",
            "backend JobRecoveryService is missing (A7)")
    rig.ensure_backend()
    rig.wait_redis()
    # Start the hook-enabled consumer directly; a normal one would claim the
    # job first and then the hook could never fire.
    rig.stop_ai(AI_B_PORT)
    rig.stop_ai(AI_A_PORT, hard=False)
    lesson, _ = rig.new_lesson("f06")
    job_id = rig.submit(lesson)
    watch = JobWatch(job_id, result, timeline)
    rig.start_ai(AI_A_PORT, "ai-service-f06.log",
                 {"MACT_AI_FAULT_STOP_AFTER_RESULT_PUT": "1"})
    timeline.mark("ai_started_with_fault_hook")

    running = watch.until(lambda row: row.get("status") == "RUNNING", 120)
    if running is None:
        raise ScenarioSkip("job never became RUNNING")
    attempt = int(running.get("attempt") or 0)
    exited = wait_until(lambda: find_ai_pid(AI_A_PORT) is None, 300, 1.0)
    timeline.mark("worker_exited_after_put", pid_gone=bool(exited))

    probe = result_objects(job_id, attempt)
    index_present = result_index_exists(job_id, attempt)
    pending = redis_message_pending(job_id)

    add_assertion(result, "worker_exited", bool(exited), True, exited)
    add_assertion(result, "result_object_written", probe.get("hasEnvelope"), True,
                  probe)
    add_assertion(result, "redis_index_not_written", index_present is False, False,
                  index_present)
    add_assertion(result, "stream_message_still_pending",
                  pending is not None and pending >= 1, ">=1", pending)

    force_expired_lease(job_id)
    timeline.mark("lease_expired", accelerated=True,
                  method="UPDATE lease_until = now() - 30s")
    terminal = watch.until_terminal(300)
    row = watch.last or {}
    counts = rig.counts(lesson, job_id)
    result["counts"] = {"before": {}, "after": counts,
                        "delta": count_delta({}, counts)}
    add_assertion(result, "job_reached_terminal", terminal is not None, True,
                  terminal is not None)
    add_assertion(result, "terminal_succeeded", row.get("status") == "SUCCEEDED",
                  "SUCCEEDED", row.get("status"))
    add_assertion(result, "attempt_unchanged", row.get("attempt") == attempt,
                  attempt, row.get("attempt"))
    add_assertion(result, "observations_written", counts.get("observations", 0) > 0,
                  ">0", counts.get("observations"))
    duplicate_ok = counts.get("observations") == counts.get("distinctObservationWindows")
    result["duplicateCheck"] = {"ok": duplicate_ok,
                               "detail": "observations == distinct window_index",
                               "observations": counts.get("observations"),
                               "distinctWindows": counts.get("distinctObservationWindows")}
    add_assertion(result, "no_duplicate_business_rows", duplicate_ok, True,
                  result["duplicateCheck"])
    result["notes"].append(
        "Uses the A5 test hook MACT_AI_FAULT_STOP_AFTER_RESULT_PUT=1, which must stop "
        "the worker after envelope.json is written and before mark_pending/ACK.")
    return result


# --- F07 ------------------------------------------------------------------
def scenario_response_lost(ctx: dict) -> dict:
    """F07: backend committed, HTTP response lost -> retry is idempotent."""
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    rig: Rig = ctx["rig"]
    rig.ensure_services()

    lesson, _ = rig.new_lesson("f07")
    job_id = rig.submit(lesson)
    watch = JobWatch(job_id, result, timeline)
    terminal = watch.until_terminal(300)
    if terminal is None:
        raise ScenarioSkip("job did not reach a terminal state in time")
    row = watch.last or {}
    if row.get("status") != "SUCCEEDED":
        raise ScenarioSkip(f"job terminal state is {row.get('status')}, expected SUCCEEDED")

    stored = job_row(job_id).get("result_metadata") or ""
    try:
        replay_payload = json.loads(stored)
    except ValueError:
        raise ScenarioSkip("result_metadata is not JSON; cannot replay the callback")
    replay_payload.setdefault("attempt", row.get("attempt"))

    before = rig.counts(lesson, job_id)
    first = svc_complete(job_id, replay_payload)
    second = svc_complete(job_id, replay_payload)
    timeline.mark("duplicate_callbacks_replayed",
                  first=first["data"].get("reason"),
                  second=second["data"].get("reason"))
    after = rig.counts(lesson, job_id)
    result["counts"] = {"before": before, "after": after,
                        "delta": count_delta(before, after)}

    add_assertion(result, "first_retry_http_200", first["httpStatus"] == 200, 200,
                  first["httpStatus"])
    add_assertion(result, "first_retry_already_terminal",
                  first["data"].get("applied") is False, False,
                  first["data"].get("applied"))
    add_assertion(result, "first_retry_reason_already_terminal",
                  first["data"].get("reason") == "already_terminal",
                  "already_terminal", first["data"].get("reason"))
    add_assertion(result, "second_retry_reason_already_terminal",
                  second["data"].get("reason") == "already_terminal",
                  "already_terminal", second["data"].get("reason"))
    delta = result["counts"]["delta"]
    unchanged = all(int(value) == 0 for value in delta.values())
    result["duplicateCheck"] = {
        "ok": unchanged,
        "detail": "two duplicate completions changed no business row",
        "delta": delta,
    }
    add_assertion(result, "no_business_rows_added", unchanged, delta, delta)
    final_row = job_row(job_id)
    add_assertion(result, "attempt_unchanged", final_row.get("attempt") == row.get("attempt"),
                  row.get("attempt"), final_row.get("attempt"))
    add_assertion(result, "still_succeeded", final_row.get("status") == "SUCCEEDED",
                  "SUCCEEDED", final_row.get("status"))
    result["notes"].append(
        "The lost response is modelled by replaying the exact stored result_metadata "
        "twice; a real socket reset produces the same backend decision path.")
    return result


# --- F09 ------------------------------------------------------------------
def scenario_minio_unavailable(ctx: dict) -> dict:
    """F09: store outage must defer recovery; only NOT_FOUND may requeue."""
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    rig: Rig = ctx["rig"]
    caps = ctx["caps"]
    require(result, caps, "backendRecoveryService",
            "backend JobRecoveryService is missing (A7)")
    if not caps.get("hasRecoveryColumns"):
        raise ScenarioSkip("migration V28 recovery columns are missing")
    rig.ensure_backend()
    rig.wait_redis()
    rig.ensure_ai(AI_A_PORT, "ai-service.log")
    rig.stop_ai(AI_B_PORT)          # one claimant: F09 needs a single owner
    timeline.mark("second_consumer_absent")

    lesson, _ = rig.new_lesson("f09")
    job_id = rig.submit(lesson)
    watch = JobWatch(job_id, result, timeline)
    running = watch.until(lambda row: row.get("status") == "RUNNING", 120)
    if running is None:
        raise ScenarioSkip("job never became RUNNING")
    attempt = int(running.get("attempt") or 0)
    rig.stop_ai(AI_A_PORT, hard=True)
    force_expired_lease(job_id)
    timeline.mark("lease_expired", accelerated=True,
                  method="UPDATE lease_until = now() - 30s")

    stop = container_action("stop", MINIO_CONTAINER)
    timeline.mark("minio_stopped", **stop)
    try:
        # Three recovery cycles must not requeue an unreadable store.
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            watch.poll()
            time.sleep(2)
        during = watch.poll()
        result["duringOutage"] = {
            "status": during.get("status"), "attempt": during.get("attempt"),
            "recoveryCount": during.get("recovery_count"),
            "nextRecoveryAt": during.get("next_recovery_at"),
            "resultProbe": result_objects(job_id, attempt),
        }
    finally:
        start = container_action("start", MINIO_CONTAINER)
        timeline.mark("minio_started", **start)
        started = time.monotonic()
        while time.monotonic() - started < 90:
            try:
                minio_client().list_buckets()
                break
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(2)

    during = result.get("duringOutage", {})
    add_assertion(result, "store_outage_not_treated_as_missing",
                  during.get("status") == "RUNNING", "RUNNING", during.get("status"))
    add_assertion(result, "store_outage_did_not_requeue",
                  int(during.get("recoveryCount") or 0) == 0, 0,
                  during.get("recoveryCount"))
    add_assertion(result, "store_outage_probe_reported_error",
                  bool(during.get("resultProbe", {}).get("error")), True,
                  during.get("resultProbe", {}).get("error"))

    requeued = watch.until(lambda row: int(row.get("recovery_count") or 0) >= 1
                           or row.get("status") in TERMINAL, 240)
    add_assertion(result, "requeued_after_store_recovered", requeued is not None,
                  "recovery_count>=1", requeued and requeued.get("recovery_count"))
    # The scenario SIGKILLed the only consumer before the outage; without a
    # worker the requeued job would sit in QUEUED forever and the terminal
    # assertions below would fail for an unrelated reason. Restart it only
    # after the store is confirmed reachable.
    if http_ok(f"http://127.0.0.1:{AI_A_PORT}/ready") is False:
        rig.start_ai(AI_A_PORT, "ai-service.log")
    terminal = watch.until_terminal(420)
    row = watch.last or {}
    counts = rig.counts(lesson, job_id)
    result["counts"] = {"before": {}, "after": counts,
                        "delta": count_delta({}, counts)}
    add_assertion(result, "job_reached_terminal", terminal is not None, True,
                  terminal is not None)
    add_assertion(result, "terminal_succeeded", row.get("status") == "SUCCEEDED",
                  "SUCCEEDED", row.get("status"))
    add_assertion(result, "recovered_with_new_attempt",
                  int(row.get("attempt") or 0) == attempt + 1, attempt + 1,
                  row.get("attempt"))
    add_assertion(result, "observations_written", counts.get("observations", 0) > 0,
                  ">0", counts.get("observations"))
    duplicate_ok = counts.get("observations") == counts.get("distinctObservationWindows")
    result["duplicateCheck"] = {"ok": duplicate_ok,
                               "detail": "observations == distinct window_index",
                               "observations": counts.get("observations"),
                               "distinctWindows": counts.get("distinctObservationWindows")}
    add_assertion(result, "no_duplicate_business_rows", duplicate_ok, True,
                  result["duplicateCheck"])
    result["notes"].append(
        f"Only {MINIO_CONTAINER} is stopped/started. The 403 branch (wrong credentials) "
        "is not exercised here: it needs a credential rotation and is covered by the "
        "JobResultStore unit tests; unreachable-vs-NOT_FOUND is proven by the "
        "outage->RUNNING then reachable->requeue sequence above.")
    result["notes"].append(
        "After the store is reachable again the requeued job is executed by a new "
        "attempt and the inference runs once more. That is the expected F09 behaviour "
        "(an ERROR during the outage is never treated as NOT_FOUND; only a confirmed "
        "missing result may trigger a re-run), not duplicate inference: the failed "
        "attempt stored nothing, so there is exactly one result object per attempt and "
        "observations == distinct window_index.")
    return result


# --- F01/F02/F03 ----------------------------------------------------------
def _start_job_running(rig: Rig, result: dict, timeline: Timeline, label: str
                       ) -> tuple[str, str, int, JobWatch]:
    lesson, _ = rig.new_lesson(label)
    job_id = rig.submit(lesson)
    watch = JobWatch(job_id, result, timeline)
    queued = watch.until(lambda row: row.get("status") == "QUEUED", 60)
    if queued is None:
        raise ScenarioSkip("job never became QUEUED")
    claim = svc_claim(job_id, lease_seconds=120)
    if not (claim["httpStatus"] == 200 and claim["data"].get("claimed")):
        raise ScenarioSkip(f"could not claim a fresh job: {claim}")
    attempt = int(claim["data"].get("attempt") or 0)
    timeline.mark("claimed", attempt=attempt)
    return lesson, job_id, attempt, watch


def scenario_f01_f03(ctx: dict) -> dict:
    """F01 valid lease, F02 renew/complete during scan, F03 two scanners."""
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    rig: Rig = ctx["rig"]
    caps = ctx["caps"]
    require(result, caps, "backendRecoveryService",
            "backend JobRecoveryService is missing (A7); F01/F03 cannot be observed")
    # No consumer may claim these drills, so Redis/backend are checked directly
    # instead of ensure_services() (which would start an AI only to kill it).
    rig.ensure_backend()
    rig.wait_redis()
    stopped = {str(port): rig.stop_ai(port) for port in (AI_A_PORT, AI_B_PORT)}
    timeline.mark("consumers_stopped", **stopped)

    # F01: a live lease must be neither reclaimed nor re-claimed.
    _, job1, attempt1, watch1 = _start_job_running(
        rig, result, timeline, "f01")
    before1 = watch1.poll()
    time.sleep(25)                       # >= 5 recovery intervals (5s each)
    after1 = watch1.poll()
    add_assertion(result, "f01_still_running", after1.get("status") == "RUNNING",
                  "RUNNING", after1.get("status"))
    add_assertion(result, "f01_attempt_unchanged", after1.get("attempt") == attempt1,
                  attempt1, after1.get("attempt"))
    add_assertion(result, "f01_not_requeued", int(after1.get("recovery_count") or 0) == 0,
                  0, after1.get("recovery_count"))
    claim_live = svc_claim(job1)
    add_assertion(result, "f01_claim_refused_while_leased",
                  claim_live["data"].get("claimed") is False, False,
                  claim_live["data"])
    add_assertion(result, "f01_claim_reason_leased",
                  claim_live["data"].get("reason") == "leased", "leased",
                  claim_live["data"].get("reason"))
    add_assertion(result, "f01_lease_not_shortened",
                  after1.get("lease_until") == before1.get("lease_until"),
                  before1.get("lease_until"), after1.get("lease_until"))

    # F02a: lease expires, then a heartbeat renews it before the scanner acts.
    _, job2, attempt2, watch2 = _start_job_running(
        rig, result, timeline, "f02")
    force_expired_lease(job2)
    hb = svc_heartbeat(job2, attempt2)
    timeline.mark("heartbeat_renewed_before_scan",
                  extended=hb["data"].get("extended"))
    watch2.poll()
    add_assertion(result, "f02_heartbeat_extended",
                  hb["data"].get("extended") is True, True, hb["data"])
    time.sleep(25)
    after2 = watch2.poll()
    add_assertion(result, "f02_renewed_job_not_reclaimed",
                  after2.get("status") == "RUNNING"
                  and int(after2.get("recovery_count") or 0) == 0,
                  {"status": "RUNNING", "recoveryCount": 0},
                  {"status": after2.get("status"),
                   "recoveryCount": after2.get("recovery_count")})
    add_assertion(result, "f02_attempt_unchanged", after2.get("attempt") == attempt2,
                  attempt2, after2.get("attempt"))

    # F02b: the job completes while a scan is pending; no reclaim afterwards.
    lesson3, job3, attempt3, watch3 = _start_job_running(
        rig, result, timeline, "f02b")
    force_expired_lease(job3)
    done = svc_complete(job3, recipe_result(lesson3, job3, attempt3))
    timeline.mark("completed_during_scan", applied=done["data"].get("applied"))
    time.sleep(25)
    after3 = watch3.poll()
    add_assertion(result, "f02_completion_applied",
                  done["data"].get("applied") is True, True, done["data"])
    add_assertion(result, "f02_completed_job_not_reclaimed",
                  after3.get("status") in TERMINAL
                  and int(after3.get("recovery_count") or 0) == 0,
                  {"terminal": True, "recoveryCount": 0},
                  {"status": after3.get("status"),
                   "recoveryCount": after3.get("recovery_count")})

    # F03: after a requeue, concurrent claims may advance attempt exactly once.
    _, job4, attempt4, watch4 = _start_job_running(
        rig, result, timeline, "f03")
    rig.stop_ai(AI_A_PORT, hard=True)
    force_expired_lease(job4)
    requeued = watch4.until(lambda row: row.get("status") == "QUEUED", 180)
    add_assertion(result, "f03_recovery_requeued", requeued is not None, "QUEUED",
                  requeued and requeued.get("status"))
    claims: list[dict] = []
    lock = threading.Lock()

    def _claim() -> None:
        response = svc_claim(job4)
        with lock:
            claims.append(response["data"])

    threads = [threading.Thread(target=_claim) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    claimed = [item for item in claims if item.get("claimed")]
    attempts_after = sorted({int(item.get("attempt") or 0) for item in claims})
    timeline.mark("concurrent_claims", count=len(claims),
                  claimed=len(claimed), attempts=attempts_after)
    add_assertion(result, "f03_exactly_one_claim_wins", len(claimed) == 1, 1,
                  len(claimed))
    add_assertion(result, "f03_all_claims_report_same_attempt",
                  len(attempts_after) == 1, 1, attempts_after)
    add_assertion(result, "f03_attempt_advanced_once",
                  attempts_after == [attempt4 + 1], [attempt4 + 1], attempts_after)
    final = job_row(job4)
    add_assertion(result, "f03_db_attempt_matches", final.get("attempt") == attempt4 + 1,
                  attempt4 + 1, final.get("attempt"))
    result["duplicateCheck"] = {
        "ok": len(claimed) == 1 and attempts_after == [attempt4 + 1],
        "detail": "6 concurrent claims on a requeued job produced one winner and one "
                  "attempt increment",
        "claimedCount": len(claimed),
        "attempts": attempts_after,
    }
    per_job = {}
    for job in (job1, job2, job3, job4):
        per_job[job] = {
            "observations": count_rows("behavior_observations",
                                       f"job_id={sql_literal(job)}"),
            "distinctWindows": _distinct_windows(job),
        }
    result["counts"] = {"before": {}, "after": per_job,
                        "delta": count_delta({}, {
                            key: value["observations"]
                            for key, value in per_job.items()})}
    result["notes"].append(
        "F01-F03 are driven over the service-token HTTP protocol plus direct DB reads; "
        "the 5s recovery interval and forced lease expiry keep each wait under 30s. "
        "F02/F03 rely on the conditional UPDATE in JobRecoveryService: a reclaimed "
        "job would show recoveryCount>0 or a second attempt increment.")
    return result


# --- F10/F11/F12 ----------------------------------------------------------
def scenario_f10_f12(ctx: dict) -> dict:
    """F10 stale result after requeue, F11 stale heartbeat/callback, F12 exhaustion."""
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    rig: Rig = ctx["rig"]
    caps = ctx["caps"]
    require(result, caps, "backendRecoveryService",
            "backend JobRecoveryService is missing (A7); F10-F12 need a real requeue")
    rig.ensure_backend()
    rig.wait_redis()
    stopped = {str(port): rig.stop_ai(port) for port in (AI_A_PORT, AI_B_PORT)}
    timeline.mark("consumers_stopped", **stopped)

    # F10: requeue, then the old attempt's result arrives.
    lesson, job_id, attempt, watch = _start_job_running(
        rig, result, timeline, "f10")
    before = rig.counts(lesson, job_id)
    force_expired_lease(job_id)
    requeued = watch.until(lambda row: row.get("status") == "QUEUED", 180)
    if requeued is None:
        raise ScenarioSkip("recovery did not requeue the job; cannot test F10")
    late = svc_complete(job_id, recipe_result(lesson, job_id, attempt))
    timeline.mark("stale_result_after_requeue", applied=late["data"].get("applied"),
                  reason=late["data"].get("reason"))
    after = rig.counts(lesson, job_id)
    add_assertion(result, "f10_stale_result_rejected",
                  late["data"].get("applied") is False, False,
                  late["data"].get("applied"))
    add_assertion(result, "f10_reason_not_running_or_stale",
                  late["data"].get("reason") in {"not_running", "stale_attempt"},
                  ["not_running", "stale_attempt"], late["data"].get("reason"))
    add_assertion(result, "f10_job_still_queued",
                  job_row(job_id).get("status") == "QUEUED", "QUEUED",
                  job_row(job_id).get("status"))
    result["counts"] = {"before": before, "after": after,
                        "delta": count_delta(before, after)}
    add_assertion(result, "f10_no_business_rows_from_stale_result",
                  all(int(v) == 0 for v in result["counts"]["delta"].values()),
                  "all zero", result["counts"]["delta"])

    # F11: new attempt running; old heartbeat and old callback must not interfere.
    new_claim = svc_claim(job_id, lease_seconds=120)
    new_attempt = int(new_claim["data"].get("attempt") or 0)
    guard = new_claim["data"].get("attempt") == attempt + 1
    add_assertion(result, "f11_new_attempt_started", guard, attempt + 1,
                  new_claim["data"].get("attempt"))
    row_before_hb = job_row(job_id)
    old_hb = svc_heartbeat(job_id, attempt)
    row_after_old_hb = job_row(job_id)
    add_assertion(result, "f11_old_heartbeat_rejected",
                  old_hb["data"].get("extended") is False, False,
                  old_hb["data"])
    add_assertion(result, "f11_old_heartbeat_did_not_extend_lease",
                  row_after_old_hb.get("lease_until") == row_before_hb.get("lease_until"),
                  row_before_hb.get("lease_until"),
                  row_after_old_hb.get("lease_until"))
    good_hb = svc_heartbeat(job_id, new_attempt)
    add_assertion(result, "f11_current_heartbeat_accepted",
                  good_hb["data"].get("extended") is True, True, good_hb["data"])
    old_complete = svc_complete(job_id, recipe_result(lesson, job_id, attempt))
    add_assertion(result, "f11_old_callback_stale_attempt",
                  old_complete["data"].get("reason") == "stale_attempt",
                  "stale_attempt", old_complete["data"])
    final_attempt = job_row(job_id).get("attempt")
    add_assertion(result, "f11_attempt_not_rolled_back", final_attempt == new_attempt,
                  new_attempt, final_attempt)

    # F12: three recoveries with no result must end in an explicit FAILED.
    exhausted = None
    for _ in range(6):
        row = job_row(job_id)
        if row.get("status") in TERMINAL:
            exhausted = row
            break
        if row.get("status") == "QUEUED":
            claim = svc_claim(job_id, lease_seconds=120)
            timeline.mark("exhaustion_claim", **{k: claim["data"].get(k)
                                                 for k in ("claimed", "attempt")})
        force_expired_lease(job_id)
        watch.poll()
        time.sleep(12)
    else:
        exhausted = job_row(job_id)
    timeline.mark("exhaustion_state", status=exhausted.get("status"),
                  recoveryCount=exhausted.get("recovery_count"))
    add_assertion(result, "f12_exhaustion_terminal",
                  exhausted.get("status") == "FAILED", "FAILED",
                  exhausted.get("status"))
    add_assertion(result, "f12_failure_reason_recorded",
                  bool((exhausted.get("error_message") or "").strip()), True,
                  exhausted.get("error_message"))
    add_assertion(result, "f12_recovery_capped",
                  int(exhausted.get("recovery_count") or 0) <= 3, "<=3",
                  exhausted.get("recovery_count"))
    dup = svc_complete(job_id, recipe_result(lesson, job_id, exhausted.get("attempt")))
    add_assertion(result, "f12_terminal_is_idempotent",
                  dup["data"].get("reason") == "already_terminal", "already_terminal",
                  dup["data"])
    final = rig.counts(lesson, job_id)
    result["duplicateCheck"] = {
        "ok": True,
        "detail": "no attempt increment beyond the cap; terminal completion stays "
                  "already_terminal",
        "recoveryCount": exhausted.get("recovery_count"),
        "terminalStatusCode": dup["httpStatus"],
        "finalCounts": final,
    }
    result["counts"]["afterExhaustion"] = final
    result["notes"].append(
        "MAX_ATTEMPTS is 3 via MACT_RECOVERY_MAX_ATTEMPTS. Each cycle forces the lease "
        "to expire with SQL (accelerated) because no consumer is running. F12 duplicate "
        "messages are covered by the concurrent-claim drill in f01-f03 plus the "
        "already_terminal replay in response-lost.")
    return result


# --- F13 ------------------------------------------------------------------
def sample_probe(label: str, timeline: Timeline) -> dict:
    live = http_get(f"{AI_BASE}/live")
    ready = http_get(f"{AI_BASE}/ready")
    sample = {
        "label": label,
        "at_s": timeline.elapsed(),
        "liveStatus": live.get("status"),
        "liveBody": live.get("body"),
        "readyStatus": ready.get("status"),
        "readyBody": ready.get("body"),
    }
    timeline.mark("probe_sample", label=label, live=live.get("status"),
                  ready=ready.get("status"))
    return sample


def scenario_readiness(ctx: dict) -> dict:
    """F13: /live stays 200 while /ready tracks the real Redis/queue ability."""
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    rig: Rig = ctx["rig"]
    caps = ctx["caps"]
    require(result, caps, "readinessModule",
            "ai-service readiness module is missing (A4)")
    if not VIDEO_10MIN.exists():
        raise ScenarioSkip(f"long video fixture missing: {VIDEO_10MIN}")

    samples: list[dict] = []
    rig.ensure_backend()
    rig.wait_redis()
    if not http_ok(f"{AI_BASE}/ready"):
        rig.start_ai(AI_A_PORT, "ai-service.log")
    time.sleep(3)
    samples.append(sample_probe("healthy", timeline))

    # Idle: no jobs submitted, no inference; must not be judged dead.
    time.sleep(20)
    samples.append(sample_probe("idle_20s", timeline))

    # Long inference: the consumer loop is busy; /ready must stay 200.
    lesson, _ = rig.new_lesson("f13")
    job_id = rig.submit(lesson, VIDEO_10MIN)
    watch = JobWatch(job_id, result, timeline)
    running = watch.until(lambda row: row.get("status") == "RUNNING", 180)
    if running is None:
        raise ScenarioSkip("job never became RUNNING; cannot sample long inference")
    time.sleep(8)
    samples.append(sample_probe("long_inference", timeline))

    # Redis down: /live stays 200, /ready must turn 503 and recover by itself.
    stop = container_action("stop", REDIS_CONTAINER)
    timeline.mark("redis_stopped", **stop)
    outage: list[dict] = []
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        probe = sample_probe("redis_down", timeline)
        outage.append(probe)
        if probe.get("readyStatus") == 503:
            break
        time.sleep(5)
    start = container_action("start", REDIS_CONTAINER)
    timeline.mark("redis_started", **start)
    rig.wait_redis()
    recovered = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        probe = sample_probe("redis_recovered", timeline)
        if probe.get("readyStatus") == 200:
            recovered = probe
            break
        time.sleep(5)
    samples.append(recovered or {"label": "redis_recovered", "readyStatus": None})

    # Consumer process killed: /live is expected to be unreachable (the probe
    # must not report a dead process as healthy), and a restart recovers both.
    kill_elapsed = rig.stop_ai(AI_A_PORT, hard=True)
    dead = {"label": "consumer_killed", "killSeconds": kill_elapsed}
    dead["liveStatus"] = http_get(f"{AI_BASE}/live").get("status")
    dead["readyStatus"] = http_get(f"{AI_BASE}/ready").get("status")
    timeline.mark("probe_sample", label="consumer_killed",
                  live=dead["liveStatus"], ready=dead["readyStatus"])
    samples.append(dead)
    rig.start_ai(AI_A_PORT, "ai-service.log")
    time.sleep(3)
    samples.append(sample_probe("consumer_restarted", timeline))

    result["samples"] = samples
    by_label = {}
    for sample in samples:
        by_label.setdefault(sample["label"], []).append(sample)
    healthy = by_label.get("healthy", [{}])[0]
    idle = by_label.get("idle_20s", [{}])[0]
    busy = by_label.get("long_inference", [{}])[0]
    down = [s for s in outage if s.get("readyStatus") == 503]
    add_assertion(result, "f13_healthy_live_200", healthy.get("liveStatus") == 200,
                  200, healthy.get("liveStatus"))
    add_assertion(result, "f13_healthy_ready_200", healthy.get("readyStatus") == 200,
                  200, healthy.get("readyStatus"))
    add_assertion(result, "f13_idle_ready_200", idle.get("readyStatus") == 200, 200,
                  idle.get("readyStatus"))
    add_assertion(result, "f13_long_inference_ready_200",
                  busy.get("readyStatus") == 200, 200, busy.get("readyStatus"))
    add_assertion(result, "f13_redis_down_ready_503", bool(down), 503,
                  down and down[0].get("readyStatus"))
    add_assertion(result, "f13_redis_down_live_200",
                  all(s.get("liveStatus") == 200 for s in outage) if outage else False,
                  "all 200", [s.get("liveStatus") for s in outage])
    add_assertion(result, "f13_redis_down_reason_reported",
                  bool(down) and "redis" in json.dumps(down[0].get("readyBody") or {}).lower(),
                  "reason mentions redis", down and down[0].get("readyBody"))
    add_assertion(result, "f13_recovered_ready_200",
                  bool(recovered) and recovered.get("readyStatus") == 200, 200,
                  recovered and recovered.get("readyStatus"))
    add_assertion(result, "f13_killed_process_not_reported_healthy",
                  dead["liveStatus"] in (None, 502, 503), "connection refused",
                  dead["liveStatus"])
    restarted = by_label.get("consumer_restarted", [{}])[0]
    add_assertion(result, "f13_restart_recovers_ready",
                  restarted.get("readyStatus") == 200
                  and restarted.get("liveStatus") == 200,
                  {"live": 200, "ready": 200},
                  {"live": restarted.get("liveStatus"),
                   "ready": restarted.get("readyStatus")})
    add_assertion(result, "f13_probe_has_no_secrets",
                  not any(token in json.dumps(samples)
                          for token in ("redis://", "macteval_round2",
                                        SERVICE_TOKEN, PG_PASSWORD)),
                  "no DSN/password/token", "checked")
    result["duplicateCheck"] = {"ok": True,
                                "detail": "probe sampling performs no business writes",
                                "counts": rig.counts(lesson, job_id)}
    result["notes"].append(
        "Only the isolated mact-cap-redis container is stopped/started. /live is expected "
        "to be unreachable once the process is SIGKILLed: a dead process must never be "
        "reported healthy by any probe. The long-inference sample waits for RUNNING and "
        "then samples after 8s of real model work.")
    return result


# --- F08 ------------------------------------------------------------------
def scenario_ttl_boundary(ctx: dict) -> dict:
    """F08: undelivered results never expire; retention applies only after delivery.

    Accelerated: no 24h wait. Three concrete手法 are recorded in the JSON.
    """
    result = ctx["result"]
    timeline: Timeline = ctx["timeline"]
    caps = ctx["caps"]
    require(result, caps, "resultStoreModule",
            "ai-service result_store.py is missing (A3)")

    probe_script = (
        "import json,sys;"
        "sys.path.insert(0, 'ai-service');"
        "from app.workers import result_store as rs;"
        "from app.core.config import settings;"
        "job='ttl-probe-0001';"
        "ref1=rs.save_result(job,1,{'status':'succeeded','n':1,'pad':'x'*100});"
        "rs.mark_pending(ref1);"
        "ref2=rs.save_result(job,2,{'status':'succeeded','n':2,'pad':'y'*100});"
        "rs.mark_pending(ref2);"
        # TTL of attempt 1 must be read BEFORE clear_pending deletes that key;
        # reading it afterwards always yields -2 (no such key) and would make the
        # "no TTL" assertion report a false failure.
        "redis=rs._get_redis();"
        "ttl1_before=redis.ttl(rs.PENDING_HASH_TEMPLATE.format(job_id=job,attempt=1));"
        "rs.clear_pending(job,1);"
        "print(json.dumps({'retention':settings.result_retention_after_delivery_seconds,"
        "'pendingAlert':settings.result_pending_alert_seconds,"
        "'ttl1BeforeCleanup':ttl1_before,"
        "'ref1':ref1.__dict__,'ref2':ref2.__dict__}));"
    )
    env = os.environ.copy()
    env.update({k: v for k, v in AI_ENV.items() if k.startswith("MACT_AI_")})
    env["PYTHONPATH"] = str(AI_SERVICE)
    proc = subprocess.run([sys.executable, "-c", probe_script], cwd=str(AI_SERVICE),
                          env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    timeline.mark("result_store_probe", returncode=proc.returncode)
    if proc.returncode != 0:
        raise ScenarioSkip(
            "result_store probe failed (dependency or store wiring missing): "
            + (proc.stderr or "").strip()[-600:])
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ScenarioSkip("result_store probe produced no JSON: "
                           + proc.stdout[-300:])

    job = "ttl-probe-0001"
    ttl1_before_cleanup = payload.get("ttl1BeforeCleanup")
    ttl1 = result_index_ttl(job, 1)          # -2 is expected: clear_pending removed it
    ttl2 = result_index_ttl(job, 2)
    exists2 = result_index_exists(job, 2)
    probe = result_objects(job, 2)
    timeline.mark("ttl_probe_observed", ttl1BeforeCleanup=ttl1_before_cleanup,
                  ttl1AfterCleanup=ttl1, ttl2=ttl2, attempt2Index=exists2)

    add_assertion(result, "f08_index_has_no_ttl_attempt1",
                  ttl1_before_cleanup == -1, -1, ttl1_before_cleanup)
    add_assertion(result, "f08_index_has_no_ttl_attempt2", ttl2 == -1, -1, ttl2)
    add_assertion(result, "f08_old_attempt_cleanup_keeps_new_attempt", exists2 is True,
                  True, exists2)
    add_assertion(result, "f08_result_object_still_readable_after_cleanup",
                  probe.get("hasEnvelope") is True, True, probe)
    add_assertion(result, "f08_retention_is_days_not_hours",
                  int(payload.get("retention") or 0) >= 7 * 24 * 3600,
                  ">=604800", payload.get("retention"))
    add_assertion(result, "f08_pending_alert_configured",
                  int(payload.get("pendingAlert") or 0) > 0, ">0",
                  payload.get("pendingAlert"))
    result["accelerated"] = True
    result["method"] = {
        "indexTtl": "redis TTL of analysis:result:{job}:{attempt} must be -1; attempt 1 "
                    "is sampled inside the probe before clear_pending deletes the key",
        "retentionConfig": "result_retention_after_delivery_seconds is read from the "
                           "real settings object (7 days), not assumed",
        "indexIsolation": "clear_pending(attempt=1) is executed through the real "
                          "result_store code and attempt 2 must survive",
        "notDone": "no object was back-dated and no process was made to wait 24h or "
                   "7 days; the expiry job itself is not executed",
    }
    # Cleanup of the probe objects (scoped to the probe job only).
    cleanup = minio_drop_result(job, 1)
    cleanup2 = minio_drop_result(job, 2)
    redis_cli("del", f"analysis:result:{job}:1", f"analysis:result:{job}:2")
    redis_cli("srem", "analysis:results:pending", job)
    result["cleanup"] = {"attempt1": cleanup, "attempt2": cleanup2}
    result["duplicateCheck"] = {"ok": True,
                                "detail": "probe job is synthetic and removed after use",
                                "jobId": job}
    result["notes"].append(
        "ACCELERATED: no 24h/7d wait was performed. See result.method for the exact "
        "substitutions; the unexecuted part is stated in result.method.notDone.")
    return result


# --- scenario registry ----------------------------------------------------
SCENARIOS: dict[str, Callable[[dict], dict]] = {
    "result-before-redis-loss": scenario_result_before_redis_loss,
    "requeue-after-worker-exit": scenario_requeue_after_worker_exit,
    "crash-after-put": scenario_crash_after_put,
    "response-lost": scenario_response_lost,
    "minio-unavailable": scenario_minio_unavailable,
    "f01-f03": scenario_f01_f03,
    "f10-f12": scenario_f10_f12,
    "readiness": scenario_readiness,
    "ttl-boundary": scenario_ttl_boundary,
}

REQUIRED_KEYS = ("scenario", "startedAt", "timeline", "jobId", "attempts",
                 "terminal", "counts", "duplicateCheck", "pass", "assertions",
                 "notes", "notRun")


def run_scenario(name: str, api: Api, out_dir: Path,
                 caps: dict | None = None) -> dict:
    """Run one scenario and always return a schema-complete result dict."""
    timeline = Timeline()
    result = new_result(name)
    rig = Rig(api, out_dir, timeline, result)
    ctx = {"api": api, "result": result, "timeline": timeline, "rig": rig,
           "outDir": out_dir, "caps": caps if caps is not None else capability_matrix()}
    try:
        returned = SCENARIOS[name](ctx)
        if isinstance(returned, dict):
            for key in REQUIRED_KEYS:
                returned.setdefault(key, result.get(key))
            result = returned
    except ScenarioSkip as exc:
        result["notRun"] = str(exc)
        result["pass"] = False
        timeline.mark("scenario_skipped", reason=str(exc))
        print(f"    SKIP {name}: {exc}", flush=True)
    except IsolationError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken drill must still be recorded
        result["error"] = f"{type(exc).__name__}: {exc}"
        timeline.mark("scenario_error", error=result["error"])
        add_assertion(result, "scenario_completed_without_exception", False,
                      "no exception", result["error"])
        print(f"    ERROR {name}: {result['error']}", flush=True)
    finally:
        # Stop only consumers this drill started. Stopping 18082/18083
        # unconditionally would kill a service another operator (or another
        # agent's capacity run) is deliberately holding, including on the
        # skip path where this scenario never started anything.
        try:
            for port in list(rig.ai):
                rig.stop_ai(port)
        except Exception:  # noqa: BLE001 - cleanup must not mask the result
            pass
    result["timeline"] = timeline.events
    result["elapsedSeconds"] = timeline.elapsed()
    if result.get("terminal") is None and result.get("jobId"):
        try:
            row = job_row(result["jobId"])
            result["terminal"] = {"status": row.get("status"),
                                  "attempt": row.get("attempt"),
                                  "recoveryCount": row.get("recovery_count")}
        except Exception:  # noqa: BLE001 - job may not exist for a probe scenario
            pass
    result["pass"] = derive_pass(result)
    for key in REQUIRED_KEYS:
        result.setdefault(key, None if key != "timeline" else [])
    return result


def merge_summary(results: Mapping[str, Mapping[str, Any]],
                  manifest: Mapping[str, Any] | None = None) -> dict:
    """Merge per-scenario results, preserving ``pass`` and ``notRun``."""
    summary: dict[str, Any] = {}
    for name, result in results.items():
        summary[name] = {
            "pass": bool(result.get("pass")),
            "notRun": result.get("notRun"),
            "error": result.get("error"),
            "assertions": len(result.get("assertions") or []),
            "failedAssertions": [item.get("name") for item in
                                 (result.get("assertions") or [])
                                 if not item.get("ok")],
            "elapsedSeconds": result.get("elapsedSeconds"),
            "duplicateCheck": result.get("duplicateCheck"),
        }
    passed = [name for name, item in summary.items() if item["pass"]]
    skipped = [name for name, item in summary.items() if item["notRun"]]
    failed = [name for name, item in summary.items()
              if not item["pass"] and not item["notRun"]]
    summary["totals"] = {"scenarios": len(results), "passed": len(passed),
                         "skipped": len(skipped), "failed": len(failed),
                         "passedNames": passed, "skippedNames": skipped,
                         "failedNames": failed}
    if manifest is not None:
        summary["manifest"] = dict(manifest)
    return summary


def run_manifest(caps: Mapping[str, Any]) -> dict:
    def _sha(path: Path) -> str | None:
        if not path.exists():
            return None
        import hashlib
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                                       text=True, encoding="utf-8",
                                       errors="replace").strip()
    except Exception:  # noqa: BLE001
        head = None
    return {
        "capturedAt": now_iso(),
        "gitHead": head,
        "isolatedTarget": {"pgContainer": PG_CONTAINER, "pgPort": PG_PORT,
                           "database": PG_DB, "redisContainer": REDIS_CONTAINER,
                           "redisPort": REDIS_PORT,
                           "minioContainer": MINIO_CONTAINER,
                           "minioPort": MINIO_PORT, "backendPort": BACKEND_PORT,
                           "aiPorts": [AI_A_PORT, AI_B_PORT]},
        "backendJar": str(JAR),
        "backendJarSha256": _sha(JAR),
        "checkpointSha256": _sha(CHECKPOINT),
        "video90sSha256": _sha(VIDEO_90S),
        "video10minSha256": _sha(VIDEO_10MIN),
        "capabilities": dict(caps),
        "backendEnv": {k: BACKEND_ENV[k] for k in (
            "MACT_RECOVERY_INTERVAL_MS", "MACT_RECOVERY_LEASE_GRACE_SECONDS",
            "MACT_RECOVERY_MAX_ATTEMPTS", "MACT_OUTBOX_STALE_QUEUED_SECONDS")},
        "aiEnv": {k: AI_ENV[k] for k in (
            "MACT_AI_DEVICE", "MACT_AI_BEHAVIOR_WEIGHTS",
            "MACT_AI_RESULT_BUCKET", "MACT_AI_RESULT_PREFIX")},
    }


# --- self-check (read-only, no inference, no injection) -------------------
def self_check(verbose: bool = True) -> tuple[bool, dict]:
    """Read-only validation: target allow-list, containers, ports, DB, imports."""
    report: dict[str, Any] = {"checks": [], "ok": False, "capturedAt": now_iso()}

    def check(name: str, ok: bool, detail: Any = None,
              optional: bool = False) -> bool:
        """Record one check. ``optional`` items are reported but not fatal."""
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail,
                                 "optional": optional})
        if verbose:
            tag = "ok" if ok else ("info" if optional else "FAIL")
            print(f"  [{tag}] {name}"
                  + (f" :: {detail}" if detail is not None and not ok else ""),
                  flush=True)
        return bool(ok)

    try:
        target = assert_isolated_target()
        check("isolated_target_allowlist", True, target)
    except IsolationError as exc:
        check("isolated_target_allowlist", False, str(exc))
        report["ok"] = False
        return False, report

    rejected_cases = (
        ("container_user_compose", {**target, "pgContainer": "personal_design-postgres-1"}),
        ("redis_port_6379", {**target, "redisPort": 6379}),
        ("database_macteval", {**target, "database": "macteval"}),
        ("previous_round_container", {**target, "pgContainer": "mact-cap-postgres"}),
        ("unknown_port", {**target, "backendPort": 18080}),
    )
    for label, bad in rejected_cases:
        try:
            assert_isolated_target(bad)
            check(f"rejects:{label}", False,
                  "assert_isolated_target accepted a forbidden target")
        except IsolationError:
            check(f"rejects:{label}", True)

    try:
        listing = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        rows = [line for line in listing.stdout.splitlines() if line]
        check("docker_available", listing.returncode == 0,
              (listing.stderr or "").strip()[:200])
        names = {row.split("|")[0] for row in rows}
        for container in sorted(ALLOWED_CONTAINERS):
            check(f"container_exists:{container}", container in names,
                  sorted(names))
        for row in rows:
            parts = row.split("|")
            if len(parts) < 2 or parts[0] not in ALLOWED_CONTAINERS:
                continue
            check(f"container_running:{parts[0]}", parts[1].startswith("Up"), row)
    except Exception as exc:  # noqa: BLE001
        check("docker_available", False, f"{type(exc).__name__}: {exc}")

    for label, port in (("postgres", PG_PORT), ("redis", REDIS_PORT),
                        ("minio", MINIO_PORT)):
        check(f"port_bindable:{label}:{port}", port in ALLOWED_PORTS, port)
        check(f"port_reachable:{label}:{port}", port_open(port), "connect_ex failed")

    try:
        version = psql_one("select current_database(), current_user")
        check("database_reachable", version is not None, version)
        check("database_name_is_isolated", bool(version) and version[0] == PG_DB,
              version)
        tables = {row[0] for row in psql(
            "select table_name from information_schema.tables "
            "where table_schema='public'")}
        report["tables"] = sorted(tables)
        for table in ("analysis_jobs", "task_outbox", "behavior_observations",
                      "lesson_window_features", "suggestions",
                      "behavior_evidence_frames", "lesson_transcripts"):
            # Informational: a freshly created database has no schema until the
            # backend's first Flyway run. Scenarios that need rows will fail loudly.
            check(f"table_present:{table}", table in tables,
                  sorted(tables)[:20], optional=True)
        report["capabilities"] = capability_matrix()
    except Exception as exc:  # noqa: BLE001
        check("database_reachable", False, f"{type(exc).__name__}: {exc}")
        report["capabilities"] = {}

    try:
        pong = redis_cli("ping", timeout=10)
        check("redis_ping", pong == "PONG", pong)
        check("redis_stream_exists_or_creatable", True,
              redis_cli("xlen", JOB_STREAM, timeout=10))
    except Exception as exc:  # noqa: BLE001
        check("redis_ping", False, f"{type(exc).__name__}: {exc}")

    try:
        buckets = [b.name for b in minio_client().list_buckets()]
        check("minio_reachable", True, buckets)
        check("minio_bucket_present", MINIO_BUCKET in buckets, buckets,
              optional=True)
    except Exception as exc:  # noqa: BLE001
        check("minio_reachable", False, f"{type(exc).__name__}: {exc}")

    try:
        for module in ("psutil", "requests", "minio"):
            __import__(module)
        check("python_imports", True)
    except Exception as exc:  # noqa: BLE001
        check("python_imports", False, f"{type(exc).__name__}: {exc}")

    check("backend_jar_present", JAR.exists(), str(JAR))
    stale, newer = jar_is_stale()
    check("backend_jar_newer_than_sources", not stale,
          f"{len(newer)} source file(s) changed after the jar: {newer[:6]}; "
          "run 'mvn -f backend/pom.xml package -DskipTests' before executing "
          "any scenario, otherwise the drills test an older binary")
    check("checkpoint_present", CHECKPOINT.exists(), str(CHECKPOINT))
    check("video_90s_present", VIDEO_90S.exists(), str(VIDEO_90S))
    check("video_10min_present", VIDEO_10MIN.exists(), str(VIDEO_10MIN))
    check("ai_service_dir_present", (AI_SERVICE / "app" / "main.py").exists(),
          str(AI_SERVICE))
    check("output_dir_writable",
          _dir_writable(FAULT_DIR), str(FAULT_DIR))

    report["ok"] = all(item["ok"] for item in report["checks"]
                       if not item.get("optional"))
    report["failed"] = [item["name"] for item in report["checks"]
                        if not item["ok"] and not item.get("optional")]
    report["informational"] = [item["name"] for item in report["checks"]
                               if not item["ok"] and item.get("optional")]
    return report["ok"], report


def jar_is_stale() -> tuple[bool, list[str]]:
    """True when Java sources changed after the packaged jar was built.

    A stale jar would exercise the previous protocol implementation while the
    sources look correct, which is exactly the kind of false green this harness
    must prevent. Returns ``(stale, names_of_newer_sources)``.
    """
    if not JAR.exists():
        return True, ["jar missing"]
    jar_mtime = JAR.stat().st_mtime
    newer = sorted(path.name for path in
                   (ROOT / "backend" / "src" / "main" / "java").rglob("*.java")
                   if path.stat().st_mtime > jar_mtime)
    return bool(newer), newer


def _dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# --- CLI ------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recovery-protocol fault drills (round-2 isolated rig).")
    parser.add_argument("scenario", nargs="?", default=None,
                        help="scenario name, or 'all'")
    parser.add_argument("--all", action="store_true", help="run every scenario")
    parser.add_argument("--list", action="store_true", help="list scenario names")
    parser.add_argument("--self-check", action="store_true",
                        help="read-only target/container/DB/import validation")
    parser.add_argument("--out", default="", help="output directory")
    parser.add_argument("--allow-self-check-only", action="store_true",
                        help="exit 0 when --self-check fails (not recommended)")
    args = parser.parse_args(argv)

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    if args.self_check and args.scenario is None and not args.all:
        print("== self-check (read-only; no inference, no fault injection) ==")
        ok, report = self_check()
        out_dir = Path(args.out) if args.out else FAULT_DIR / (
            "self-check-" + time.strftime("%H%M%S"))
        write_json(out_dir / "self-check.json", report)
        print(json.dumps({"ok": ok, "out": str(out_dir),
                          "failed": report.get("failed"),
                          "informational": report.get("informational")},
                         ensure_ascii=True))
        return 0 if ok or args.allow_self_check_only else 1

    try:
        assert_isolated_target()
    except IsolationError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 3

    if args.all:
        names = list(SCENARIOS)
    elif args.scenario in SCENARIOS:
        names = [args.scenario]
    elif args.scenario is None:
        parser.error("give a scenario name, --all, --list or --self-check")
        return 2
    else:
        parser.error(f"unknown scenario: {args.scenario}")
        return 2

    caps = capability_matrix()
    out_dir = Path(args.out) if args.out else FAULT_DIR / (
        f"{'all' if args.all else names[0]}-{time.strftime('%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "capabilities.json", caps)

    api = Api(BASE)
    results: dict[str, dict] = {}
    for name in names:
        print(f"==> {name}", flush=True)
        result = run_scenario(name, api, out_dir, caps)
        results[name] = result
        write_json(out_dir / f"{name}.json", result)
        print(json.dumps({"scenario": name, "pass": result.get("pass"),
                          "notRun": result.get("notRun"),
                          "error": result.get("error"),
                          "failed": [a["name"] for a in result.get("assertions", [])
                                     if not a.get("ok")]},
                         ensure_ascii=True), flush=True)

    summary = merge_summary(results, run_manifest(caps))
    write_json(out_dir / "summary.json", summary)
    print(json.dumps({"out": str(out_dir), "totals": summary["totals"]},
                     ensure_ascii=True))
    # Order matters: a hard failure outranks an incomplete run, and *any* skip
    # makes the run non-green because a skipped drill verified nothing.
    if summary["totals"]["failed"]:
        return 2
    if summary["totals"]["skipped"] or not summary["totals"]["passed"]:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
