#!/usr/bin/env python
"""只读的可观测性快照探针（P1-4）。

背景：本仓库当前**没有** Prometheus 抓取端点（无 ``micrometer-registry-prometheus``，
actuator 只暴露 ``health,info``），ai-service 也没有 ``/metrics``。因此凡是能从
PostgreSQL 读到的指标族，先用这个脚本以 SQL 快照出来；读不到的（Redis Stream
PEL / consumer lag、进程内耗时直方图）在 ``skipped`` 里显式列出，不伪造。

安全约束（fail closed）：

1. 只允许目标容器 ``mact-ui-postgres``（``docker inspect`` 校验容器名与运行状态）。
   用户自己的 ``personal_design-*`` 与其它演练容器一律拒绝。
2. 库名必须以 ``macteval_`` 开头，且不在拒绝名单（``macteval`` / ``postgres`` /
   ``template0`` / ``template1``）。
3. docker 不可达、容器不在运行、库不存在 —— 一律非零退出，不做任何查询。
4. **只读**：只发 ``SELECT``。没有 INSERT / UPDATE / DELETE / DDL，可重复执行。

用法：

    python scripts/ops/observability_probe.py --print-queries   # 不碰 docker，打印 SQL
    python scripts/ops/observability_probe.py --json            # 快照 JSON 到 stdout
    python scripts/ops/observability_probe.py --json --out docs/reviews/evidence/obs.json

退出码：0 = 无 page 级发现；2 = 存在 page 级发现（阈值见
``docs/operations/observability-2026-10-28.md``）；1 = 守卫失败或查询失败。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from typing import Any

#: 只允许这一个容器。与 scripts/ops/verify_outbox_repair.py 保持同一白名单。
ALLOWED_CONTAINER = "mact-ui-postgres"
FORBIDDEN_CONTAINER_PREFIXES = ("personal_design-", "mact-cap-", "mact-r2-")
FORBIDDEN_DATABASES = {"macteval", "postgres", "template0", "template1"}
DB_PREFIX = "macteval_"

DEFAULT_DATABASE = "macteval_ui"
DEFAULT_USER = "macteval"

PSQL_TIMEOUT_SECONDS = 60

#: 阈值与 docs/operations/observability-2026-10-28.md §4 一一对应。
THRESHOLD_OLDEST_PENDING_SECONDS = 300      # warn/page，等于 outbox 重发宽限
THRESHOLD_QUEUED_OLDEST_SECONDS = 1800      # 30 分钟
THRESHOLD_STUCK_MEDIA_PENDING_HOURS = 24    # 与 cleanupExpiredAssets 的 24h 一致


class ProbeError(RuntimeError):
    """守卫失败或查询失败：调用方以退出码 1 结束。"""


# ---------------------------------------------------------------------------
# SQL catalogue
#
# 只读 SELECT。表名/列名取自 Flyway 迁移（不是猜测）：
#   task_outbox              V23__business_integrity_and_report_versions.sql:100
#   behavior_evidence_frames V25__behavior_evidence_frames.sql:8   （无 size 列；V27 只加 metadata）
#   lesson_window_features   V18__create_lesson_window_features.sql:2 + V30（video_job_id/audio_job_id）
#   evaluation_reports       V1__init_schema.sql:3 + V4/V5/V6/V9/V23 增列
#   analysis_jobs            V2__expand_schema.sql:275 + V22/V23/V28/V32 增列
#   media_assets             V24__media_assets_and_referential_integrity.sql:18
# ---------------------------------------------------------------------------

TABLE_EXISTS_SQL = (
    "SELECT to_regclass('public.{table}') IS NOT NULL"
)

QUERIES: list[dict[str, str]] = [
    {
        "name": "outbox.summary",
        "table": "task_outbox",
        "description": "outbox 总量 / 未投递 / 最老未投递事件年龄 / parked / park sentinel / 重试分布",
        "sql": """
SELECT json_build_object(
  'total', count(*),
  'pending', count(*) FILTER (WHERE published_at IS NULL),
  -- V33 park 也会把 published_at 置 NULL（这是刻意的，见 runbook §5.2b），
  -- 所以 parked 行必然落进 pending。把两者分开，否则最老未投递年龄会被
  -- park 行永久污染，页面阈值就失去意义。
  'pendingExcludingParked', count(*) FILTER (WHERE published_at IS NULL
                                             AND last_error NOT LIKE 'repair_failed:%'),
  'oldestPendingAgeSeconds', COALESCE(ROUND(EXTRACT(EPOCH FROM (
      now() - min(created_at) FILTER (WHERE published_at IS NULL
                                      AND last_error NOT LIKE 'repair_failed:%')))::numeric, 3), 0),
  'parked', count(*) FILTER (WHERE last_error LIKE 'repair_failed:%'),
  'parkSentinelRows', count(*) FILTER (WHERE next_retry_at > now() + INTERVAL '1 year'),
  'retried', count(*) FILTER (WHERE attempt > 0),
  'maxAttempt', COALESCE(max(attempt), 0),
  'oldestPendingCreatedAt', COALESCE(to_char(
      min(created_at) FILTER (WHERE published_at IS NULL
                              AND last_error NOT LIKE 'repair_failed:%'),
      'YYYY-MM-DD HH24:MI:SS'), '')
)::text FROM task_outbox
""".strip(),
    },
    {
        "name": "outbox.retryHistogram",
        "table": "task_outbox",
        "description": "attempt 分桶（0 / 1 / 2-3 / 4-6 / 7+）",
        "sql": """
SELECT COALESCE(json_agg(json_build_object('bucket', bucket, 'count', cnt) ORDER BY ord), '[]'::json)::text
FROM (
  SELECT CASE WHEN attempt = 0 THEN '0'
              WHEN attempt = 1 THEN '1'
              WHEN attempt <= 3 THEN '2-3'
              WHEN attempt <= 6 THEN '4-6'
              ELSE '7+' END AS bucket,
         count(*) AS cnt,
         min(CASE WHEN attempt = 0 THEN 0
                  WHEN attempt = 1 THEN 1
                  WHEN attempt <= 3 THEN 2
                  WHEN attempt <= 6 THEN 3
                  ELSE 4 END) AS ord
    FROM task_outbox
   GROUP BY 1
) t
""".strip(),
    },
    {
        "name": "outbox.parkedByReason",
        "table": "task_outbox",
        "description": "parked 事件按 last_error 归因（V33 的 repair_failed:* 前缀）",
        "sql": """
SELECT COALESCE(json_agg(json_build_object('lastError', last_error, 'count', cnt)
                         ORDER BY cnt DESC, last_error), '[]'::json)::text
FROM (
  SELECT left(last_error, 120) AS last_error, count(*) AS cnt
    FROM task_outbox
   WHERE last_error LIKE 'repair_failed:%'
   GROUP BY 1
) t
""".strip(),
    },
    {
        "name": "reports.byDataSource",
        "table": "evaluation_reports",
        "description": "报告按 data_source 计数（REAL / EMPTY / DEMO / UNKNOWN）",
        "sql": """
SELECT COALESCE(json_object_agg(COALESCE(data_source, '(null)'), cnt), '{}'::json)::text
FROM (SELECT data_source, count(*) AS cnt FROM evaluation_reports GROUP BY 1) t
""".strip(),
    },
    {
        "name": "reports.climateNull",
        "table": "evaluation_reports",
        "description": "课堂氛围分为 null 的报告数（分 data_source 拆开：REAL 为 null 才是缺陷）",
        "sql": """
SELECT json_build_object(
  'total', count(*),
  'climateNull', count(*) FILTER (WHERE climate_score IS NULL),
  'climateNullReal', count(*) FILTER (WHERE climate_score IS NULL
                                       AND upper(COALESCE(data_source, '')) = 'REAL'),
  'climateNullEmpty', count(*) FILTER (WHERE climate_score IS NULL
                                        AND upper(COALESCE(data_source, '')) = 'EMPTY')
)::text FROM evaluation_reports
""".strip(),
    },
    {
        "name": "reports.status",
        "table": "evaluation_reports",
        "description": "报告按 status 计数（draft / published）",
        "sql": """
SELECT COALESCE(json_object_agg(COALESCE(status, '(null)'), cnt), '{}'::json)::text
FROM (SELECT status, count(*) AS cnt FROM evaluation_reports GROUP BY 1) t
""".strip(),
    },
    {
        "name": "windows.byJobVersionPair",
        "table": "lesson_window_features",
        "description": "窗口行按 (lesson_session_id, video_job_id, audio_job_id) 版本对分组；null 安全",
        "sql": """
SELECT COALESCE(json_agg(json_build_object(
         'lessonSessionId', lesson_session_id,
         'videoJobId', video_job_id,
         'audioJobId', audio_job_id,
         'windows', cnt,
         'firstWindowIndex', first_idx,
         'lastWindowIndex', last_idx) ORDER BY cnt DESC, lesson_session_id), '[]'::json)::text
FROM (
  -- uq_lesson_window_version 保证 (lesson, video_job, audio_job, window_index) 唯一，
  -- 所以 count(*) 就是该版本的窗口数；再给首尾索引，便于发现"窗口编号不连续"。
  SELECT lesson_session_id, video_job_id, audio_job_id,
         count(*) AS cnt,
         min(window_index) AS first_idx,
         max(window_index) AS last_idx
    FROM lesson_window_features
   GROUP BY 1, 2, 3
) t
""".strip(),
    },
    {
        "name": "windows.summary",
        "table": "lesson_window_features",
        "description": "窗口总量 / 无融合分的窗口 / 两个 job id 皆为空的历史行数",
        "sql": """
SELECT json_build_object(
  'total', count(*),
  'distinctLessons', count(DISTINCT lesson_session_id),
  'fusedScoreNull', count(*) FILTER (WHERE fused_score IS NULL),
  'legacyRowsNoJobIds', count(*) FILTER (WHERE video_job_id IS NULL AND audio_job_id IS NULL)
)::text FROM lesson_window_features
""".strip(),
    },
    {
        "name": "jobs.byStatus",
        "table": "analysis_jobs",
        "description": "任务按 status 计数",
        "sql": """
SELECT COALESCE(json_object_agg(COALESCE(status, '(null)'), cnt), '{}'::json)::text
FROM (SELECT status, count(*) AS cnt FROM analysis_jobs GROUP BY 1) t
""".strip(),
    },
    {
        "name": "jobs.queueHealth",
        "table": "analysis_jobs",
        "description": "最老 QUEUED 年龄 / 过期租约的 RUNNING / 心跳过旧(>alert-after 900s)的 RUNNING / 补偿待办",
        "sql": """
SELECT json_build_object(
  'queued', count(*) FILTER (WHERE status = 'QUEUED'),
  'oldestQueuedAgeSeconds', COALESCE(ROUND(EXTRACT(EPOCH FROM (
      now() - min(created_at) FILTER (WHERE status = 'QUEUED')))::numeric, 3), 0),
  'running', count(*) FILTER (WHERE status = 'RUNNING'),
  'runningExpiredLease', count(*) FILTER (WHERE status = 'RUNNING'
                                          AND lease_until IS NOT NULL
                                          AND lease_until < now()),
  'runningStaleHeartbeat', count(*) FILTER (WHERE status = 'RUNNING'
                                            AND heartbeat_at IS NOT NULL
                                            AND heartbeat_at < now() - INTERVAL '900 seconds'),
  'ingestPending', count(*) FILTER (WHERE ingest_state = 'PENDING'),
  'alignmentPending', count(*) FILTER (WHERE alignment_state = 'PENDING'),
  'alignmentFailed', count(*) FILTER (WHERE alignment_state = 'FAILED'),
  'failed', count(*) FILTER (WHERE status = 'FAILED'),
  'partial', count(*) FILTER (WHERE status = 'PARTIAL')
)::text FROM analysis_jobs
""".strip(),
    },
    {
        "name": "jobs.durations",
        "table": "analysis_jobs",
        "description": "队列等待（started_at-created_at）与处理时长（completed_at-started_at）的 p50/p95/max，按整秒",
        "sql": """
SELECT json_build_object(
  'queueWaitSeconds', COALESCE((
    SELECT json_build_object(
             'samples', count(*),
             'p50', COALESCE(ROUND(percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (started_at - created_at)))::numeric, 1), 0),
             'p95', COALESCE(ROUND(percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (started_at - created_at)))::numeric, 1), 0),
             'max', COALESCE(ROUND(max(EXTRACT(EPOCH FROM (started_at - created_at)))::numeric, 1), 0))
      FROM analysis_jobs
     WHERE started_at IS NOT NULL AND created_at IS NOT NULL), '{}'::json),
  'processingSeconds', COALESCE((
    SELECT json_build_object(
             'samples', count(*),
             'p50', COALESCE(ROUND(percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at)))::numeric, 1), 0),
             'p95', COALESCE(ROUND(percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at)))::numeric, 1), 0),
             'max', COALESCE(ROUND(max(EXTRACT(EPOCH FROM (completed_at - started_at)))::numeric, 1), 0))
      FROM analysis_jobs
     WHERE completed_at IS NOT NULL AND started_at IS NOT NULL), '{}'::json)
)::text
""".strip(),
    },
    {
        "name": "evidence.frames",
        "table": "behavior_evidence_frames",
        "description": "证据帧总数 / 涉及任务数与课堂数 / 时间范围（该表**没有** size 列，见 evidence.sizeColumns）",
        "sql": """
SELECT json_build_object(
  'frames', count(*),
  'distinctJobs', count(DISTINCT job_id),
  'distinctLessons', count(DISTINCT lesson_session_id),
  'oldestCreatedAt', COALESCE(to_char(min(created_at), 'YYYY-MM-DD HH24:MI:SS'), ''),
  'newestCreatedAt', COALESCE(to_char(max(created_at), 'YYYY-MM-DD HH24:MI:SS'), '')
)::text FROM behavior_evidence_frames
""".strip(),
    },
    {
        "name": "evidence.sizeColumns",
        "table": "behavior_evidence_frames",
        "description": "探测证据帧表是否存在 size/bytes 列；为空数组表示无法从 SQL 汇总字节数",
        "sql": """
SELECT COALESCE(json_agg(column_name ORDER BY column_name), '[]'::json)::text
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'behavior_evidence_frames'
  AND (column_name ~ 'size'
       OR column_name ~ 'bytes'
       OR column_name ~ 'length')
""".strip(),
    },
    {
        "name": "media.assets",
        "table": "media_assets",
        "description": "媒体资产按 status 计数与 size_bytes 汇总；卡在 PENDING 超过 24h 的上传会话数",
        "sql": """
SELECT json_build_object(
  'byStatus', COALESCE((SELECT json_object_agg(COALESCE(status, '(null)'), cnt)
                          FROM (SELECT status, count(*) AS cnt FROM media_assets GROUP BY 1) s), '{}'::json),
  'totalBytes', COALESCE(sum(size_bytes), 0),
  'stuckPendingOver24h', count(*) FILTER (WHERE status = 'PENDING'
                                          AND created_at < now() - INTERVAL '24 hours')
)::text FROM media_assets
""".strip(),
    },
    {
        "name": "audit.outboxRepair",
        "table": "outbox_repair_audit",
        "description": "V33 修复审计按 outcome 计数（repaired / parked / skipped_not_json / error）",
        "sql": """
SELECT COALESCE(json_object_agg(COALESCE(outcome, '(null)'), cnt), '{}'::json)::text
FROM (SELECT outcome, count(*) AS cnt FROM outbox_repair_audit GROUP BY 1) t
""".strip(),
    },
]


def printable_queries() -> str:
    """``--print-queries`` 的输出：不触碰 docker，不读数据库。"""
    lines: list[str] = [
        "-- observability_probe.py: read-only SQL it would run",
        f"-- target container : {ALLOWED_CONTAINER}",
        f"-- target database  : {DEFAULT_DATABASE} (must start with '{DB_PREFIX}')",
        "-- every statement below is a SELECT; no DML, no DDL",
        "",
    ]
    for q in QUERIES:
        lines.append(f"-- [{q['name']}] table={q['table']}")
        lines.append(f"-- {q['description']}")
        lines.append(q["sql"].rstrip() + ";")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Isolation guard
# ---------------------------------------------------------------------------
def _run(cmd: list[str], *, stdin: str | None = None,
         timeout: float = PSQL_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as exc:
        raise ProbeError(f"docker CLI not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"command timed out after {timeout}s: {' '.join(cmd)}") from exc


def assert_isolated_target(container: str, database: str) -> dict[str, Any]:
    """返回校验后的目标；任何不符都以 :class:`ProbeError` abort（fail closed）。"""
    problems: list[str] = []

    if container != ALLOWED_CONTAINER:
        problems.append(f"container={container!r} is not the rig container "
                        f"{ALLOWED_CONTAINER!r}")
    if container.startswith(FORBIDDEN_CONTAINER_PREFIXES):
        problems.append(f"container={container!r} is a user/drill container")
    if not database.startswith(DB_PREFIX):
        problems.append(f"database={database!r} does not start with {DB_PREFIX!r}")
    if database in FORBIDDEN_DATABASES:
        problems.append(f"database={database!r} is a user/template database")

    if problems:
        raise ProbeError("refusing to touch a non-isolated target: " + "; ".join(problems))

    # docker daemon 可达性：inspect 失败即拒绝（而不是把 "没输出" 当成空结果）。
    inspected = _run(["docker", "inspect", "-f",
                      "{{.Name}}|{{.State.Running}}", container])
    if inspected.returncode != 0:
        raise ProbeError(
            "refusing: cannot inspect container "
            f"{container!r} (docker unreachable or container absent): "
            f"{inspected.stderr.strip()[:300]}")
    raw = inspected.stdout.strip()
    name, _, running = raw.partition("|")
    actual_name = name.lstrip("/")
    if actual_name != ALLOWED_CONTAINER:
        raise ProbeError(f"refusing: container resolved to {actual_name!r}, "
                         f"expected {ALLOWED_CONTAINER!r}")
    if running.strip() != "true":
        raise ProbeError(f"refusing: container {container!r} is not running "
                         f"(State.Running={running.strip()!r})")

    return {
        "container": actual_name,
        "database": database,
        "allowedContainer": ALLOWED_CONTAINER,
        "databasePrefixRequired": DB_PREFIX,
        "isolation": "rig-only",
    }


def psql(container: str, database: str, user: str, sql: str) -> subprocess.CompletedProcess:
    return _run(["docker", "exec", "-e", "PGCLIENTENCODING=UTF8", container,
                 "psql", "-U", user, "-d", database, "-X", "-q", "-A", "-t",
                 "-v", "ON_ERROR_STOP=1", "-c", sql])


def table_exists(container: str, database: str, user: str, table: str) -> bool:
    sql = TABLE_EXISTS_SQL.format(table=table)
    proc = psql(container, database, user, sql)
    if proc.returncode != 0:
        raise ProbeError(f"psql failed probing table {table!r}: "
                         f"{proc.stderr.strip()[:300]}")
    return proc.stdout.strip().lower() in {"t", "true"}


def query_json(container: str, database: str, user: str,
               name: str, sql: str) -> Any:
    proc = psql(container, database, user, sql)
    if proc.returncode != 0:
        raise ProbeError(f"psql failed for {name}: {proc.stderr.strip()[:400]}")
    text = proc.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"query {name} did not return JSON: {exc}: {text[:200]}") from exc


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
def _scalar(payload: Any, key: str, default: float = 0) -> float:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
    return default


def evaluate_findings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def add(signal: str, severity: str, value: Any, threshold: Any,
            message: str, runbook: str) -> None:
        findings.append({
            "signal": signal,
            "severity": severity,
            "value": value,
            "threshold": threshold,
            "message": message,
            "runbook": runbook,
        })

    outbox = snapshot.get("outbox") or {}
    summary = outbox.get("summary") or {}
    if isinstance(summary, dict):
        parked = _scalar(summary, "parked")
        if parked > 0:
            add("outbox.parked", "warn", int(parked), 0,
                "存在被 V33 park 的事件，需要人工归因；不要手工把 last_error 改回 NULL",
                "§5.2b 历史 outbox 坏消息被 park（V33 起）")
        oldest = _scalar(summary, "oldestPendingAgeSeconds")
        if oldest > THRESHOLD_OLDEST_PENDING_SECONDS:
            add("outbox.oldestPendingAgeSeconds", "page", round(oldest, 3),
                THRESHOLD_OLDEST_PENDING_SECONDS,
                "未投递（且未被 park）的事件超过 outbox 重发宽限，publisher 或 Redis 可能已停摆",
                "§5.2 Redis 短暂不可用")
        sentinel = _scalar(summary, "parkSentinelRows")
        if sentinel < parked:
            add("outbox.parkSentinelMismatch", "warn",
                {"parkSentinelRows": int(sentinel), "parked": int(parked)}, "equal",
                "部分 parked 行的 next_retry_at 没有推到 100 年后：findPending 会再次选中它，"
                "park 被自动重发流程绕过",
                "§5.2b 历史 outbox 坏消息被 park（V33 起）")

    jobs = snapshot.get("jobs") or {}
    health = jobs.get("queueHealth") or {}
    if isinstance(health, dict):
        queued_age = _scalar(health, "oldestQueuedAgeSeconds")
        if queued_age > THRESHOLD_QUEUED_OLDEST_SECONDS:
            add("jobs.oldestQueuedAgeSeconds", "page", round(queued_age, 3),
                THRESHOLD_QUEUED_OLDEST_SECONDS,
                "有任务排队超过 30 分钟：消费者可能停止或 outbox 未投递",
                "§5.1 AI 消费者停止")
        stale_hb = _scalar(health, "runningStaleHeartbeat")
        if stale_hb > 0:
            add("jobs.runningStaleHeartbeat", "warn", int(stale_hb), 0,
                "RUNNING 任务心跳超过 900 秒（mact.recovery.alert-after-seconds 默认）",
                "§5.3 回调失败 / worker 中断")
        alignment_failed = _scalar(health, "alignmentFailed")
        if alignment_failed > 0:
            add("jobs.alignmentFailed", "warn", int(alignment_failed), 0,
                "窗口对齐补偿耗尽：报告会缺少该任务的窗口行",
                "§5.3 回调失败 / worker 中断")

    reports = snapshot.get("reports") or {}
    climate = reports.get("climateNull") or {}
    if isinstance(climate, dict):
        null_real = _scalar(climate, "climateNullReal")
        if null_real > 0:
            add("reports.climateNullReal", "warn", int(null_real), 0,
                "data_source=REAL 但课堂氛围分为 null：报告不可发布（发布接口会 409）；"
                "通常是对齐未写入窗口行",
                "§5.3 回调失败 / worker 中断")

    media = snapshot.get("media") or {}
    assets = media.get("assets") or {}
    if isinstance(assets, dict):
        stuck = _scalar(assets, "stuckPendingOver24h")
        if stuck > 0:
            add("media.stuckPendingOver24h", "warn", int(stuck), 0,
                "上传会话超过 24 小时仍为 PENDING：对象可能已写入但未确认",
                "§5.5 媒体与证据帧")

    evidence = snapshot.get("evidence") or {}
    if evidence.get("available") is True and evidence.get("sizeColumns") == []:
        findings.append({
            "signal": "evidence.totalBytes",
            "severity": "info",
            "value": None,
            "threshold": None,
            "message": "behavior_evidence_frames 没有 size/bytes 列，SQL 无法汇总字节数；"
                       "只能报帧数。需要字节数时用 mc/du 扫对象存储",
            "runbook": "§5.5 媒体与证据帧",
        })

    return findings


def collect(container: str, database: str, user: str) -> dict[str, Any]:
    target = assert_isolated_target(container, database)

    databases = psql(container, database, user,
                     "SELECT current_database()")
    if databases.returncode != 0:
        raise ProbeError(f"cannot connect to database {database!r}: "
                         f"{databases.stderr.strip()[:300]}")

    snapshot: dict[str, Any] = {
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "targets": target,
        "outbox": {},
        "reports": {},
        "jobs": {},
        "windows": {},
        "evidence": {"available": False},
        "media": {},
        "audit": {},
        "skipped": [],
        "findings": [],
    }

    existing: dict[str, bool] = {}
    for table in sorted({q["table"] for q in QUERIES}):
        existing[table] = table_exists(container, database, user, table)

    section_for = {
        "outbox.summary": ("outbox", "summary"),
        "outbox.retryHistogram": ("outbox", "retryHistogram"),
        "outbox.parkedByReason": ("outbox", "parkedByReason"),
        "reports.byDataSource": ("reports", "byDataSource"),
        "reports.climateNull": ("reports", "climateNull"),
        "reports.status": ("reports", "status"),
        "windows.byJobVersionPair": ("windows", "byJobVersionPair"),
        "windows.summary": ("windows", "summary"),
        "jobs.byStatus": ("jobs", "byStatus"),
        "jobs.queueHealth": ("jobs", "queueHealth"),
        "jobs.durations": ("jobs", "durations"),
        "evidence.frames": ("evidence", "frames"),
        "evidence.sizeColumns": ("evidence", "sizeColumns"),
        "media.assets": ("media", "assets"),
        "audit.outboxRepair": ("audit", "outboxRepair"),
    }

    for query in QUERIES:
        table = query["table"]
        section, key = section_for[query["name"]]
        if not existing.get(table, False):
            if section == "evidence":
                snapshot["evidence"]["available"] = False
                snapshot["evidence"]["reason"] = f"table {table} missing"
            else:
                snapshot[section].setdefault("unavailable", []).append(
                    {"table": table, "reason": "table missing"})
            snapshot["skipped"].append({
                "metric": query["name"],
                "reason": f"table {table} does not exist in this database",
            })
            continue
        value = query_json(container, database, user, query["name"], query["sql"])
        if section == "evidence":
            snapshot["evidence"]["available"] = True
        snapshot[section][key] = value

    # 明确声明：这些话目前只能从进程内/Redis 读，SQL 快照不覆盖。
    snapshot["skipped"].extend([
        {"metric": "redis.stream.pending",
         "reason": "needs XINFO GROUPS / XPENDING on the rig Redis; not in PostgreSQL. "
                   "ai-service reads it opportunistically at "
                   "ai-service/app/workers/base.py:1218 (XLEN) and :1222 (XPENDING), "
                   "surfaced via GET :8001/ready -> details.pending"},
        {"metric": "redis.consumer.restarts",
         "reason": "in-process counter only: ai-service/app/core/readiness.py:43 "
                   "(_consumer_restarts), surfaced via /ready details.consumer_restarts"},
        {"metric": "jobs.queueWaitDuration.crossAttempt",
         "reason": "jobs.durations derives wait from started_at - created_at on the SAME row; "
                   "a requeued job overwrites started_at, so the first wait is lost. "
                   "A true per-attempt histogram needs a job_attempts table."},
        {"metric": "jobs.processingDuration.histogram",
         "reason": "jobs.durations gives p50/p95/max from completed_at - started_at; that is a "
                   "row-level aggregate, not a time series, so no rate or percentile trend "
                   "exists without micrometer-registry-prometheus"},
        {"metric": "http.request.latency",
         "reason": "no micrometer-registry-prometheus; only actuator health/info are exposed "
                   "(backend/src/main/resources/application.yml:218-226)"},
    ])

    snapshot["findings"] = evaluate_findings(snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def render_human(snapshot: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"generated_at : {snapshot['generated_at']}")
    lines.append(f"target       : {snapshot['targets']['container']} / "
                 f"{snapshot['targets']['database']}")
    outbox = (snapshot.get("outbox") or {}).get("summary") or {}
    if outbox:
        lines.append(
            "outbox       : total={total} pending={pending} "
            "pendingExcludingParked={pendingExcludingParked} "
            "oldestPending={oldestPendingAgeSeconds}s parked={parked} "
            "parkSentinel={parkSentinelRows} maxAttempt={maxAttempt}".format(**{
                k: outbox.get(k) for k in ("total", "pending", "pendingExcludingParked",
                                           "oldestPendingAgeSeconds", "parked",
                                           "parkSentinelRows", "maxAttempt")
            }))
    health = (snapshot.get("jobs") or {}).get("queueHealth") or {}
    if health:
        lines.append("jobs         : " + " ".join(
            f"{k}={health.get(k)}" for k in ("queued", "oldestQueuedAgeSeconds", "running",
                                             "runningExpiredLease", "failed", "partial")
            if k in health))
    frames = (snapshot.get("evidence") or {}).get("frames") or {}
    if frames:
        lines.append(f"evidence     : frames={frames.get('frames')} "
                     f"jobs={frames.get('distinctJobs')} lessons={frames.get('distinctLessons')} "
                     + ("bytes=" + ",".join(
                         (snapshot.get("evidence") or {}).get("sizeColumns") or [])
                        if (snapshot.get("evidence") or {}).get("sizeColumns")
                        else "bytes=unavailable(no size column)"))
    durations = ((snapshot.get("jobs") or {}).get("durations") or {})
    if durations:
        wait = durations.get("queueWaitSeconds") or {}
        proc = durations.get("processingSeconds") or {}
        lines.append(f"durations    : queueWait p50={wait.get('p50')}s p95={wait.get('p95')}s "
                     f"(n={wait.get('samples')}) | processing p50={proc.get('p50')}s "
                     f"p95={proc.get('p95')}s (n={proc.get('samples')})")
    lines.append("")
    if snapshot["findings"]:
        lines.append("findings:")
        for item in snapshot["findings"]:
            lines.append(f"  [{item['severity']:>4}] {item['signal']} = {item['value']} "
                         f"(threshold {item['threshold']}) -- {item['message']}")
    else:
        lines.append("findings: none above threshold")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="observability_probe.py",
        description="Read-only observability snapshot against the isolated rig database.",
        epilog="Exit codes: 0 = no page-level finding, 2 = page-level finding, "
               "1 = guard/query failure. See docs/operations/observability-2026-10-28.md.")
    parser.add_argument("--container", default=ALLOWED_CONTAINER,
                        help=f"must be exactly {ALLOWED_CONTAINER!r} (anything else is refused)")
    parser.add_argument("--database", default=DEFAULT_DATABASE,
                        help=f"must start with {DB_PREFIX!r}; user databases are refused")
    parser.add_argument("--user", default=DEFAULT_USER, help="psql role inside the container")
    parser.add_argument("--json", action="store_true", help="emit the snapshot as JSON")
    parser.add_argument("--out", default="", help="also write the JSON snapshot to this path")
    parser.add_argument("--print-queries", action="store_true",
                        help="print the SQL that would run and exit 0 (no docker, no database)")
    args = parser.parse_args(argv)

    if args.print_queries:
        print(printable_queries())
        return 0

    try:
        snapshot = collect(args.container, args.database, args.user)
    except ProbeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    if args.out:
        from pathlib import Path
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        if not args.json:
            print(f"snapshot written: {path}")

    if args.json:
        print(payload)
    else:
        print(render_human(snapshot))

    page = [f for f in snapshot["findings"] if f["severity"] == "page"]
    return 2 if page else 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，中文 findings 会变成乱码；与
    # scripts/ops/export_capacity_evidence.py:814 保持同一处理。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
