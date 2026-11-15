#!/usr/bin/env python
"""Bring up an isolated UI-acceptance rig for the classroom report evidence page.

This is *not* a test fixture generator that fakes data: it drives the real
HTTP -> outbox -> Redis -> AI -> MinIO -> PostgreSQL chain and only then opens
the teacher page. Every screenshot taken against it therefore shows real
inference output, real private images and real report versions.

Isolation
---------
* database  : ``macteval_ui`` in the round-2 PostgreSQL container
              (``mact-r2-postgres``, 127.0.0.1:15532) — never the user's DB
* redis     : ``mact-cap-redis`` (127.0.0.1:6380)
* minio     : ``mact-r2-minio`` (127.0.0.1:9100, public endpoint 9100)
* backend   : 127.0.0.1:8000 (the port the Vite proxy expects)
* ai-service: 127.0.0.1:8001
* frontend  : Vite dev server on 127.0.0.1:5173

Refuses to run if any target deviates from that allow-list, and never touches
``personal_design-*`` containers, ports 5432/6379/9000, or the user's services.

Usage
-----
    python scripts/verify/browser_rig.py start            # backend + AI
    python scripts/verify/browser_rig.py seed --video <120min.mp4>
    python scripts/verify/browser_rig.py frontend         # vite dev server
    python scripts/verify/browser_rig.py status
    python scripts/verify/browser_rig.py stop
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RIG_DIR = ROOT / "data" / "tmp" / "ui-rig-2026-10-26"
LOG_DIR = RIG_DIR / "logs"
STATE = RIG_DIR / "rig.json"

#: Dedicated containers for the UI rig. They deliberately do NOT reuse the
#: fault-injection Redis/MinIO (6380/9100) or the round-2 PostgreSQL (15532):
#: those belong to other drills and must not be mutated or contended while this
#: rig is seeding a 120-minute analysis.
#: rig 的容器归入用户的 compose 项目，避免在 Docker Desktop 里变成孤立容器。
#: 与 docker-compose.yml 的 project 名一致（compose 默认取目录名）。
COMPOSE_PROJECT = "personal_design"

PG_CONTAINER = "mact-ui-postgres"
PG_PORT = 15534
PG_DB = "macteval_ui"
PG_USER = "macteval"
PG_PASSWORD = "macteval"
REDIS_CONTAINER = "mact-ui-redis"
REDIS_PORT = 16380
MINIO_CONTAINER = "mact-ui-minio"
MINIO_PORT = 19100
MINIO_CONSOLE_PORT = 19101
MINIO_USER = "uirig"
MINIO_SECRET = "uirig_secret"
BACKEND_PORT = 8000
AI_PORT = 8001
FRONTEND_PORT = 5173
AI_SERVICE = ROOT / "ai-service"
JAR = ROOT / "backend" / "target" / "mact-eval-backend-1.0.0.jar"
CHECKPOINT = ROOT / "docs" / "experiments" / "runs" / "scb5-yolo" / "train" / "weights" / "best.pt"
FACE_WEIGHTS = ROOT / "models" / "yolov8n.pt"

JWT_SECRET = "ui-rig-jwt-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
SERVICE_TOKEN = "ui-rig-service-token-2026-10-26"
BASE = f"http://127.0.0.1:{BACKEND_PORT}/api"

#: Demo teacher created by this rig (password is a local throwaway value).
#
# 可用环境变量覆盖：CI / nightly 里教师账号来自 secrets，与这个本地默认值
# 不同。此前写死导致 **seed 用一个教师、E2E 用另一个**：`/reports/page`
# 按 teacherId 过滤（ReportService.java:108），于是 E2E 看不到刚 seeded 的
# 报告，表现为"没有报告"而不是"账号不一致"。
# 空字符串要回退到默认值：`os.environ.get(k, default)` 只在**变量不存在**时
# 给默认值，写成 `VAR=` 会得到空串。实测（MACT_RIG_TEACHER_USER= 时用户名
# 变成 ）那样登录必然失败，而报错是"账号密码错误"，与根因无关。
TEACHER = {
    "username": os.environ.get("MACT_RIG_TEACHER_USER", "").strip()
                or "13800002026",
    "password": os.environ.get("MACT_RIG_TEACHER_PASS", "").strip()
                or "UiRigPass2026",
    "name": "界面验收教师",
}


# --------------------------------------------------------------------------- #
# isolation guard
# --------------------------------------------------------------------------- #

def assert_isolated() -> dict:
    """Refuse to start anything unless every target is the dedicated rig one."""
    problems = []
    if PG_DB in {"macteval", "postgres", "macteval_capacity", "macteval_round2"}:
        problems.append(f"database {PG_DB!r} is not rig-private")
    if PG_PORT in {5432, 15532, 15533} or REDIS_PORT in {6379, 6380}             or MINIO_PORT in {9000, 9100}:
        problems.append("a port collides with the user's project or another drill")
    if BACKEND_PORT in {18081, 18082} or AI_PORT in {18082, 18083}:
        problems.append("backend/AI ports collide with the fault-injection rig")
    for port in (BACKEND_PORT, AI_PORT, FRONTEND_PORT):
        if not _port_free(port):
            problems.append(f"port {port} is already in use")
    if problems:
        raise SystemExit("refusing to start the UI rig:\n  - " + "\n  - ".join(problems))
    return {"database": PG_DB, "backend": BACKEND_PORT, "ai": AI_PORT,
            "redis": REDIS_PORT, "minio": MINIO_PORT, "frontend": FRONTEND_PORT}


def _port_free(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _docker(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _docker_state(container: str) -> dict | None:
    result = _docker("inspect", "--format", "{{json .State}}", container)
    if result.returncode != 0:
        return None
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return {"running": bool(state.get("Running")), "oomKilled": bool(state.get("OOMKilled"))}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def _request(method: str, path: str, body=None, token: str | None = None,
             timeout: float = 120.0) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            parsed = {"raw": payload.decode("utf-8", "replace")[:400]}
        return exc.code, parsed


def _wait_http(url: str, timeout: float, label: str) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return round(time.monotonic() - started, 2)
        except Exception:  # noqa: BLE001 - not ready yet
            pass
        time.sleep(1)
    raise SystemExit(f"{label} did not become ready within {timeout}s ({url})")


# --------------------------------------------------------------------------- #
# services
# --------------------------------------------------------------------------- #

def backend_env() -> dict:
    env = os.environ.copy()
    env.update({
        "MACT_JWT_SECRET": JWT_SECRET,
        "MACT_SERVICE_TOKEN": SERVICE_TOKEN,
        "MACT_DB_URL": f"jdbc:postgresql://127.0.0.1:{PG_PORT}/{PG_DB}",
        "MACT_DB_USERNAME": PG_USER,
        "MACT_DB_PASSWORD": PG_PASSWORD,
        "SPRING_DATA_REDIS_HOST": "127.0.0.1",
        "SPRING_DATA_REDIS_PORT": str(REDIS_PORT),
        "MACT_MINIO_ENDPOINT": f"http://127.0.0.1:{MINIO_PORT}",
        "MACT_MINIO_PUBLIC_ENDPOINT": f"http://127.0.0.1:{MINIO_PORT}",
        "MACT_MINIO_ACCESS_KEY": MINIO_USER,
        "MACT_MINIO_SECRET_KEY": MINIO_SECRET,
        "MACT_MINIO_ENABLED": "true",
        "MACT_AI_SERVICE_URL": f"http://127.0.0.1:{AI_PORT}",
        "MACT_DEMO_ENABLED": "false",
        "SERVER_PORT": str(BACKEND_PORT),
        # Faster recovery so a UI run does not wait minutes for a requeue.
        "MACT_RECOVERY_INTERVAL_MS": "5000",
        "MACT_OUTBOX_RECONCILE_INTERVAL_MS": "15000",
    })
    return env


def ai_env() -> dict:
    env = os.environ.copy()
    env.update({
        "KMP_DUPLICATE_LIB_OK": "TRUE",
        "MACT_AI_REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
        "MACT_AI_MINIO_ENDPOINT": f"127.0.0.1:{MINIO_PORT}",
        "MACT_AI_MINIO_ACCESS_KEY": MINIO_USER,
        "MACT_AI_MINIO_SECRET_KEY": MINIO_SECRET,
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
        "MACT_AI_FACE_WEIGHTS": str(FACE_WEIGHTS),
        "PYTHONUNBUFFERED": "1",
    })
    return env


def ensure_containers() -> dict:
    """Create + start the rig's own postgres/redis/minio containers (idempotent).

    Uses bind mounts under ``data/tmp/ui-rig-2026-10-26`` so the rig survives a
    container restart and nothing lands in the user's volumes. Refuses to touch
    any container whose name does not start with ``mact-ui-``.
    """
    for name in (PG_CONTAINER, REDIS_CONTAINER, MINIO_CONTAINER):
        if not name.startswith("mact-ui-"):
            raise SystemExit(f"refusing to create non-rig container {name!r}")
    RIG_DIR.mkdir(parents=True, exist_ok=True)
    # Each entry is the full `docker run` argv *after* `docker run -d --name <n>`.
    # Options must precede the image; the container command must follow it.
    mounts = {
        PG_CONTAINER: [
            "-p", f"127.0.0.1:{PG_PORT}:5432",
            "-e", f"POSTGRES_USER={PG_USER}",
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", f"POSTGRES_DB={PG_DB}",
            "-v", f"{RIG_DIR.as_posix()}/pg:/var/lib/postgresql/data",
            "postgres:16-alpine"],

        REDIS_CONTAINER: [
            "-p", f"127.0.0.1:{REDIS_PORT}:6379",
            "-v", f"{RIG_DIR.as_posix()}/redis:/data",
            "redis:7-alpine"],
        MINIO_CONTAINER: [
            "-p", f"127.0.0.1:{MINIO_PORT}:9000",
            "-p", f"127.0.0.1:{MINIO_CONSOLE_PORT}:9001",
            "-e", f"MINIO_ROOT_USER={MINIO_USER}",
            "-e", f"MINIO_ROOT_PASSWORD={MINIO_SECRET}",
            "-e", f"MINIO_SERVER_URL=http://127.0.0.1:{MINIO_PORT}",
            "-v", f"{RIG_DIR.as_posix()}/minio:/data",
            "minio/minio:latest",
            "server", "/data", "--console-address", ":9001"],
    }
    created = []
    for name, args in mounts.items():
        inspect = _docker("inspect", "--format", "{{.State.Running}}", name)
        if inspect.returncode == 0:
            if inspect.stdout.strip() != "true":
                _docker("start", name)
                created.append({"container": name, "action": "started"})
            continue
        env = os.environ.copy()
        env["MSYS_NO_PATHCONV"] = "1"
        # 关键：打上 compose 项目的 label，让 rig 容器在 Docker Desktop 里
        # 归入 personal_design 这一组，而不是散成孤立的容器。
        #
        # 为什么重要（2026-10-30 用户反馈）：`docker run` 直接创建的容器不属
        # 任何 compose 项目，Docker Desktop 会把它们单列；`docker compose down`
        # 清不到它们，用户也看不出它们和项目的关系——于是几千 MB 的镜像与
        # 容器在磁盘上无人认领，直到把 C 盘占满。
        #
        # 加 label 不改变隔离性（仍用独立端口 15534/16380/19100 与独立 bind
        # mount），只改归属显示与可发现性。
        compose_labels = [
            "--label", f"com.docker.compose.project={COMPOSE_PROJECT}",
            "--label", f"com.docker.compose.service={name}",
            "--label", "com.docker.compose.oneoff=False",
            "--label", f"mact.role=ui-acceptance-rig",
        ]
        result = subprocess.run(["docker", "run", "-d", "--name", name,
                                 *compose_labels, *args],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env=env, timeout=300)
        if result.returncode != 0:
            raise SystemExit(f"docker run {name} failed: {result.stderr.strip()}")
        created.append({"container": name, "action": "created", "id": result.stdout.strip()[:12]})

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pg_ok = _docker("exec", PG_CONTAINER, "pg_isready", "-U", PG_USER).returncode == 0
        redis_ok = _docker("exec", REDIS_CONTAINER, "redis-cli", "ping").stdout.strip() == "PONG"
        minio_ok = True
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{MINIO_PORT}/minio/health/live", timeout=3) as response:
                minio_ok = response.status == 200
        except Exception:  # noqa: BLE001 - still starting
            minio_ok = False
        if pg_ok and redis_ok and minio_ok:
            return {"actions": created, "postgres": pg_ok, "redis": redis_ok, "minio": minio_ok}
        time.sleep(2)
    raise SystemExit("rig containers did not become ready within 120s")


def ensure_database() -> str:
    """Create the rig database if missing (idempotent, never drops anything)."""
    exists = _docker("exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", "postgres",
                     "-A", "-t", "-c",
                     f"select 1 from pg_database where datname='{PG_DB}'")
    if exists.returncode != 0:
        raise SystemExit(f"cannot inspect databases: {exists.stderr.strip()}")
    if exists.stdout.strip() == "1":
        return "exists"
    created = _docker("exec", PG_CONTAINER, "createdb", "-U", PG_USER, PG_DB)
    if created.returncode != 0:
        raise SystemExit(f"createdb failed: {created.stderr.strip()}")
    return "created"


def start_services() -> dict:
    targets = assert_isolated()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    containers = ensure_containers()
    if not JAR.exists():
        raise SystemExit(f"backend jar missing: {JAR} (run mvn -f backend/pom.xml package -DskipTests)")
    db_state = ensure_database()

    backend_log = (LOG_DIR / "backend-8000.log").open("a", encoding="utf-8")
    backend = subprocess.Popen(["java", "-jar", str(JAR)], cwd=str(ROOT),
                               env=backend_env(), stdout=backend_log,
                               stderr=subprocess.STDOUT)
    backend_ready = _wait_http(BASE + "/health", 180, "backend")

    ai_log = (LOG_DIR / "ai-8001.log").open("a", encoding="utf-8")
    ai = subprocess.Popen([sys.executable, "-u", "-m", "uvicorn", "app.main:app",
                           "--host", "127.0.0.1", "--port", str(AI_PORT)],
                          cwd=str(AI_SERVICE), env=ai_env(), stdout=ai_log,
                          stderr=subprocess.STDOUT)
    ai_ready = _wait_http(f"http://127.0.0.1:{AI_PORT}/ready", 240, "ai-service")

    state = {
        "targets": targets,
        "database": db_state,
        "containers": containers,
        "backendPid": backend.pid,
        "aiPid": ai.pid,
        "backendReadySeconds": backend_ready,
        "aiReadySeconds": ai_ready,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    RIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def stop_services() -> dict:
    if not STATE.exists():
        return {"stopped": [], "note": "no rig state file"}
    state = json.loads(STATE.read_text(encoding="utf-8"))
    stopped = []
    for key in ("aiPid", "backendPid"):
        pid = state.get(key)
        if not pid:
            continue
        # 记录击停前它是否还活着：对一个已经不存在的 PID 报 "stopped" 是假信息。
        was_alive = _pid_alive(pid)
        try:
            if was_alive:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True)
                else:
                    os.kill(pid, 15)
            stopped.append({"pid": pid, "role": key,
                            "action": "killed" if was_alive else "already-dead"})
        except Exception as exc:  # noqa: BLE001
            stopped.append({"pid": pid, "role": key, "error": str(exc)})
    return {"stopped": stopped}


def remove_containers() -> dict:
    """Stop and delete this rig's own containers.

    The rig creates ``mact-ui-*`` with ``docker run``, so those containers sit
    outside any compose stack and nothing reclaims them. Leaving them behind
    means three permanently-exited containers holding writable layers on the
    system drive, which is exactly what this project must not accumulate.

    Only the three names in the module constants are touched, and the
    ``mact-ui-`` prefix is re-checked here so a typo cannot delete someone
    else's container. Data lives in bind mounts under ``data/tmp`` and
    therefore survives -- rebuilding a seeded 120-minute lesson is expensive.
    """
    allowed = {PG_CONTAINER, REDIS_CONTAINER, MINIO_CONTAINER}
    for name in allowed:
        if not name.startswith("mact-ui-"):
            raise SystemExit(f"refusing to remove non-rig container {name!r}")
    actions = []
    for name in sorted(allowed):
        if _docker("inspect", name).returncode != 0:
            actions.append({"container": name, "action": "absent"})
            continue
        _docker("stop", name)
        removed = _docker("rm", name)
        actions.append({
            "container": name,
            "action": "removed" if removed.returncode == 0 else "remove-failed",
            "error": removed.stderr.strip()[:200] if removed.returncode else "",
        })
    return {"containers": actions}


# --------------------------------------------------------------------------- #
# seeding through the real pipeline
# --------------------------------------------------------------------------- #

def login_or_register() -> str:
    status, body = _request("POST", "/auth/login",
                            {"username": TEACHER["username"], "password": TEACHER["password"]})
    if status == 200:
        return body["data"]["token"]
    status, body = _request("POST", "/auth/register", {**TEACHER, "role": "teacher"})
    if status != 200:
        raise SystemExit(f"register failed: {status} {body}")
    return body["data"]["token"]


#: 服务端单文件上限（与 mact.media.max-bytes / 前端 MAX_FILE_BYTES 一致）。
#: 在这里也检查一次，是为了让**本地种子**在发出请求之前就失败，并给出可执行的
#: 修复建议——否则报错会是一条 init 阶段的 413，读的人只能看到"被拒"，
#: 看不出"该换成 480p 的版本"。
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024

#: 已知可用的 480p 夹具（由 capacity 管线从同一源视频生成）。
#: 源文件 classroom-120min.mp4 是 2.47 GiB，超过 2 GiB 上限；480p 版是 276 MB。
SMALLER_480P = "data/tmp/capacity-2026-10-28/inputs/120min-480p.mp4"


def seed(video: Path, sample_interval: int, min_observations: int) -> dict:
    if not video.exists():
        raise SystemExit(f"video not found: {video}")
    # seed 自己建目录。此前只有 start/ensure_containers 会 mkdir，`seed` 在
    # 结尾写 RIG_DIR/seed.json 时目录可能不存在——实测后果是**一小时的分析
    # 跑完之后**才抛 FileNotFoundError，前面全部白跑。CI 上没有 start 这一步，
    # 所以这不是理论风险。
    RIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    size = video.stat().st_size
    if size > MAX_MEDIA_BYTES:
        raise SystemExit(
            f"video too large for the server-side cap: {size:,} B "
            f"({size / 1024 ** 3:.2f} GiB) > {MAX_MEDIA_BYTES:,} B (2 GiB)\n"
            f"  SEC-03 之后 init 会返回 413，直传再多也进不去。\n"
            f"  用同源的 480p 版本：{SMALLER_480P}\n"
            f"  （若该文件不存在，先跑 scripts/ops/capacity_matrix.py --setup 生成输入，"
            f"或自行 ffmpeg -vf scale=-2:480 转一份。）")
    token = login_or_register()
    steps: list[dict] = []

    status, body = _request("POST", "/courses", {
        "name": "界面验收课程", "code": f"UI{int(time.time()) % 100000}", "department": "验收",
    }, token)
    course_id = body["data"]["id"]
    steps.append({"step": "course", "status": status, "id": course_id})

    status, body = _request("POST", "/sessions", {
        "courseId": course_id, "classId": "class-001", "title": "界面验收课堂（120 分钟）",
    }, token)
    lesson_id = body["data"]["id"]
    steps.append({"step": "session", "status": status, "id": lesson_id})

    status, body = _request("POST", "/upload/media", {
        "mediaType": "video", "fileName": video.name, "lessonSessionId": lesson_id,
        # SEC-03：init 需声明大小与 MIME；confirm 用 statObject 实测值对照。
        "sizeBytes": video.stat().st_size, "contentType": "video/mp4",
    }, token)
    upload = body["data"]
    steps.append({"step": "upload-url", "status": status, "assetId": upload["assetId"]})

    put = subprocess.run(
        ["curl", "-s", "-o", os.devnull, "-w", "%{http_code}", "-X", "PUT",
         "--data-binary", f"@{video}", "-H", "Content-Type: video/mp4", upload["uploadUrl"]],
        capture_output=True, text=True, timeout=3600)
    steps.append({"step": "minio-put", "httpCode": put.stdout.strip()})

    status, body = _request("POST", "/upload/media/confirm", {
        "assetId": upload["assetId"], "objectName": upload["objectName"],
        "lessonSessionId": lesson_id, "totalSeats": 45,
        "sampleInterval": sample_interval, "windowDuration": 60,
        "minObservations": min_observations,
    }, token)
    job_id = body["data"]["jobId"]
    steps.append({"step": "confirm", "status": status, "jobId": job_id})

    started = time.monotonic()
    terminal = None
    while time.monotonic() - started < 3600:
        status, body = _request("GET", f"/jobs/{job_id}", token=token)
        state = body.get("data") or {}
        if state.get("status") in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
            terminal = state
            break
        time.sleep(5)
    if terminal is None:
        raise SystemExit("analysis job did not reach a terminal state within 1h")
    steps.append({
        "step": "analysis",
        "position": terminal.get("status"),
        "seconds": round(time.monotonic() - started, 1),
    })
    if terminal.get("status") != "SUCCEEDED":
        raise SystemExit(f"analysis did not succeed: {terminal.get('status')} "
                         f"{terminal.get('errorMessage')}")

    status, body = _request("POST", f"/reports/generate/{lesson_id}", token=token)
    steps.append({"step": "generate-report", "status": status,
                  "version": (body.get("data") or {}).get("version")})
    status, body = _request("POST", f"/reports/{lesson_id}/publish", token=token)
    steps.append({"step": "publish", "status": status,
                  "version": (body.get("data") or {}).get("version")})

    status, body = _request(
        "GET", f"/reports/{lesson_id}/behavior-evidence?version="
               f"{((body.get('data') or {}).get('version') or 1)}", token=token)
    frames = (body.get("data") or [])
    steps.append({
        "step": "evidence",
        "status": status,
        "frames": len(frames),
        "actionFrames": sum(1 for f in frames
                            if (f.get("metadata") or {}).get("detectionSource") == "behavior_model"),
        "signedUrls": sum(1 for f in frames if f.get("url")),
    })

    result = {
        "targets": json.loads(STATE.read_text(encoding="utf-8"))["targets"] if STATE.exists() else None,
        "teacher": {"username": TEACHER["username"], "password": TEACHER["password"]},
        "lessonId": lesson_id,
        "jobId": job_id,
        "video": str(video),
        "videoBytes": video.stat().st_size,
        "steps": steps,
        "seededAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "notes": [
            "Frames are selected images, not action events or student counts.",
            "Evidence URLs are short-lived presigned links; they are never stored in the report snapshot.",
        ],
    }
    (RIG_DIR / "seed.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return result


def start_frontend() -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (LOG_DIR / "vite-5173.log").open("a", encoding="utf-8")
    # Windows resolves `pnpm` to a .cmd shim, which Popen cannot exec directly.
    pnpm = shutil.which("pnpm") or shutil.which("pnpm.cmd")
    if not pnpm:
        raise SystemExit("pnpm not found on PATH; install it or start Vite manually")
    proc = subprocess.Popen(
        [pnpm, "--filter", "@mact-eval/teacher-web", "exec", "vite",
         "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, shell=False)
    ready = _wait_http(f"http://127.0.0.1:{FRONTEND_PORT}/", 180, "vite")
    return {"frontendPid": proc.pid, "readySeconds": ready}


def _pid_alive(pid: int) -> bool:
    """PID 是否仍存在。用 psutil（若可用）并退回 tasklist/`os.kill(pid, 0)`。"""
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001 - psutil 缺失时退回系统调用
        pass
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}"],
                                 capture_output=True, text=True, timeout=20)
            return str(int(pid)) in (out.stdout or "")
        os.kill(int(pid), 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    out: dict = {"rigState": None, "probes": {}}
    if STATE.exists():
        state = json.loads(STATE.read_text(encoding="utf-8"))
        # rig.json 只在 start 时写入 PID，进程死了它照样声称"健康"。
        # 这里显式标注每个 PID 是否还活着，并给出一个 stale 总标志——
        # 否则读的人会看到 backendPid=13660 与 backend=null 并排，却不知道
        # 哪个是过期信息。
        liveness = {key: _pid_alive(state.get(key) or 0)
                    for key in ("backendPid", "aiPid")}
        state["pidAlive"] = liveness
        state["stale"] = not all(liveness.values())
        out["rigState"] = state
    for name, url in {
        "backend": BASE + "/health",
        "ai_live": f"http://127.0.0.1:{AI_PORT}/live",
        "ai_ready": f"http://127.0.0.1:{AI_PORT}/ready",
        "frontend": f"http://127.0.0.1:{FRONTEND_PORT}/",
    }.items():
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                out["probes"][name] = response.status
        except urllib.error.HTTPError as exc:
            out["probes"][name] = exc.code
        except Exception:  # noqa: BLE001
            out["probes"][name] = None
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    seed_parser = sub.add_parser("seed")
    seed_parser.add_argument("--video", required=True)
    seed_parser.add_argument("--sample-interval", type=int, default=2)
    seed_parser.add_argument("--min-observations", type=int, default=5)
    sub.add_parser("frontend")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("destroy")
    args = parser.parse_args(argv)

    if args.command == "start":
        payload = start_services()
    elif args.command == "seed":
        payload = seed(Path(args.video), args.sample_interval, args.min_observations)
    elif args.command == "frontend":
        payload = start_frontend()
    elif args.command == "status":
        payload = status()
    elif args.command == "destroy":
        payload = {**stop_services(), **remove_containers()}
    else:
        payload = stop_services()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
