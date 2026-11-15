#!/usr/bin/env python3
"""UI-acceptance rig fault drills (mact-ui-postgres / mact-ui-redis / mact-ui-minio).

Two fault harnesses already existed, but both are bound to rigs that no longer
exist: ``scripts/ops/fault_injection.py`` targets ``mact-cap-*`` and
``scripts/ops/recovery_faults.py`` targets ``mact-r2-postgres`` /
``mact-cap-redis`` / ``mact-r2-minio``.  The rig that is actually running is the
UI acceptance rig brought up by ``scripts/verify/browser_rig.py``:

    mact-ui-postgres  127.0.0.1:15534   database macteval_ui
    mact-ui-redis     127.0.0.1:16380   stream analysis:jobs, group ai-workers
    mact-ui-minio     127.0.0.1:19100   bucket analysis-artifacts
    backend (java -jar ...)  127.0.0.1:8000
    ai-service (uvicorn)     127.0.0.1:8001

This module drills *that* rig and nothing else.

Safety model
------------
Every action that can stop, pause, kill or delete something first goes through
:func:`guard_targets`, which fails closed when:

* a container name is not exactly one of the three rig containers.  Anything
  else (``personal_design-*``, ``milvus-*``, ``on_call_agent``, the retired
  ``mact-cap-*`` / ``mact-r2-*`` drills) is refused by name, not by hoping;
* the database is not exactly ``macteval_ui`` — checked as a constant *and*
  against ``current_database()`` of the live container;
* the backend PID recorded in ``rig.json`` is missing, is not a ``java``
  process, or is not the process holding the listening socket on port 8000;
* a container mount is not a bind mount, or its host source is outside this
  repository.  The rig binds ``data/tmp/ui-rig-2026-10-26/{pg,redis,minio}``;
  a named volume would mean this is a different container wearing a rig name;
* a published host port is not the rig's own port (15534 / 16380 / 19100-19101).

The resolved targets and every ``docker inspect`` mount source are printed
before any scenario runs, so a transcript always shows what was touched.

Scenarios (``--list``)
----------------------
    redis-outage           read APIs survive a Redis stop; queued job still runs
    double-consumer        a second uvicorn on the same stream processes nothing
    consumer-kill          SIGKILL mid-job; lease/claim recovery completes it
    duplicate-callback     the same completion posted twice changes no row
    late-stale-attempt     an older attempt cannot overwrite the newer result
    minio-object-missing   a deleted evidence object 404s, never 500s, and is restored
    backend-restart        the process is replaced; the report reads back identically
    postgres-blip          a paused database fails loudly and recovers by itself

Each scenario records initial state -> injected fault -> recovery condition ->
final state, plus an assertion list and a cleanup result.  "The process came
back" is never accepted as proof: assertions read database rows and API bodies.

A scenario whose precondition is missing is recorded as ``skipped`` with the
reason and makes the run non-zero (unless ``--allow-incomplete``), so "not
tested" can never read as green.  ``--self-check`` validates targets, mounts,
ports, permissions, files and service reachability without injecting anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
RIG_DIR = ROOT / "data" / "tmp" / "ui-rig-2026-10-26"
RIG_STATE = RIG_DIR / "rig.json"
LOG_DIR = RIG_DIR / "logs"
BACKEND_LOG = "backend-8000.log"
AI_LOG = "ai-8001.log"

# --- the only targets this module may touch -------------------------------
PG_CONTAINER = "mact-ui-postgres"
PG_PORT = 15534
PG_DB = "macteval_ui"
PG_USER = "macteval"
REDIS_CONTAINER = "mact-ui-redis"
REDIS_PORT = 16380
MINIO_CONTAINER = "mact-ui-minio"
MINIO_PORT = 19100
MINIO_CONSOLE_PORT = 19101
MINIO_USER = "uirig"
MINIO_SECRET = "uirig_secret"
MINIO_BUCKET = "analysis-artifacts"

BACKEND_PORT = 8000
AI_PORT = 8001
SECOND_AI_PORT = 8002
FRONTEND_PORT = 5173

REDIS_STREAM = "analysis:jobs"
REDIS_GROUP = "ai-workers"

ALLOWED_CONTAINERS = frozenset({PG_CONTAINER, REDIS_CONTAINER, MINIO_CONTAINER})
ALLOWED_PORTS = frozenset({PG_PORT, REDIS_PORT, MINIO_PORT, MINIO_CONSOLE_PORT,
                           BACKEND_PORT, AI_PORT, SECOND_AI_PORT, FRONTEND_PORT})
EXPECTED_PORTS = {
    PG_CONTAINER: {PG_PORT},
    REDIS_CONTAINER: {REDIS_PORT},
    MINIO_CONTAINER: {MINIO_PORT, MINIO_CONSOLE_PORT},
}
FORBIDDEN_PORTS = frozenset({5432, 6379, 9000, 9001, 15532, 15533, 18081, 18082,
                             18083, 9091, 19530})
FORBIDDEN_CONTAINERS = frozenset({"on_call_agent"})
#: Refused by prefix: another project's stack and the retired drill rigs.
FORBIDDEN_PREFIXES = ("personal_design-", "mact-cap-", "mact-r2-", "milvus-",
                      "on_call_agent")

JWT_SECRET = "ui-rig-jwt-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
SERVICE_TOKEN = "ui-rig-service-token-2026-10-26"
BASE = f"http://127.0.0.1:{BACKEND_PORT}/api"
AI_BASE = f"http://127.0.0.1:{AI_PORT}"

#: How long a kill may take to be *proven* (taskkill returns before teardown).
DEATH_PROOF_TIMEOUT_SECONDS = 15.0

JAR = ROOT / "backend" / "target" / "mact-eval-backend-1.0.0.jar"
AI_SERVICE = ROOT / "ai-service"
CHECKPOINT = ROOT / "docs" / "experiments" / "runs" / "scb5-yolo" / "train" / "weights" / "best.pt"
VIDEO_SHORT = ROOT / "data" / "raw" / "public" / "classroom" / "sav-sample90s.mp4"
VIDEO_LONG = ROOT / "data" / "tmp" / "capacity-2026-10-26" / "video" / "fault-10min.mp4"
EVIDENCE_DIR = ROOT / "docs" / "reviews" / "evidence" / "2026-10-28-faults"

TEACHER = {"username": "13800002026", "password": "UiRigPass2026", "name": "界面验收教师"}
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}

TABLES = ("analysis_jobs", "task_outbox", "behavior_observations",
          "lesson_window_features", "behavior_evidence_frames", "evaluation_reports",
          "lesson_sessions", "courses", "media_assets")

NOTES = [
    "Containers: only mact-ui-postgres / mact-ui-redis / mact-ui-minio; database macteval_ui.",
    "Evidence-frame counts are selected images, not action events or student counts.",
    "No presigned URL is persisted here; the object key is recorded instead.",
]


class GuardError(RuntimeError):
    """A target is outside the UI rig; nothing may be touched."""


class ScenarioSkip(RuntimeError):
    """A precondition is absent; recorded as ``skipped`` with this reason."""


# --------------------------------------------------------------------------- #
# docker / process plumbing
# --------------------------------------------------------------------------- #
def docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Git Bash rewrites /-prefixed arguments into Windows paths unless disabled.
    env["MSYS_NO_PATHCONV"] = "1"
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def inspect_json(fmt: str, container: str) -> Any:
    result = docker("inspect", "--format", fmt, container, timeout=60)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def assert_container_allowed(name: str, action: str) -> None:
    """Fail-closed allow-list for any container action, checked at call time."""
    if name in FORBIDDEN_CONTAINERS:
        raise GuardError(f"refusing {action}: {name!r} is explicitly forbidden")
    for prefix in FORBIDDEN_PREFIXES:
        if name.startswith(prefix):
            raise GuardError(f"refusing {action}: {name!r} matches forbidden prefix {prefix!r}")
    if name not in ALLOWED_CONTAINERS:
        raise GuardError(f"refusing {action}: container {name!r} is not a UI rig target "
                         f"{sorted(ALLOWED_CONTAINERS)}")


def assert_port_allowed(port: int, label: str) -> None:
    if port in FORBIDDEN_PORTS:
        raise GuardError(f"refusing: {label} port {port} is a forbidden port")
    if port not in ALLOWED_PORTS:
        raise GuardError(f"refusing: {label} port {port} is not a UI rig port "
                         f"{sorted(ALLOWED_PORTS)}")


def container_state(name: str) -> dict | None:
    assert_container_allowed(name, "inspect")
    state = inspect_json("{{json .State}}", name)
    if not isinstance(state, dict):
        return None
    return {"running": bool(state.get("Running")), "paused": bool(state.get("Paused")),
            "status": state.get("Status"), "oomKilled": bool(state.get("OOMKilled"))}


def normalize_mount_path(raw: str) -> Path:
    return Path(str(raw).replace("\\", "/")).resolve()


def inside_repo(path: Path) -> bool:
    try:
        path.relative_to(ROOT)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# fail-closed target guard
# --------------------------------------------------------------------------- #
def read_rig_state() -> dict:
    if not RIG_STATE.exists():
        raise GuardError(f"refusing: rig state missing at {RIG_STATE}; start the rig with "
                         "scripts/verify/browser_rig.py start")
    try:
        return json.loads(RIG_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"refusing: rig state unreadable ({exc})")


def psutil_module():
    try:
        import psutil  # noqa: PLC0415 - optional dependency, reported by self-check
    except ImportError as exc:  # pragma: no cover - environment problem
        raise GuardError(f"refusing: psutil is required to verify PIDs ({exc})")
    return psutil


def find_listen_pids(port: int) -> set[int]:
    psutil = psutil_module()
    pids: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="inet"):
            if (conn.status == psutil.CONN_LISTEN and conn.laddr
                    and conn.laddr.port == port and conn.pid):
                pids.add(int(conn.pid))
    except (psutil.AccessDenied, OSError):
        return set()
    return pids


def describe_pid(pid: int) -> dict:
    psutil = psutil_module()
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"pid": pid, "name": None, "cmdline": [], "alive": False}
    try:
        proc = psutil.Process(pid)
        return {"pid": pid, "name": proc.name(), "cmdline": proc.cmdline()[:8],
                "alive": proc.is_running()}
    except psutil.NoSuchProcess:
        return {"pid": pid, "name": None, "cmdline": [], "alive": False}
    except psutil.AccessDenied:
        return {"pid": pid, "name": None, "cmdline": [], "alive": None}


def tasklist_has_pid(pid: int) -> bool | None:
    """Windows cross-check: is ``pid`` still listed? ``None`` when unavailable.

    ``psutil.Process.is_running()`` keeps answering True for a process that has
    been signalled but not yet torn down (a torch/CUDA teardown takes a few
    seconds and ``taskkill`` returns as soon as the kill is delivered, not when
    the process object disappears). ``tasklist`` is the OS's own answer, so it
    is used to settle the "did it really die" question instead of trusting one
    call taken immediately after the signal.
    """
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
    except Exception:  # noqa: BLE001 - absence of an answer is not proof of death
        return None
    if result.returncode != 0:
        return None
    return f'"{pid}"' in (result.stdout or "")


def pid_alive(pid: int) -> bool | None:
    """Authoritative liveness for a PID this drill owns.

    True = definitely running, False = definitely gone, None = could not tell.
    """
    psutil = psutil_module()
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return tasklist_has_pid(pid)
    return True


def verify_backend_pid(rig: dict) -> dict:
    """The recorded backend PID must be a java process holding port 8000."""
    pid = rig.get("backendPid")
    if not isinstance(pid, int) or pid <= 0:
        raise GuardError(f"refusing: rig.json has no usable backendPid ({pid!r})")
    info = describe_pid(pid)
    if info["alive"] is not True:
        raise GuardError(f"refusing: backend PID {pid} is not a live process ({info})")
    if info["name"] not in ("java.exe", "java"):
        raise GuardError(f"refusing: backend PID {pid} is {info['name']!r}, not java")
    joined = " ".join(info["cmdline"])
    if "mact-eval-backend" not in joined:
        raise GuardError(f"refusing: backend PID {pid} does not run the MACT-Eval jar "
                         f"({joined!r})")
    listeners = find_listen_pids(BACKEND_PORT)
    if listeners and pid not in listeners:
        raise GuardError(f"refusing: backend PID {pid} does not listen on {BACKEND_PORT} "
                         f"(listeners={sorted(listeners)})")
    info["listensOnBackendPort"] = pid in listeners
    return info


def verify_ai_pid(rig: dict) -> dict:
    pid = rig.get("aiPid")
    info = describe_pid(pid) if isinstance(pid, int) else {"pid": pid, "alive": False}
    if info.get("alive") is not True:
        raise GuardError(f"refusing: ai-service PID {pid} is not a live process ({info})")
    if not (info.get("name") or "").lower().startswith("python"):
        raise GuardError(f"refusing: ai-service PID {pid} is {info.get('name')!r}, not python")
    listeners = find_listen_pids(AI_PORT)
    if listeners and pid not in listeners:
        raise GuardError(f"refusing: ai-service PID {pid} does not listen on {AI_PORT}")
    info["listensOnAiPort"] = pid in listeners
    return info


def guard_targets(*, require_running: bool = True) -> dict:
    """Validate every target, print what was resolved, raise on any deviation."""
    rig = read_rig_state()
    report: dict[str, Any] = {"rigStateFile": str(RIG_STATE), "containers": {}, "ports": {},
                              "mounts": {}, "backendPid": None, "aiPid": None,
                              "database": None}

    assert_port_allowed(PG_PORT, "postgres")
    assert_port_allowed(REDIS_PORT, "redis")
    assert_port_allowed(MINIO_PORT, "minio")
    assert_port_allowed(BACKEND_PORT, "backend")

    for name in (PG_CONTAINER, REDIS_CONTAINER, MINIO_CONTAINER):
        assert_container_allowed(name, "inspect")
        state = container_state(name)
        if state is None:
            raise GuardError(f"refusing: container {name} does not exist")
        if require_running and not state["running"]:
            raise GuardError(f"refusing: container {name} is not running ({state})")
        if state.get("paused"):
            raise GuardError(f"refusing: container {name} is paused; unpause it first")
        report["containers"][name] = state

        mounts = inspect_json("{{json .Mounts}}", name)
        if not isinstance(mounts, list) or not mounts:
            raise GuardError(f"refusing: {name} has no mount information")
        sources = []
        for mount in mounts:
            if mount.get("Type") != "bind":
                raise GuardError(f"refusing: {name} mount {mount.get('Destination')} is "
                                 f"{mount.get('Type')!r}, not a bind mount inside the repo")
            path = normalize_mount_path(mount.get("Source") or "")
            if not inside_repo(path):
                raise GuardError(f"refusing: {name} mount source {path} is outside {ROOT}")
            sources.append(str(path))
        report["mounts"][name] = sources

        ports = inspect_json("{{json .NetworkSettings.Ports}}", name) or {}
        published = set()
        for bindings in ports.values():
            for binding in bindings or []:
                host = binding.get("HostPort")
                if host and str(host).isdigit():
                    published.add(int(host))
        if published != EXPECTED_PORTS[name]:
            raise GuardError(f"refusing: {name} publishes {sorted(published)}, expected "
                             f"{sorted(EXPECTED_PORTS[name])}")
        if published & FORBIDDEN_PORTS:
            raise GuardError(f"refusing: {name} publishes a forbidden port "
                             f"{sorted(published & FORBIDDEN_PORTS)}")
        report["ports"][name] = sorted(published)

    actual_db = psql_scalar("select current_database();")
    if actual_db != PG_DB:
        raise GuardError(f"refusing: connected database is {actual_db!r}, expected {PG_DB!r}")
    report["database"] = actual_db

    report["backendPid"] = verify_backend_pid(rig)
    report["aiPid"] = verify_ai_pid(rig)
    report["targets"] = rig.get("targets")
    return report


def print_targets(report: Mapping[str, Any]) -> None:
    print("== resolved targets (fail-closed guard) ==")
    print(f"   repository      : {ROOT}")
    print(f"   rig state       : {report.get('rigStateFile')}")
    print(f"   database        : {report.get('database')}")
    for name in sorted(ALLOWED_CONTAINERS):
        state = report["containers"].get(name, {})
        print(f"   container       : {name} running={state.get('running')} "
              f"ports={report['ports'].get(name)}")
        for source in report["mounts"].get(name, []):
            print(f"     mount source  : {source}")
    backend = report.get("backendPid") or {}
    ai = report.get("aiPid") or {}
    print(f"   backend pid     : {backend.get('pid')} {backend.get('name')} "
          f"listens={backend.get('listensOnBackendPort')}")
    print(f"   ai-service pid  : {ai.get('pid')} {ai.get('name')} "
          f"listens={ai.get('listensOnAiPort')}")


# --------------------------------------------------------------------------- #
# SQL (macteval_ui only)
# --------------------------------------------------------------------------- #
def sql_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def psql(sql: str, timeout: float = 120.0) -> list[list[str]]:
    """Run SQL on the rig database. Every call re-checks the container name."""
    assert_container_allowed(PG_CONTAINER, "psql")
    result = docker("exec", "-e", "PGCLIENTENCODING=UTF8", PG_CONTAINER, "psql",
                    "-U", PG_USER, "-d", PG_DB, "-A", "-t", "-F", "\x1f",
                    "-v", "ON_ERROR_STOP=1", "-c", sql, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {(result.stderr or '').strip()[:600]}")
    return [line.split("\x1f") for line in result.stdout.splitlines() if line.strip()]


def psql_scalar(sql: str, timeout: float = 120.0) -> str | None:
    rows = psql(sql, timeout=timeout)
    return rows[0][0] if rows and rows[0] else None


def redis_cli(*args: str, timeout: float = 30.0) -> str:
    assert_container_allowed(REDIS_CONTAINER, "redis-cli")
    result = docker("exec", REDIS_CONTAINER, "redis-cli", *args, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"redis-cli failed: {(result.stderr or '').strip()[:400]}")
    return result.stdout.strip()


def redis_stream_length() -> int | None:
    try:
        return int(redis_cli("XLEN", REDIS_STREAM))
    except (RuntimeError, ValueError):
        return None


def redis_consumer_count() -> int | None:
    try:
        out = redis_cli("XINFO", "GROUPS", REDIS_STREAM)
    except RuntimeError:
        return None
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "name" and index + 1 < len(lines) \
                and lines[index + 1].strip() == REDIS_GROUP:
            for offset in range(index, min(index + 12, len(lines) - 1)):
                if lines[offset].strip() == "consumers":
                    try:
                        return int(lines[offset + 1].strip())
                    except ValueError:
                        return None
    return None


def job_row(job_id: str) -> dict | None:
    rows = psql(
        "select id, type, status, attempt, coalesce(recovery_count,0), "
        "coalesce(to_char(lease_until,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
        "coalesce(error_message,''), coalesce(result_metadata,''), "
        "coalesce(ingest_state,''), coalesce(lesson_session_id,'') "
        f"from analysis_jobs where id={sql_literal(job_id)}")
    if not rows:
        return None
    keys = ("id", "type", "status", "attempt", "recovery_count", "lease_until",
            "error_message", "result_metadata", "ingest_state", "lesson_session_id")
    row = dict(zip(keys, rows[0]))
    for key in ("attempt", "recovery_count"):
        try:
            row[key] = int(row[key])
        except (TypeError, ValueError):
            row[key] = None
    return row


def job_heartbeat_at(job_id: str) -> str | None:
    """Last heartbeat the backend recorded, or None.

    Used to prove a killed worker stopped renewing its lease: a heartbeat that
    advances after the SIGKILL would mean the "dead" process was still alive and
    owns the attempt, which makes any later requeue prove nothing.
    """
    value = psql_scalar(
        "select coalesce(to_char(heartbeat_at,'YYYY-MM-DD HH24:MI:SS.MS'),'') "
        f"from analysis_jobs where id={sql_literal(job_id)}")
    return value or None


def count_rows(table: str, where: str) -> int:
    if table not in TABLES:
        raise GuardError(f"refusing: {table!r} is not a table this drill reads")
    value = psql_scalar(f"select count(*) from {table} where {where}")
    return int(value) if value and value.isdigit() else 0


def distinct_windows(job_id: str) -> int:
    value = psql_scalar("select count(distinct window_index) from behavior_observations "
                        f"where job_id={sql_literal(job_id)}")
    return int(value) if value and value.isdigit() else 0


def business_counts(job_id: str, lesson_id: str) -> dict:
    return {
        "observations": count_rows("behavior_observations", f"job_id={sql_literal(job_id)}"),
        "distinctWindows": distinct_windows(job_id),
        "evidenceFrames": count_rows("behavior_evidence_frames",
                                     f"job_id={sql_literal(job_id)}"),
        "windowFeatures": count_rows("lesson_window_features",
                                     f"lesson_session_id={sql_literal(lesson_id)}"),
        "reports": count_rows("evaluation_reports",
                              f"lesson_session_id={sql_literal(lesson_id)}"),
    }


def foreign_running_jobs(own_ids: Iterable[str] = ()) -> list[str]:
    """RUNNING jobs this drill does not own.

    A container stop, a consumer SIGKILL or a backend restart would abort
    whatever is currently being analysed, so every scenario that injects such a
    fault refuses to run while someone else's job is in flight.  This is what
    keeps the harness safe on a rig that is simultaneously seeding a long run.
    """
    rows = psql("select id from analysis_jobs where status='RUNNING'")
    own = {str(item) for item in own_ids}
    return [row[0] for row in rows if row and row[0] not in own]


def require_idle(own_ids: Iterable[str] = ()) -> None:
    busy = foreign_running_jobs(own_ids)
    if busy:
        raise ScenarioSkip(
            "another analysis job is RUNNING on this rig and this scenario would "
            f"interrupt it: {busy}")


def force_expired_lease(job_id: str, seconds_ago: int = 180) -> None:
    """Make a RUNNING lease look long expired (accelerated; recorded as such)."""
    psql("update analysis_jobs set lease_until = now() - interval "
         f"'{int(seconds_ago)} seconds' where id={sql_literal(job_id)} "
         "and status='RUNNING'")


# --------------------------------------------------------------------------- #
# HTTP (standard library only)
# --------------------------------------------------------------------------- #
def http(method: str, url: str, body: Any = None, token: str | None = None,
         timeout: float = 30.0) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - connection refused is an observation
        return {"status": None, "error": f"{type(exc).__name__}: {exc}", "body": {},
                "elapsedMs": round((time.monotonic() - started) * 1000, 1)}
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        parsed = {"raw": raw[:400].decode("utf-8", "replace")}
    return {"status": status, "body": parsed, "error": None,
            "elapsedMs": round((time.monotonic() - started) * 1000, 1)}


def api(path: str, method: str = "GET", body: Any = None, token: str | None = None,
        timeout: float = 30.0) -> dict:
    return http(method, BASE + path, body=body, token=token, timeout=timeout)


def svc_post(path: str, body: Mapping[str, Any] | None = None,
             timeout: float = 30.0) -> dict:
    """Service-token endpoints (claim / heartbeat / complete)."""
    data = None
    headers = {"X-Service-Token": SERVICE_TOKEN, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except Exception as exc:  # noqa: BLE001
        return {"status": None, "data": {}, "error": f"{type(exc).__name__}: {exc}"}
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    data_out = parsed.get("data") if isinstance(parsed, dict) else None
    return {"status": status, "data": data_out or {}, "error": None,
            "message": (parsed or {}).get("message") if isinstance(parsed, dict) else None}


def wait_http(url: str, timeout: float, label: str, interval: float = 1.0) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if http("GET", url, timeout=5.0).get("status") == 200:
            return round(time.monotonic() - started, 2)
        time.sleep(interval)
    raise TimeoutError(f"{label} did not answer 200 within {timeout}s ({url})")


def wait_until(fn: Callable[[], Any], timeout: float, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


# --------------------------------------------------------------------------- #
# rig processes (started exactly the way browser_rig does)
# --------------------------------------------------------------------------- #
def browser_rig_module():
    """Import the rig launcher so the env is byte-identical to its own starts."""
    verify_dir = ROOT / "scripts" / "verify"
    if str(verify_dir) not in sys.path:
        # Appended, never inserted: stdlib must keep precedence over any file
        # that happens to sit in that directory.
        sys.path.append(str(verify_dir))
    import browser_rig  # noqa: PLC0415 - imported once, used for env + constants

    mismatches = []
    for attr, expected in (("PG_CONTAINER", PG_CONTAINER), ("PG_PORT", PG_PORT),
                           ("PG_DB", PG_DB), ("PG_USER", PG_USER),
                           ("REDIS_CONTAINER", REDIS_CONTAINER),
                           ("REDIS_PORT", REDIS_PORT),
                           ("MINIO_CONTAINER", MINIO_CONTAINER),
                           ("MINIO_PORT", MINIO_PORT),
                           ("MINIO_USER", MINIO_USER), ("MINIO_SECRET", MINIO_SECRET),
                           ("BACKEND_PORT", BACKEND_PORT), ("AI_PORT", AI_PORT),
                           ("SERVICE_TOKEN", SERVICE_TOKEN)):
        actual = getattr(browser_rig, attr, None)
        if actual != expected:
            mismatches.append(f"{attr}: browser_rig={actual!r} drill={expected!r}")
    if mismatches:
        raise GuardError("browser_rig.py and this drill disagree about the rig: "
                         + "; ".join(mismatches))
    return browser_rig


def backend_env() -> dict:
    return dict(browser_rig_module().backend_env())


def ai_env() -> dict:
    return dict(browser_rig_module().ai_env())


def log_path(name: str) -> Path:
    return LOG_DIR / name


def log_size(name: str) -> int:
    try:
        return log_path(name).stat().st_size
    except OSError:
        return 0


def log_since(name: str, offset: int) -> str:
    try:
        with log_path(name).open("rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _assert_spawned_process_is_serving(pid: int, port: int, label: str) -> None:
    """Prove the process this drill spawned is the one answering on ``port``.

    ``wait_http`` succeeding only proves *something* serves that port: another
    operator or agent may have (re)started the rig in the meantime, in which
    case the child we spawned died on ADDRINUSE while the HTTP probe went green
    against the other process. Recording that dead PID in ``rig.json`` is worse
    than failing, because the next ``browser_rig stop`` would then no-op on a
    live service. So the spawned PID must be alive *and* one of the listeners.
    """
    if pid_alive(pid) is not True:
        raise RuntimeError(f"{label} child PID {pid} exited during startup "
                           "(another instance may already own the port)")
    listeners = find_listen_pids(port)
    if pid not in listeners:
        # An empty set is NOT a pass: ``wait_http`` already got a 200 from this
        # port a moment ago, so "nobody listens" means our own observation is
        # broken (or the child lost the port). Either way the spawned PID cannot
        # be proven to serve it, and recording it would be a guess.
        raise RuntimeError(
            f"{label} child PID {pid} is not observed listening on {port} "
            f"(listeners={sorted(listeners) or 'none observed'}); cannot prove this "
            "child owns the port")


def start_backend(log_name: str = BACKEND_LOG) -> subprocess.Popen:
    """Spawn the backend exactly like ``browser_rig.start_services`` does."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not JAR.exists():
        raise ScenarioSkip(f"backend jar missing: {JAR}")
    handle = log_path(log_name).open("a", encoding="utf-8")
    proc = subprocess.Popen(["java", "-jar", str(JAR)], cwd=str(ROOT),
                            env=backend_env(), stdout=handle, stderr=subprocess.STDOUT)
    try:
        wait_http(BASE + "/health", 240, "backend")
    except TimeoutError:
        proc.kill()
        raise
    _assert_spawned_process_is_serving(proc.pid, BACKEND_PORT, "backend")
    return proc


def start_ai(port: int, log_name: str) -> subprocess.Popen:
    """Spawn an ai-service consumer on ``port`` with the rig's own environment."""
    assert_port_allowed(port, "ai-service")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not (AI_SERVICE / "app" / "main.py").exists():
        raise ScenarioSkip(f"ai-service sources missing: {AI_SERVICE}")
    handle = log_path(log_name).open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(AI_SERVICE), env=ai_env(), stdout=handle, stderr=subprocess.STDOUT)
    try:
        wait_http(f"http://127.0.0.1:{port}/ready", 300, f"ai-service:{port}")
    except TimeoutError:
        proc.kill()
        raise
    _assert_spawned_process_is_serving(proc.pid, port, "ai-service")
    return proc


def kill_pid(pid: int, hard: bool = True, timeout: float = 20.0) -> dict:
    """Terminate one PID and *prove* it died before returning.

    ``taskkill`` reports success when the kill is delivered, not when the
    process object is gone; a torch/CUDA worker takes a couple of seconds to
    tear down. ``ok`` therefore means "a liveness probe confirmed the process is
    gone", never "taskkill exited 0". ``alive`` is the post-proof state.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"pid": pid, "ok": False, "alive": None,
                "error": f"not a PID: {pid!r}", "seconds": 0.0}
    started = time.monotonic()
    # A PID that was already gone must never be reported as "killed by us": the
    # caller would treat a dead-on-arrival target as proof the fault landed.
    if pid_alive(pid) is not True:
        return {"pid": pid, "ok": False, "alive": False,
                "error": "process was not running before the kill",
                "seconds": round(time.monotonic() - started, 2)}
    try:
        if os.name == "nt":
            args = ["taskkill", "/PID", str(pid), "/T"]
            if hard:
                args.append("/F")
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            how = "taskkill"
        else:
            os.kill(pid, 9 if hard else 15)
            result = None
            how = "signal"
    except Exception as exc:  # noqa: BLE001 - cleanup must observe the failure
        return {"pid": pid, "ok": False, "alive": None,
                "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.monotonic() - started, 2)}

    # Poll for real death instead of sampling once right after the signal.
    deadline = time.monotonic() + max(5.0, DEATH_PROOF_TIMEOUT_SECONDS)
    first_probe = None
    final_alive = pid_alive(pid)
    while time.monotonic() < deadline:
        alive = pid_alive(pid)
        if first_probe is None:
            first_probe = alive
        final_alive = alive
        if alive is False:
            break
        time.sleep(0.25)
    return {"pid": pid, "ok": final_alive is False, "alive": final_alive,
            "aliveImmediatelyAfterSignal": first_probe,
            "how": how, "hard": hard,
            "returncode": getattr(result, "returncode", None),
            "tasklistPresent": tasklist_has_pid(pid),
            "seconds": round(time.monotonic() - started, 2)}


def update_rig_state(**updates: Any) -> dict:
    """Keep ``rig.json`` honest after this drill restarts a process.

    ``browser_rig stop`` and any later drill read PID fields from this file; a
    stale PID there would make the next stop a no-op on the live process.
    Only PID-ish keys are written back.
    """
    allowed = {"backendPid", "aiPid"}
    unknown = set(updates) - allowed
    if unknown:
        raise GuardError(f"refusing to write unexpected rig.json keys: {sorted(unknown)}")
    # Only record a PID that is demonstrably alive. A dead PID here makes the
    # next 'browser_rig stop' a silent no-op on a live service.
    for key, value in updates.items():
        if pid_alive(value) is not True:
            raise GuardError(f"refusing to record {key}={value!r}: that process is not alive")
    state = read_rig_state()
    state.update(updates)
    RIG_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {key: state.get(key) for key in allowed}


# --------------------------------------------------------------------------- #
# scenario bookkeeping
# --------------------------------------------------------------------------- #
class Timeline:
    def __init__(self) -> None:
        self._started = time.monotonic()
        self.events: list[dict] = []

    def mark(self, event: str, **fields: Any) -> dict:
        entry: dict[str, Any] = {"at_s": round(time.monotonic() - self._started, 3),
                                 "event": event}
        entry.update({key: value for key, value in fields.items() if value is not None})
        self.events.append(entry)
        return entry

    def elapsed(self) -> float:
        return round(time.monotonic() - self._started, 3)


def new_result(name: str) -> dict:
    return {
        "scenario": name,
        "status": "pending",
        "initial": {},
        "injected": {},
        "recovered": {},
        "assertions": [],
        "cleanup": {"ok": None, "detail": "not evaluated"},
        "timeline": [],
        "duration_s": None,
        "skippedReason": None,
        "error": None,
        "notes": list(NOTES),
    }


def add_assertion(result: dict, name: str, ok: bool, expected: Any, actual: Any) -> bool:
    result["assertions"].append({"name": name, "ok": bool(ok),
                                 "expected": expected, "actual": actual})
    return bool(ok)


def derive_status(result: dict) -> str:
    if result.get("skippedReason"):
        return "skipped"
    assertions = result.get("assertions") or []
    if not assertions:
        return "failed"
    if result.get("error"):
        return "failed"
    if not all(item.get("ok") for item in assertions):
        return "failed"
    if (result.get("cleanup") or {}).get("ok") is not True:
        return "failed"
    return "passed"


def record_cleanup(result: dict, ok: bool, detail: str) -> None:
    result["cleanup"] = {"ok": bool(ok), "detail": detail}
    if not ok:
        print(f"    CLEANUP FAILED: {detail}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# domain helpers
# --------------------------------------------------------------------------- #
def login() -> str:
    """Log in as the rig's own teacher (never creates a new account)."""
    response = api("/auth/login", "POST",
                   {"username": TEACHER["username"], "password": TEACHER["password"]})
    token = ((response.get("body") or {}).get("data") or {}).get("token")
    if response.get("status") != 200 or not token:
        raise ScenarioSkip(
            f"cannot log in as the rig teacher ({TEACHER['username']}): "
            f"HTTP {response.get('status')} {response.get('error')}")
    return token


def upload_video(token: str, video: Path, suffix: str) -> tuple[str, str]:
    """Create a course + session and upload/confirm ``video``; returns (lesson, job)."""
    if not video.exists():
        raise ScenarioSkip(f"video fixture missing: {video}")
    created = api("/courses", "POST", {"name": f"故障演练课程-{suffix}",
                                       "code": f"FD{suffix}", "department": "故障演练"},
                  token=token)
    course = ((created.get("body") or {}).get("data") or {}).get("id")
    if not course:
        raise ScenarioSkip(f"course creation failed: HTTP {created.get('status')}")
    session = api("/sessions", "POST", {"courseId": course, "classId": "class-001",
                                        "title": f"故障演练课堂-{suffix}"}, token=token)
    lesson = ((session.get("body") or {}).get("data") or {}).get("id")
    if not lesson:
        raise ScenarioSkip(f"session creation failed: HTTP {session.get('status')}")
    upload = api("/upload/media", "POST",
                 {"mediaType": "video", "fileName": video.name,
                  "lessonSessionId": lesson,
                  # SEC-03：init 需声明大小与 MIME
                  "sizeBytes": video.stat().st_size,
                  "contentType": "video/mp4"}, token=token)
    data = (upload.get("body") or {}).get("data") or {}
    if not data.get("uploadUrl"):
        raise ScenarioSkip(f"upload session failed: HTTP {upload.get('status')}")
    with video.open("rb") as handle:
        request = urllib.request.Request(
            data["uploadUrl"], data=handle.read(), method="PUT",
            headers={"Content-Type": "video/mp4"})
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:
                put_status = response.status
        except Exception as exc:  # noqa: BLE001
            raise ScenarioSkip(f"video PUT to MinIO failed: {type(exc).__name__}: {exc}")
    if put_status not in (200, 201, 204):
        raise ScenarioSkip(f"video PUT to MinIO returned HTTP {put_status}")
    confirm = api("/upload/media/confirm", "POST",
                  {"assetId": data["assetId"], "objectName": data["objectName"],
                   "lessonSessionId": lesson, "totalSeats": 45,
                   "sampleInterval": 2, "windowDuration": 60,
                   "minObservations": 5}, token=token)
    job = ((confirm.get("body") or {}).get("data") or {}).get("jobId")
    if not job:
        raise ScenarioSkip(f"media confirm failed: HTTP {confirm.get('status')} "
                           f"{confirm.get('body')}")
    return lesson, job


def job_status(token: str, job_id: str) -> dict:
    response = api(f"/jobs/{job_id}", token=token)
    return ((response.get("body") or {}).get("data") or {}) if response.get("status") == 200 else {}


def wait_terminal(token: str, job_id: str, timeout: float,
                  timeline: Timeline | None = None) -> dict | None:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        status = job_status(token, job_id).get("status")
        if status != previous:
            if timeline is not None:
                timeline.mark("job_state", job_id=job_id, status=status)
            previous = status
        if status in TERMINAL:
            return job_status(token, job_id)
        time.sleep(2)
    return None


def report_signature(token: str, lesson: str, version: int | None = None) -> dict:
    """Everything a report must keep across a restart: score, windows, frames."""
    query = f"?version={version}" if version else ""
    report = api(f"/reports/{lesson}{query}", token=token)
    detail = (report.get("body") or {}).get("data") or {}
    evidence = api(f"/reports/{lesson}/behavior-evidence{query}", token=token)
    frames = (evidence.get("body") or {}).get("data") or []
    return {
        "reportHttp": report.get("status"),
        "evidenceHttp": evidence.get("status"),
        "climateScore": detail.get("climateScore"),
        "version": detail.get("version"),
        "status": detail.get("status"),
        "windowCount": ((detail.get("evidence") or {}).get("windowCount")),
        "coverage": ((detail.get("evidence") or {}).get("coverage")),
        "analysisVideoJobId": detail.get("analysisVideoJobId"),
        "frameCount": len(frames),
        "frameIds": sorted(str(frame.get("id")) for frame in frames),
        "frameWindows": sorted({frame.get("windowIndex") for frame in frames
                                if frame.get("windowIndex") is not None}),
    }


def pick_evidence_frame(token: str, lesson: str) -> dict:
    """One frame that has a signed URL, with its bucket/key from the database."""
    evidence = api(f"/reports/{lesson}/behavior-evidence", token=token)
    frames = (evidence.get("body") or {}).get("data") or []
    if not frames:
        raise ScenarioSkip(f"lesson {lesson} has no evidence frames to exercise")
    frame = frames[0]
    row = psql("select bucket, object_key from behavior_evidence_frames where id="
               f"{sql_literal(frame.get('id'))}")
    if not row:
        raise ScenarioSkip(f"evidence frame {frame.get('id')} vanished from the database")
    return {"id": frame.get("id"), "url": frame.get("url"), "code": frame.get("code"),
            "bucket": row[0][0], "key": row[0][1],
            "evidenceHttp": evidence.get("status"), "frameCount": len(frames)}


def minio_client():
    try:
        from minio import Minio  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise ScenarioSkip(f"python 'minio' package is missing: {exc}")
    return Minio(f"127.0.0.1:{MINIO_PORT}", access_key=MINIO_USER,
                 secret_key=MINIO_SECRET, secure=False)


def minio_get(bucket: str, key: str) -> bytes:
    if bucket != MINIO_BUCKET:
        raise GuardError(f"refusing: bucket {bucket!r} is not {MINIO_BUCKET!r}")
    client = minio_client()
    response = None
    try:
        response = client.get_object(bucket, key)
        return response.read()
    finally:
        if response is not None:
            response.close()
            response.release_conn()


def minio_put(bucket: str, key: str, payload: bytes, content_type: str) -> None:
    import io

    if bucket != MINIO_BUCKET:
        raise GuardError(f"refusing: bucket {bucket!r} is not {MINIO_BUCKET!r}")
    minio_client().put_object(bucket, key, io.BytesIO(payload), len(payload),
                              content_type=content_type or "application/octet-stream")


def minio_remove(bucket: str, key: str) -> None:
    if bucket != MINIO_BUCKET:
        raise GuardError(f"refusing: bucket {bucket!r} is not {MINIO_BUCKET!r}")
    minio_client().remove_object(bucket, key)


def minio_exists(bucket: str, key: str) -> bool:
    try:
        minio_client().stat_object(bucket, key)
        return True
    except Exception:  # noqa: BLE001 - any error means "not fetchable"
        return False


def http_status_of(url: str, timeout: float = 15.0) -> int | None:
    """Status code of a presigned GET; ``None`` when the connection itself failed."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(64)
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return None


def container_action(action: str, container: str) -> dict:
    """start/stop/pause/unpause one rig container; anything else refuses first."""
    if action not in ("start", "stop", "pause", "unpause"):
        raise GuardError(f"refusing: container action {action!r} is not allowed")
    assert_container_allowed(container, action)
    result = docker(action, container, timeout=180)
    return {"action": action, "container": container,
            "returncode": result.returncode,
            "stderr": (result.stderr or "").strip()[:300]}


def drill_recipe(lesson: str, job_id: str, attempt: int | None = None,
                 digest: str = "fault-drill", windows: int = 2) -> dict:
    """A structurally valid completion body, used where inference must not race.

    Deliberately carries ``observations`` but no ``evidence_frames``: frame rows
    point at MinIO objects, and a synthetic key would leave a row whose image can
    never load. Observations alone still exercise the real ingest and
    window-alignment path, which is what these scenarios assert.
    """
    observations = [
        {"window_index": index, "window_start_ms": index * 60000,
         "window_end_ms": (index + 1) * 60000, "coverage": 0.9,
         "visible_seat_count": 10, "total_configured_seats": 45,
         "in_seat_count": 9, "leave_seat_count": 0, "hand_raised_count": 2,
         "standing_count": 0, "upright_posture_count": 8,
         "upright_proportion": "0.8", "data_quality": "OK"}
        for index in range(windows)
    ]
    payload: dict[str, Any] = {
        "status": "succeeded",
        "observations": observations,
        "window_count": windows,
        "model_digest": digest,
        "config": {"drill": True, "lessonSessionId": lesson},
    }
    if attempt is not None:
        payload["attempt"] = attempt
    return payload


# --------------------------------------------------------------------------- #
# scenario 1: redis outage
# --------------------------------------------------------------------------- #
def scenario_redis_outage(ctx: dict) -> dict:
    """Read APIs keep answering while Redis is down; the queued job still runs."""
    result, timeline, token = ctx["result"], ctx["timeline"], ctx["token"]
    require_idle()
    baseline = api("/reports", token=token)
    baseline_rows = len((baseline.get("body") or {}).get("data") or [])
    result["initial"] = {
        "redis": container_state(REDIS_CONTAINER),
        "reportsHttp": baseline.get("status"),
        "reportCount": baseline_rows,
        "streamLength": redis_stream_length(),
        "consumers": redis_consumer_count(),
    }
    if baseline.get("status") != 200:
        raise ScenarioSkip(f"read API baseline failed before injection: HTTP "
                           f"{baseline.get('status')}")

    stop = container_action("stop", REDIS_CONTAINER)
    timeline.mark("redis_stopped", **stop)
    if stop["returncode"] != 0:
        record_cleanup(result, False, f"docker stop failed: {stop['stderr']}")
        raise RuntimeError(f"docker stop {REDIS_CONTAINER} failed: {stop['stderr']}")

    try:
        # 1) Read APIs must keep working (no 5xx) and must not silently empty out.
        probes = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            probe = api("/reports", token=token, timeout=40)
            rows = (probe.get("body") or {}).get("data")
            probes.append({"http": probe.get("status"), "error": probe.get("error"),
                           "rows": len(rows) if isinstance(rows, list) else None,
                           "ms": probe.get("elapsedMs")})
            time.sleep(2)
        result["injected"] = {"redisStopped": True, "readProbes": probes,
                              "redisState": container_state(REDIS_CONTAINER)}
        statuses = [probe["http"] for probe in probes]
        row_counts = [probe["rows"] for probe in probes if probe["rows"] is not None]
        add_assertion(result, "read_api_no_5xx_during_outage",
                      all(code == 200 for code in statuses), "all 200", statuses)
        # Vacuous truth guard: an empty list would make all() pass without a
        # single successful read, so at least one probe must have returned rows.
        add_assertion(result, "read_api_not_silently_empty",
                      bool(row_counts) and all(count == baseline_rows
                                               for count in row_counts),
                      f"{baseline_rows} rows on every probe", row_counts)

        # 2) A job submitted during the outage must be durably queued, not lost:
        #    the outbox insert succeeds, only the publish step fails.
        suffix = uuid.uuid4().hex[:8]
        lesson, job_id = upload_video(token, VIDEO_SHORT, suffix)
        ctx["jobId"], ctx["lessonId"] = job_id, lesson
        timeline.mark("job_submitted_during_outage", jobId=job_id, lesson=lesson)
        queued = job_row(job_id)

        def _deferred_outbox() -> list[str] | None:
            rows = psql(
                "select coalesce(to_char(published_at,'YYYY-MM-DD HH24:MI:SS'),''), "
                "attempt, coalesce(last_error,'') from task_outbox where "
                f"aggregate_id={sql_literal(job_id)} order by created_at desc limit 1")
            if not rows:
                return None
            published, attempt_value, error = rows[0]
            # The publish loop must have tried and failed at least once while
            # staying unpublished; "attempt 0" means it has not tried yet.
            if published == "" and attempt_value.isdigit() and int(attempt_value) >= 1:
                return rows[0]
            return None

        outbox = wait_until(_deferred_outbox, 90, 2)
        result["injected"].update({"jobId": job_id, "lessonId": lesson,
                                   "jobStatus": (queued or {}).get("status"),
                                   "outbox": ({"published_at": outbox[0],
                                               "attempt": outbox[1],
                                               "last_error": outbox[2]}
                                              if outbox else None)})
        add_assertion(result, "job_durably_queued_without_redis",
                      (queued or {}).get("status") == "QUEUED", "QUEUED",
                      (queued or {}).get("status"))
        add_assertion(result, "outbox_publish_deferred_not_lost",
                      bool(outbox) and outbox[0] == "" and int(outbox[1]) >= 1,
                      "published_at NULL, attempt>=1", outbox)
    finally:
        start = container_action("start", REDIS_CONTAINER)
        timeline.mark("redis_started", **start)
        started_ok = False
        try:
            started_ok = bool(wait_until(lambda: redis_cli("PING") == "PONG", 90, 2))
        except Exception as exc:  # noqa: BLE001 - reported below
            timeline.mark("redis_restart_failed", error=str(exc))
        record_cleanup(result, started_ok,
                       "redis restarted" if started_ok else "redis did not answer PING")

    if not result["cleanup"]["ok"]:
        result["recovered"] = {"redis": "not running"}
        add_assertion(result, "redis_restored_after_fault", False, "PONG",
                      "no PONG")
        return result

    # 3) After restart: consumer rejoins, the queued event is published, job runs.
    ready_body = wait_until(
        lambda: (lambda probe: probe.get("body") if probe.get("status") == 200 else None)(
            http("GET", f"{AI_BASE}/ready", timeout=10)), 180, 3)
    ready_state = ((ready_body or {}).get("details") or {}).get("consumer_state")
    job_id = ctx.get("jobId")
    terminal = wait_terminal(token, job_id, 900, timeline) if job_id else None
    final = job_row(job_id) if job_id else None
    counts = business_counts(job_id, ctx.get("lessonId")) if job_id else {}
    result["recovered"] = {
        "aiReady": ready_body is not None, "consumerState": ready_state,
        "consumers": redis_consumer_count(),
        "jobStatus": (final or {}).get("status"),
        "attempt": (final or {}).get("attempt"),
        "recoveryCount": (final or {}).get("recovery_count"),
        "counts": counts,
        "elapsedSinceRedisStart": timeline.elapsed(),
    }
    # Consumer names are timestamped and never pruned, so a raw count says
    # nothing about *this* restart. The process's own /ready body reports the
    # consumer state it reached after the Redis outage, which is the fact here.
    add_assertion(result, "consumer_rejoined_after_restart", ready_state == "running",
                  "consumer_state=running",
                  {"ready": ready_body is not None, "consumer_state": ready_state})
    add_assertion(result, "queued_job_completed_after_restart",
                  bool(terminal) and (final or {}).get("status") == "SUCCEEDED",
                  "SUCCEEDED", (final or {}).get("status"))
    add_assertion(result, "queued_job_ran_once", (final or {}).get("attempt") == 1, 1,
                  (final or {}).get("attempt"))
    add_assertion(result, "observations_ingested", counts.get("observations", 0) > 0,
                  ">0", counts.get("observations"))
    add_assertion(result, "no_duplicate_windows",
                  counts.get("observations") == counts.get("distinctWindows"), True,
                  {"observations": counts.get("observations"),
                   "distinctWindows": counts.get("distinctWindows")})
    result["notes"].append(
        "The Redis stop is a graceful docker stop, so the RDB snapshot survives and the "
        "consumer group normally still exists on restart. If the group is recreated at "
        "id=0 the worker re-reads old messages and ACKs them as terminal; that is expected "
        "and is not duplicate inference.")
    return result


# --------------------------------------------------------------------------- #
# scenario 2: double consumer
# --------------------------------------------------------------------------- #
def count_log_marker(name: str, marker: str) -> int:
    try:
        return log_path(name).read_text(encoding="utf-8", errors="replace").count(marker)
    except OSError:
        return 0


def scenario_double_consumer(ctx: dict) -> dict:
    """Two consumers on one stream/group: exactly one processes a submitted job."""
    result, timeline, token = ctx["result"], ctx["timeline"], ctx["token"]
    require_idle()
    if tcp_open(SECOND_AI_PORT):
        raise ScenarioSkip(f"port {SECOND_AI_PORT} is already in use; cannot start a "
                           "second consumer")
    second_log = "ai-8002-double-consumer.log"
    first_log = AI_LOG
    consumers_before = redis_consumer_count()
    result["initial"] = {"consumers": consumers_before,
                         "streamLength": redis_stream_length(),
                         "secondPortFree": True}
    try:
        proc = start_ai(SECOND_AI_PORT, second_log)
        ctx["secondPid"] = proc.pid
    except ScenarioSkip:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ScenarioSkip(f"second consumer did not start: {type(exc).__name__}: {exc}")
    timeline.mark("second_consumer_started", port=SECOND_AI_PORT, pid=proc.pid)
    try:
        # Consumer names include a wall-clock suffix and are never pruned, so a
        # raw count is ambiguous. Bind the claim to *this* process instead: its
        # own /ready body reports the consumer state it actually reached.
        second_ready = wait_until(
            lambda: (lambda probe: probe.get("body") if probe.get("status") == 200 else None)(
                http("GET", f"http://127.0.0.1:{SECOND_AI_PORT}/ready", timeout=10)), 180, 3)
        second_state = ((second_ready or {}).get("details") or {}).get("consumer_state")
        actual_consumers = redis_consumer_count()
        result["injected"] = {"secondConsumerPort": SECOND_AI_PORT, "pid": proc.pid,
                              "consumersBefore": consumers_before,
                              "consumersAfterStart": actual_consumers,
                              "secondConsumerState": second_state}
        joined = second_state == "running"
        add_assertion(result, "second_consumer_joined_the_same_group", joined,
                      "second /ready consumer_state=running", second_state)
        if not joined:
            raise ScenarioSkip(f"second consumer never reached consumer_state=running "
                               f"(got {second_state!r})")

        # Markers are counted from the byte offset taken *before* submitting:
        # sampling the logs after the upload could miss a start that already
        # happened (the outbox publishes within ~2s).
        offset_first = log_size(first_log)
        offset_second = log_size(second_log)
        suffix = uuid.uuid4().hex[:8]
        lesson, job_id = upload_video(token, VIDEO_SHORT, suffix)
        ctx["jobId"], ctx["lessonId"] = job_id, lesson
        marker = f"Starting video analysis: lesson={lesson}"
        timeline.mark("job_submitted", jobId=job_id, lesson=lesson)

        terminal = wait_terminal(token, job_id, 900, timeline)
        final = job_row(job_id) or {}
        first_starts = log_since(first_log, offset_first).count(marker)
        second_starts = log_since(second_log, offset_second).count(marker)
        counts = business_counts(job_id, lesson)
        result["recovered"] = {
            "jobStatus": final.get("status"), "attempt": final.get("attempt"),
            "recoveryCount": final.get("recovery_count"),
            "inferenceStarts": {"consumer-8001": first_starts,
                                "consumer-8002": second_starts},
            "counts": counts,
        }
        add_assertion(result, "job_completed", bool(terminal)
                      and final.get("status") == "SUCCEEDED", "SUCCEEDED",
                      final.get("status"))
        add_assertion(result, "exactly_one_consumer_ran_inference",
                      first_starts + second_starts == 1, 1,
                      {"consumer-8001": first_starts, "consumer-8002": second_starts})
        add_assertion(result, "single_attempt", final.get("attempt") == 1, 1,
                      final.get("attempt"))
        add_assertion(result, "no_recovery_requeue", int(final.get("recovery_count") or 0) == 0,
                      0, final.get("recovery_count"))
        add_assertion(result, "no_duplicate_windows",
                      counts.get("observations") == counts.get("distinctWindows"), True,
                      {"observations": counts.get("observations"),
                       "distinctWindows": counts.get("distinctWindows")})
        result["notes"].append(
            "Inference starts are counted per consumer log; XREADGROUP hands each message "
            "to exactly one consumer, so a second start would mean the message was "
            "reclaimed and re-run.")
    finally:
        second = ctx.get("secondPid")
        clean = True
        detail = "second consumer stopped"
        if second:
            killed = kill_pid(second)
            timeline.mark("second_consumer_stopped", **killed)
            still_alive = pid_alive(second)
            if still_alive is None:
                clean = False
                detail = f"cannot prove PID {second} died: {killed}"
            elif still_alive:
                clean = False
                detail = f"second consumer PID {second} still alive: {killed}"
        gone = wait_until(lambda: not tcp_open(SECOND_AI_PORT), 60, 2)
        if not gone:
            clean = False
            detail = f"port {SECOND_AI_PORT} still listening after kill"
        record_cleanup(result, clean, detail)
    return result


# --------------------------------------------------------------------------- #
# scenario 3: consumer kill
# --------------------------------------------------------------------------- #
def scenario_consumer_kill(ctx: dict) -> dict:
    """SIGKILL mid-inference: the lease expires, the job is reclaimed and completes."""
    result, timeline, token = ctx["result"], ctx["timeline"], ctx["token"]
    require_idle()
    rig = read_rig_state()
    ai_pid = rig.get("aiPid")
    if pid_alive(ai_pid) is not True:
        raise ScenarioSkip(f"ai-service PID {ai_pid} is not running "
                           f"({describe_pid(ai_pid)})")
    if not VIDEO_LONG.exists():
        raise ScenarioSkip(f"long video fixture missing: {VIDEO_LONG}")

    # Which backend process owns this attempt? Recovery (lease expiry -> requeue)
    # is *its* job, so if someone else restarts the backend mid-run the requeue
    # would belong to a different process and this drill would be crediting the
    # wrong code. Recorded now and re-checked before the assertions are trusted.
    backend_pid_before = rig.get("backendPid")

    suffix = uuid.uuid4().hex[:8]
    lesson, job_id = upload_video(token, VIDEO_LONG, suffix)
    ctx["jobId"], ctx["lessonId"] = job_id, lesson
    runner = wait_until(lambda: (job_row(job_id) or {}).get("status") == "RUNNING", 180, 2)
    if not runner:
        raise ScenarioSkip("job never became RUNNING; cannot kill an idle consumer")
    marker = f"Starting video analysis: lesson={lesson}"
    started = wait_until(lambda: count_log_marker(AI_LOG, marker) >= 1, 180, 2)
    if not started:
        raise ScenarioSkip("inference never started; the kill would not land mid-job")
    lease_before = (job_row(job_id) or {}).get("lease_until")
    heartbeat_before = job_heartbeat_at(job_id)
    result["initial"] = {"aiPid": ai_pid, "jobStatus": "RUNNING",
                         "leaseUntil": lease_before,
                         "heartbeatAt": heartbeat_before,
                         "inferenceStarted": True,
                         "counts": business_counts(job_id, lesson)}

    killed_at = time.monotonic()
    killed = kill_pid(ai_pid)
    ai_alive_after = pid_alive(ai_pid)
    timeline.mark("ai_service_killed", **killed)
    result["injected"] = {"killedPid": ai_pid, "kill": killed,
                          "aiAliveAfterKill": ai_alive_after}
    # ``killed["ok"]`` already means "proven gone" (kill_pid polls for death);
    # the assertion states that fact positively instead of comparing a bool to
    # its own negation.
    killed_ok = killed.get("ok") is True and ai_alive_after is False
    add_assertion(result, "worker_process_killed", killed_ok,
                  "worker PID gone (proven by liveness poll)",
                  "dead" if killed_ok else "alive")
    if not killed_ok:
        # Nothing else in this scenario means anything if the worker survived:
        # any later "recovery" would have happened with the original worker
        # still heartbeating, which proves nothing about SIGKILL recovery.
        result["timeline"].append({"at_s": timeline.elapsed(),
                                   "event": "kill_not_proven",
                                   "pid": ai_pid, "kill": killed})
        restarted = restore_consumer(ctx, result)
        record_cleanup(result, restarted,
                       "worker was never confirmed dead; ai-service left running"
                       if restarted else
                       "无法杀死 worker（taskkill 未生效）且 ai-service 未在 8001 运行")
        raise ScenarioSkip(
            f"无法杀死 worker（taskkill 未生效）: PID {ai_pid} still alive after "
            f"{killed.get('seconds')}s ({killed})")

    # The attempt must be business-dead too. A 30s heartbeat is what keeps a
    # lease alive, so a surviving worker would renew it and the requeue below
    # would only happen by accident. Watching the row is the only honest test:
    # sample (status, heartbeat_at) while the job is still RUNNING and flag any
    # heartbeat that advances past the pre-kill value. The final value is NOT
    # used for this: ``requeueExpired`` sets heartbeat_at = NULL as part of the
    # requeue itself, so "NULL afterwards" proves nothing either way.
    lease_still_held = (job_row(job_id) or {}).get("status") == "RUNNING"
    heartbeat_samples: list[dict] = []
    advanced_after_kill = False
    left_running = None
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        row = job_row(job_id) or {}
        heartbeat_now = job_heartbeat_at(job_id)
        still_running = row.get("status") == "RUNNING"
        if still_running and heartbeat_now and heartbeat_before                 and heartbeat_now > heartbeat_before:
            advanced_after_kill = True
        heartbeat_samples.append({"at_s": round(time.monotonic() - killed_at, 1),
                                  "status": row.get("status"),
                                  "heartbeatAt": heartbeat_now})
        if not still_running:
            left_running = row
            break
        time.sleep(3)
    requeue_seconds = round(time.monotonic() - killed_at, 1)
    after_kill = left_running or (job_row(job_id) or {})
    timeline.mark("job_left_running", status=after_kill.get("status"),
                  recoveryCount=after_kill.get("recovery_count"),
                  seconds=requeue_seconds)
    result["injected"]["heartbeatAtAfterKill"] = job_heartbeat_at(job_id)
    result["injected"]["heartbeatSamples"] = heartbeat_samples
    result["injected"]["heartbeatAdvancedAfterKill"] = advanced_after_kill
    add_assertion(result, "no_heartbeat_after_kill", not advanced_after_kill,
                  f"heartbeat_at never advances past {heartbeat_before} while RUNNING",
                  {"advanced": advanced_after_kill,
                   "lastObservedWhileRunning": next(
                       (sample["heartbeatAt"] for sample in reversed(heartbeat_samples)
                        if sample["status"] == "RUNNING"), None),
                   "samples": len(heartbeat_samples)})
    result["recovered"] = {"requeueSeconds": requeue_seconds,
                           "statusAfterExpiry": after_kill.get("status"),
                           "recoveryCount": after_kill.get("recovery_count"),
                           "leaseWasHeldAfterKill": lease_still_held}
    add_assertion(result, "job_not_stuck_running_after_kill", bool(left_running), True,
                  after_kill.get("status"))
    # With the worker proven dead, the requeue must come from lease expiry, not
    # from a live worker's own failure path.
    add_assertion(result, "requeue_followed_a_dead_worker", lease_still_held,
                  "job still RUNNING right after the kill (lease owned by the dead pid)",
                  lease_still_held)

    restarted = None
    try:
        restarted = start_ai(AI_PORT, AI_LOG)
        ctx["restartedAiPid"] = restarted.pid
        update_rig_state(aiPid=restarted.pid)
        timeline.mark("ai_service_restarted", pid=restarted.pid)
        terminal = wait_terminal(token, job_id, 900, timeline)
        final = job_row(job_id) or {}
        counts = business_counts(job_id, lesson)
        total = round(time.monotonic() - killed_at, 1)
        result["recovered"].update({
            "completedSecondsAfterKill": total,
            "jobStatus": final.get("status"), "attempt": final.get("attempt"),
            "recoveryCount": final.get("recovery_count"), "counts": counts,
        })
        add_assertion(result, "job_completed_after_takeover",
                      bool(terminal) and final.get("status") == "SUCCEEDED", "SUCCEEDED",
                      final.get("status"))
        add_assertion(result, "takeover_used_a_new_attempt", final.get("attempt") == 2, 2,
                      final.get("attempt"))
        add_assertion(result, "recovery_was_counted",
                      int(final.get("recovery_count") or 0) >= 1, ">=1",
                      final.get("recovery_count"))
        add_assertion(result, "observations_ingested", counts.get("observations", 0) > 0,
                      ">0", counts.get("observations"))
        add_assertion(result, "no_duplicate_windows",
                      counts.get("observations") == counts.get("distinctWindows"), True,
                      {"observations": counts.get("observations"),
                       "distinctWindows": counts.get("distinctWindows")})
        backend_pid_after = read_rig_state().get("backendPid")
        result["recovered"]["backendPidBefore"] = backend_pid_before
        result["recovered"]["backendPidAfter"] = backend_pid_after
        # A concurrent operator restart would make the requeue/completion belong
        # to another process. That is not this drill's evidence, so it is stated
        # as an assertion instead of being quietly absorbed into a green run.
        add_assertion(result, "backend_unchanged_during_run",
                      backend_pid_before == backend_pid_after,
                      backend_pid_before, backend_pid_after)
        add_assertion(result, "restarted_consumer_is_ours",
                      restarted is not None
                      and restarted.pid in find_listen_pids(AI_PORT),
                      "drill-spawned PID owns 8001",
                      {"spawned": getattr(restarted, "pid", None),
                       "listeners": sorted(find_listen_pids(AI_PORT))})
        result["notes"].append(
            f"Recovery took {total}s after the SIGKILL (lease {lease_before} plus the "
            "recovery grace window); the number is real, not accelerated.")
    finally:
        alive = tcp_open(AI_PORT) and http("GET", AI_BASE + "/ready", timeout=5).get("status") == 200
        if not alive:
            try:
                proc = start_ai(AI_PORT, AI_LOG)
                ctx["restartedAiPid"] = proc.pid
                update_rig_state(aiPid=proc.pid)
                alive = True
            except Exception as exc:  # noqa: BLE001
                alive = False
                timeline.mark("ai_restart_failed", error=str(exc))
        record_cleanup(result, alive,
                       "ai-service running on 8001 and rig.json updated"
                       if alive else "ai-service is NOT running on 8001")
    return result


# --------------------------------------------------------------------------- #
# scenario 4: duplicate callback
# --------------------------------------------------------------------------- #
def stop_consumer_for_manual_drive(ctx: dict, result: dict, label: str) -> int | None:
    """Stop the rig's only consumer so a scenario owns the claim protocol.

    Returns the PID that was stopped (``None`` when it was already stopped).
    The caller must restart it in its ``finally``.
    """
    rig = read_rig_state()
    pid = rig.get("aiPid")
    if pid_alive(pid) is not True:
        return None
    killed = kill_pid(pid)
    ctx["timeline"].mark(f"{label}_consumer_stopped", **killed)
    result.setdefault("injected", {})["consumerStopped"] = killed
    # A kill that was not proven dead must abort the scenario: a surviving
    # consumer would race this drill for the claim and the result would be a lie.
    if killed.get("ok") is not True or not wait_until(
            lambda: not tcp_open(AI_PORT), 60, 2):
        restore_consumer(ctx, result)
        raise ScenarioSkip(
            f"无法停止消费者 PID {pid}（kill 未生效）: {killed}; cannot own the claim")
    return pid


def restore_consumer(ctx: dict, result: dict) -> bool:
    """Restart the consumer this scenario stopped and keep rig.json truthful."""
    alive = http("GET", AI_BASE + "/ready", timeout=5).get("status") == 200
    if alive:
        return True
    try:
        proc = start_ai(AI_PORT, AI_LOG)
        ctx["restartedAiPid"] = proc.pid
        update_rig_state(aiPid=proc.pid)
        return True
    except Exception as exc:  # noqa: BLE001
        result["timeline"].append({"at_s": None, "event": "consumer_restore_failed",
                                   "error": f"{type(exc).__name__}: {exc}"})
        return False


def scenario_duplicate_callback(ctx: dict) -> dict:
    """The same completion posted twice is idempotent and never a 500."""
    result, timeline, token = ctx["result"], ctx["timeline"], ctx["token"]
    require_idle()
    # Inside the try: if stopping the consumer leaves it half-dead, the finally
    # below still restarts it instead of leaving the rig without a worker.
    stopped = None
    try:
        stopped = stop_consumer_for_manual_drive(ctx, result, "duplicate")
        suffix = uuid.uuid4().hex[:8]
        lesson, job_id = upload_video(token, VIDEO_SHORT, suffix)
        ctx["jobId"], ctx["lessonId"] = job_id, lesson
        claim = svc_post(f"/jobs/{job_id}/claim", {"leaseSeconds": 120})
        attempt = int((claim.get("data") or {}).get("attempt") or 0)
        if (claim.get("data") or {}).get("claimed") is not True:
            raise ScenarioSkip(f"could not claim the fresh job: {claim}")
        payload = drill_recipe(lesson, job_id, attempt)
        before = business_counts(job_id, lesson)
        result["initial"] = {"jobStatus": "RUNNING", "attempt": attempt,
                             "countsBefore": before,
                             "consumerStoppedPid": stopped}

        first = svc_post(f"/jobs/{job_id}/complete", payload)
        after_first = business_counts(job_id, lesson)
        second = svc_post(f"/jobs/{job_id}/complete", payload)
        timeline.mark("callbacks_posted",
                      first=first.get("data"), second=second.get("data"))
        after = business_counts(job_id, lesson)
        final = job_row(job_id) or {}
        result["injected"] = {"firstCallback": first, "secondCallback": second}
        result["recovered"] = {"jobStatus": final.get("status"),
                               "attempt": final.get("attempt"),
                               "countsAfterFirst": after_first,
                               "countsAfterSecond": after}
        add_assertion(result, "first_callback_applied",
                      (first.get("data") or {}).get("applied") is True, True,
                      first.get("data"))
        add_assertion(result, "second_callback_clean_success_or_conflict",
                      second.get("status") == 200
                      and (second.get("data") or {}).get("applied") is False
                      and (second.get("data") or {}).get("reason") == "already_terminal",
                      "HTTP 200 applied=false reason=already_terminal",
                      {"http": second.get("status"), "data": second.get("data")})
        add_assertion(result, "second_callback_not_5xx",
                      second.get("status") is not None and second.get("status") < 500,
                      "<500", second.get("status"))
        add_assertion(result, "job_succeeded", final.get("status") == "SUCCEEDED",
                      "SUCCEEDED", final.get("status"))
        add_assertion(result, "attempt_not_advanced", final.get("attempt") == attempt,
                      attempt, final.get("attempt"))
        add_assertion(result, "first_callback_wrote_observations",
                      after_first.get("observations") == before.get("observations") + 2, 2,
                      after_first.get("observations") - before.get("observations"))
        # The duplicate is the interesting one: every business table must be
        # byte-for-byte stable between the two callbacks.
        delta = {key: after.get(key, 0) - after_first.get(key, 0) for key in after}
        add_assertion(result, "duplicate_callback_changed_no_business_row",
                      all(value == 0 for value in delta.values()), "all zero", delta)
        result["duplicateCheck"] = {"ok": all(value == 0 for value in delta.values()),
                                    "deltaAfterSecondCallback": delta,
                                    "observations": after.get("observations"),
                                    "windowFeatures": after.get("windowFeatures")}
        result["notes"].append(
            "The consumer is stopped only so this drill can own the claim path; the "
            "callback itself is the real POST /jobs/{id}/complete endpoint with the "
            "service token, and idempotency is proven by business-row counts taken after "
            "the first callback and after the duplicate.")
    finally:
        ok = restore_consumer(ctx, result)
        record_cleanup(result, ok, "consumer restored" if ok else
                       "consumer could NOT be restarted on 8001")
    return result


# --------------------------------------------------------------------------- #
# scenario 5: late stale attempt
# --------------------------------------------------------------------------- #
def scenario_late_stale_attempt(ctx: dict) -> dict:
    """An older attempt cannot overwrite the newer attempt's result."""
    result, timeline, token = ctx["result"], ctx["timeline"], ctx["token"]
    require_idle()
    # Inside the try so the finally always restarts a consumer it stopped.
    stopped = None
    try:
        stopped = stop_consumer_for_manual_drive(ctx, result, "stale")
        suffix = uuid.uuid4().hex[:8]
        lesson, job_id = upload_video(token, VIDEO_SHORT, suffix)
        ctx["jobId"], ctx["lessonId"] = job_id, lesson
        claim1 = svc_post(f"/jobs/{job_id}/claim", {"leaseSeconds": 60})
        attempt1 = int((claim1.get("data") or {}).get("attempt") or 0)
        if (claim1.get("data") or {}).get("claimed") is not True or attempt1 != 1:
            raise ScenarioSkip(f"could not claim attempt 1: {claim1}")
        result["initial"] = {"jobStatus": "RUNNING", "attempt": attempt1,
                             "consumerStoppedPid": stopped}

        # Let the lease expire so the recovery scanner requeues and a second
        # attempt can take over. Forced with SQL (accelerated, disclosed).
        force_expired_lease(job_id, 300)
        timeline.mark("lease_expired", accelerated=True,
                      method="UPDATE lease_until = now() - 300s")
        requeued = wait_until(
            lambda: (job_row(job_id) or {}).get("status") == "QUEUED", 180, 2)
        if not requeued:
            raise ScenarioSkip("recovery never requeued the expired attempt")
        claim2 = svc_post(f"/jobs/{job_id}/claim", {"leaseSeconds": 300})
        attempt2 = int((claim2.get("data") or {}).get("attempt") or 0)
        if (claim2.get("data") or {}).get("claimed") is not True or attempt2 != attempt1 + 1:
            raise ScenarioSkip(f"could not claim attempt 2: {claim2}")
        timeline.mark("attempt2_claimed", attempt=attempt2)

        stale_body = drill_recipe(lesson, job_id, attempt1, digest="drill-attempt-1")
        stale = svc_post(f"/jobs/{job_id}/complete", stale_body)
        while_running = job_row(job_id) or {}
        result["injected"] = {"staleCallbackAttempt": attempt1,
                              "currentAttempt": attempt2,
                              "staleCallback": stale,
                              "jobAfterStaleCallback": {
                                  "status": while_running.get("status"),
                                  "attempt": while_running.get("attempt")}}
        add_assertion(result, "stale_callback_rejected",
                      (stale.get("data") or {}).get("applied") is False, False,
                      stale.get("data"))
        add_assertion(result, "stale_callback_reason_stale_attempt",
                      (stale.get("data") or {}).get("reason") == "stale_attempt",
                      "stale_attempt", (stale.get("data") or {}).get("reason"))
        add_assertion(result, "stale_callback_not_5xx",
                      stale.get("status") is not None and stale.get("status") < 500,
                      "<500", stale.get("status"))
        add_assertion(result, "job_still_running_on_new_attempt",
                      while_running.get("status") == "RUNNING"
                      and while_running.get("attempt") == attempt2,
                      {"status": "RUNNING", "attempt": attempt2},
                      {"status": while_running.get("status"),
                       "attempt": while_running.get("attempt")})
        add_assertion(result, "stale_body_not_stored",
                      "drill-attempt-1" not in (while_running.get("result_metadata") or ""),
                      "no attempt-1 body", "absent" if "drill-attempt-1"
                      not in (while_running.get("result_metadata") or "") else "present")

        good_body = drill_recipe(lesson, job_id, attempt2, digest="drill-attempt-2")
        good = svc_post(f"/jobs/{job_id}/complete", good_body)
        after_good = job_row(job_id) or {}
        late = svc_post(f"/jobs/{job_id}/complete", stale_body)
        final = job_row(job_id) or {}
        counts = business_counts(job_id, lesson)
        timeline.mark("newer_attempt_completed", applied=(good.get("data") or {}).get("applied"),
                      late=late.get("data"))
        result["recovered"] = {"newerApplied": good.get("data"), "lateCallback": late,
                               "finalStatus": final.get("status"),
                               "finalAttempt": final.get("attempt"),
                               "resultMetadataHasAttempt2":
                                   "drill-attempt-2" in (final.get("result_metadata") or ""),
                               "counts": counts}
        add_assertion(result, "newer_attempt_applied",
                      (good.get("data") or {}).get("applied") is True, True, good.get("data"))
        add_assertion(result, "job_succeeded_on_newer_attempt",
                      after_good.get("status") == "SUCCEEDED", "SUCCEEDED",
                      after_good.get("status"))
        add_assertion(result, "late_callback_after_success_rejected",
                      (late.get("data") or {}).get("applied") is False, False, late.get("data"))
        add_assertion(result, "late_callback_reason_already_terminal",
                      (late.get("data") or {}).get("reason") == "already_terminal",
                      "already_terminal", (late.get("data") or {}).get("reason"))
        add_assertion(result, "newer_result_not_overwritten",
                      "drill-attempt-2" in (final.get("result_metadata") or "")
                      and "drill-attempt-1" not in (final.get("result_metadata") or ""),
                      "attempt-2 digest present, attempt-1 absent",
                      {"attempt2": "drill-attempt-2" in (final.get("result_metadata") or ""),
                       "attempt1": "drill-attempt-1" in (final.get("result_metadata") or "")})
        add_assertion(result, "final_attempt_is_newest", final.get("attempt") == attempt2,
                      attempt2, final.get("attempt"))
        add_assertion(result, "no_duplicate_windows",
                      counts.get("observations") == counts.get("distinctWindows"), True,
                      {"observations": counts.get("observations"),
                       "distinctWindows": counts.get("distinctWindows")})
        result["notes"].append(
            "The lease expiry is forced with SQL so the requeue happens in seconds instead "
            "of 120s lease + 60s grace; the decision path exercised is the real one.")
    finally:
        ok = restore_consumer(ctx, result)
        record_cleanup(result, ok, "consumer restored" if ok else
                       "consumer could NOT be restarted on 8001")
    return result


# --------------------------------------------------------------------------- #
# scenario 6: missing MinIO object
# --------------------------------------------------------------------------- #
def scenario_minio_object_missing(ctx: dict) -> dict:
    """A deleted evidence object yields a 404 URL, never a 500, and is restored."""
    result, timeline, token = ctx["result"], ctx["timeline"], ctx["token"]
    seed_file = RIG_DIR / "seed.json"
    seeded_lesson = None
    if seed_file.exists():
        try:
            seeded_lesson = json.loads(seed_file.read_text(encoding="utf-8")).get("lessonId")
        except (OSError, json.JSONDecodeError):
            seeded_lesson = None
    if seeded_lesson:
        frame = pick_evidence_frame(token, seeded_lesson)
        lesson = seeded_lesson
    else:
        raise ScenarioSkip("no seeded lesson with evidence frames (run browser_rig seed)")
    if not frame.get("url"):
        raise ScenarioSkip(f"frame {frame.get('id')} has no signed URL to exercise")

    original = minio_get(frame["bucket"], frame["key"])
    original_sha = hashlib.sha256(original).hexdigest()
    before_status = http_status_of(frame["url"])
    result["initial"] = {"lesson": lesson, "frameId": frame["id"], "key": frame["key"],
                         "bytes": len(original), "sha256": original_sha,
                         "urlStatusBefore": before_status,
                         "consumerLogOffset": log_size(BACKEND_LOG)}
    if before_status != 200:
        raise ScenarioSkip(f"presigned URL for {frame['key']} returned {before_status} "
                           "before the fault; cannot attribute a later 404 to the deletion")

    deleted = False
    try:
        minio_remove(frame["bucket"], frame["key"])
        deleted = True
        timeline.mark("object_deleted", key=frame["key"])
        frame_after = pick_evidence_frame(token, lesson)
        missing_status = http_status_of(frame_after.get("url") or "")
        result["injected"] = {"deleted": True,
                              "evidenceHttpAfterDelete": frame_after.get("evidenceHttp"),
                              "framesListedAfterDelete": frame_after.get("frameCount"),
                              "urlStatusAfterDelete": missing_status,
                              "frameStillListed": bool(frame_after.get("id"))}
        add_assertion(result, "evidence_api_still_200",
                      frame_after.get("evidenceHttp") == 200, 200,
                      frame_after.get("evidenceHttp"))
        add_assertion(result, "presigned_url_404_not_500",
                      missing_status in (403, 404), [403, 404], missing_status)
        add_assertion(result, "frame_row_still_returned",
                      frame_after.get("id") == frame.get("id"), frame.get("id"),
                      frame_after.get("id"))

        # No retry storm: repeated reads must not add ERROR lines for the frame.
        offset = result["initial"]["consumerLogOffset"]
        for _ in range(3):
            api(f"/reports/{lesson}/behavior-evidence", token=token)
            http_status_of(pick_evidence_frame(token, lesson).get("url") or "")
        tail = log_since(BACKEND_LOG, offset)
        frame_hits = tail.count(frame["key"]) + tail.count(str(frame["id"]))
        # Scoped to this object: an unrelated ERROR elsewhere in the log must not
        # make the assertion fail, and this object's retries must not be hidden.
        errors = [line for line in tail.splitlines()
                  if ("ERROR" in line or "NoSuchKey" in line)
                  and (frame["key"] in line or str(frame["id"]) in line)]
        result["recovered"] = {"logLinesMentioningFrame": frame_hits,
                               "errorLinesAfterDeletion": errors[:5],
                               "errorLineCount": len(errors)}
        add_assertion(result, "no_retry_storm_in_backend_log", frame_hits <= 6, "<=6",
                      frame_hits)
        add_assertion(result, "no_error_lines_for_missing_object", len(errors) == 0, 0,
                      len(errors))
    finally:
        restored_ok = False
        detail = "object restored"
        if not deleted:
            # The delete itself raised, so nothing was removed. Never report that
            # as an unrestored object, and never swallow the original exception
            # with a `return` inside this `finally` (hence no early return here).
            timeline.mark("object_restored", skipped="object was never deleted")
            record_cleanup(result, True, "nothing to restore: the object was never deleted")
        else:
            try:
                minio_put(frame["bucket"], frame["key"], original, "image/jpeg")
                restored_ok = minio_exists(frame["bucket"], frame["key"])
                again = minio_get(frame["bucket"], frame["key"])
                restored_ok = (restored_ok
                               and hashlib.sha256(again).hexdigest() == original_sha)
                refreshed = pick_evidence_frame(token, lesson)
                restored_ok = (restored_ok
                               and http_status_of(refreshed.get("url") or "") == 200)
            except Exception as exc:  # noqa: BLE001
                restored_ok = False
                detail = (f"RESTORE FAILED for {frame['bucket']}/{frame['key']}: "
                          f"{type(exc).__name__}: {exc}")
            timeline.mark("object_restored", ok=restored_ok)
            record_cleanup(result, restored_ok, detail if not restored_ok else
                           "object re-uploaded with identical sha256 and fetchable again")
    return result


# --------------------------------------------------------------------------- #
# scenario 7: backend restart
# --------------------------------------------------------------------------- #
def scenario_backend_restart(ctx: dict) -> dict:
    """The backend process is replaced; a report reads back byte-identically."""
    result, timeline, token = ctx["result"], ctx["timeline"], ctx["token"]
    require_idle()
    seed_file = RIG_DIR / "seed.json"
    if not seed_file.exists():
        raise ScenarioSkip("seed.json missing; no published report to compare")
    seed = json.loads(seed_file.read_text(encoding="utf-8"))
    lesson = seed.get("lessonId")
    job_id = seed.get("jobId")
    if not lesson:
        raise ScenarioSkip("seed.json has no lessonId")
    before = report_signature(token, lesson)
    job_before = job_row(job_id) if job_id else None
    counts_before = business_counts(job_id, lesson) if job_id else {}
    if before.get("reportHttp") != 200 or before.get("evidenceHttp") != 200:
        raise ScenarioSkip(f"cannot read the seeded report before restart: {before}")
    rig = read_rig_state()
    pid = rig.get("backendPid")
    result["initial"] = {"backendPid": pid, "signature": before,
                         "jobStatus": (job_before or {}).get("status"),
                         "counts": counts_before}

    killed = kill_pid(pid)
    timeline.mark("backend_stopped", **killed)
    down = wait_until(lambda: not tcp_open(BACKEND_PORT), 60, 1)
    result["injected"] = {"killedPid": pid, "kill": killed, "portClosed": bool(down)}

    restarted = None
    try:
        restarted = start_backend()
        ctx["restartedBackendPid"] = restarted.pid
        update_rig_state(backendPid=restarted.pid)
        timeline.mark("backend_started", pid=restarted.pid)
    finally:
        if restarted is None or pid_alive(restarted.pid) is not True:
            record_cleanup(result, False,
                           f"backend did NOT restart; rig.json still points at {pid}")

    if restarted is None or not tcp_open(BACKEND_PORT):
        # No point asserting on a service that is not there: the failed cleanup
        # above already marks the scenario failed and names what is broken.
        add_assertion(result, "backend_restarted", False, "listening on 8000",
                      f"restarted={bool(restarted)}")
        return result

    token = login()
    after = report_signature(token, lesson)
    job_after = job_row(job_id) if job_id else None
    counts_after = business_counts(job_id, lesson) if job_id else {}
    result["recovered"] = {"backendPid": (restarted.pid if restarted else None),
                           "signature": after, "jobStatus": (job_after or {}).get("status"),
                           "counts": counts_after,
                           "rigState": {"backendPid": read_rig_state().get("backendPid")}}
    for key in ("climateScore", "windowCount", "coverage", "version", "status",
                "analysisVideoJobId", "frameCount", "frameIds", "frameWindows"):
        add_assertion(result, f"report_{key}_identical", before.get(key) == after.get(key),
                      before.get(key), after.get(key))
    add_assertion(result, "health_after_restart",
                  http("GET", BASE + "/health").get("status") == 200, 200,
                  http("GET", BASE + "/health").get("status"))
    add_assertion(result, "job_state_unchanged",
                  (job_before or {}).get("status") == (job_after or {}).get("status")
                  and (job_before or {}).get("attempt") == (job_after or {}).get("attempt"),
                  {"status": (job_before or {}).get("status"),
                   "attempt": (job_before or {}).get("attempt")},
                  {"status": (job_after or {}).get("status"),
                   "attempt": (job_after or {}).get("attempt")})
    add_assertion(result, "business_counts_unchanged", counts_before == counts_after,
                  counts_before, counts_after)
    if result["cleanup"]["ok"] is None:
        record_cleanup(result, True, "backend restarted and rig.json updated")
    result["notes"].append(
        "The restart uses the same java -jar invocation and environment as "
        "browser_rig.start_services; only the PID changes, which is written back to "
        "rig.json so a later 'browser_rig stop' still reaches the live process.")
    return result


# --------------------------------------------------------------------------- #
# scenario 8: postgres blip
# --------------------------------------------------------------------------- #
def scenario_postgres_blip(ctx: dict) -> dict:
    """A paused database fails loudly (never silently) and recovers by itself."""
    result, timeline, token = ctx["result"], ctx["timeline"], ctx["token"]
    require_idle()
    seed_file = RIG_DIR / "seed.json"
    lesson = None
    if seed_file.exists():
        try:
            lesson = json.loads(seed_file.read_text(encoding="utf-8")).get("lessonId")
        except (OSError, json.JSONDecodeError):
            lesson = None
    before = report_signature(token, lesson) if lesson else None
    if before and before.get("reportHttp") != 200:
        raise ScenarioSkip(f"cannot read the seeded report before the blip: {before}")
    result["initial"] = {"postgres": container_state(PG_CONTAINER), "signature": before}

    paused = container_action("pause", PG_CONTAINER)
    timeline.mark("postgres_paused", **paused)
    if paused["returncode"] != 0:
        raise ScenarioSkip(f"docker pause failed: {paused['stderr']}")
    probes: list[dict] = []
    try:
        # Bounded on purpose: each probe can block until the client timeout while
        # the database is frozen, and stacking requests would only exhaust the
        # backend's Hikari pool (which is the thing under test, not a stress test).
        for _ in range(3):
            probe = api("/reports", token=token, timeout=60)
            body = probe.get("body") or {}
            entry = {"http": probe.get("status"), "error": probe.get("error"),
                     "code": body.get("code"), "message": body.get("message"),
                     "hasData": isinstance(body.get("data"), list),
                     "ms": probe.get("elapsedMs")}
            entry["healthHttp"] = http("GET", BASE + "/health", timeout=10).get("status")
            probes.append(entry)
            timeline.mark("read_probe", http=entry["http"], ms=entry["ms"])
            if entry["http"] is None or entry["http"] != 200:
                break
            time.sleep(2)
    finally:
        unpaused = container_action("unpause", PG_CONTAINER)
        timeline.mark("postgres_unpaused", **unpaused)
        back = False
        try:
            back = bool(wait_until(lambda: psql_scalar("select 1;") == "1", 120, 2))
        except Exception as exc:  # noqa: BLE001 - never mask the original failure
            timeline.mark("postgres_unpause_probe_error",
                          error=f"{type(exc).__name__}: {exc}")
        record_cleanup(result, back,
                       "postgres answers SQL again" if back
                       else "postgres is NOT answering after unpause")

    result["injected"] = {"probes": probes}
    bad = [probe for probe in probes
           if probe.get("http") is None or (probe.get("http") or 0) >= 500]
    silent = [probe for probe in probes
              if probe.get("http") == 200 and probe.get("hasData") is False]
    add_assertion(result, "db_backed_read_fails_loudly", bool(bad), ">=1 probe at 5xx/timeout",
                  [probe.get("http") for probe in probes])
    add_assertion(result, "no_silent_wrong_answer", not silent, "no empty 200",
                  [probe.get("http") for probe in silent])
    add_assertion(result, "error_is_explicit",
                  all((probe.get("code") or 500) != 200 for probe in bad),
                  "non-200 code in body",
                  [(probe.get("code"), probe.get("message")) for probe in bad][:3])

    if not result["cleanup"]["ok"]:
        return result
    recovered = wait_until(lambda: api("/reports", token=token, timeout=60).get("status") == 200,
                           180, 3)
    after = report_signature(token, lesson) if lesson else None
    result["recovered"] = {"reportsHttpAfterUnpause": bool(recovered), "signature": after}
    add_assertion(result, "read_api_recovers_after_unpause", bool(recovered), 200,
                  "200" if recovered else "not 200")
    if before and after:
        add_assertion(result, "report_unchanged_after_blip",
                      before.get("climateScore") == after.get("climateScore")
                      and before.get("frameIds") == after.get("frameIds"),
                      {"climateScore": before.get("climateScore"),
                       "frames": len(before.get("frameIds") or [])},
                      {"climateScore": after.get("climateScore"),
                       "frames": len(after.get("frameIds") or [])})
    result["notes"].append(
        "docker pause (SIGSTOP) is used instead of stop/start: cheaper and it does not "
        "risk the bind-mounted data directory. /health is a liveness endpoint and may stay "
        "200 while the database is frozen; that is recorded per probe and not treated as "
        "proof of a working read path.")
    return result


SCENARIOS: dict[str, Callable[[dict], dict]] = {
    "redis-outage": scenario_redis_outage,
    "double-consumer": scenario_double_consumer,
    "consumer-kill": scenario_consumer_kill,
    "duplicate-callback": scenario_duplicate_callback,
    "late-stale-attempt": scenario_late_stale_attempt,
    "minio-object-missing": scenario_minio_object_missing,
    "backend-restart": scenario_backend_restart,
    "postgres-blip": scenario_postgres_blip,
}


def run_scenario(name: str, token: str, out_dir: Path) -> dict:
    """Run one scenario; always return a schema-complete result dict."""
    timeline = Timeline()
    result = new_result(name)
    ctx = {"result": result, "timeline": timeline, "token": token, "outDir": out_dir}
    try:
        returned = SCENARIOS[name](ctx)
        if isinstance(returned, dict):
            for key in ("initial", "injected", "recovered", "assertions", "cleanup",
                        "notes"):
                returned.setdefault(key, result.get(key))
            result = returned
    except ScenarioSkip as exc:
        result["skippedReason"] = str(exc)
        timeline.mark("scenario_skipped", reason=str(exc))
        print(f"    SKIP {name}: {exc}", flush=True)
    except GuardError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken drill must still be recorded
        result["error"] = f"{type(exc).__name__}: {exc}"
        timeline.mark("scenario_error", error=result["error"])
        add_assertion(result, "scenario_completed_without_exception", False,
                      "no exception", result["error"])
        print(f"    ERROR {name}: {result['error']}", flush=True)
    result["timeline"] = timeline.events
    result["duration_s"] = timeline.elapsed()
    result["status"] = derive_status(result)
    return result


# --------------------------------------------------------------------------- #
# self-check (read-only)
# --------------------------------------------------------------------------- #
def _dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def jar_is_stale() -> tuple[bool, list[str]]:
    if not JAR.exists():
        return True, ["jar missing"]
    jar_mtime = JAR.stat().st_mtime
    newer = sorted(path.name for path in (ROOT / "backend" / "src" / "main" / "java")
                   .rglob("*.java") if path.stat().st_mtime > jar_mtime)
    return bool(newer), newer


def self_check() -> tuple[bool, dict]:
    """Validate targets/perms/paths/mounts and service reachability. No injection."""
    check = {"id": "self-check", "checks": [], "optional": [], "targets": None,
             "warnings": []}

    def record(name: str, ok: bool, actual: Any, *, optional: bool = False) -> None:
        entry = {"name": name, "ok": bool(ok), "actual": actual,
                 "optional": optional}
        (check["optional"] if optional else check["checks"]).append(entry)
        if optional and not ok:
            check["warnings"].append({"name": name, "actual": actual})
        print(("ok    " if ok else ("warn  " if optional else "FAIL  ")) + name
              + ("" if ok else f"  [{actual}]"))

    try:
        targets = guard_targets()
        check["targets"] = targets
        print_targets(targets)
        record("guard_targets", True, "resolved")
    except GuardError as exc:
        record("guard_targets", False, str(exc))
        return False, check
    except Exception as exc:  # noqa: BLE001
        record("guard_targets", False, f"{type(exc).__name__}: {exc}")
        return False, check

    # Python side dependencies and paths.
    for module in ("psutil", "minio", "requests"):
        try:
            __import__(module)
            record(f"python_import:{module}", True, "present")
        except Exception as exc:  # noqa: BLE001
            record(f"python_import:{module}", module in ("psutil", "minio"), str(exc),
                   optional=module == "requests")
    record("backend_jar_present", JAR.exists(), str(JAR))
    stale, newer = jar_is_stale()
    record("backend_jar_matches_sources", not stale,
           f"{len(newer)} java source(s) newer than the jar: {newer[:6]}", optional=True)
    record("ai_service_sources_present", (AI_SERVICE / "app" / "main.py").exists(),
           str(AI_SERVICE))
    record("checkpoint_present", CHECKPOINT.exists(), str(CHECKPOINT), optional=True)
    record("video_short_present", VIDEO_SHORT.exists(), str(VIDEO_SHORT), optional=True)
    record("video_long_present", VIDEO_LONG.exists(), str(VIDEO_LONG), optional=True)
    record("rig_logs_dir_writable", _dir_writable(LOG_DIR), str(LOG_DIR))
    record("evidence_dir_writable", _dir_writable(EVIDENCE_DIR), str(EVIDENCE_DIR))
    record("second_ai_port_free", not tcp_open(SECOND_AI_PORT), SECOND_AI_PORT)

    # Live services (read-only probes).
    for label, url, expected in (
            ("backend_health", BASE + "/health", (200,)),
            ("backend_reports_requires_auth", BASE + "/reports", (401, 403)),
            ("ai_live", AI_BASE + "/live", (200,)),
            ("ai_ready", AI_BASE + "/ready", (200,))):
        record(label, http("GET", url, timeout=10).get("status") in expected,
               http("GET", url, timeout=10).get("status"))
    record("redis_ping", tcp_open(REDIS_PORT) and redis_cli("PING") == "PONG", REDIS_PORT)
    record("minio_health",
           http("GET", f"http://127.0.0.1:{MINIO_PORT}/minio/health/live",
                timeout=10).get("status") == 200, MINIO_PORT)

    try:
        tables = {row[0] for row in psql("select table_name from information_schema.tables "
                                         "where table_schema='public'")}
        check["tables"] = sorted(tables)
        for table in TABLES:
            record(f"table_present:{table}", table in tables, sorted(tables)[:20])
        record("database_name", psql_scalar("select current_database();") == PG_DB,
               psql_scalar("select current_database();"))
        flyway = psql_scalar("select max(version::int) from flyway_schema_history "
                             "where success")
        # Numeric comparison: "9" >= "33" is False for strings, which would make
        # a database stuck at V9 look ahead of V33 and hide a missing migration.
        record("flyway_at_v33",
               flyway is not None and flyway.isdigit() and int(flyway) >= 33, flyway)
    except Exception as exc:  # noqa: BLE001
        record("database_reachable", False, f"{type(exc).__name__}: {exc}")

    try:
        client = minio_client()
        buckets = [bucket.name for bucket in client.list_buckets()]
        record("minio_bucket_present", MINIO_BUCKET in buckets, buckets)
        objects = list(client.list_objects(MINIO_BUCKET, prefix="lessons/", recursive=False))
        record("evidence_objects_present", bool(objects), len(objects))
    except Exception as exc:  # noqa: BLE001
        record("minio_reachable", False, f"{type(exc).__name__}: {exc}")

    try:
        consumers = redis_consumer_count()
        record("redis_group_present", consumers is not None,
               {"group": REDIS_GROUP, "consumers": consumers})
        record("redis_stream_present", redis_stream_length() is not None,
               redis_stream_length())
    except Exception as exc:  # noqa: BLE001
        record("redis_group_present", False, f"{type(exc).__name__}: {exc}")

    try:
        seed = json.loads((RIG_DIR / "seed.json").read_text(encoding="utf-8"))
        record("seed_lesson_present", bool(seed.get("lessonId")), seed.get("lessonId"),
               optional=True)
    except (OSError, json.JSONDecodeError):
        record("seed_lesson_present", False, "seed.json missing", optional=True)

    busy = foreign_running_jobs()
    record("rig_idle_for_fault_injection", not busy, busy, optional=True)

    ok = all(item["ok"] for item in check["checks"])
    check["ok"] = ok
    check["failed"] = [item["name"] for item in check["checks"] if not item["ok"]]
    check["informational"] = [item["name"] for item in check["optional"] if not item["ok"]]
    return ok, check


# --------------------------------------------------------------------------- #
# evidence + CLI
# --------------------------------------------------------------------------- #
def write_evidence(payload: Mapping[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"fault-drill-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf-8")
    return path


def build_summary(run_id: str, targets: Mapping[str, Any],
                  results: Mapping[str, Mapping[str, Any]], allow_incomplete: bool) -> dict:
    ordered = {name: results[name] for name in results}
    passed = [name for name, item in ordered.items() if item.get("status") == "passed"]
    skipped = [name for name, item in ordered.items() if item.get("status") == "skipped"]
    failed = [name for name, item in ordered.items() if item.get("status") == "failed"]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "runId": run_id,
        "targets": targets,
        "scenarios": ordered,
        "totals": {"scenarios": len(ordered), "passed": len(passed),
                   "skipped": len(skipped), "failed": len(failed),
                   "passedNames": passed, "skippedNames": skipped,
                   "failedNames": failed},
        "allowIncomplete": allow_incomplete,
        # An empty run proves nothing, so it is never "passed".
        "passed": bool(ordered) and not failed and not skipped,
        "notes": NOTES,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fault drills for the UI acceptance rig (mact-ui-*).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", nargs="?", default=None,
                        help="scenario name, or 'all'")
    parser.add_argument("--all", action="store_true", help="run every scenario")
    parser.add_argument("--list", action="store_true", help="list scenario names")
    parser.add_argument("--self-check", action="store_true",
                        help="read-only target/permission validation (no injection)")
    parser.add_argument("--print-targets", action="store_true",
                        help="print the resolved targets and exit")
    parser.add_argument("--out", default="",
                        help="evidence directory (default docs/reviews/evidence/"
                             "2026-10-28-faults)")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="exit 0 even when a scenario was skipped (not recommended)")
    parser.add_argument("--write-self-check", action="store_true",
                        help="also persist the self-check report as evidence")
    args = parser.parse_args(argv)

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    out_dir = Path(args.out) if args.out else EVIDENCE_DIR

    if args.print_targets:
        try:
            targets = guard_targets()
        except GuardError as exc:
            print(f"ABORT: {exc}", file=sys.stderr)
            return 3
        print_targets(targets)
        return 0

    if args.self_check and args.scenario is None and not args.all:
        print("== self-check (read-only; nothing is injected) ==")
        ok, report = self_check()
        if args.write_self_check:
            path = write_evidence({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "selfCheck": report, "passed": ok}, out_dir)
            print(f"\nself-check evidence: {path}")
        print(json.dumps({"ok": ok, "failed": report.get("failed"),
                          "informational": report.get("informational")},
                         ensure_ascii=False))
        return 0 if ok else 1

    try:
        targets = guard_targets()
    except GuardError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return 3
    print_targets(targets)

    if args.all:
        names = list(SCENARIOS)
    elif args.scenario == "all":
        names = list(SCENARIOS)
    elif args.scenario in SCENARIOS:
        names = [args.scenario]
    elif args.scenario is None:
        parser.error("give a scenario name, --all, --list or --self-check")
        return 2
    else:
        parser.error(f"unknown scenario: {args.scenario}")
        return 2

    try:
        token = login()
    except ScenarioSkip as exc:
        # Cannot even authenticate: every scenario would skip for the same reason,
        # so say it once and exit non-zero instead of writing N identical skips.
        print(f"ABORT: {exc}", file=sys.stderr)
        return 4
    run_id = uuid.uuid4().hex[:12]
    results: dict[str, dict] = {}
    for name in names:
        print(f"==> {name}", flush=True)
        result = run_scenario(name, token, out_dir)
        results[name] = result
        print(json.dumps({"scenario": name, "status": result.get("status"),
                          "skipped": result.get("skippedReason"),
                          "error": result.get("error"),
                          "failed": [item["name"] for item in result.get("assertions", [])
                                     if not item.get("ok")]}, ensure_ascii=False),
              flush=True)

    summary = build_summary(run_id, targets, results, args.allow_incomplete)
    path = write_evidence(summary, out_dir)
    print(f"\nevidence written: {path}")
    print(json.dumps({"runId": run_id, "totals": summary["totals"],
                      "passed": summary["passed"]}, ensure_ascii=False, indent=2))

    if summary["totals"]["failed"]:
        return 2
    if summary["totals"]["skipped"]:
        return 0 if args.allow_incomplete else 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
