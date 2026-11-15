#!/usr/bin/env python
"""Shared helpers for the isolated long-session capacity and fault drills.

The helpers are intentionally small and dependency-light so the measurements
can be reproduced with the same commands recorded in the operations report.
Nothing here touches the user's default Compose project: every container name
used by the callers starts with ``mact-cap-``.
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import psutil
import requests

ROOT = Path(__file__).resolve().parents[2]
ISO_DIR = ROOT / "data" / "tmp" / "capacity-2026-10-26"
PG_CONTAINER = "mact-cap-postgres"
REDIS_CONTAINER = "mact-cap-redis"
MINIO_CONTAINER = "mact-cap-minio"
PG_DB = "macteval_capacity"
JOB_STREAM = "analysis:jobs"
JOB_GROUP = "ai-workers"


class ApiError(RuntimeError):
    def __init__(self, status: int, body: Any, method: str, path: str):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.token: str | None = None

    def call(self, method: str, path: str, body=None, token: str | None = None,
             allow_error: bool = False, timeout: float = 60.0):
        headers = {"Content-Type": "application/json"}
        use_token = token if token is not None else self.token
        if use_token:
            headers["Authorization"] = f"Bearer {use_token}"
        response = self.session.request(
            method, self.base + path, json=body, headers=headers, timeout=timeout)
        try:
            payload = response.json() if response.content else {}
        except json.JSONDecodeError:
            payload = {"raw": response.text[:1000]}
        if response.status_code >= 400 and not allow_error:
            raise ApiError(response.status_code, payload, method, path)
        return response.status_code, payload

    def login_or_register(self, username: str, password: str, name: str = "容量测试教师") -> str:
        status, body = self.call(
            "POST", "/auth/login", {"username": username, "password": password},
            allow_error=True)
        if status == 200:
            self.token = body["data"]["token"]
            return self.token
        status, body = self.call("POST", "/auth/register", {
            "username": username,
            "password": password,
            "name": name,
            "role": "teacher",
        })
        self.token = body["data"]["token"]
        return self.token

    def create_course(self, suffix: str) -> str:
        status, body = self.call("POST", "/courses", {
            "name": f"容量课程-{suffix}",
            "code": f"CAP{suffix[-8:]}",
            "department": "容量测试",
        })
        return body["data"]["id"]

    def create_session(self, course_id: str, title: str, class_id: str = "class-001") -> str:
        status, body = self.call("POST", "/sessions", {
            "courseId": course_id,
            "classId": class_id,
            "title": title,
        })
        return body["data"]["id"]

    def upload_media(self, lesson_id: str, file_path: Path, media_type: str,
                     total_seats: int, sample_interval: int,
                     window_duration: int, min_observations: int,
                     timeout: float = 1800.0) -> dict:
        content_type = "video/mp4" if media_type == "video" else "audio/mp4"
        # SEC-03（2026-10-28）：init 现在必须声明大小与 MIME，服务端据此
        # 在 confirm 用 statObject 的实测值对照；声明值不匹配会被拒绝。
        status, body = self.call("POST", "/upload/media", {
            "mediaType": media_type,
            "fileName": file_path.name,
            "lessonSessionId": lesson_id,
            "sizeBytes": file_path.stat().st_size,
            "contentType": content_type,
        })
        upload = body["data"]
        with file_path.open("rb") as handle:
            put = requests.put(
                upload["uploadUrl"], data=handle, timeout=timeout,
                headers={"Content-Type": content_type})
        if put.status_code not in (200, 201, 204):
            raise RuntimeError(f"MinIO PUT failed: HTTP {put.status_code} {put.text[:500]}")
        status, body = self.call("POST", "/upload/media/confirm", {
            "assetId": upload["assetId"],
            "objectName": upload["objectName"],
            "lessonSessionId": lesson_id,
            "totalSeats": total_seats,
            "sampleInterval": sample_interval,
            "windowDuration": window_duration,
            "minObservations": min_observations,
        })
        return {
            "assetId": upload["assetId"],
            "objectName": upload["objectName"],
            "jobId": body["data"]["jobId"],
            "status": body["data"]["status"],
        }

    def get_job(self, job_id: str) -> dict:
        status, body = self.call("GET", f"/jobs/{job_id}")
        return body["data"]

    def behavior(self, lesson_id: str) -> list:
        status, body = self.call("GET", f"/behavior/{lesson_id}")
        return body["data"]

    def window_features(self, lesson_id: str) -> dict:
        status, body = self.call("GET", f"/window-features/{lesson_id}")
        return body["data"]


def wait_job(api: Api, job_id: str, timeout_s: float, poll_s: float = 2.0) -> dict:
    started = time.monotonic()
    history = []
    terminal = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
    last_status = None
    while time.monotonic() - started < timeout_s:
        job = api.get_job(job_id)
        status = job.get("status")
        now = time.monotonic()
        if status != last_status:
            history.append({"at_s": round(now - started, 3), "status": status,
                            "progress": job.get("progress")})
            last_status = status
        if status in terminal:
            return {"job": job, "history": history,
                    "wait_seconds": round(now - started, 3)}
        time.sleep(poll_s)
    return {"job": api.get_job(job_id), "history": history,
            "wait_seconds": round(time.monotonic() - started, 3), "timeout": True}


def docker_exec(container: str, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)


def psql(sql: str, timeout: float = 60.0) -> list[list[str]]:
    # Windows Docker CLI otherwise emits the container locale's bytes, which
    # corrupts Chinese labels before Python can decode them as UTF-8.
    result = subprocess.run(
        ["docker", "exec", "-e", "PGCLIENTENCODING=UTF8", PG_CONTAINER,
         "psql", "-U", "macteval", "-d", PG_DB,
         "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.strip()}")
    return [line.split("|") for line in result.stdout.strip().splitlines() if line]


def psql_one(sql: str) -> list[str] | None:
    rows = psql(sql)
    return rows[0] if rows else None


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def job_row(job_id: str) -> dict:
    row = psql_one(
        "select status, attempt, "
        "to_char(created_at,'YYYY-MM-DD HH24:MI:SS.MS'), "
        "to_char(started_at,'YYYY-MM-DD HH24:MI:SS.MS'), "
        "to_char(completed_at,'YYYY-MM-DD HH24:MI:SS.MS'), "
        "to_char(heartbeat_at,'YYYY-MM-DD HH24:MI:SS.MS'), "
        "to_char(lease_until,'YYYY-MM-DD HH24:MI:SS.MS'), "
        "coalesce(error_message,''), "
        "coalesce(result_metadata,'{}')::json->>'sampled_frame_count', "
        "coalesce(result_metadata,'{}')::json->>'frame_count', "
        "coalesce(result_metadata,'{}')::json->>'window_count', "
        "coalesce(result_metadata,'{}')::json->>'effective_sample_interval_sec', "
        "coalesce(result_metadata,'{}')::json->>'truncated', "
        "coalesce(result_metadata,'{}')::json->>'model_digest', "
        "coalesce(result_metadata,'{}')::json->>'action_evidence_model' "
        f"from analysis_jobs where id={sql_literal(job_id)}")
    if row is None:
        raise RuntimeError(f"job not found in DB: {job_id}")
    keys = ["status", "attempt", "created_at", "started_at", "completed_at",
            "heartbeat_at", "lease_until", "error_message", "sampled_frame_count",
            "frame_count", "window_count", "effective_sample_interval_sec",
            "truncated", "model_digest", "action_evidence_model"]
    return dict(zip(keys, row))


def count_rows(table: str, where: str) -> int:
    row = psql_one(f"select count(*) from {table} where {where}")
    return int(row[0]) if row else 0


def transcript_metrics(job_id: str) -> dict | None:
    rows = psql(
        "select coalesce(length(text),0), "
        "case when segments is null then 0 else json_array_length(segments::json) end, "
        "coalesce(media_duration_ms,0), coalesce(model_version,''), "
        "coalesce(language,'') "
        f"from lesson_transcripts where job_id={sql_literal(job_id)} limit 1")
    if not rows:
        return None
    chars, segments, duration, model, language = rows[0]
    return {"chars": int(chars), "segments": int(segments),
            "mediaDurationMs": int(duration), "modelVersion": model,
            "language": language}


def speech_window_count(lesson_id: str) -> int:
    row = psql_one(
        "select count(*) from lesson_window_features "
        f"where lesson_session_id={sql_literal(lesson_id)} "
        "and speech_score is not null")
    return int(row[0]) if row else 0


def redis_cli(*args: str, timeout: float = 30.0) -> str:
    result = docker_exec(REDIS_CONTAINER, "redis-cli", *args, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"redis-cli failed: {result.stderr.strip()}")
    return result.stdout.strip()


def redis_info_safe() -> dict:
    try:
        out = redis_cli("info", "persistence")
    except Exception:
        return {}
    info = {}
    for line in out.splitlines():
        if ":" in line and not line.startswith("#"):
            key, value = line.split(":", 1)
            info[key.strip()] = value.strip()
    return info


def stream_length() -> int:
    try:
        return int(redis_cli("xlen", JOB_STREAM))
    except Exception:
        return -1


def pending_summary() -> str:
    try:
        return redis_cli("xpending", JOB_STREAM, JOB_GROUP)
    except Exception as exc:
        return f"unavailable: {exc}"


def pending_callbacks() -> list[str]:
    try:
        out = redis_cli("smembers", "analysis:pending_callbacks")
        return [line for line in out.splitlines() if line]
    except Exception:
        return []


def find_service_pids() -> dict[str, int | None]:
    backend = None
    ai = None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if backend is None and "mact-eval-backend" in cmd and proc.info["name"] in (
                "java.exe", "java"):
            backend = proc.info["pid"]
        if ai is None and "uvicorn" in cmd and "app.main:app" in cmd and "--port" in cmd:
            # Git Bash's nohup.exe wrapper also shows the child command line;
            # prefer the real Python process so RSS/CPU are not sampled from
            # the short-lived wrapper (which reports ~0% and ~6 MB).
            if proc.info["name"] not in ("python.exe", "python"):
                continue
            parts = cmd.split()
            if "18082" in parts:
                ai = proc.info["pid"]
    return {"backend": backend, "ai": ai}


def find_pid_by_port(port: int) -> int | None:
    """Find the real Python/Java process listening on an isolated test port."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if proc.info["name"] not in ("python.exe", "python", "java.exe", "java"):
            continue
        if f"--port {port}" in cmd or f"--port={port}" in cmd:
            return proc.info["pid"]
    return None


def outbox_row(job_id: str) -> dict | None:
    rows = psql(
        "select coalesce(to_char(published_at,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
        "attempt, coalesce(to_char(next_retry_at,'YYYY-MM-DD HH24:MI:SS.MS'),''), "
        "coalesce(last_error,'') "
        f"from task_outbox where aggregate_id={sql_literal(job_id)} "
        "order by created_at desc limit 1")
    if not rows:
        return None
    return dict(zip(["published_at", "attempt", "next_retry_at", "last_error"], rows[0]))


def _nvidia() -> dict:
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        first = result.stdout.strip().splitlines()[0]
        values = [v.strip() for v in first.split(",")]
        def number(value):
            try:
                return float(value)
            except ValueError:
                return None
        return {
            "gpu_util_pct": number(values[0]),
            "gpu_mem_used_mb": number(values[1]),
            "gpu_mem_total_mb": number(values[2]),
            "gpu_temp_c": number(values[3]),
            "gpu_power_w": number(values[4]),
        }
    except Exception:
        return {}


def _docker_stats(names: Iterable[str]) -> list[dict]:
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}", *names],
            capture_output=True, text=True, timeout=30)
        rows = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) != 3:
                continue
            usage = parts[2].split("/")
            used = usage[0].strip().replace("MiB", "").replace("GiB", "")
            used_mb = None
            try:
                used_mb = float(used) * (1024 if "GiB" in usage[0] else 1)
            except ValueError:
                pass
            rows.append({
                "name": parts[0],
                "container_cpu_pct": parts[1].replace("%", ""),
                "container_mem_mb": used_mb,
            })
        return rows
    except Exception:
        return []


class ResourceSampler:
    """Sample host, service-process, GPU and isolated-container resources."""

    def __init__(self, out_dir: Path, interval_s: float = 2.0):
        self.out_dir = out_dir
        self.interval_s = max(0.5, interval_s)
        self.csv_path = out_dir / "resource-samples.csv"
        self.container_path = out_dir / "container-samples.csv"
        self.rows: list[dict] = []
        self.container_rows: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._pids: dict[str, int | None] = {}
        self._backend_proc = None
        self._ai_proc = None
        self._last_containers = 0.0
        self._container_counter = 0
        self.fields = ["elapsed_s", "timestamp", "host_cpu_pct", "host_mem_used_mb",
                       "host_mem_pct", "backend_cpu_pct", "backend_rss_mb",
                       "ai_cpu_pct", "ai_rss_mb", "gpu_util_pct",
                       "gpu_mem_used_mb", "gpu_mem_total_mb", "gpu_temp_c",
                       "gpu_power_w"]

    def _resolve(self):
        self._pids = find_service_pids()
        if self._pids.get("backend"):
            self._backend_proc = psutil.Process(self._pids["backend"])
            self._backend_proc.cpu_percent(None)
        if self._pids.get("ai"):
            self._ai_proc = psutil.Process(self._pids["ai"])
            self._ai_proc.cpu_percent(None)

    def _sample_once(self):
        elapsed = round(time.monotonic() - self._started, 3)
        mem = psutil.virtual_memory()
        row = {
            "elapsed_s": elapsed,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "host_cpu_pct": psutil.cpu_percent(None),
            "host_mem_used_mb": round(mem.used / 1024 / 1024, 1),
            "host_mem_pct": mem.percent,
            "backend_cpu_pct": None,
            "backend_rss_mb": None,
            "ai_cpu_pct": None,
            "ai_rss_mb": None,
        }
        for key, proc in (("backend", self._backend_proc), ("ai", self._ai_proc)):
            if proc is None:
                continue
            try:
                row[f"{key}_cpu_pct"] = round(proc.cpu_percent(None), 2)
                row[f"{key}_rss_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        row.update(_nvidia())
        self.rows.append(row)
        self._container_counter += 1
        if self._container_counter >= 5:
            self._container_counter = 0
            for item in _docker_stats([PG_CONTAINER, REDIS_CONTAINER, MINIO_CONTAINER]):
                item["elapsed_s"] = elapsed
                self.container_rows.append(item)

    def _run(self):
        self._resolve()
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fields)
            writer.writeheader()
            while not self._stop.is_set():
                try:
                    self._sample_once()
                    writer.writerow({key: self.rows[-1].get(key) for key in self.fields})
                    handle.flush()
                except Exception:
                    # Sampling must never abort the measured job.
                    pass
                self._stop.wait(self.interval_s)
        with self.container_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["elapsed_s", "name", "container_cpu_pct",
                                    "container_mem_mb"])
            writer.writeheader()
            writer.writerows(self.container_rows)

    def start(self):
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        return self.summary()

    def summary(self) -> dict:
        def peak(key):
            values = [r[key] for r in self.rows if isinstance(r.get(key), (int, float))]
            return max(values) if values else None
        def mean(key):
            values = [r[key] for r in self.rows if isinstance(r.get(key), (int, float))]
            return round(statistics.mean(values), 2) if values else None
        logical = psutil.cpu_count(logical=True) or 1
        return {
            "samples": len(self.rows),
            "duration_s": round(time.monotonic() - self._started, 2),
            "host_logical_cpus": logical,
            "ai_cpu_peak_host_pct": (round(peak("ai_cpu_pct") / logical, 2)
                                     if peak("ai_cpu_pct") is not None else None),
            "ai_cpu_mean_host_pct": (round(mean("ai_cpu_pct") / logical, 2)
                                     if mean("ai_cpu_pct") is not None else None),
            "backend_cpu_peak_host_pct": (round(peak("backend_cpu_pct") / logical, 2)
                                          if peak("backend_cpu_pct") is not None else None),
            "host_cpu_peak_pct": peak("host_cpu_pct"),
            "host_cpu_mean_pct": mean("host_cpu_pct"),
            "host_mem_peak_mb": peak("host_mem_used_mb"),
            "backend_cpu_peak_pct": peak("backend_cpu_pct"),
            "backend_cpu_mean_pct": mean("backend_cpu_pct"),
            "backend_rss_peak_mb": peak("backend_rss_mb"),
            "ai_cpu_peak_pct": peak("ai_cpu_pct"),
            "ai_cpu_mean_pct": mean("ai_cpu_pct"),
            "ai_rss_peak_mb": peak("ai_rss_mb"),
            "gpu_util_peak_pct": peak("gpu_util_pct"),
            "gpu_mem_peak_mb": peak("gpu_mem_used_mb"),
            "gpu_temp_peak_c": peak("gpu_temp_c"),
            "gpu_power_peak_w": peak("gpu_power_w"),
            "service_pids": self._pids,
        }


def make_chart(sampler: ResourceSampler, out_path: Path, title: str,
               run_mode: str | None = None) -> None:
    """Draw the three-panel resource curve from sampled rows.

    Each panel has two independent y axes, so the two axes are colour-coded:
    the left series keeps a solid line and the left tick labels/spine share its
    colour, the right series uses a dashed line with matching tick colours.
    That prevents a small right-axis value (GPU memory on a card with
    utilisation near zero) from being read as a large percentage on the left.

    ``run_mode`` is only an annotation switch: ``"concurrent"`` states that the
    second consumer's own process memory was not sampled. It never changes any
    plotted value.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = [r for r in sampler.rows if r.get("elapsed_s") is not None]
    if not rows:
        return
    xs = [r["elapsed_s"] for r in rows]

    # Colour-blind-safe pair with a large luminance gap: blue solid (left axis),
    # orange dashed (right axis).
    blue = "#1f77b4"
    orange = "#d95f02"
    host_grey = "#595959"
    backend_colour = "#6a3d9a"
    ai_colour = "#1b9e77"

    fig, axes = plt.subplots(3, 1, figsize=(12, 10.5), sharex=True)

    # ---- panel 1: CPU (multithread sums; may exceed 100%) -------------------
    cpu = axes[0]
    cpu.plot(xs, [r.get("host_cpu_pct") for r in rows],
             color=host_grey, linestyle="-", linewidth=1.4,
             label="host CPU % (system wide)")
    cpu.plot(xs, [r.get("backend_cpu_pct") for r in rows],
             color=backend_colour, linestyle="-", linewidth=1.4,
             label="backend CPU % (all threads; may exceed 100%)")
    cpu.plot(xs, [r.get("ai_cpu_pct") for r in rows],
             color=ai_colour, linestyle="-", linewidth=1.4,
             label="ai-service CPU % (all threads; may exceed 100%)")
    cpu.set_ylabel("CPU % (sum over threads)")
    # Headroom keeps the legend box off the curves without rescaling the data
    # itself (only the displayed axis range changes).
    cpu_peak = max((value for r in rows for value in (
        r.get("host_cpu_pct"), r.get("backend_cpu_pct"), r.get("ai_cpu_pct"))
        if isinstance(value, (int, float))), default=0.0)
    cpu.set_ylim(0, cpu_peak * 1.32 if cpu_peak > 0 else 1)
    cpu.legend(loc="upper left", fontsize=8.5, framealpha=0.9)
    cpu.grid(alpha=0.3)

    # ---- panel 2: memory (host used vs process RSS) -------------------------
    mem = axes[1]
    mem.plot(xs, [r.get("host_mem_used_mb") for r in rows],
             color=host_grey, linestyle="-", alpha=0.55,
             label="host used MB (left axis)")
    mem.set_ylabel("Host used memory MB (left axis)", color=host_grey)
    mem.tick_params(axis="y", colors=host_grey)
    mem.spines["left"].set_color(host_grey)
    mem.grid(alpha=0.3)
    host_peak = max((r.get("host_mem_used_mb") for r in rows
                     if isinstance(r.get("host_mem_used_mb"), (int, float))),
                    default=None)
    host_low = min((r.get("host_mem_used_mb") for r in rows
                    if isinstance(r.get("host_mem_used_mb"), (int, float))),
                   default=None)
    if host_peak:
        # Axis headroom only, so the upper-left legend sits on empty space; the
        # plotted values are untouched.
        span = max(host_peak - host_low, 1.0)
        mem.set_ylim(host_low - span * 0.05, host_peak + span * 0.30)
    mem2 = mem.twinx()
    mem2.plot(xs, [r.get("backend_rss_mb") for r in rows],
              color=backend_colour, linestyle="-", linewidth=1.4,
              label="backend RSS MB (right axis)")
    mem2.plot(xs, [r.get("ai_rss_mb") for r in rows],
              color=ai_colour, linestyle="--", linewidth=1.4,
              label="ai-service RSS MB (right axis)")
    mem2.set_ylabel("Process RSS MB (right axis)", color=backend_colour)
    mem2.tick_params(axis="y", colors=backend_colour)
    mem2.spines["right"].set_color(backend_colour)
    rss_peak = max((value for r in rows for value in (
        r.get("backend_rss_mb"), r.get("ai_rss_mb"))
        if isinstance(value, (int, float))), default=None)
    if rss_peak:
        # Headroom so the upper-right legend never sits on top of a curve.
        mem2.set_ylim(0, rss_peak * 1.55)
    mem.legend(loc="upper left", fontsize=8.5, framealpha=0.9)
    mem2.legend(loc="upper right", fontsize=8.5, framealpha=0.9)

    # ---- panel 3: GPU (utilisation left 0-100, whole-card memory right) -----
    gpu = axes[2]
    gpu.plot(xs, [r.get("gpu_util_pct") for r in rows],
             color=blue, linestyle="-", linewidth=2.0,
             label="GPU util % (0-100, left axis)")
    gpu.set_ylabel("GPU utilisation % (left axis)", color=blue)
    gpu.set_ylim(0, 100)
    gpu.tick_params(axis="y", colors=blue)
    gpu.spines["left"].set_color(blue)
    gpu.grid(alpha=0.3)
    gpu2 = gpu.twinx()
    gpu2.plot(xs, [r.get("gpu_mem_used_mb") for r in rows],
              color=orange, linestyle="--", linewidth=1.8,
              label="GPU memory MB (whole card, right axis)")
    gpu2.set_ylabel("GPU memory MB (whole card, right axis)", color=orange)
    gpu2.tick_params(axis="y", colors=orange)
    gpu2.spines["right"].set_color(orange)
    mem_totals = [r.get("gpu_mem_total_mb") for r in rows
                  if isinstance(r.get("gpu_mem_total_mb"), (int, float))]
    if mem_totals:
        gpu2.set_ylim(0, max(mem_totals) * 1.1)
        gpu2.grid(False)
    gpu.set_xlabel("Elapsed seconds")
    gpu.legend(loc="upper left", fontsize=8.5, framealpha=0.9)
    gpu2.legend(loc="upper right", fontsize=8.5, framealpha=0.9)

    # ---- semantics notes (outside the axes, never over the legends) ---------
    fig.suptitle(title, fontsize=13)
    fig.text(0.005, 0.012,
             "GPU memory is whole-card used memory reported by nvidia-smi "
             "(all processes), not this service's own allocation.",
             fontsize=9, ha="left", va="bottom")
    fig.text(0.005, 0.031,
             "CPU % is the sum across threads (psutil all-thread percent), "
             "so a single service can exceed 100%.",
             fontsize=9, ha="left", va="bottom")
    # Callers that do not pass run_mode still get the right annotation when the
    # run label itself says it was a concurrent drill.
    concurrent = "concurrent" in (run_mode or title or "").lower()
    fig.text(0.005, 0.050,
             ("two consumers ran concurrently: the second consumer's "
              "independent process memory was not sampled"
              if concurrent else "single consumer"),
             fontsize=9.5, ha="left", va="bottom",
             color="#b2182b" if concurrent else "#333333",
             fontweight="bold")

    fig.tight_layout(rect=(0, 0.075, 1, 0.95))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    fig.savefig(out_path.with_suffix(".svg"))
    plt.close(fig)

def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[index]
