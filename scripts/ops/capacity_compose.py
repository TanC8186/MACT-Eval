#!/usr/bin/env python
"""Measure one (or two concurrent) real analysis job(s) inside the CPU Compose stack.

This is the containerised counterpart of ``scripts/ops/long_session_capacity.py``.
The host-native script samples OS processes by PID; here the measured services
are containers, so the sampler asks ``docker stats`` / ``docker inspect`` for CPU,
cgroup memory usage and the cgroup memory limit, and records ``OOMKilled``.

What is reused from :mod:`capacity_common` (not reimplemented):
``Api`` (auth/course/session/upload flow), ``wait_job``, ``write_json``,
``make_chart`` (same three-panel figure and colour semantics) and ``_nvidia``
for whole-card GPU memory. ``capacity_common.psql`` is *not* reused because it
hard-codes the host-native drill's ``mact-cap-postgres`` container name; this
module talks to the Compose project's own PostgreSQL container instead.

Usage:
    docker compose -p mact-cap3 -f docker-compose.yml \\
        -f docker-compose.capacity-cpu.yml --env-file data/tmp/round3-capacity/capacity.env \\
        up -d postgres redis minio backend ai-service

    python scripts/ops/capacity_compose.py \\
        --video data/raw/public/classroom/sav-sample90s.mp4 \\
        --label compose-smoke-90s --sample-interval 2 \\
        --out data/tmp/round3-capacity

    # two consumers (scale ai-service=2) and two concurrent jobs
    python scripts/ops/capacity_compose.py --video <video> --label compose-concurrent \\
        --concurrency 2 --out data/tmp/round3-capacity

Exit code: 0 when every check passes, 2 when a check fails, 1 on a setup error.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity_common import (  # noqa: E402
    Api, _nvidia, make_chart, wait_job, write_json,
)

PROJECT = "mact-cap3"
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "data" / "tmp" / "round3-capacity"

#: Compose v2 default container naming for project ``mact-cap3``.
CONTAINER_NAMES = {
    "postgres": f"{PROJECT}-postgres-1",
    "redis": f"{PROJECT}-redis-1",
    "minio": f"{PROJECT}-minio-1",
    "backend": f"{PROJECT}-backend-1",
    "ai-1": f"{PROJECT}-ai-service-1",
    "ai-2": f"{PROJECT}-ai-service-2",
}

#: Columns of ``resource-samples.csv``; the first block keeps the column names
#: ``capacity_common.make_chart`` reads, so figures stay comparable across runs.
SAMPLE_FIELDS = [
    "elapsed_s", "timestamp", "sample_gap_s",
    "host_cpu_pct", "host_mem_used_mb", "host_mem_pct",
    "ai_cpu_pct", "ai_rss_mb", "ai_mem_limit_mb",
    "backend_cpu_pct", "backend_rss_mb", "backend_mem_limit_mb",
    "gpu_util_pct", "gpu_mem_used_mb", "gpu_mem_total_mb",
]

CONTAINER_FIELDS = [
    "elapsed_s", "name", "role", "container_cpu_pct",
    "container_mem_used_mb", "container_mem_limit_mb", "container_mem_pct",
]


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without Docker)
# --------------------------------------------------------------------------- #
_SIZE_UNITS = {
    "b": 1.0 / (1024 * 1024),
    "kb": 1.0 / 1024, "kib": 1.0 / 1024,
    "mb": 1.0, "mib": 1.0,
    "gb": 1024.0, "gib": 1024.0,
    "tb": 1024.0 * 1024, "tib": 1024.0 * 1024,
}


def parse_size_mb(text: str) -> float | None:
    """Parse ``docker stats`` sizes such as ``5.023MiB`` / ``1.2GiB`` into MiB."""
    raw = str(text or "").strip().replace(",", "")
    if not raw or raw in {"0B", "N/A"}:
        return 0.0 if raw == "0B" else None
    split = len(raw)
    while split > 0 and (raw[split - 1].isalpha() or raw[split - 1] == " "):
        split -= 1
    try:
        number = float(raw[:split].strip())
    except ValueError:
        return None
    unit = raw[split:].strip().lower()
    factor = _SIZE_UNITS.get(unit)
    if factor is None:
        return None
    return round(number * factor, 4)


def _parse_percent(text: str) -> float | None:
    try:
        return float(str(text).strip().replace("%", ""))
    except (TypeError, ValueError):
        return None


def parse_docker_stats_json(text: str) -> dict[str, dict]:
    """Parse ``docker stats --no-stream --format json`` output.

    Returns ``{container name: {cpu_pct, mem_used_mb, mem_limit_mb, mem_pct}}``.
    Malformed lines are skipped instead of aborting the measurement.
    """
    result: dict[str, dict] = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = payload.get("Name") or payload.get("Container") or ""
        if not name:
            continue
        usage = str(payload.get("MemUsage") or "")
        used_text, _, limit_text = usage.partition("/")
        result[name] = {
            "cpu_pct": _parse_percent(payload.get("CPUPerc", "")),
            "mem_used_mb": parse_size_mb(used_text),
            "mem_limit_mb": parse_size_mb(limit_text),
            "mem_pct": _parse_percent(payload.get("MemPerc", "")),
        }
    return result


def parse_container_state(text: str) -> dict:
    """Parse one ``docker inspect --format`` line.

    Format contract: ``OOMKilled|ExitCode|RestartCount|HostConfig.Memory`` where
    the first field is ``true``/``false`` and the last is a byte count.
    """
    parts = str(text or "").strip().split("|")
    if len(parts) != 4:
        return {"oom_killed": None, "exit_code": None,
                "restart_count": None, "memory_limit_bytes": None,
                "raw": text.strip()}

    def _int(value: str) -> int | None:
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return None

    flag = parts[0].strip().lower()
    return {
        "oom_killed": (True if flag == "true" else False if flag == "false" else None),
        "exit_code": _int(parts[1]),
        "restart_count": _int(parts[2]),
        "memory_limit_bytes": _int(parts[3]),
        "raw": text.strip(),
    }


def container_inspect_format() -> str:
    return "{{.State.OOMKilled}}|{{.State.ExitCode}}|{{.RestartCount}}|{{.HostConfig.Memory}}"


CGROUP_PROBE = (
    "printf 'memory.max=%s\\n' \"$(cat /sys/fs/cgroup/memory.max 2>/dev/null)\"; "
    "printf 'memory.peak=%s\\n' \"$(cat /sys/fs/cgroup/memory.peak 2>/dev/null)\"; "
    "printf 'memory.current=%s\\n' \"$(cat /sys/fs/cgroup/memory.current 2>/dev/null)\"; "
    "printf 'cpu.max=%s\\n' \"$(cat /sys/fs/cgroup/cpu.max 2>/dev/null)\"; "
    "printf 'oom_kill=%s\\n' "
    "\"$(grep -h '^oom_kill ' /sys/fs/cgroup/memory.events 2>/dev/null | awk '{print $2}')\"; "
    "printf 'nr_throttled=%s\\n' "
    "\"$(grep -h '^nr_throttled ' /sys/fs/cgroup/cpu.stat 2>/dev/null | awk '{print $2}')\"; "
    "printf 'nr_periods=%s\\n' "
    "\"$(grep -h '^nr_periods ' /sys/fs/cgroup/cpu.stat 2>/dev/null | awk '{print $2}')\""
)


def parse_cgroup_probe(text: str) -> dict:
    """Parse :data:`CGROUP_PROBE` output into numeric cgroup v2 values.

    This is the *inside-the-container* view of the quota: ``memory.max`` is the
    real cgroup limit and ``memory.peak`` is the kernel's own high-water mark,
    which is independent of the ``docker stats`` sampling cadence. Missing or
    ``max``/``n/a`` values become ``None`` rather than a fabricated number.
    """
    result: dict[str, Any] = {}
    for line in str(text or "").splitlines():
        key, _, raw = line.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        if raw in {"", "max", "n/a"}:
            result[key] = None
            continue
        if key == "cpu.max":
            parts = raw.split()
            result["cpu_quota_us"] = int(parts[0]) if parts and parts[0].isdigit() else None
            result["cpu_period_us"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            continue
        try:
            result[key] = int(raw)
        except ValueError:
            result[key] = None
    return result


def read_cgroup(name: str) -> dict:
    """Read cgroup v2 counters from inside a container (best effort)."""
    result = _run(["docker", "exec", name, "sh", "-c", CGROUP_PROBE], 30)
    if result.returncode != 0:
        return {"error": (result.stderr or "").strip()[:300]}
    return parse_cgroup_probe(result.stdout)


def container_started_at(name: str) -> str | None:
    """Container start time (UTC, ISO-8601) or ``None`` if unavailable."""
    result = _run(["docker", "inspect", "--format", "{{.State.StartedAt}}", name], 20)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def api_base(host: str = "127.0.0.1", port: int = 18000,
             context_path: str = "/api") -> str:
    """Build the backend API base URL from host/port (``/api`` context path)."""
    path = "/" + context_path.strip("/") if context_path.strip("/") else ""
    return f"http://{host}:{int(port)}{path}"


def effective_sampling(gaps: Iterable[float]) -> dict:
    """Summarise real sampling gaps; the requested interval is not evidence."""
    values = [round(float(g), 3) for g in gaps if g is not None]
    if not values:
        return {"samples": 0, "requested_interval_s": None,
                "median_gap_s": None, "max_gap_s": None,
                "mean_gap_s": None, "drift_factor": None}
    median = statistics.median(values)
    return {
        "samples": len(values),
        "median_gap_s": round(median, 3),
        "max_gap_s": round(max(values), 3),
        "mean_gap_s": round(statistics.mean(values), 3),
    }


def compute_checks(*, jobs: list[dict], expected_windows: int | None,
                   min_observations: int, container_states: dict[str, dict],
                   sample_peaks: dict[str, float | None],
                   limits_mb: dict[str, float | None],
                   watched_containers: list[str],
                   sampling: dict,
                   cgroup: dict[str, dict] | None = None,
                   media_type: str = "video") -> dict:
    """Evaluate the acceptance checks that go into ``summary.json``.

    ``jobs`` items carry ``status``, ``windows``, ``observations`` and
    ``effective_interval_s``; ``container_states`` holds parsed
    :func:`parse_container_state` results; ``sample_peaks``/``limits_mb`` hold
    per-container memory peaks and cgroup limits in MiB.

    ``min_observations`` is the per-window sampling requirement sent to the
    worker, not a row count: the worker writes one aggregated behaviour
    observation per window, so the assertable invariant is that every window
    produced an observation row.
    """
    oom = {name: container_states.get(name, {}).get("oom_killed")
           for name in watched_containers}
    over_limit = {}
    for name in watched_containers:
        peak = sample_peaks.get(name)
        limit = limits_mb.get(name)
        over_limit[name] = (peak is not None and limit is not None
                            and limit > 0 and peak >= limit)
    coverage_ok = True
    if expected_windows is not None:
        coverage_ok = all(
            (job.get("windows") or 0) >= max(1, expected_windows - 2)
            for job in jobs)
    # One aggregated behaviour observation row per analysed window is the
    # invariant; ``min_observations`` (5) is a per-window frame requirement sent
    # to the worker, never a row-count threshold. Audio jobs write no behaviour
    # observations at all, so the check only applies to video runs.
    observations_cover = bool(jobs) and all(
        (j.get("observations") or 0) >= (j.get("windows") or 0) > 0 for j in jobs)
    if media_type != "video":
        observations_cover = True
    # cgroup v2 view: memory.peak is the kernel high-water mark, so it does not
    # depend on whether a docker stats sample happened to land on the peak.
    cgroup_peak_mb = {}
    cgroup_over_limit = {}
    cgroup_oom_kills = {}
    for name in watched_containers:
        probe = (cgroup or {}).get(name) or {}
        peak = probe.get("memory.peak")
        limit = probe.get("memory.max")
        cgroup_peak_mb[name] = round(peak / 1024 / 1024, 1) if isinstance(peak, int) else None
        cgroup_over_limit[name] = (isinstance(peak, int) and isinstance(limit, int)
                                   and limit > 0 and peak >= limit)
        cgroup_oom_kills[name] = probe.get("oom_kill")
    return {
        "allJobsSucceeded": bool(jobs) and all(j.get("status") == "SUCCEEDED" for j in jobs),
        "allJobsProducedWindows": bool(jobs) and all((j.get("windows") or 0) > 0 for j in jobs),
        "windowCoverage": coverage_ok,
        "observationsCoverEveryWindow": observations_cover,
        "audioTranscriptPresent": (True if media_type == "video"
                                   else all((j.get("transcriptChars") or 0) > 0
                                            for j in jobs)),
        "noOomKilled": bool(watched_containers) and all(v is False for v in oom.values()),
        "memoryWithinLimit": all(v is False for v in over_limit.values()),
        "cgroupMemoryPeakWithinLimit": all(v is False for v in cgroup_over_limit.values()),
        "cgroupOomKillZero": bool(watched_containers) and all(
            v == 0 for v in cgroup_oom_kills.values()),
        "samplingIntervalRecorded": sampling.get("median_gap_s") is not None,
        "details": {
            "oomKilled": oom,
            "peakOverLimit": over_limit,
            "memoryPeakMb": sample_peaks,
            "memoryLimitMb": limits_mb,
            "cgroupMemoryPeakMb": cgroup_peak_mb,
            "cgroupPeakOverLimit": cgroup_over_limit,
            "cgroupOomKills": cgroup_oom_kills,
            "effectiveSampling": sampling,
            "expectedWindowsPerJob": expected_windows,
            "minObservationsPerWindow": min_observations,
        },
    }


# --------------------------------------------------------------------------- #
# Docker-facing helpers
# --------------------------------------------------------------------------- #
def _run(args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def container_exists(name: str) -> bool:
    return _run(["docker", "inspect", "--format", "{{.Id}}", name], 20).returncode == 0


def inspect_container(name: str) -> dict:
    result = _run(["docker", "inspect", "--format", container_inspect_format(), name], 30)
    if result.returncode != 0:
        return {"oom_killed": None, "exit_code": None, "restart_count": None,
                "memory_limit_bytes": None, "raw": result.stderr.strip()}
    return parse_container_state(result.stdout)


def docker_stats(names: list[str]) -> dict[str, dict]:
    if not names:
        return {}
    result = _run(["docker", "stats", "--no-stream", "--format", "json", *names], 60)
    if result.returncode != 0:
        return {}
    return parse_docker_stats_json(result.stdout)


def psql(container: str, sql: str, database: str = "macteval",
         user: str = "macteval", timeout: float = 60.0) -> list[list[str]]:
    result = subprocess.run(
        ["docker", "exec", "-e", "PGCLIENTENCODING=UTF8", container,
         "psql", "-U", user, "-d", database, "-A", "-t", "-F", "|",
         "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.strip()}")
    return [line.split("|") for line in result.stdout.strip().splitlines() if line]


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def job_row(container: str, job_id: str) -> dict:
    rows = psql(container,
                "select status, attempt, "
                "coalesce(result_metadata,'{}')::json->>'sampled_frame_count', "
                "coalesce(result_metadata,'{}')::json->>'window_count', "
                "coalesce(result_metadata,'{}')::json->>'effective_sample_interval_sec', "
                "coalesce(result_metadata,'{}')::json->>'truncated', "
                "coalesce(error_message,'') "
                f"from analysis_jobs where id={_sql_literal(job_id)}")
    if not rows:
        return {}
    keys = ["status", "attempt", "sampled_frame_count", "window_count",
            "effective_sample_interval_sec", "truncated", "error_message"]
    return dict(zip(keys, rows[0]))


def count_rows(container: str, table: str, where: str) -> int:
    rows = psql(container, f"select count(*) from {table} where {where}")
    return int(rows[0][0]) if rows else 0


def transcript_metrics(container: str, job_id: str) -> dict | None:
    """Read the transcript row a job wrote (segments, chars, model version)."""
    rows = psql(container,
                "select coalesce(length(text),0), "
                "case when segments is null then 0 else json_array_length(segments::json) end, "
                "coalesce(media_duration_ms,0), coalesce(model_version,''), "
                "coalesce(language,'') "
                f"from lesson_transcripts where job_id={_sql_literal(job_id)} limit 1")
    if not rows:
        return None
    chars, segments, duration, model, language = rows[0]
    return {"chars": int(chars), "segments": int(segments),
            "mediaDurationMs": int(duration), "modelVersion": model,
            "language": language}


def media_duration(path: Path) -> float | None:
    try:
        result = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", str(path)], 60)
        return round(float(result.stdout.strip()), 3)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Container resource sampler
# --------------------------------------------------------------------------- #
class ContainerResourceSampler:
    """Sample container CPU/cgroup memory plus host CPU/RAM and whole-card GPU.

    ``rows`` uses the same keys ``capacity_common.make_chart`` expects
    (``host_cpu_pct``, ``ai_cpu_pct``, ``ai_rss_mb``, ``backend_*``, ``gpu_*``),
    so the figure is directly comparable with the host-native CUDA run. With two
    ai-service containers ``ai_cpu_pct``/``ai_rss_mb`` are the **sum** of both
    consumers, never one worker standing in for two.
    """

    def __init__(self, out_dir: Path, ai_containers: list[str],
                 backend_container: str = CONTAINER_NAMES["backend"],
                 interval_s: float = 2.0):
        self.out_dir = out_dir
        self.interval_s = max(1.0, float(interval_s))
        self.ai_containers = list(ai_containers)
        self.backend_container = backend_container
        self.rows: list[dict] = []
        self.container_rows: list[dict] = []
        self.gaps: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._last_elapsed: float | None = None
        self._errors: list[str] = []

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> "ContainerResourceSampler":
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        self._write_csv()
        return self.summary()

    # -- sampling ----------------------------------------------------------- #
    def sample_once(self) -> dict:
        started = time.monotonic()
        elapsed = round(started - self._started, 3)
        if self._last_elapsed is not None:
            self.gaps.append(round(elapsed - self._last_elapsed, 3))
        self._last_elapsed = elapsed
        try:
            import psutil
            mem = psutil.virtual_memory()
            row = {
                "elapsed_s": elapsed,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "sample_gap_s": self.gaps[-1] if self.gaps else None,
                "host_cpu_pct": psutil.cpu_percent(None),
                "host_mem_used_mb": round(mem.used / 1024 / 1024, 1),
                "host_mem_pct": mem.percent,
                "ai_cpu_pct": None, "ai_rss_mb": None, "ai_mem_limit_mb": None,
                "backend_cpu_pct": None, "backend_rss_mb": None,
                "backend_mem_limit_mb": None,
            }
        except Exception as exc:  # noqa: BLE001 - sampling must not abort the job
            self._errors.append(f"host sample: {exc}")
            row = {"elapsed_s": elapsed,
                   "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "sample_gap_s": self.gaps[-1] if self.gaps else None}
        stats = docker_stats(self.ai_containers + [self.backend_container])
        ai_cpu = [stats[n]["cpu_pct"] for n in self.ai_containers
                  if stats.get(n, {}).get("cpu_pct") is not None]
        ai_mem = [stats[n]["mem_used_mb"] for n in self.ai_containers
                  if stats.get(n, {}).get("mem_used_mb") is not None]
        ai_limit = [stats[n]["mem_limit_mb"] for n in self.ai_containers
                    if stats.get(n, {}).get("mem_limit_mb") is not None]
        row["ai_cpu_pct"] = round(sum(ai_cpu), 2) if ai_cpu else None
        row["ai_rss_mb"] = round(sum(ai_mem), 1) if ai_mem else None
        row["ai_mem_limit_mb"] = round(sum(ai_limit), 1) if ai_limit else None
        backend = stats.get(self.backend_container) or {}
        row["backend_cpu_pct"] = backend.get("cpu_pct")
        row["backend_rss_mb"] = backend.get("mem_used_mb")
        row["backend_mem_limit_mb"] = backend.get("mem_limit_mb")
        row.update(_nvidia())
        for name, item in stats.items():
            self.container_rows.append({
                "elapsed_s": elapsed,
                "name": name,
                "role": "ai" if name in self.ai_containers else "backend",
                "container_cpu_pct": item.get("cpu_pct"),
                "container_mem_used_mb": item.get("mem_used_mb"),
                "container_mem_limit_mb": item.get("mem_limit_mb"),
                "container_mem_pct": item.get("mem_pct"),
            })
        self.rows.append(row)
        return row

    def _run(self) -> None:
        while not self._stop.is_set():
            tick = time.monotonic()
            try:
                self.sample_once()
            except Exception as exc:  # noqa: BLE001
                self._errors.append(f"sample: {exc}")
            spent = time.monotonic() - tick
            self._stop.wait(max(0.2, self.interval_s - spent))

    def _write_csv(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with (self.out_dir / "resource-samples.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=SAMPLE_FIELDS)
            writer.writeheader()
            for row in self.rows:
                writer.writerow({key: row.get(key) for key in SAMPLE_FIELDS})
        with (self.out_dir / "container-samples.csv").open(
                "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CONTAINER_FIELDS)
            writer.writeheader()
            writer.writerows(self.container_rows)

    # -- summary ------------------------------------------------------------ #
    def peaks(self) -> dict[str, float | None]:
        """Per-container memory peaks (MiB) from the detailed samples."""
        result: dict[str, float | None] = {}
        for name in self.ai_containers + [self.backend_container]:
            values = [row["container_mem_used_mb"] for row in self.container_rows
                      if row["name"] == name
                      and isinstance(row.get("container_mem_used_mb"), (int, float))]
            result[name] = max(values) if values else None
        return result

    def cpu_peaks(self) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        for name in self.ai_containers + [self.backend_container]:
            values = [row["container_cpu_pct"] for row in self.container_rows
                      if row["name"] == name
                      and isinstance(row.get("container_cpu_pct"), (int, float))]
            result[name] = max(values) if values else None
        return result

    def summary(self) -> dict:
        def peak(key):
            values = [r[key] for r in self.rows if isinstance(r.get(key), (int, float))]
            return max(values) if values else None

        def mean(key):
            values = [r[key] for r in self.rows if isinstance(r.get(key), (int, float))]
            return round(statistics.mean(values), 2) if values else None

        return {
            "samples": len(self.rows),
            "containerSamples": len(self.container_rows),
            "duration_s": (round(time.monotonic() - self._started, 2)
                           if self._started else None),
            "requested_interval_s": self.interval_s,
            "effectiveSampling": effective_sampling(self.gaps),
            "host_cpu_peak_pct": peak("host_cpu_pct"),
            "host_cpu_mean_pct": mean("host_cpu_pct"),
            "host_mem_peak_mb": peak("host_mem_used_mb"),
            "ai_container_cpu_peak_pct": peak("ai_cpu_pct"),
            "ai_container_cpu_mean_pct": mean("ai_cpu_pct"),
            "ai_container_mem_peak_mb": peak("ai_rss_mb"),
            "ai_container_mem_limit_mb": peak("ai_mem_limit_mb"),
            "backend_container_cpu_peak_pct": peak("backend_cpu_pct"),
            "backend_container_mem_peak_mb": peak("backend_rss_mb"),
            "backend_container_mem_limit_mb": peak("backend_mem_limit_mb"),
            "per_container_mem_peak_mb": self.peaks(),
            "per_container_cpu_peak_pct": self.cpu_peaks(),
            "gpu_util_peak_pct": peak("gpu_util_pct"),
            "gpu_mem_peak_mb": peak("gpu_mem_used_mb"),
            "ai_containers": self.ai_containers,
            "backend_container": self.backend_container,
            "samplingErrors": self._errors[:20],
            "note": ("CPU percentages are docker stats values: percent of the whole "
                     "host, so a 6-CPU-limited container tops out near 600%. "
                     "With two ai-service containers ai_cpu_pct/ai_rss_mb are sums."),
        }


# --------------------------------------------------------------------------- #
# Job driving
# --------------------------------------------------------------------------- #
def run_jobs(api: Api, media: Path, media_type: str, args, lessons: list[str]) -> list[dict]:
    """Upload and wait for ``len(lessons)`` jobs, concurrently when >1."""
    uploads: list[dict] = [{} for _ in lessons]
    results: list[dict] = [{} for _ in lessons]
    errors: list[str | None] = [None for _ in lessons]

    def worker(index: int) -> None:
        try:
            uploads[index] = api.upload_media(
                lessons[index], media, media_type, args.total_seats,
                args.sample_interval, args.window_duration, args.min_observations)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            errors[index] = f"upload: {exc}"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(lessons))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    def waiter(index: int) -> None:
        job_id = uploads[index].get("jobId")
        if not job_id:
            return
        try:
            results[index] = wait_job(api, job_id, timeout_s=args.timeout, poll_s=2.0)
        except Exception as exc:  # noqa: BLE001
            errors[index] = f"wait: {exc}"

    threads = [threading.Thread(target=waiter, args=(i,)) for i in range(len(lessons))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [{"upload": uploads[i], "wait": results[i], "error": errors[i]}
            for i in range(len(lessons))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, help="real video or audio file")
    parser.add_argument("--label", required=True)
    parser.add_argument("--media-type", choices=["video", "audio"], default="video")
    parser.add_argument("--api-base", default=api_base(),
                        help="default http://127.0.0.1:18000/api")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--sample-interval", type=float, default=2.0,
                        help="docker stats sampling interval in seconds")
    parser.add_argument("--window-duration", type=int, default=60)
    parser.add_argument("--min-observations", type=int, default=5)
    parser.add_argument("--total-seats", type=int, default=30)
    parser.add_argument("--concurrency", type=int, choices=[1, 2], default=1,
                        help="1 job, or 2 jobs on two ai-service consumers")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--skip-action-assertion", action="store_true",
                        help="skip the behaviour-checkpoint assertions (no checkpoint mounted)")
    args = parser.parse_args(argv)

    media = Path(args.video)
    if not media.is_file():
        print(f"media not found: {media}", file=sys.stderr)
        return 1
    out = Path(args.out) / args.label
    out.mkdir(parents=True, exist_ok=True)

    project = args.project
    names = {
        "postgres": f"{project}-postgres-1",
        "backend": f"{project}-backend-1",
        "ai-1": f"{project}-ai-service-1",
        "ai-2": f"{project}-ai-service-2",
    }
    ai_containers = [names["ai-1"]]
    if args.concurrency == 2:
        if not container_exists(names["ai-2"]):
            print(f"{names['ai-2']} is not running; start it with "
                  f"--scale ai-service=2 before using --concurrency 2", file=sys.stderr)
            return 1
        ai_containers.append(names["ai-2"])
    for required in (names["postgres"], names["backend"], *ai_containers):
        if not container_exists(required):
            print(f"required container missing: {required}", file=sys.stderr)
            return 1

    suffix = uuid.uuid4().hex[:8]
    api = Api(args.api_base)
    api.login_or_register(f"compose-capacity-{suffix}", "capacity123")
    course_id = api.create_course(suffix)
    lessons = [
        api.create_session(course_id, f"容器容量课堂-{args.label}-{i + 1}-{suffix}")
        for i in range(args.concurrency)
    ]

    duration = media_duration(media)
    metadata = {
        "label": args.label,
        "runMode": "compose-cpu-int8",
        "project": project,
        "containers": {"backend": names["backend"], "ai": ai_containers},
        "media": str(media),
        "mediaType": args.media_type,
        "mediaBytes": media.stat().st_size,
        "mediaDurationS": duration,
        "sampleInterval": args.sample_interval,
        "windowDuration": args.window_duration,
        "minObservations": args.min_observations,
        "concurrency": args.concurrency,
        "apiBase": args.api_base,
        "lessons": lessons,
        "courseId": course_id,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "device": "cpu",
        "whisper": {"device": "cpu", "computeType": "int8"},
    }

    sampler = ContainerResourceSampler(
        out, ai_containers, backend_container=names["backend"],
        interval_s=args.sample_interval).start()
    try:
        runs = run_jobs(api, media, args.media_type, args, lessons)
    finally:
        resources = sampler.stop()

    job_details = []
    for index, run in enumerate(runs):
        job_id = run["upload"].get("jobId") or ""
        db = job_row(names["postgres"], job_id) if job_id else {}
        lesson_id = lessons[index]
        windows = count_rows(names["postgres"], "lesson_window_features",
                             f"lesson_session_id={_sql_literal(lesson_id)}")
        observations = (count_rows(names["postgres"], "behavior_observations",
                                   f"job_id={_sql_literal(job_id)}") if job_id else 0)
        evidence = (count_rows(names["postgres"], "behavior_evidence_frames",
                               f"job_id={_sql_literal(job_id)}") if job_id else 0)
        transcript = transcript_metrics(names["postgres"], job_id) if job_id else None
        job_details.append({
            "index": index,
            "lessonId": lesson_id,
            "jobId": job_id,
            "status": db.get("status"),
            "attempt": db.get("attempt"),
            "sampledFrames": db.get("sampled_frame_count"),
            "effectiveIntervalS": db.get("effective_sample_interval_sec"),
            "truncated": db.get("truncated"),
            "errorMessage": db.get("error_message"),
            "windows": windows,
            "observations": observations,
            "evidenceFrames": evidence,
            "transcriptChars": (transcript or {}).get("chars"),
            "transcriptSegments": (transcript or {}).get("segments"),
            "transcriptModel": (transcript or {}).get("modelVersion"),
            "elapsedS": run["wait"].get("wait_seconds"),
            "history": run["wait"].get("history"),
            "pollError": run["error"],
        })

    watched = ai_containers + [names["backend"]]
    states = {name: inspect_container(name) for name in watched}
    sample_peaks = sampler.peaks()
    # Kernel-side cgroup counters: memory.peak/oom_kill are authoritative and do
    # not depend on whether a sampler tick landed on the peak.
    # Kernel-side cgroup counters: memory.peak/oom_kill are authoritative and do
    # not depend on whether a sampler tick landed on the peak. memory.peak covers
    # the container's whole lifetime, so the start time is recorded next to it.
    cgroup = {name: read_cgroup(name) for name in watched}
    container_uptime = {name: container_started_at(name) for name in watched}
    for name in watched:
        if isinstance(cgroup.get(name), dict):
            cgroup[name]["containerStartedAt"] = container_uptime.get(name)
            cgroup[name]["peakScope"] = ("whole container lifetime, including any "
                                         "earlier runs since it started")
    limits_mb = {}
    for name in watched:
        state_limit = states[name].get("memory_limit_bytes")
        cgroup_limit = cgroup[name].get("memory.max")
        if state_limit:
            limits_mb[name] = round(state_limit / 1024 / 1024, 1)
        elif isinstance(cgroup_limit, int):
            limits_mb[name] = round(cgroup_limit / 1024 / 1024, 1)
        else:
            reported = [row["container_mem_limit_mb"] for row in sampler.container_rows
                        if row["name"] == name
                        and isinstance(row.get("container_mem_limit_mb"), (int, float))]
            limits_mb[name] = max(reported) if reported else None
    expected_windows = (max(1, int(duration // args.window_duration))
                        if duration and args.media_type == "video" else None)
    sampling = resources.get("effectiveSampling") or {}
    checks = compute_checks(
        jobs=job_details, expected_windows=expected_windows,
        min_observations=args.min_observations, container_states=states,
        sample_peaks=sample_peaks, limits_mb=limits_mb,
        watched_containers=watched, sampling=sampling, cgroup=cgroup,
        media_type=args.media_type)
    if args.skip_action_assertion:
        checks["actionModelAssertionSkipped"] = True

    action_frames = 0
    for job in job_details:
        if job["jobId"]:
            action_frames += count_rows(
                names["postgres"], "behavior_evidence_frames",
                f"job_id={_sql_literal(job['jobId'])} and metadata is not null "
                "and metadata::json->>'detectionSource'='behavior_model'")

    make_chart(sampler, out / "resource-curve.png",
               f"Container capacity ({args.label}, CPU/int8)",
               run_mode="concurrent" if args.concurrency > 1 else "single")
    payload = {
        "metadata": metadata,
        "jobs": job_details,
        "resources": resources,
        "containers": {
            "states": states,
            "memoryPeakMb": sample_peaks,
            "memoryLimitMb": limits_mb,
            "cpuPeakPct": sampler.cpu_peaks(),
            "cgroup": cgroup,
        },
        "counts": {"behaviorModelFrames": action_frames,
                   "expectedWindowsPerJob": expected_windows},
        "checks": checks,
        "notes": [
            "CPU/int8 container timings are not comparable with the host-native CUDA run.",
            "evidenceFrames counts images, not action events or students.",
            "docker stats CPU percent is relative to the whole host, not one core.",
            "The adaptive worker sample cap still applies; effective_sample_interval_sec "
            "in the job result is the authority for inference sampling.",
            ("Figure caption caveat: the shared host-native chart helper prints "
             "'the second consumer's ... memory was not sampled' for concurrent runs. "
             "That limitation belongs to the PID-based drill; here every ai-service "
             "container has its own row in container-samples.csv and the ai-service "
             "series in resource-samples.csv is their sum."
             if args.concurrency > 1 else
             "Figure caption: single consumer; the ai-service series is one container."),
            "cgroup memory.peak covers the container's whole lifetime; see "
            "containers.cgroup[*].containerStartedAt and peakScope.",
        ],
    }
    write_json(out / "summary.json", payload)

    print(json.dumps({
        "label": args.label,
        "concurrency": args.concurrency,
        "jobs": [{k: job[k] for k in ("status", "attempt", "windows", "observations",
                                      "elapsedS", "effectiveIntervalS")}
                 for job in job_details],
        "containerMemPeakMb": sample_peaks,
        "containerMemLimitMb": limits_mb,
        "oomKilled": {name: states[name].get("oom_killed") for name in watched},
        "effectiveSampling": sampling,
        "checks": {k: v for k, v in checks.items() if k != "details"},
    }, ensure_ascii=True, indent=2))
    return 0 if all(v for k, v in checks.items() if k != "details") else 2


if __name__ == "__main__":
    sys.exit(main())
