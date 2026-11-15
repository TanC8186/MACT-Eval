#!/usr/bin/env python
"""容量矩阵（P1-6）：在 CPU/int8 Compose 栈内按声明式矩阵逐行测量。

## 这个脚本补的是什么

`capacity_compose.py` 只能跑"一个（或两个）任务、同一个媒体文件"，且它把证据帧
只记成**张数**、把硬件写成文档里的字面量。本脚本是它的矩阵化补位：

| 缺口 | 本脚本的处理 |
|---|---|
| 只能 1/2 并发 | `ROWS` 里声明 2/4 并发，按行 `--scale ai-service=N` |
| 只有单媒体 | `av-combined` 行在**同一课堂**上同时发 VIDEO_ANALYSIS + AUDIO_ANALYSIS |
| 证据帧只有张数 | 绑定目录 `du` + MinIO 对象清单，行前/行后各取一次，给出**字节增量** |
| 只有 90 s / 40 min | 40 / 80 / 120 min 三点，且是**同一源视频的嵌套前缀** |
| 没有失败率/清理时间/磁盘增长 | 每行都记 `failureRate` / `cleanup` / `disk` 字段 |
| 硬件是字面量 | `measure_hardware()` 现场读 `psutil`/`platform`，读不到就记 `null` |
| 没有 isSla 口径 | 复用 `api_latency_probe.py` 的 `Api`/`summarize`，写 `isSla: false` |

## 复用了什么（不重写）

* `capacity_common.Api`（登录/建课/建课堂/两段式上传/`wait_job`/`write_json`）
* `api_latency_probe.Api` + `summarize`（延迟统计口径与另一份证据完全一致）
* `observability_probe` 的**表名与列名口径**（见其 SQL catalogue 注释）：只沿用
  "从 PostgreSQL 读这几张表"的约定，不 import，因为它的隔离守卫只允许
  `mact-ui-postgres`，而本脚本的容器属于 `mact-capm` 项目。

## 数据落盘（硬约束）

所有容器数据都在 E: 盘：`scripts/ops/capacity_matrix_volumes.yml` 把 postgres /
redis / minio 的命名卷改成绑定挂载，并把 ai-service 的 `~/.cache` 换成 E: 盘上的
离线模型副本（只读）。验证方式见 `--dry-run` 打印的 `config --volumes` 命令：
输出必须为空。

## 用法

    # 只打印计划，不碰 docker、不跑 ffmpeg
    python -I scripts/ops/capacity_matrix.py --dry-run

    # 跑矩阵的一个子集
    python -I scripts/ops/capacity_matrix.py --rows single-40min,av-combined \\
        --out docs/reviews/evidence/2026-10-28-capacity

    # 起栈（可重复执行；每行按需扩缩）
    python -I scripts/ops/capacity_matrix.py --setup
    python -I scripts/ops/capacity_matrix.py --rows smoke-90s --out ...
    python -I scripts/ops/capacity_matrix.py --teardown

退出码：0 = 选中行全部 `passed`；2 = 有 `failed` 或 `not-run`；1 = 环境/前置错误。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_latency_probe import Api as ProbeApi, percentile, summarize  # noqa: E402
from capacity_common import Api, wait_job, write_json  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = "mact-capm"
DATA_ROOT = REPO_ROOT / "data" / "tmp" / "capacity-2026-10-28"
STACK_DIR = DATA_ROOT / PROJECT
INPUT_DIR = DATA_ROOT / "inputs"
DEFAULT_OUT = REPO_ROOT / "docs" / "reviews" / "evidence" / "2026-10-28-capacity"

COMPOSE_BASE = REPO_ROOT / "docker-compose.yml"
COMPOSE_CPU = REPO_ROOT / "docker-compose.capacity-cpu.yml"
COMPOSE_VOLUMES = Path(__file__).resolve().parent / "capacity_matrix_volumes.yml"

#: 媒体来源。40/80/120 三点都从**同一段** 1080p 源转成 480p 后按前缀裁切，
#: 这样三条曲线是同一内容的嵌套前缀，"每媒体分钟成本"才可比较。
SOURCE_120 = DATA_ROOT.parent / "ui-rig-2026-10-27" / "classroom-120min.mp4"

API_PORT = 18000
MINIO_PORT = 19000
MINIO_USER = "macteval"
MINIO_SECRET = "macteval_secret"
MEDIA_BUCKET = "classroom-raw-media"
ARTIFACT_BUCKET = "analysis-artifacts"

#: 只允许把数据写在这两个目录下（写入前校验，避免误写 C:）。
ALLOWED_WRITE_ROOTS = (REPO_ROOT / "data" / "tmp", REPO_ROOT / "docs" / "reviews" / "evidence")


# --------------------------------------------------------------------------- #
# 行声明
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class JobSpec:
    """一个要被提交的分析任务。"""

    key: str
    media_type: str          # video | audio
    input_name: str          # INPUT_DIR 下的文件名
    lesson: str              # 同名的任务共享同一课堂（av-combined 用）


@dataclass(frozen=True)
class Row:
    id: str
    title: str
    jobs: tuple[JobSpec, ...]
    consumers: int
    #: 预计耗时下限（秒）。只用于打印，不参与判定。
    estimate_s: int = 0
    note: str = ""


def build_rows() -> dict[str, Row]:
    rows = [
        Row("smoke-90s", "90 s 冒烟（管道验证）",
            (JobSpec("v", "video", "90s-480p.mp4", "L1"),), consumers=1, estimate_s=60,
            note="只用于证明起栈/上传/落库/磁盘计量全链路可用，不是容量结论"),
        Row("single-40min", "单任务 40 min 视频",
            (JobSpec("v", "video", "40min-480p.mp4", "L1"),), consumers=1, estimate_s=420),
        Row("single-80min", "单任务 80 min 视频",
            (JobSpec("v", "video", "80min-480p.mp4", "L1"),), consumers=1, estimate_s=840),
        Row("single-120min", "单任务 120 min 视频",
            (JobSpec("v", "video", "120min-480p.mp4", "L1"),), consumers=1, estimate_s=1260),
        Row("concurrent-2", "2 并发（2 个消费者，各 40 min 视频）",
            (JobSpec("v1", "video", "40min-480p.mp4", "L1"),
             JobSpec("v2", "video", "40min-480p.mp4", "L2")), consumers=2, estimate_s=600),
        Row("concurrent-4", "4 并发（4 个消费者，各 40 min 视频）",
            (JobSpec("v1", "video", "40min-480p.mp4", "L1"),
             JobSpec("v2", "video", "40min-480p.mp4", "L2"),
             JobSpec("v3", "video", "40min-480p.mp4", "L3"),
             JobSpec("v4", "video", "40min-480p.mp4", "L4")), consumers=4, estimate_s=900),
        Row("av-combined", "视频+音频同课堂（40 min 视频 + 40 min ASR）",
            (JobSpec("v", "video", "40min-480p.mp4", "L1"),
             JobSpec("a", "audio", "40min-audio.wav", "L1")), consumers=2, estimate_s=900,
            note="同一 lesson：VIDEO_ANALYSIS + AUDIO_ANALYSIS，验证两模态在同一课堂叠加"),
    ]
    return {row.id: row for row in rows}


ROWS = build_rows()


# --------------------------------------------------------------------------- #
# 外部命令
# --------------------------------------------------------------------------- #
def run(cmd: list[str], timeout: float = 300.0, env: dict | None = None,
        cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env, cwd=str(cwd or REPO_ROOT))


def compose_env() -> dict:
    """Compose 需要的环境变量；`MACT_CAP_*` 由 override 文件强制要求。"""
    env = dict(os.environ)
    env["MACT_CAP_DATA_ROOT"] = str(STACK_DIR).replace("\\", "/")
    env["MACT_CAP_REPO_ROOT"] = str(REPO_ROOT).replace("\\", "/")
    tmp = REPO_ROOT / ".cache" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = str(tmp)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def compose_cmd(project: str = PROJECT) -> list[str]:
    return ["docker", "compose", "-p", project,
            "-f", str(COMPOSE_BASE), "-f", str(COMPOSE_CPU), "-f", str(COMPOSE_VOLUMES),
            "--env-file", str(STACK_DIR / "capacity.env")]


def compose(*args: str, timeout: float = 900.0) -> subprocess.CompletedProcess:
    return run(compose_cmd() + list(args), timeout=timeout, env=compose_env())


CONTAINERS = {
    "postgres": f"{PROJECT}-postgres-1",
    "redis": f"{PROJECT}-redis-1",
    "minio": f"{PROJECT}-minio-1",
    "backend": f"{PROJECT}-backend-1",
}


def ai_container(index: int) -> str:
    return f"{PROJECT}-ai-service-{index}"


# --------------------------------------------------------------------------- #
# 硬件（现场测量，绝不写字面量）
# --------------------------------------------------------------------------- #
def _powershell_cpu() -> dict:
    """Windows 上补 CPU 型号与物理核数；失败返回空字典，不编造。"""
    if platform.system() != "Windows":
        return {}
    try:
        proc = run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Processor | "
                    "Select-Object Name,NumberOfCores,MaxClockSpeed | ConvertTo-Json -Compress"],
                   timeout=60)
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        payload = json.loads(proc.stdout.strip())
        if isinstance(payload, list):
            payload = payload[0]
        return {
            "cpuModel": (payload.get("Name") or "").strip() or None,
            "physicalCores": payload.get("NumberOfCores"),
            "maxClockMhz": payload.get("MaxClockSpeed"),
        }
    except Exception:  # noqa: BLE001 - 硬件信息缺失记 null，不影响测量
        return {}


def measure_hardware() -> dict:
    """测量硬件。任何一项读不到就写 ``None`` 并记入 ``unavailable``。"""
    info: dict[str, Any] = {"measuredAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "source": "psutil+platform", "unavailable": []}
    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        return {"measuredAt": info["measuredAt"], "source": "psutil missing",
                "error": str(exc), "logicalCpus": None, "totalRamGb": None,
                "cpuModel": None, "physicalCores": None, "unavailable": ["all"]}

    info["logicalCpus"] = psutil.cpu_count(logical=True)
    info["physicalCores"] = psutil.cpu_count(logical=False)
    mem = psutil.virtual_memory()
    info["totalRamGb"] = round(mem.total / 1024 ** 3, 2)
    info["availableRamGb"] = round(mem.available / 1024 ** 3, 2)
    info["ramUsedPct"] = mem.percent
    info["platform"] = f"{platform.system()} {platform.release()} {platform.machine()}"
    info["python"] = platform.python_version()

    extra = _powershell_cpu()
    if extra.get("cpuModel"):
        info["cpuModel"] = extra["cpuModel"]
    else:
        info["cpuModel"] = platform.processor() or None
        if not info["cpuModel"]:
            info["unavailable"].append("cpuModel")
    if extra.get("physicalCores"):
        info["physicalCores"] = extra["physicalCores"]
    if extra.get("maxClockMhz"):
        info["maxClockMhz"] = extra["maxClockMhz"]

    # Docker 侧配额同样现场读：容器能用到多少 CPU/内存由 VM 决定，不是宿主总量。
    try:
        proc = run(["docker", "info", "--format",
                    "{{.NCPU}}|{{.MemTotal}}|{{.DockerRootDir}}"], timeout=60)
        if proc.returncode == 0 and proc.stdout.strip():
            ncpu, _, rest = proc.stdout.strip().partition("|")
            mem_total, _, root = rest.partition("|")
            info["docker"] = {
                "vcpus": int(ncpu) if ncpu.strip().isdigit() else None,
                "memTotalBytes": int(mem_total) if mem_total.strip().isdigit() else None,
                "memTotalGb": (round(int(mem_total) / 1024 ** 3, 2)
                               if mem_total.strip().isdigit() else None),
                "dockerRootDir": root.strip() or None,
                "note": ("DockerRootDir 位于 Docker Desktop VM 内，其磁盘在 C:；"
                         "因此命名卷/可写层数据都算 C: 盘，本脚本一律用绑定挂载。"),
            }
    except Exception:  # noqa: BLE001
        info["unavailable"].append("docker.info")
    return info


def docker_volume_count() -> int | None:
    try:
        proc = run(["docker", "volume", "ls", "-q"], timeout=60)
        if proc.returncode != 0:
            return None
        return len([line for line in proc.stdout.splitlines() if line.strip()])
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 磁盘计量
# --------------------------------------------------------------------------- #
def dir_bytes(path: Path) -> int:
    """递归求和目录字节数。目录不存在返回 0（而不是抛错）。"""
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def dir_file_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    for _root, _dirs, files in os.walk(path):
        count += len(files)
    return count


def minio_usage(bucket: str | None = None) -> dict:
    """用 MinIO Python 客户端统计对象数与字节数（表里没有 size 列，字节只能这样取）。"""
    try:
        from minio import Minio
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"minio client missing: {exc}"}
    try:
        client = Minio(f"127.0.0.1:{MINIO_PORT}", access_key=MINIO_USER,
                       secret_key=MINIO_SECRET, secure=False)
        buckets = [bucket] if bucket else [b.name for b in client.list_buckets()]
        detail: dict[str, dict] = {}
        for name in buckets:
            objects = 0
            size = 0
            for obj in client.list_objects(name, recursive=True):
                objects += 1
                size += int(obj.size or 0)
            detail[name] = {"objects": objects, "bytes": size,
                            "mb": round(size / 1024 / 1024, 2)}
        return {"available": True, "buckets": detail,
                "totalBytes": sum(v["bytes"] for v in detail.values()),
                "totalObjects": sum(v["objects"] for v in detail.values())}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def measure_job_objects(lesson_id: str, job_id: str) -> dict:
    """逐 job 统计 ``analysis-artifacts`` 里的对象：证据帧 vs 持久化结果。

    为什么必须拆：整桶增量里含每 job 2 个结果 JSON（随窗口数与转写文本增长），
    把它们摊到帧数上会让"每帧字节"虚高，而且会让"每帧恒定"这个结论变成
    每任务固定开销的假象。证据帧在 ``lessons/<lesson>/jobs/<job>/``，
    结果在 ``analysis-results/<job>/``，前缀完全可区分，所以按前缀各算一次。
    """
    empty = {"frameBytes": None, "frameObjects": None,
             "resultBytes": None, "resultObjects": None,
             "frameNames": [], "resultNames": []}
    try:
        from minio import Minio
        client = Minio(f"127.0.0.1:{MINIO_PORT}", access_key=MINIO_USER,
                       secret_key=MINIO_SECRET, secure=False)
    except Exception as exc:  # noqa: BLE001
        return {**empty, "error": f"{type(exc).__name__}: {exc}"}
    try:
        frame_bytes = frame_objects = 0
        frame_names = []
        for obj in client.list_objects(ARTIFACT_BUCKET,
                                       prefix=f"lessons/{lesson_id}/jobs/{job_id}/",
                                       recursive=True):
            frame_bytes += int(obj.size or 0)
            frame_objects += 1
            frame_names.append(obj.object_name)
        result_bytes = result_objects = 0
        result_names = []
        for obj in client.list_objects(ARTIFACT_BUCKET,
                                       prefix=f"analysis-results/{job_id}/",
                                       recursive=True):
            result_bytes += int(obj.size or 0)
            result_objects += 1
            result_names.append(obj.object_name)
        return {"frameBytes": frame_bytes, "frameObjects": frame_objects,
                "resultBytes": result_bytes, "resultObjects": result_objects,
                "frameNames": frame_names, "resultNames": result_names}
    except Exception as exc:  # noqa: BLE001
        return {**empty, "error": f"{type(exc).__name__}: {exc}"}


def disk_snapshot() -> dict:
    """一行测量所需的磁盘口径快照：绑定目录 + MinIO 对象 + PG 逻辑大小。"""
    snap: dict[str, Any] = {
        "takenAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bindMounts": {
            "postgres": dir_bytes(STACK_DIR / "pg"),
            "minio": dir_bytes(STACK_DIR / "minio"),
            "redis": dir_bytes(STACK_DIR / "redis"),
            "aiWork": dir_bytes(STACK_DIR / "ai-work"),
            "uploads": dir_bytes(STACK_DIR / "uploads"),
            "backendUploads": dir_bytes(STACK_DIR / "backend-uploads"),
        },
        "minioObjects": minio_usage(),
    }
    snap["bindMountPaths"] = {
        "postgres": str(STACK_DIR / "pg"), "minio": str(STACK_DIR / "minio"),
        "redis": str(STACK_DIR / "redis"), "aiWork": str(STACK_DIR / "ai-work"),
        "uploads": str(STACK_DIR / "uploads"),
        "backendUploads": str(STACK_DIR / "backend-uploads"),
    }
    snap["dbLogical"] = pg_database_size()
    return snap


def disk_delta(before: dict, after: dict) -> dict:
    """两个快照的差值；缺失字段记 ``None`` 而不是 0。"""
    def _d(section: str, key: str):
        a = (before.get(section) or {}).get(key)
        b = (after.get(section) or {}).get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return None
        return b - a

    delta = {f"{name}Bytes": _d("bindMounts", name) for name in
             ("postgres", "minio", "redis", "aiWork", "uploads", "backendUploads")}
    mb = {}
    for key, value in delta.items():
        mb[key.replace("Bytes", "Mb")] = round(value / 1024 / 1024, 2) if value is not None else None
    before_minio = before.get("minioObjects") or {}
    after_minio = after.get("minioObjects") or {}
    delta["minio"] = {
        "bytesDelta": (_diff(after_minio.get("totalBytes"), before_minio.get("totalBytes"))),
        "objectsDelta": (_diff(after_minio.get("totalObjects"), before_minio.get("totalObjects"))),
        "available": bool(after_minio.get("available")),
        "perBucketAfter": after_minio.get("buckets"),
        # 分桶增量：classroom-raw-media 是上传的原始媒体，analysis-artifacts 才含
        # 证据帧 + 结果 JSON。只报总量的"证据帧字节"会把 90 MB 视频算进去，
        # 所以这里必须拆开，否则证据帧的存储成本被高估两个数量级。
        "perBucketDelta": {
            name: {
                "bytesDelta": _diff((after_minio.get("buckets") or {}).get(name, {}).get("bytes"),
                                    (before_minio.get("buckets") or {}).get(name, {}).get("bytes")),
                "objectsDelta": _diff(
                    (after_minio.get("buckets") or {}).get(name, {}).get("objects"),
                    (before_minio.get("buckets") or {}).get(name, {}).get("objects")),
            }
            for name in sorted(set((before_minio.get("buckets") or {}))
                               | set((after_minio.get("buckets") or {})))
        },
    }
    db_before = (before.get("dbLogical") or {}).get("bytes")
    db_after = (after.get("dbLogical") or {}).get("bytes")
    delta["pgDatabase"] = {"bytesBefore": db_before, "bytesAfter": db_after,
                           "bytesDelta": _diff(db_after, db_before)}
    delta["mb"] = mb
    return delta


def _diff(after, before):
    if not isinstance(after, (int, float)) or not isinstance(before, (int, float)):
        return None
    return after - before


# --------------------------------------------------------------------------- #
# PostgreSQL 读取（只读 SELECT）
# --------------------------------------------------------------------------- #
def psql(sql: str, timeout: float = 60.0) -> list[list[str]]:
    proc = run(["docker", "exec", "-e", "PGCLIENTENCODING=UTF8", CONTAINERS["postgres"],
                "psql", "-U", "macteval", "-d", "macteval", "-A", "-t", "-F", "|",
                "-v", "ON_ERROR_STOP=1", "-c", sql], timeout=timeout, env=compose_env())
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr.strip()[:400]}")
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line]


def psql_one(sql: str) -> list[str] | None:
    rows = psql(sql)
    return rows[0] if rows else None


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def pg_database_size() -> dict:
    try:
        row = psql_one("select pg_database_size(current_database()), "
                       "pg_size_pretty(pg_database_size(current_database()))")
        if not row:
            return {}
        return {"bytes": int(row[0]), "pretty": row[1]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def job_times(job_id: str) -> dict:
    """任务时间线：队列等待与 AI 处理时长都从这三列算，不靠客户端计时。

    另外返回 ``startedAt``/``completedAt`` 绝对时间：判断"并发是否真的重叠"必须
    用数据库时间戳，而不是客户端各自记的 wall clock（两个线程的计时器互不知情，
    任务既可能真的并行，也可能被串行处理而看起来同样快）。
    """
    try:
        row = psql_one(
            "select status, attempt, "
            "coalesce(round(extract(epoch from (started_at - created_at))::numeric, 3), -1), "
            "coalesce(round(extract(epoch from (completed_at - started_at))::numeric, 3), -1), "
            "coalesce(round(extract(epoch from (completed_at - created_at))::numeric, 3), -1), "
            "coalesce(error_message, ''), "
            "coalesce(result_metadata,'{}')::json->>'sampled_frame_count', "
            "coalesce(result_metadata,'{}')::json->>'effective_sample_interval_sec', "
            "coalesce(result_metadata,'{}')::json->>'truncated', "
            "coalesce(ingest_state,''), coalesce(alignment_state,''), "
            "coalesce(to_char(started_at,'YYYY-MM-DD\"T\"HH24:MI:SS.MS'), ''), "
            "coalesce(to_char(completed_at,'YYYY-MM-DD\"T\"HH24:MI:SS.MS'), ''), "
            "coalesce(extract(epoch from started_at)::numeric, -1), "
            "coalesce(extract(epoch from completed_at)::numeric, -1) "
            f"from analysis_jobs where id={sql_literal(job_id)}")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}
    if not row:
        return {}
    keys = ["status", "attempt", "queueWaitS", "processingS", "endToEndS",
            "errorMessage", "sampledFrames", "effectiveIntervalS", "truncated",
            "ingestState", "alignmentState", "startedAt", "completedAt",
            "startedEpoch", "completedEpoch"]
    out = dict(zip(keys, row))
    for key in ("queueWaitS", "processingS", "endToEndS"):
        try:
            value = float(out[key])
            out[key] = None if value < 0 else value
        except (TypeError, ValueError):
            out[key] = None
    for key in ("startedEpoch", "completedEpoch"):
        try:
            value = float(out[key])
            out[key] = None if value < 0 else value
        except (TypeError, ValueError):
            out[key] = None
    return out


def _min_or_none(values):
    items = [v for v in values if v]
    return min(items) if items else None


def _max_or_none(values):
    items = [v for v in values if v]
    return max(items) if items else None


def assert_overlap(jobs: list[dict]) -> dict:
    """用数据库时间戳判定并发任务是否**真的**重叠。

    判定规则：设最早 ``startedAt`` 与最早 ``completedAt``；若**每一个**任务的
    ``startedAt`` 都早于最早的 ``completedAt``，则这些任务在时间上必然同时在跑
    （不管它们被几个消费者处理）。否则说明是排队后串行执行，客户端看到的短墙钟
    只是"最后一个任务的时间"，吞吐会被高估。

    返回里同时给出 ``maxConcurrentObserved``：对每个时间点统计区间覆盖数，
    它是"实际同时在跑的任务数"的下界，用来区分 4 个消费者真的各跑一个任务，
    还是 2 个消费者各领了两个。
    """
    timed = [j for j in jobs
             if isinstance(j.get("startedEpoch"), (int, float))
             and isinstance(j.get("completedEpoch"), (int, float))]
    result: dict[str, Any] = {
        "jobsWithTimestamps": len(timed),
        "jobsDeclared": len(jobs),
    }
    if len(timed) < 2:
        result.update(concurrent=False, reason="少于 2 个任务有时间戳，谈不上重叠")
        return result
    first_start = min(j["startedEpoch"] for j in timed)
    first_complete = min(j["completedEpoch"] for j in timed)
    last_start = max(j["startedEpoch"] for j in timed)
    # 文本时间戳只用于展示；缺失时返回 None，不能让 min()/max() 去比较 None。
    result["firstStartedAt"] = _min_or_none(j.get("startedAt") for j in timed)
    result["firstCompletedAt"] = _min_or_none(j.get("completedAt") for j in timed)
    result["lastStartedAt"] = _max_or_none(j.get("startedAt") for j in timed)
    result["firstStartToFirstCompleteS"] = round(first_complete - first_start, 3)
    result["allStartedBeforeFirstCompleted"] = all(
        j["startedEpoch"] < first_complete for j in timed)

    # 区间覆盖扫描：按开始/结束事件排序，统计同时在跑的峰值。
    events = []
    for job in timed:
        events.append((job["startedEpoch"], 1))
        events.append((job["completedEpoch"], -1))
    events.sort(key=lambda item: (item[0], item[1]))  # 同一时刻先结束后开始
    running = 0
    peak = 0
    for _ts, delta in events:
        running += delta
        peak = max(peak, running)
    result["maxConcurrentObserved"] = peak
    # 真正的重叠强度：所有任务处理时长之和 / 墙钟跨度。串行时≈1，全并行时≈并发数。
    span = max(j["completedEpoch"] for j in timed) - first_start
    total = sum(j["processingS"] or 0 for j in timed)
    result["processingLoadFactor"] = round(total / span, 3) if span > 0 else None
    result["concurrencyClaimedVsObserved"] = {
        "declaredJobs": len(timed), "maxConcurrentObserved": peak,
    }
    result["concurrent"] = bool(result["allStartedBeforeFirstCompleted"] and peak >= 2)
    result["reason"] = (
        "所有任务都在最早完成时刻之前启动，且同一时刻至少 2 个任务在跑：并发为真"
        if result["concurrent"] else
        "存在任务在最早完成时刻之后才启动：这些任务是被串行处理的，"
        "墙钟吞吐不能按并发计")
    return result


def count_rows(table: str, where: str) -> int:
    try:
        row = psql_one(f"select count(*) from {table} where {where}")
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------- #
# 容器采样
# --------------------------------------------------------------------------- #
def docker_stats(names: list[str]) -> dict[str, dict]:
    if not names:
        return {}
    proc = run(["docker", "stats", "--no-stream", "--format", "json", *names],
               timeout=90, env=compose_env())
    if proc.returncode != 0:
        return {}
    out: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = payload.get("Name") or ""
        usage = str(payload.get("MemUsage") or "")
        used, _, limit = usage.partition("/")
        out[name] = {
            "cpuPct": _num(str(payload.get("CPUPerc", "")).replace("%", "")),
            "memUsedMb": _size_mb(used),
            "memLimitMb": _size_mb(limit),
            "memPct": _num(str(payload.get("MemPerc", "")).replace("%", "")),
        }
    return out


def _num(text: str) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


_UNITS = {"b": 1 / 1024 / 1024, "kb": 1 / 1024, "kib": 1 / 1024, "mb": 1.0, "mib": 1.0,
          "gb": 1024.0, "gib": 1024.0, "tb": 1024.0 ** 2, "tib": 1024.0 ** 2}


def _size_mb(text: str) -> float | None:
    raw = str(text or "").strip().replace(",", "")
    if not raw:
        return None
    if raw in {"0B", "N/A"}:
        return 0.0 if raw == "0B" else None
    split = len(raw)
    while split > 0 and (raw[split - 1].isalpha() or raw[split - 1] == " "):
        split -= 1
    try:
        number = float(raw[:split].strip())
    except ValueError:
        return None
    factor = _UNITS.get(raw[split:].strip().lower())
    return round(number * factor, 3) if factor else None


def inspect_container(name: str) -> dict:
    proc = run(["docker", "inspect", "--format",
                "{{.State.OOMKilled}}|{{.State.ExitCode}}|{{.RestartCount}}|"
                "{{.HostConfig.Memory}}|{{.State.StartedAt}}", name], timeout=60,
               env=compose_env())
    if proc.returncode != 0:
        return {"available": False, "error": proc.stderr.strip()[:200]}
    parts = proc.stdout.strip().split("|")
    if len(parts) != 5:
        return {"available": False, "error": proc.stdout.strip()[:200]}
    limit = _num(parts[3])
    return {
        "available": True,
        "oomKilled": parts[0].strip().lower() == "true",
        "exitCode": int(float(parts[1])) if parts[1].strip() else None,
        "restartCount": int(float(parts[2])) if parts[2].strip() else None,
        "memoryLimitMb": round(limit / 1024 / 1024, 1) if limit else None,
        "startedAt": parts[4].strip() or None,
    }


CGROUP_PROBE = (
    "printf 'memory.max=%s\\n' \"$(cat /sys/fs/cgroup/memory.max 2>/dev/null)\"; "
    "printf 'memory.peak=%s\\n' \"$(cat /sys/fs/cgroup/memory.peak 2>/dev/null)\"; "
    "printf 'oom_kill=%s\\n' "
    "\"$(grep -h '^oom_kill ' /sys/fs/cgroup/memory.events 2>/dev/null | awk '{print $2}')\"; "
    "printf 'nr_throttled=%s\\n' "
    "\"$(grep -h '^nr_throttled ' /sys/fs/cgroup/cpu.stat 2>/dev/null | awk '{print $2}')\""
)


def cgroup_probe(name: str) -> dict:
    """内核侧 high-water mark：不依赖 docker stats 采样是否正好落在峰值上。"""
    proc = run(["docker", "exec", name, "sh", "-c", CGROUP_PROBE], timeout=60,
               env=compose_env())
    if proc.returncode != 0:
        return {"available": False, "error": proc.stderr.strip()[:200]}
    out: dict[str, Any] = {"available": True}
    for line in proc.stdout.splitlines():
        key, _, raw = line.partition("=")
        raw = raw.strip()
        try:
            out[key.strip()] = int(raw)
        except ValueError:
            out[key.strip()] = None
    peak = out.get("memory.peak")
    out["memoryPeakMb"] = round(peak / 1024 / 1024, 1) if isinstance(peak, int) else None
    return out


class Sampler:
    """在后台按固定间隔采样容器 CPU/RSS 与宿主 CPU/内存。"""

    def __init__(self, names: list[str], interval_s: float = 2.0):
        self.names = list(names)
        self.interval_s = max(1.0, float(interval_s))
        self.rows: list[dict] = []
        self.gaps: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._last: float | None = None
        self.errors: list[str] = []

    def start(self) -> "Sampler":
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=60)
        return self.summary()

    def _loop(self) -> None:
        while not self._stop.is_set():
            tick = time.monotonic()
            try:
                self._sample()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{type(exc).__name__}: {exc}")
            spent = time.monotonic() - tick
            self._stop.wait(max(0.2, self.interval_s - spent))

    def _sample(self) -> None:
        elapsed = round(time.monotonic() - self._started, 3)
        if self._last is not None:
            self.gaps.append(round(elapsed - self._last, 3))
        self._last = elapsed
        row: dict[str, Any] = {"elapsedS": elapsed,
                               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            import psutil
            mem = psutil.virtual_memory()
            row["hostCpuPct"] = psutil.cpu_percent(None)
            row["hostMemUsedMb"] = round(mem.used / 1024 / 1024, 1)
            row["hostMemPct"] = mem.percent
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"host: {exc}")
        stats = docker_stats(self.names)
        ai = [n for n in self.names if n.startswith(f"{PROJECT}-ai-service")]
        ai_cpu = [stats[n]["cpuPct"] for n in ai
                  if stats.get(n, {}).get("cpuPct") is not None]
        ai_mem = [stats[n]["memUsedMb"] for n in ai
                  if stats.get(n, {}).get("memUsedMb") is not None]
        row["aiCpuSumPct"] = round(sum(ai_cpu), 2) if ai_cpu else None
        row["aiMemSumMb"] = round(sum(ai_mem), 1) if ai_mem else None
        row["perContainer"] = {n: stats.get(n) for n in self.names}
        self.rows.append(row)

    def summary(self) -> dict:
        def peak(key):
            values = [r[key] for r in self.rows if isinstance(r.get(key), (int, float))]
            return max(values) if values else None

        def mean(key):
            values = [r[key] for r in self.rows if isinstance(r.get(key), (int, float))]
            return round(statistics.mean(values), 2) if values else None

        per_container = {}
        for name in self.names:
            mem = [r["perContainer"][name]["memUsedMb"] for r in self.rows
                   if r.get("perContainer", {}).get(name, {}).get("memUsedMb") is not None]
            cpu = [r["perContainer"][name]["cpuPct"] for r in self.rows
                   if r.get("perContainer", {}).get(name, {}).get("cpuPct") is not None]
            per_container[name] = {
                "memPeakMb": max(mem) if mem else None,
                "cpuPeakPct": max(cpu) if cpu else None,
            }
        gaps = self.gaps
        return {
            "samples": len(self.rows),
            "requestedIntervalS": self.interval_s,
            "medianGapS": round(statistics.median(gaps), 3) if gaps else None,
            "maxGapS": round(max(gaps), 3) if gaps else None,
            "hostCpuPeakPct": peak("hostCpuPct"),
            "hostMemPeakMb": peak("hostMemUsedMb"),
            "aiCpuSumPeakPct": peak("aiCpuSumPct"),
            "aiMemSumPeakMb": peak("aiMemSumMb"),
            "perContainer": per_container,
            "errors": self.errors[:20],
            "note": ("docker stats 的 CPU% 以整机为基数，6 核限额的容器峰值可接近 600%；"
                     "多消费者时 aiCpuSumPct 是各容器之和。"),
        }


# --------------------------------------------------------------------------- #
# API 延迟探针（行内）与行后报告端点探针
# --------------------------------------------------------------------------- #
class InlineLatency:
    """任务运行期间串行打进 GET 接口，口径与 api_latency_probe 完全一致。

    只打**行前就已存在**的端点：报告详情/证据需要先有一条已生成报告，放在
    行后单独一轮，避免把 404 记成延迟失败。
    """

    ENDPOINTS = {"list": "/reports/page?page=0&size=20", "jobs": "/jobs?limit=50"}

    def __init__(self, base: str, user: str, password: str, interval_s: float = 2.0):
        self.api = ProbeApi(base)
        self.api.login(user, password)
        self.interval_s = max(0.2, float(interval_s))
        self.samples: dict[str, list[dict]] = {k: [] for k in self.ENDPOINTS}
        self.startedAt = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "InlineLatency":
        self.startedAt = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        return self.summary()

    def _loop(self) -> None:
        keys = list(self.ENDPOINTS)
        index = 0
        while not self._stop.is_set():
            key = keys[index % len(keys)]
            index += 1
            try:
                status, _body, ms = self.api.call("GET", self.ENDPOINTS[key])
                self.samples[key].append({"status": status, "ms": ms,
                                          "at": time.strftime("%H:%M:%S")})
            except Exception:  # noqa: BLE001
                self.samples[key].append({"status": 0, "ms": 0.0,
                                          "at": time.strftime("%H:%M:%S")})
            self._stop.wait(self.interval_s)

    def summary(self) -> dict:
        endpoints = {}
        for key, path in self.ENDPOINTS.items():
            endpoints[key] = {"path": path, **summarize(self.samples[key])}
        worst = max((ep["failureRate"] for ep in endpoints.values()), default=0.0)
        return {
            "phase": "during-run",
            "startedAt": self.startedAt,
            "client": "single-threaded urllib (serial, rotating endpoints)",
            "concurrency": 1,
            "samplingIntervalSeconds": self.interval_s,
            # 与 api_latency_probe 同一句话：单机串行请求的数字不是 SLA。
            "isSla": False,
            "endpoints": endpoints,
            "worstFailureRate": worst,
        }


def post_run_report_probe(base: str, user: str, password: str,
                          lesson_id: str, samples: int, interval: float) -> dict:
    """行后对 /reports/{lesson} 与证据端点做固定样本测量（此时报告已生成）。"""
    api = ProbeApi(base)
    api.login(user, password)
    targets = {
        "list": "/reports/page?page=0&size=20",
        "detail": f"/reports/{lesson_id}",
        "evidence": f"/reports/{lesson_id}/behavior-evidence",
    }
    out = {"phase": "after-run", "client": "single-threaded urllib (serial)",
           "concurrency": 1, "samplesPerEndpoint": samples,
           "samplingIntervalSeconds": interval, "isSla": False,
           "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "endpoints": {}}
    for key, path in targets.items():
        collected = []
        for _ in range(samples):
            status, _body, ms = api.call("GET", path)
            collected.append({"status": status, "ms": ms})
            if interval:
                time.sleep(interval)
        out["endpoints"][key] = {"path": path, **summarize(collected)}
    out["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return out


# --------------------------------------------------------------------------- #
# 输入媒体准备
# --------------------------------------------------------------------------- #
def input_path(name: str) -> Path:
    return INPUT_DIR / name


def ffmpeg_plan() -> list[dict]:
    """返回需要生成的输入文件及其 ffmpeg 命令（幂等：已存在则跳过）。"""
    src = SOURCE_120
    full = input_path("120min-480p.mp4")
    plan = [
        {"output": str(full),
         "command": ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                     "-i", str(src), "-vf", "scale=854:480", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "96k",
                     "-movflags", "+faststart", str(full)],
         "reason": "1080p 源转 480p：容器内检测按 480p 解码，与上一轮 40 min 口径一致"},
    ]
    for seconds, name in ((4800, "80min-480p.mp4"), (2400, "40min-480p.mp4"),
                          (90, "90s-480p.mp4")):
        target = input_path(name)
        plan.append({
            "output": str(target),
            "command": ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                        "-i", str(full), "-t", str(seconds), "-c", "copy",
                        "-movflags", "+faststart", str(target)],
            "reason": (f"从同一 480p 源按前缀裁切 {seconds} s：40/80/120 是同一内容的"
                       "嵌套前缀，每媒体分钟成本可比较"),
        })
    wav = input_path("40min-audio.wav")
    plan.append({
        "output": str(wav),
        "command": ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                    "-i", str(input_path("40min-480p.mp4")), "-vn",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        "reason": "ASR 行用 16 kHz 单声道 wav，与上一轮 40 min ASR 的口径一致",
    })
    return plan


def ensure_inputs(dry_run: bool, log: list[str]) -> dict[str, dict]:
    """按计划生成缺失的输入；--dry-run 只报告哪些会生成。"""
    results: dict[str, dict] = {}
    if not SOURCE_120.is_file():
        raise SystemExit(f"源视频不存在：{SOURCE_120}")
    for step in ffmpeg_plan():
        target = Path(step["output"])
        exists = target.is_file() and target.stat().st_size > 0
        results[target.name] = {"path": str(target), "exists": exists,
                                "command": " ".join(step["command"]),
                                "reason": step["reason"]}
        if exists or dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        log.append(f"[inputs] {' '.join(step['command'])}")
        proc = run(step["command"], timeout=3600, env=compose_env())
        if proc.returncode != 0:
            raise SystemExit(f"ffmpeg 失败（{target.name}）：{proc.stderr[-800:]}")
        results[target.name]["bytes"] = target.stat().st_size
    if not dry_run:
        for info in results.values():
            info["bytes"] = Path(info["path"]).stat().st_size if Path(info["path"]).is_file() else 0
            info["durationS"] = probe_duration(Path(info["path"]))
    return results


def probe_duration(path: Path) -> float | None:
    try:
        proc = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", str(path)], timeout=120)
        return round(float(proc.stdout.strip()), 3)
    except Exception:  # noqa: BLE001
        return None


def probe_streams(path: Path) -> list[dict]:
    try:
        proc = run(["ffprobe", "-v", "error", "-show_entries",
                    "stream=index,codec_type,codec_name,width,height", "-of", "json", str(path)],
                   timeout=120)
        return (json.loads(proc.stdout or "{}").get("streams") or [])
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# 栈生命周期
# --------------------------------------------------------------------------- #
def health_wait(timeout_s: float = 300) -> dict:
    """等 backend /api/health 与每个 ai-service /ready 都 200。"""
    import urllib.request

    def get(url: str) -> int:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status
        except Exception:  # noqa: BLE001
            return 0

    deadline = time.monotonic() + timeout_s
    backend = 0
    ai: dict[str, int] = {}
    while time.monotonic() < deadline:
        backend = get(f"http://127.0.0.1:{API_PORT}/api/health")
        ai = {}
        for index in range(1, 7):
            name = ai_container(index)
            port = run(["docker", "port", name, "8001"], timeout=30, env=compose_env())
            if port.returncode != 0 or not port.stdout.strip():
                continue
            host_port = port.stdout.strip().splitlines()[0].rsplit(":", 1)[-1]
            ai[name] = get(f"http://127.0.0.1:{host_port}/ready")
        if backend == 200 and ai and all(v == 200 for v in ai.values()):
            return {"ok": True, "backend": backend, "ai": ai,
                    "waitedS": round(timeout_s - (deadline - time.monotonic()), 1)}
        time.sleep(3)
    return {"ok": False, "backend": backend, "ai": ai, "timeoutS": timeout_s}


def ai_containers_present() -> list[str]:
    proc = run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=60, env=compose_env())
    if proc.returncode != 0:
        return []
    prefix = f"{PROJECT}-ai-service-"
    return sorted(name for name in proc.stdout.split() if name.startswith(prefix))


def stack_up(consumers: int, log: list[str]) -> dict:
    """起栈并把 ai-service 扩到 ``consumers`` 个副本。

    ai-service 用 ``--force-recreate``：cgroup 的 ``memory.peak`` / ``oom_kill`` 是
    **容器生命周期**内的计数，复用上一行的容器会让本行峰值带上上一行的历史。
    重建一次约 10 s（模型在首个任务时加载），换来每一行都是独立口径。
    """
    started = time.monotonic()
    proc = compose("up", "-d", "postgres", "redis", "minio", "ai-service", "backend")
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr[-1500:], "command": "up"}

    # 先删掉超出本行并发度的副本：compose v2 的 `up --scale` 不会主动减少副本。
    removed = []
    for name in ai_containers_present():
        try:
            index = int(name.rsplit("-", 1)[-1])
        except ValueError:
            continue
        if index > consumers:
            run(["docker", "rm", "-f", name], timeout=120, env=compose_env())
            removed.append(name)

    scale = compose("up", "-d", "--no-deps", "--force-recreate",
                    "--scale", f"ai-service={consumers}", "ai-service")
    if scale.returncode != 0:
        return {"ok": False, "error": scale.stderr[-1500:], "command": "scale"}
    health = health_wait()
    return {"ok": health["ok"], "health": health, "removedExtraReplicas": removed,
            "recreatedAiService": True,
            "seconds": round(time.monotonic() - started, 2), "consumers": consumers}


def stack_down(log: list[str]) -> dict:
    """收尾。**不加 -v**：绑定目录由调用方按需清理，卷本身不存在。"""
    started = time.monotonic()
    proc = compose("down", "--remove-orphans", timeout=600)
    return {"ok": proc.returncode == 0, "seconds": round(time.monotonic() - started, 2),
            "stderr": proc.stderr.strip()[-500:] if proc.returncode != 0 else ""}


# --------------------------------------------------------------------------- #
# 单行执行
# --------------------------------------------------------------------------- #
def wait_ai_work_idle(max_wait: float = 60.0) -> dict:
    """等 worker 删掉自己的临时媒体副本（清理时间的可测量部分）。"""
    root = STACK_DIR / "ai-work"
    started = time.monotonic()
    while time.monotonic() - started < max_wait:
        if dir_file_count(root) == 0:
            return {"seconds": round(time.monotonic() - started, 2), "tempFilesLeft": 0,
                    "bytesLeft": 0}
        time.sleep(1.0)
    return {"seconds": round(time.monotonic() - started, 2),
            "tempFilesLeft": dir_file_count(root), "bytesLeft": dir_bytes(root),
            "timedOut": True}


ASR_PREFLIGHT = (
    "from faster_whisper import WhisperModel; "
    "m = WhisperModel('small', device='cpu', compute_type='int8'); "
    "print('asr-model-ok')"
)


def asr_preflight(containers: list[str], timeout: float = 300.0) -> dict:
    """在真跑 ASR 之前先确认离线模型能加载。

    为什么必须显式做：`HF_HUB_OFFLINE=1` 下缓存不完整时，WhisperModel 只在
    **任务执行时**才抛 `LocalEntryNotFoundError`，任务变成 FAILED，而同一行的
    视频任务照样 SUCCEEDED——矩阵会以"半个成功"的形式掩盖 ASR 从未跑过。
    这里在提交前先用同一个解释器加载一次，失败就整行记 failed 并给出原因。
    """
    results = {}
    for name in containers:
        proc = run(["docker", "exec", name, "python", "-c", ASR_PREFLIGHT],
                   timeout=timeout, env=compose_env())
        ok = proc.returncode == 0 and "asr-model-ok" in proc.stdout
        results[name] = {
            "ok": ok,
            "detail": ("离线缓存可用" if ok else
                       (proc.stderr or proc.stdout).strip().splitlines()[-1][:400]
                       if (proc.stderr or proc.stdout).strip() else f"exit {proc.returncode}"),
        }
    return {"ok": bool(results) and all(v["ok"] for v in results.values()),
            "containers": results}



def run_row(row: Row, args, run_id: str) -> dict:
    """执行一行并返回它的完整结果字典。异常一律转成 ``failed`` 而不是抛出。"""
    base_api = f"http://127.0.0.1:{API_PORT}/api"
    out_dir = Path(args.out) / row.id
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "row": row.id, "title": row.title, "runId": run_id,
        "outcome": "not-run", "reason": "",
        "declared": {"jobs": [j.__dict__ for j in row.jobs], "consumers": row.consumers,
                     "estimateS": row.estimate_s, "note": row.note},
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    missing = [j.input_name for j in row.jobs if not input_path(j.input_name).is_file()]
    if missing:
        result.update(outcome="not-run", reason=f"输入缺失：{sorted(set(missing))}")
        write_json(out_dir / "row.json", result)
        return result

    media = {name: {"path": str(input_path(name)), "bytes": input_path(name).stat().st_size,
                    "durationS": probe_duration(input_path(name)),
                    "streams": probe_streams(input_path(name))}
             for name in sorted({j.input_name for j in row.jobs})}
    result["media"] = media
    media_seconds = sum((media[j.input_name]["durationS"] or 0) for j in row.jobs)

    stack = stack_up(row.consumers, [])
    result["stack"] = stack
    if not stack.get("ok"):
        result.update(outcome="failed", reason=f"栈未就绪：{stack}")
        write_json(out_dir / "row.json", result)
        return result

    # ASR 行必须先在容器内验证离线 Whisper 模型可加载：否则失败会晚到任务阶段，
    # 表现为"视频成功、音频失败"，矩阵会以半个成功掩盖 ASR 从未真正跑过。
    if any(job.media_type == "audio" for job in row.jobs):
        preflight = asr_preflight([ai_container(i) for i in range(1, row.consumers + 1)])
        result["asrPreflight"] = preflight
        if not preflight["ok"]:
            result.update(outcome="failed",
                          reason=f"ASR 离线模型预检失败：{preflight['containers']}")
            write_json(out_dir / "row.json", result)
            return result

    suffix = uuid.uuid4().hex[:8]
    api = Api(base_api)
    user = f"capm-{row.id}-{suffix}"
    password = "capacity123"

    try:
        api.login_or_register(user, password)
        course_id = api.create_course(f"matrix-{row.id}-{suffix}")
        lessons = {key: api.create_session(course_id, f"矩阵-{row.id}-{key}-{suffix}")
                   for key in sorted({j.lesson for j in row.jobs})}
        result["lessons"] = lessons
        result["courseId"] = course_id
        result["user"] = user

        watched = [CONTAINERS["backend"], CONTAINERS["postgres"]] + \
                  [ai_container(i) for i in range(1, row.consumers + 1)]

        disk_before = disk_snapshot()
        result["diskBefore"] = disk_before

        sampler = Sampler(watched, interval_s=args.sample_interval).start()
        latency = InlineLatency(base_api, user, password,
                                interval_s=args.api_interval).start()

        wall_start = time.monotonic()
        uploads: dict[str, dict] = {}
        upload_errors: dict[str, str] = {}
        lock = threading.Lock()

        def upload(spec: JobSpec) -> None:
            try:
                info = api.upload_media(
                    lessons[spec.lesson], input_path(spec.input_name), spec.media_type,
                    args.total_seats, args.sample_interval, args.window_duration,
                    args.min_observations)
                with lock:
                    uploads[spec.key] = info
            except Exception as exc:  # noqa: BLE001
                with lock:
                    upload_errors[spec.key] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=upload, args=(spec,)) for spec in row.jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        waits: dict[str, dict] = {}
        wait_errors: dict[str, str] = {}

        def wait(spec: JobSpec) -> None:
            job_id = (uploads.get(spec.key) or {}).get("jobId")
            if not job_id:
                return
            try:
                waits[spec.key] = wait_job(api, job_id, timeout_s=args.timeout, poll_s=5.0)
            except Exception as exc:  # noqa: BLE001
                wait_errors[spec.key] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=wait, args=(spec,)) for spec in row.jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall_seconds = round(time.monotonic() - wall_start, 2)

        latency_result = latency.stop()
        resources = sampler.stop()
        cleanup = wait_ai_work_idle()
        disk_after = disk_snapshot()

        # ---- 任务明细 ------------------------------------------------------
        jobs = []
        for spec in row.jobs:
            job_id = (uploads.get(spec.key) or {}).get("jobId") or ""
            lesson_id = lessons[spec.lesson]
            times = job_times(job_id) if job_id else {}
            jobs.append({
                "key": spec.key, "mediaType": spec.media_type,
                "input": spec.input_name, "lessonId": lesson_id, "jobId": job_id,
                "uploadError": upload_errors.get(spec.key),
                "waitError": wait_errors.get(spec.key),
                "clientWallSeconds": (waits.get(spec.key) or {}).get("wait_seconds"),
                "statusHistory": (waits.get(spec.key) or {}).get("history"),
                **times,
                "windows": count_rows("lesson_window_features",
                                      f"lesson_session_id={sql_literal(lesson_id)}"),
                "observations": (count_rows("behavior_observations",
                                            f"job_id={sql_literal(job_id)}") if job_id else 0),
                "evidenceFrames": (count_rows("behavior_evidence_frames",
                                              f"job_id={sql_literal(job_id)}")
                                   if job_id else 0),
                # 逐 job 的对象计量：证据帧字节与结果 JSON 字节分开，供 §6 用。
                **({"evidenceFrameBytes": obj["frameBytes"],
                    "evidenceFrameObjects": obj["frameObjects"],
                    "resultBytes": obj["resultBytes"],
                    "resultObjects": obj["resultObjects"]}
                   if (obj := measure_job_objects(lesson_id, job_id)).get("frameBytes") is not None
                   else {}),
            })

        # ---- 报告生成（让 detail/evidence 端点有目标） ----------------------
        report = {"generated": False}
        try:
            first_lesson = lessons[row.jobs[0].lesson]
            status, body = api.call("POST", f"/reports/generate/{first_lesson}", {})
            report = {"generated": status == 200, "status": status,
                      "lessonId": first_lesson,
                      "climateScore": ((body.get("data") or {}).get("climateScore")
                                       if isinstance(body, dict) else None)}
        except Exception as exc:  # noqa: BLE001
            report = {"generated": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        result["report"] = report

        latency_after = None
        if report.get("generated") and args.post_run_samples > 0:
            try:
                latency_after = post_run_report_probe(
                    base_api, user, password, report["lessonId"],
                    args.post_run_samples, args.post_run_interval)
            except Exception as exc:  # noqa: BLE001
                latency_after = {"error": f"{type(exc).__name__}: {exc}"[:300]}

        # ---- 失败率与判定 --------------------------------------------------
        finished = [j for j in jobs if j.get("status") in
                    {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}]
        succeeded = [j for j in finished if j.get("status") == "SUCCEEDED"]
        job_failure_rate = (round(1 - len(succeeded) / len(jobs), 4) if jobs else 1.0)
        api_failure_rate = latency_result.get("worstFailureRate")
        if latency_after and not latency_after.get("error"):
            after_worst = max((ep["failureRate"]
                               for ep in latency_after.get("endpoints", {}).values()),
                              default=0.0)
            api_failure_rate = max(api_failure_rate or 0.0, after_worst)

        states = {name: inspect_container(name) for name in watched}
        # 内核侧计数对每个被观测容器都取：backend 也可能 OOM，不能只看 ai-service。
        cgroups = {name: cgroup_probe(name) for name in watched}
        cgroup_oom = {name: probe.get("oom_kill") for name, probe in cgroups.items()}
        # OOM 有两个来源：docker inspect 的 State.OOMKilled（进程最终状态）与
        # cgroup memory.events 的 oom_kill 累计（容器生命周期内）。任一非零都算发生过。
        oom_any = any(state.get("oomKilled") for state in states.values()) or any(
            isinstance(count, int) and count > 0 for count in cgroup_oom.values())

        wall_minutes = wall_seconds / 60.0 if wall_seconds else 0.0
        throughput = (round((media_seconds / 60.0) / wall_minutes, 4)
                      if wall_minutes > 0 else None)

        # 并发行必须用数据库时间戳证明"真的同时在跑"，否则墙钟吞吐会按并发数虚高。
        overlap = assert_overlap(jobs) if len(jobs) > 1 else {
            "concurrent": None, "reason": "单任务行，不涉及并发"}
        bg = compute_throughput_basis(media_seconds, jobs, wall_seconds)

        result.update({
            "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "wallSeconds": wall_seconds,
            "mediaSeconds": media_seconds,
            "throughputMediaMinPerWallMin": throughput,
            "throughputBasis": bg,
            "overlap": overlap,
            "jobs": jobs,
            "resources": resources,
            "latencyDuringRun": latency_result,
            "latencyAfterRun": latency_after,
            "containerStates": states,
            "cgroup": cgroups,
            "cgroupOomKill": cgroup_oom,
            "oomKilled": oom_any,
            "cleanup": cleanup,
            "diskAfter": disk_after,
            "diskDelta": disk_delta(disk_before, disk_after),
            "failureRate": {"jobs": job_failure_rate, "api": api_failure_rate,
                            "jobsSucceeded": len(succeeded), "jobsTotal": len(jobs)},
        })

        passes = (
            len(succeeded) == len(jobs) and len(jobs) > 0
            and not oom_any
            and all((j.get("windows") or 0) > 0 for j in jobs)
            and (api_failure_rate or 0) == 0
        )
        result["checks"] = {
            "allJobsSucceeded": len(succeeded) == len(jobs) and bool(jobs),
            "allJobsProducedWindows": bool(jobs) and all((j.get("windows") or 0) > 0
                                                         for j in jobs),
            "noOomKilled": not oom_any,
            "apiFailureRateZero": (api_failure_rate or 0) == 0,
            "tempMediaCleaned": cleanup.get("tempFilesLeft") == 0,
        }
        result["outcome"] = "passed" if passes else "failed"
        if not passes:
            failed_checks = [k for k, v in result["checks"].items() if not v]
            result["reason"] = "未通过的检查项：" + ", ".join(failed_checks)
    except Exception as exc:  # noqa: BLE001 - 一行失败不能中断整个矩阵
        result.update(outcome="failed",
                      reason=f"{type(exc).__name__}: {exc}"[:600],
                      finishedAt=time.strftime("%Y-%m-%dT%H:%M:%S"))
    finally:
        write_json(out_dir / "row.json", result)
    return result


# --------------------------------------------------------------------------- #
# 摘要
# --------------------------------------------------------------------------- #
def evidence_storage(row: dict) -> dict:
    """证据帧的字节成本，**按对象前缀拆开**，不把结果 JSON 混进"每帧"。

    三个层次必须分开报，混在一起就会得出错误的"每帧恒为 53.7 KB"：

    1. ``frameBytes`` —— ``analysis-artifacts/lessons/<lesson>/jobs/<job>/`` 下
       的 JPEG 对象。这是真正的证据帧字节，分母就是 ``behavior_evidence_frames``
       的张数。行 JSON 里每个 job 记了 ``evidenceFrameBytes``/``evidenceFrameObjects``。
    2. ``resultBytes`` —— ``analysis-artifacts/analysis-results/<job>/`` 下的持久化
       结果 JSON（每 job 2 个对象）。它随窗口数与转写文本增长，与帧数无关。
    3. ``bucketDeltaBytes`` —— 行前/行后对 ``analysis-artifacts`` 整桶求差。它应当
       约等于 1 + 2 加上同期的其它写入；对不上的差额单独报为 ``unattributedBytes``。

    ``bytesPerFrame`` 在**有逐对象测量时只用第 1 层**（真·每帧字节）；
    只有早期产物缺该字段时才退回按整桶增量摊（并明确标 ``basis``）。
    """
    delta = row.get("diskDelta") or {}
    per_bucket = (delta.get("minio") or {}).get("perBucketDelta") or {}
    if per_bucket:
        artifacts = per_bucket.get("analysis-artifacts") or {}
        raw = per_bucket.get("classroom-raw-media") or {}
        bucket_bytes, bucket_objects = artifacts.get("bytesDelta"), artifacts.get("objectsDelta")
        raw_bytes = raw.get("bytesDelta")
    else:
        before = ((row.get("diskBefore") or {}).get("minioObjects") or {}).get("buckets") or {}
        after = ((row.get("diskAfter") or {}).get("minioObjects") or {}).get("buckets") or {}
        bucket_bytes = _diff((after.get("analysis-artifacts") or {}).get("bytes"),
                             (before.get("analysis-artifacts") or {}).get("bytes"))
        bucket_objects = _diff((after.get("analysis-artifacts") or {}).get("objects"),
                               (before.get("analysis-artifacts") or {}).get("objects"))
        raw_bytes = _diff((after.get("classroom-raw-media") or {}).get("bytes"),
                          (before.get("classroom-raw-media") or {}).get("bytes"))

    jobs = row.get("jobs") or []
    frames = sum(int(job.get("evidenceFrames") or 0) for job in jobs)
    measured = [job for job in jobs
                if isinstance(job.get("evidenceFrameBytes"), int)
                and isinstance(job.get("resultBytes"), int)]
    if measured:
        frame_bytes = sum(job["evidenceFrameBytes"] for job in measured)
        frame_objects = sum(int(job.get("evidenceFrameObjects") or 0) for job in measured)
        result_bytes = sum(job["resultBytes"] for job in measured)
        result_objects = sum(int(job.get("resultObjects") or 0) for job in measured)
        basis = "per-object (lessons/<lesson>/jobs/<job>/ prefix only)"
        per_frame = round(frame_bytes / frames, 1) if frames else None
        attributed = frame_bytes + result_bytes
        unattributed = (_diff(bucket_bytes, attributed)
                        if isinstance(bucket_bytes, int) else None)
    else:
        frame_bytes = frame_objects = result_bytes = result_objects = None
        basis = "bucket delta apportioned over frames (no per-object measurement)"
        per_frame = (round(bucket_bytes / frames, 1)
                     if isinstance(bucket_bytes, int) and frames else None)
        unattributed = None

    # 溯源：补测的证据只存在行文件里的话，只读 summary.json 的读者就看不到
    # "这些字节是事后测的"。把测量时点与方法一并带进汇总。
    measurement = row.get("evidenceObjectMeasurement") or {}
    return {
        "measuredAt": measurement.get("measuredAt"),
        "backfilled": bool(measurement.get("backfilled")),
        "measurementMethod": measurement.get("method"),
        # 第 1 层：真·证据帧字节
        "frameBytes": frame_bytes,
        "frameObjects": frame_objects,
        "evidenceFrames": frames,
        "bytesPerFrame": per_frame,
        # 第 2 层：结果 JSON（与帧数无关）
        "resultBytes": result_bytes,
        "resultObjects": result_objects,
        # 第 3 层：整桶对账
        "bucketDeltaBytes": bucket_bytes,
        "bucketDeltaObjects": bucket_objects,
        "unattributedBytes": unattributed,
        "rawMediaBytes": raw_bytes,
        "basis": basis,
    }


def compute_throughput_basis(media_seconds: float, jobs: list[dict],
                             wall_seconds: float) -> dict:
    """给出吞吐的分子/分母**以及可比性警告**。

    三个数会让读者误比：

    1. ``effectiveIntervalS`` 随时长自动放大（``vision.py::_sampling_policy`` 的
       采样上限），所以 120 min 行每秒媒体只解更少的帧，"每媒体分钟成本"比
       40 min 行低——这不是"更高效"，而是**采样更稀**。
    2. 音频行（ASR）每媒体分钟成本比视频高一个数量级，和视频行比没有意义。
    3. 并发行只有在 ``assert_overlap`` 判定为真时，按并发计的吞吐才成立。

    这里把这三件事写成结构化字段，避免文档里靠读者自己推断。
    """
    intervals = sorted({str(j.get("effectiveIntervalS"))
                        for j in jobs if j.get("effectiveIntervalS")})
    media_types = sorted({j.get("mediaType") for j in jobs if j.get("mediaType")})
    sampler_note = []
    if len(intervals) > 1:
        sampler_note.append(f"行内 effectiveIntervalS 不一致：{intervals}")
    if "audio" in media_types:
        sampler_note.append(
            "本行含 audio(ASR) 任务：ASR 每秒媒体的成本远高于视频，"
            "不能与纯视频行的吞吐直接比较")
    return {
        "numerator": "mediaSeconds (所有任务媒体时长之和)",
        "denominator": "wallSeconds (从首个上传开始到全部任务终态，含上传与排队)",
        "mediaSeconds": media_seconds,
        "wallSeconds": wall_seconds,
        "effectiveSampleIntervalS": intervals or None,
        "mediaTypes": media_types,
        "comparabilityWarnings": sampler_note,
        "note": ("effectiveIntervalS 由 vision.py 的采样上限按时长自动放大，"
                 "长视频行的每媒体分钟成本天然更低；跨时长比较时必须同时看该字段。"),
    }


def load_row_results(out_root: Path) -> dict[str, dict]:
    """读取 ``<out>/<row>/row.json``：把磁盘上**已跑过的所有行**并进汇总。

    为什么不能只用"本次调用的内存结果"：矩阵会被分几次跑（先冒烟、再批量、
    再补跑失败行）。若汇总只写本次的 rows，前面跑过的行会从 summary 里消失，
    而 ``complete`` 只看本次的集合就会再次变成"我跑过的都过了"的假绿灯。
    磁盘上的 row.json 才是行的真实状态，所以汇总以它为准，内存结果覆盖同名文件。
    """
    results: dict[str, dict] = {}
    if not out_root.is_dir():
        return results
    for row_dir in sorted(out_root.iterdir()):
        path = row_dir / "row.json"
        if not row_dir.is_dir() or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            results[row_dir.name] = {
                "row": row_dir.name, "outcome": "failed",
                "reason": f"row.json 无法解析：{path}",
                "runDirectory": str(row_dir),
            }
            continue
        payload["runDirectory"] = str(row_dir)
        results[payload.get("row") or row_dir.name] = payload
    return results


def fit_result_json_model(rows: list[dict], min_observations: int = 10) -> dict:
    """用最小二乘拟合 ``resultBytes ≈ a × observations + b``，并报残差与适用域。

    ``min_observations`` 的默认值不是随手取的：结果 JSON 里有一个几乎固定的头部
    （``envelope.json`` 283 B + JSON 外壳），观测数很小时截距项主导，线性模型
    不再成立。``smoke-90s`` 只有 2 条观测，实测 6,330 B 而模型外推会给出 2 万级，
    所以它被排除在拟合之外，只作为"低观测端不适用"的边界证据保留。
    """
    points = []
    excluded = []
    for row in rows:
        for job in (row.get("jobs") or []):
            if job.get("mediaType") != "video":
                continue
            obs = job.get("observations")
            size = job.get("resultBytes")
            if not isinstance(obs, int) or not isinstance(size, int):
                continue
            item = {"row": row.get("row"), "job": job.get("key"),
                    "observations": obs, "resultBytes": size}
            (points if obs >= min_observations else excluded).append(item)
    if len(points) < 3:
        return {"available": False,
                "reason": f"only {len(points)} video tasks with >= {min_observations} observations",
                "excluded": excluded}
    xs = [p["observations"] for p in points]
    ys = [p["resultBytes"] for p in points]
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0.0
    intercept = my - slope * mx
    residuals = []
    for item in points:
        predicted = slope * item["observations"] + intercept
        residuals.append({"row": item["row"], "job": item["job"],
                          "observations": item["observations"],
                          "resultBytes": item["resultBytes"],
                          "predictedBytes": round(predicted, 1),
                          "residualBytes": round(item["resultBytes"] - predicted, 1)})
    worst = max(abs(r["residualBytes"]) for r in residuals)
    excluded_detail = []
    for item in excluded:
        predicted = slope * item["observations"] + intercept
        excluded_detail.append({**item, "predictedBytes": round(predicted, 1),
                                "residualBytes": round(item["resultBytes"] - predicted, 1)})
    return {
        "available": True,
        "form": "resultBytes = a * observations + b",
        "a": round(slope, 1), "b": round(intercept, 1),
        "n": len(points),
        "maxAbsResidualBytes": round(worst, 1),
        "domain": f"video tasks with >= {min_observations} observations",
        "excludedOutsideDomain": excluded_detail,
        "residuals": residuals,
        "note": ("低观测端不适用：结果 JSON 含固定头部，观测数很小时截距主导，"
                 "线性外推会高估。smoke-90s 因此只作边界证据，不并入拟合。"),
    }


def build_summary(rows: list[dict], hardware: dict, env: dict) -> dict:
    """汇总。

    ``complete`` 覆盖**整个声明的矩阵**，不只是本次选中的行：

    * 行的结果优先取本次内存结果，其次取磁盘上的 ``row/<id>/row.json``；
    * 声明了但磁盘上没有结果的行记 ``missing``，声明了但本次未选中且磁盘也无
      结果的行记 ``not-run``；
    * 任何一行不是 ``passed``（含 missing / not-run / failed）→ ``complete: false``。

    这样依次分批跑完的矩阵，最后一次调用能把所有行并进来；而没有跑的行永远
    不会因为"不在本次集合里"被静默丢掉。
    """
    selected = set(env.get("selectedRows") or [])
    declared = list(env.get("declaredRows") or [])
    merged: dict[str, dict] = dict(env.get("diskRows") or {})
    for row in rows:
        merged[row["row"]] = row

    outcomes: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for row_id in declared:
        found = merged.get(row_id)
        if found is None:
            if row_id in selected:
                outcomes[row_id] = "missing"
                reasons[row_id] = "本次选中但没有任何结果（进程中断或行内异常未落盘）"
            else:
                outcomes[row_id] = "not-run"
                reasons[row_id] = (f"未在本次 --rows（{sorted(selected)}）中，"
                                   "磁盘上也没有该行的 row.json")
            continue
        outcomes[row_id] = found.get("outcome") or "failed"
        reasons[row_id] = found.get("reason") or ""
    # 磁盘上有结果但**不在声明里**的行（改过矩阵配置后的残留）单独列出，不并入判定。
    undeclared = sorted(set(merged) - set(declared))

    complete = bool(declared) and all(v == "passed" for v in outcomes.values())
    ordered = [merged[row_id] for row_id in declared if row_id in merged]
    return {
        "matrix": "capacity-matrix-2026-10-28",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": {"gitHead": env.get("gitHead"), "python": env.get("python"),
                 "platform": env.get("platform")},
        "hardware": hardware,
        "isSla": False,
        "slaNote": ("所有延迟数字来自单客户端串行请求；并发、多客户端、真实网络下的 "
                    "p95/p99 未测，不能作为 SLA 引用。"),
        "complete": complete,
        "declaredRows": declared,
        "selectedRows": sorted(selected),
        "rowOutcomes": outcomes,
        "rowReasons": reasons,
        "undeclaredRowResults": undeclared,
        "rows": [{k: r.get(k) for k in (
            "row", "title", "outcome", "reason", "runDirectory", "startedAt",
            "finishedAt", "wallSeconds", "mediaSeconds",
            "throughputMediaMinPerWallMin", "throughputBasis", "overlap",
            "failureRate", "oomKilled", "cleanup", "asrPreflight")}
            | {"disk": (r.get("diskDelta") or {}).get("mb"),
               "minio": (r.get("diskDelta") or {}).get("minio"),
               "evidenceStorage": evidence_storage(r),
               "resources": {k: (r.get("resources") or {}).get(k) for k in (
                   "samples", "medianGapS", "maxGapS", "aiCpuSumPeakPct",
                   "aiMemSumPeakMb", "hostCpuPeakPct", "hostMemPeakMb")},
               "cgroupPeakMb": {name: probe.get("memoryPeakMb")
                                for name, probe in (r.get("cgroup") or {}).items()},
               "jobs": [{kk: j.get(kk) for kk in (
                   "key", "mediaType", "status", "queueWaitS", "processingS",
                   "endToEndS", "startedAt", "completedAt", "windows",
                   "observations", "evidenceFrames", "sampledFrames",
                   "effectiveIntervalS", "truncated", "errorMessage",
                   "evidenceFrameBytes", "evidenceFrameObjects",
                   "resultBytes", "resultObjects")}
                   for j in (r.get("jobs") or [])],
               "latency": {
                   "duringRun": (r.get("latencyDuringRun") or {}).get("endpoints"),
                   "afterRun": (r.get("latencyAfterRun") or {}).get("endpoints"),
               }}
            for r in ordered],
        "diskAccounting": env.get("diskAccounting"),
        # 结果 JSON 的线性模型：驱动量是观测数，且**适用域有限**。
        # smoke-90s 只有 2 条观测，截距主导，线性模型在该端点不成立，因此拟合与
        # 残差都只用 >= 40 观测的视频任务；该行作为边界证据单列，不并入统计。
        "resultJsonModel": fit_result_json_model(ordered),
        "note": ("complete=false 表示矩阵里有行未跑、缺失或未通过；不要把缺行的汇总"
                 "当结论。每行原始产物在 <out>/<row>/row.json，"
                 "rows[].runDirectory 给出它的来源目录。"),
    }


def write_markdown_summary(out_root: Path, summary: dict) -> Path:
    """写 ``summary.md``。

    真跑与 ``--summary-only`` 必须走同一个函数：两份产物若各自拼字符串，
    重算一次 JSON 就会让 md 留在旧口径（本轮的 B/frame 正是这样不一致过）。
    """
    text = ("# 容量矩阵摘要（自动生成）\n\n"
            "```\n" + render_table(summary) + "\n```\n\n"
            f"complete = {summary['complete']}\n")
    path = out_root / "summary.md"
    path.write_text(text, encoding="utf-8")
    return path


def render_table(summary: dict) -> str:
    lines = []
    header = (f"{'row':<16}{'outcome':<10}{'wall s':>9}{'media min':>11}"
              f"{'thrpt':>8}{'jobFail':>9}{'p95 run':>10}{'B/frame':>10}")
    lines.append(header)
    lines.append("-" * len(header))
    for row in summary["rows"]:
        during = ((row.get("latency") or {}).get("duringRun") or {})
        p95 = max((v.get("p95") or 0) for v in during.values()) if during else None
        evi = (row.get("evidenceStorage") or {}).get("bytesPerFrame")
        lines.append(
            f"{row['row']:<16}{row['outcome']:<10}"
            f"{(row.get('wallSeconds') or 0):>9.1f}"
            f"{((row.get('mediaSeconds') or 0) / 60):>11.1f}"
            f"{(row.get('throughputMediaMinPerWallMin') or 0):>8.2f}"
            f"{((row.get('failureRate') or {}).get('jobs', 0)):>9.2f}"
            f"{(p95 if p95 is not None else 0):>10.1f}"
            f"{(evi if evi is not None else 0):>10.1f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def print_plan(rows: list[Row], args) -> None:
    print("=== 容量矩阵计划（--dry-run，不执行任何命令）===")
    print(f"仓库根目录 : {REPO_ROOT}")
    print(f"compose 项目: {PROJECT}")
    print(f"镜像项目名 : 与 {PROJECT} 同名构建（复用 mact-cap3 镜像层缓存）")
    print(f"数据根目录 : {STACK_DIR}")
    print(f"输入目录   : {INPUT_DIR}")

    print("\n--- 1. 硬件（真实运行时会现场测量；dry-run 不测量）---")
    print("  psutil.cpu_count(logical/physical) / virtual_memory().total")
    print("  powershell Get-CimInstance Win32_Processor -> Name/NumberOfCores")
    print("  docker info -> .NCPU / .MemTotal / .DockerRootDir")

    print("\n--- 2. Compose 命令（顺序固定；第三个 -f 是我的绑定挂载 override）---")
    print("  export MACT_CAP_DATA_ROOT=%s" % str(STACK_DIR).replace("\\", "/"))
    print("  export MACT_CAP_REPO_ROOT=%s" % str(REPO_ROOT).replace("\\", "/"))
    print("  " + " ".join(compose_cmd()))
    print("\n  校验没有命名卷（必须输出为空）：")
    print("  " + " ".join(compose_cmd() + ["config", "--volumes"]))
    print("\n  起栈：")
    print("  " + " ".join(compose_cmd() + [
        "up", "-d", "postgres", "redis", "minio", "ai-service", "backend"]))
    print("  每行按并发度扩缩消费者：")
    for row in rows:
        print("  " + " ".join(compose_cmd() + [
            "up", "-d", "--no-deps", "--no-recreate",
            "--scale", f"ai-service={row.consumers}", "ai-service"]))
    print("\n  收尾（不加 -v；绑定目录按需清理）：")
    print("  " + " ".join(compose_cmd() + ["down", "--remove-orphans"]))

    print("\n--- 3. 输入媒体（ffmpeg，全部写到 %s）---" % INPUT_DIR)
    for step in ffmpeg_plan():
        state = "已存在，跳过" if Path(step["output"]).is_file() else "将生成"
        print(f"  [{state}] {step['output']}")
        print(f"           {' '.join(step['command'])}")
        print(f"           理由：{step['reason']}")

    print("\n--- 4. 行 ---")
    for row in rows:
        print(f"  {row.id:<16} consumers={row.consumers}  预计≈{row.estimate_s}s  {row.title}")
        for job in row.jobs:
            size = input_path(job.input_name)
            size_txt = (f"{size.stat().st_size / 1024 / 1024:.1f} MB"
                        if size.is_file() else "未生成")
            print(f"      {job.key:<3} {job.media_type:<6} {job.input_name:<18} "
                  f"lesson={job.lesson:<3} {size_txt}")
        if row.note:
            print(f"      备注：{row.note}")

    print("\n--- 5. 每行采集的口径 ---")
    print("  throughput         = 本行媒体分钟数 / 墙钟分钟数（含上传与排队）")
    print("  queueWaitS         = analysis_jobs.started_at - created_at（DB 时间，非客户端）")
    print("  processingS        = completed_at - started_at")
    print("  apiP50/p95/p99     = 行内串行 GET /reports/page + /jobs?limit=50，"
          f"间隔 {args.api_interval}s；行后另测 detail/evidence")
    print(f"  containerRss/Cpu   = docker stats 每 {args.sample_interval}s；"
          "另有 cgroup memory.peak / oom_kill")
    print("  diskDelta          = 绑定目录 du 行前/行后 + MinIO 对象清单（字节）")
    print("  evidenceFrames     = behavior_evidence_frames 行数（张数）+ MinIO 字节增量")
    print("  cleanup            = 任务结束后 ai-work 临时目录归零的耗时")
    print("  isSla              = false（单客户端串行，不是 SLA）")

    print("\n--- 6. not-run 判定 ---")
    print("  预计耗时超过 --max-row-minutes（当前 "
          f"{args.max_row_minutes}）的行只打印上面这些命令，结果记为 not-run 并把命令写进 JSON。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", default="",
                        help="逗号分隔的行 id；空=全部。可用：" + ",".join(ROWS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true", help="把摘要 JSON 打到 stdout")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印计划与将执行的命令，不碰 docker / ffmpeg")
    parser.add_argument("--setup", action="store_true", help="只起栈后退出")
    parser.add_argument("--teardown", action="store_true", help="只收尾后退出")
    parser.add_argument("--summary-only", action="store_true",
                        help="不跑任何行：只从 <out>/<row>/row.json 重新生成 summary")
    parser.add_argument("--sample-interval", type=float, default=2.0,
                        help="docker stats 采样间隔（秒）")
    parser.add_argument("--api-interval", type=float, default=2.0,
                        help="行内 API 延迟采样间隔（秒）")
    parser.add_argument("--post-run-samples", type=int, default=20,
                        help="行后报告端点每端点的样本数")
    parser.add_argument("--post-run-interval", type=float, default=0.2)
    parser.add_argument("--window-duration", type=int, default=60)
    parser.add_argument("--min-observations", type=int, default=5)
    parser.add_argument("--total-seats", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=7200,
                        help="单个任务等待上限（秒）")
    parser.add_argument("--max-row-minutes", type=float, default=45.0,
                        help="超过该预计耗时的行不跑，记 not-run")
    parser.add_argument("--allow-long-rows", action="store_true",
                        help="忽略 --max-row-minutes，长行也真跑")
    args = parser.parse_args(argv)

    selected_ids = ([s.strip() for s in args.rows.split(",") if s.strip()]
                    if args.rows else list(ROWS))
    unknown = [r for r in selected_ids if r not in ROWS]
    if unknown:
        print(f"未知行 id：{unknown}；可用：{list(ROWS)}", file=sys.stderr)
        return 1
    selected = [ROWS[r] for r in selected_ids]

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        print_plan(selected, args)
        return 0

    if args.teardown:
        before = docker_volume_count()
        done = stack_down([])
        after = docker_volume_count()
        print(json.dumps({"teardown": done, "volumesBefore": before,
                          "volumesAfter": after}, ensure_ascii=False, indent=2))
        return 0 if done.get("ok") and before == after else 2

    if args.summary_only:
        # 只重建汇总：行结果全部来自磁盘，不碰 docker、不跑任务。
        # 用途是补分批运行后的最终汇总，以及在没有重跑的情况下核对 complete。
        disk_rows = load_row_results(out_root)
        # 磁盘账目是上一次真跑时测的，重建汇总时不能把它替换成"未测量"——
        # 那会让读者以为数据没落盘。沿用旧值并明确标注来源。
        previous: dict = {}
        prior_path = out_root / "summary.json"
        if prior_path.is_file():
            try:
                prior = json.loads(prior_path.read_text(encoding="utf-8"))
                previous = (prior.get("environment") or {}).get("diskAccounting") or {}
            except (json.JSONDecodeError, OSError):
                previous = {}
        # environment 结转：盘账、收尾、总耗时都只能来自真跑的那一次。
        prior_env: dict = {}
        env_path = out_root / "run-environment.json"
        if env_path.is_file():
            try:
                prior_env = json.loads(env_path.read_text(encoding="utf-8")) or {}
                prior_env["carriedOverFrom"] = str(env_path)
            except (json.JSONDecodeError, OSError):
                prior_env = {}
        elif prior_path.is_file():
            try:
                prior_env = ((json.loads(prior_path.read_text(encoding="utf-8")) or {})
                             .get("environment") or {})
                prior_env["carriedOverFrom"] = str(prior_path)
            except (json.JSONDecodeError, OSError):
                prior_env = {}
        disk_accounting = dict(prior_env.get("diskAccounting") or previous)
        disk_accounting["dataRoot"] = str(DATA_ROOT)
        disk_accounting["source"] = (
            "carried over from the last real run; --summary-only does not re-measure"
            if disk_accounting.get("dataRootBytesAfter") is not None
            else "not measured (no prior run-environment.json)")
        prior_env = {k: v for k, v in prior_env.items() if k != "diskRows"}
        prior_env["carriedOverFromPreviousSummary"] = bool(prior_env)
        prior_env["declaredRows"] = list(ROWS)
        prior_env["selectedRows"] = []
        prior_env["diskAccounting"] = disk_accounting
        # 顺手把 run-environment.json 也收敛成无 diskRows 的版本（早期产物带过副本）。
        write_json(env_path, prior_env)
        summary = build_summary([], measure_hardware(), {
            "gitHead": run(["git", "rev-parse", "--short", "HEAD"],
                           timeout=30).stdout.strip() or None,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "declaredRows": list(ROWS),
            "selectedRows": [],
            "diskRows": disk_rows,
            "diskAccounting": disk_accounting,
        })
        summary["environment"] = prior_env
        write_json(out_root / "summary.json", summary)
        # summary.md 必须同步重写：只更新 summary.json 会让两份产物互相矛盾
        # （md 还带着旧口径的 B/frame），而读者往往先看 md。
        write_markdown_summary(out_root, summary)
        print(render_table(summary))
        print()
        print(f"complete = {summary['complete']}")
        return 0 if summary["complete"] else 2

    log: list[str] = []
    hardware = measure_hardware()
    volumes_before = docker_volume_count()
    data_root_bytes_before = dir_bytes(DATA_ROOT)

    if args.setup:
        inputs = ensure_inputs(False, log)
        stack = stack_up(max(row.consumers for row in selected), log)
        print(json.dumps({"hardware": hardware, "inputs": inputs, "stack": stack,
                          "volumesBefore": volumes_before,
                          "volumesAfter": docker_volume_count()},
                         ensure_ascii=False, indent=2))
        return 0 if stack.get("ok") else 2

    started = time.monotonic()
    inputs = ensure_inputs(False, log)

    rows_out: list[dict] = []
    for row in selected:
        if row.estimate_s > args.max_row_minutes * 60 and not args.allow_long_rows:
            timeout_row = {
                "row": row.id, "title": row.title, "outcome": "not-run",
                "reason": (f"预计 {row.estimate_s}s 超过 --max-row-minutes="
                           f"{args.max_row_minutes}；命令见 planCommands"),
                "planCommands": {
                    "scale": " ".join(compose_cmd() + [
                        "up", "-d", "--no-deps", "--no-recreate",
                        "--scale", f"ai-service={row.consumers}", "ai-service"]),
                    "row": (f"python -I scripts/ops/capacity_matrix.py --rows {row.id} "
                            f"--allow-long-rows --out {args.out}"),
                },
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            # not-run 也必须落盘：汇总以磁盘 row.json 为准，不落盘就等于"这一行
            # 从没出现过"，正是要避免的缺行静默。
            write_json(out_root / row.id / "row.json", timeout_row)
            rows_out.append(timeout_row)
            continue
        print(f"[row] {row.id} 开始 {time.strftime('%H:%M:%S')}", file=sys.stderr)
        result = run_row(row, args, run_id=f"{time.strftime('%Y%m%dT%H%M%S')}-{row.id}")
        print(f"[row] {row.id} -> {result['outcome']} "
              f"wall={result.get('wallSeconds')}s {result.get('reason') or ''}",
              file=sys.stderr)
        rows_out.append(result)

    teardown = stack_down(log)
    volumes_after = docker_volume_count()
    data_root_bytes_after = dir_bytes(DATA_ROOT)

    git_head = run(["git", "rev-parse", "--short", "HEAD"], timeout=30).stdout.strip() or None
    env = {
        "gitHead": git_head,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "declaredRows": list(ROWS),
        "selectedRows": [row.id for row in selected],
        "diskRows": load_row_results(out_root),
        "diskAccounting": {
            "dataRoot": str(DATA_ROOT),
            "dataRootBytesBefore": data_root_bytes_before,
            "dataRootBytesAfter": data_root_bytes_after,
            "dataRootBytesDelta": data_root_bytes_after - data_root_bytes_before,
            "dockerVolumesBefore": volumes_before,
            "dockerVolumesAfter": volumes_after,
            "dockerVolumeCountGrew": (volumes_after is not None and volumes_before is not None
                                      and volumes_after > volumes_before),
            "allPathsUnderE": str(DATA_ROOT).upper().startswith("E:"),
        },
        "teardown": teardown,
        "totalSeconds": round(time.monotonic() - started, 1),
        "inputs": inputs,
        "sampling": {"containerIntervalS": args.sample_interval,
                     "apiIntervalS": args.api_interval,
                     "postRunSamples": args.post_run_samples,
                     "postRunIntervalS": args.post_run_interval},
    }
    summary = build_summary(rows_out, hardware, env)
    # diskRows 是每行完整结果的副本，summary.rows 已含所需字段；不进 environment。
    summary["environment"] = {k: v for k, v in env.items() if k != "diskRows"}
    # environment 单独落一份：--summary-only 重建汇总时靠它结转盘账/收尾/总耗时，
    # 否则重新生成的 summary 会把"实测的磁盘账目"换成"未测量"。
    # diskRows 是每行完整结果的副本（含所有采样细节），summary.json 已经有了；
    # 写进 environment 会让这个文件膨胀到上百 KB，所以剥掉。
    write_json(out_root / "run-environment.json",
               {k: v for k, v in env.items() if k != "diskRows"})
    write_json(out_root / "summary.json", summary)
    write_markdown_summary(out_root, summary)

    print(render_table(summary))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    sys.exit(main())
