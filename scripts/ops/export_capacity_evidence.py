#!/usr/bin/env python
"""Aggregate final capacity/fault artifacts into the review evidence folder.

Inputs are the immutable summary.json files written by the measurement drivers.
The script never re-runs a job and never rewrites a measurement; it copies the
raw curves and produces a compact, machine-readable index with explicit checks.

Exit codes (also encoded in the emitted ``judgement.exitCode``):

* ``0`` — every required run/scenario is present, every required check is a real
  boolean ``True``, and the signed-URL audit actually scanned data with no hit.
* ``2`` — the judgement failed or the evidence is incomplete (missing run,
  corrupt JSON, missing/empty ``checks``, failed check, missing fault scenario,
  audit hit/error/no data).
* ``3`` — execution error (bad arguments, unreadable root, unwritable output).

Pure functions (``load_run`` / ``audit_signed_urls`` / ``required_check_keys`` /
``evaluate``) take injected paths and a query callable so the unit tests never
touch a real database.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from capacity_common import ISO_DIR, psql, write_json  # noqa: E402
except ImportError:  # pragma: no cover - keeps the judgement usable without psutil/requests
    ROOT_FALLBACK = Path(__file__).resolve().parents[2]
    ISO_DIR = ROOT_FALLBACK / "data" / "tmp" / "capacity-2026-10-26"

    def psql(sql: str, timeout: float = 60.0):
        raise RuntimeError("capacity_common.psql unavailable (psutil/requests missing)")

    def write_json(path, payload):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "reviews" / "evidence"

# 本轮正式交付要求通过的容量运行。
FINAL = [
    "final-40min-480p",
    "final-80min-480p",
    "final-120min-480p",
    "final-40min-asr",
    "final-41min-1080p",
]
CONCURRENT = ["final-concurrent-a-40min", "final-concurrent-b-40min"]
# 历史零窗口缺陷证据：只归档，不参与本轮判定。
DEFECT = ["80min-current-code", "120min-current-code"]

# 故障侧必需场景（取自 faults/all-*/summary.json 的 pass 字段）。
REQUIRED_SCENARIOS = ["redis-outage", "redis-loss", "group-loss", "worker-kill",
                      "callback-down"]

# 视频运行的必检键。driver 每次写入固定的 10 个键，但这里只要求与视频管线
# 相关的子集：音视频各有自己的必检项，不把另一模态的键当成本运行的必检项。
VIDEO_CHECK_KEYS = (
    "terminalSucceeded",
    "videoProducedWindows",
    "videoProducedObservations",
    "videoWindowCoverage",
    "actionModelMatchesCheckpoint",
    "actionModelDigestMatches",
    "behaviorModelFramesPresent",
    "behaviorModelFramesCarryExpectedDigest",
)
AUDIO_CHECK_KEYS = (
    "terminalSucceeded",
    "audioTranscriptPresent",
    "audioSpeechWindowsPresent",
)
# driver 实际会写入的全部键；不在这个集合里的键视为未知/伪造键。
KNOWN_CHECK_KEYS = tuple(dict.fromkeys(VIDEO_CHECK_KEYS + AUDIO_CHECK_KEYS))

# 签名 URL 审计的六个字段（与 V1–V28 迁移一致，全部为 TEXT/JSON 文本列）。
AUDIT_FIELDS = (
    "evaluation_report_versions.snapshot_json",
    "evaluation_reports.raw_report",
    "evaluation_reports.suggestions",
    "evaluation_reports.timeline_json",
    "evaluation_reports.suggestions_json",
    "analysis_jobs.result_metadata",
)

# 显式白名单：只有这些字段在「旧库确实缺少该表/列」时才允许记为
# not_applicable。当前交付基线 V1–V28 已包含全部六列，因此白名单为空——
# 任何缺表/缺列都按 error 处理。需要兼容更旧的库时，在这里加入
# ``"表.列": "缺少原因"``，并在审计 SQL 错误分类为 missing 时才生效。
LEGACY_OPTIONAL: dict[str, str] = {}

# 发布闭环验收至少要扫描到任务结果一份，否则只能算 incomplete，
# 不能因为「查了 0 行、命中 0 条」就宣称隐私验收通过（R02 第 4 条）。
# 「发布快照」不在这里：容量归档本来就没有报告版本（见 RELEASE_SNAPSHOT_FIELD
# 的独立判定），把它当硬覆盖集合会把合法的容量归档整批判成 incomplete（F8）。
RELEASE_COVERAGE_FIELDS = (
    "analysis_jobs.result_metadata",
)
# 发布快照单独判定：有数据则必须 0 命中；完全没有报告版本时判 ok 但显式标注
# 「本次归档不构成发布闭环隐私验收」，不静默通过。
RELEASE_SNAPSHOT_FIELD = "evaluation_report_versions.snapshot_json"
RELEASE_SNAPSHOT_ABSENT = "absent (capacity-only archive)"

# 字段级状态里已有明确失败/不适用原因的，不再重复计入「未覆盖」。
_COVERED_STATUSES = frozenset({"ok", "hit", "error", "not_applicable"})

# 命中签名 URL 痕迹的匹配条件（只 select count(*)，不打印命中内容）。
SIGNED_URL_PATTERNS = (
    "'%X-Amz%'",
    "'%Signature=%'",
    "'%127.0.0.1:9100%'",
    "'%localhost:9000%'",
)

EXIT_OK = 0
EXIT_JUDGEMENT = 2
EXIT_ERROR = 3


def required_check_keys(media_kind: str) -> tuple[str, ...]:
    """Return the mandatory ``checks`` keys for a media kind."""
    kind = (media_kind or "").strip().lower()
    if kind == "audio":
        return AUDIO_CHECK_KEYS
    return VIDEO_CHECK_KEYS


def _media_kind(data: dict, label: str) -> str:
    declared = (data.get("metadata") or {}).get("mediaType")
    if isinstance(declared, str) and declared.strip().lower() in ("video", "audio"):
        return declared.strip().lower()
    if label.lower().endswith("-asr") or "asr" in label.lower():
        return "audio"
    return "video"


def load_run(label: str, root: Path | str) -> dict:
    """Load one measurement summary without ever mutating it.

    Returns a record carrying ``loadStatus`` = ``ok`` | ``missing`` | ``corrupt``
    plus the flattened fields used by the evidence index. Missing or corrupt
    files are reported, never silently turned into an empty success.

    "Corrupt" covers both unparsable JSON and *type* damage in a valid JSON
    document (``metadata``/``counts``/``jobDb``/``resources``/``upload`` that are
    not objects). Type damage is a property of the evidence, not an execution
    error, so it must lead to a judgement (exit 2) with the index still written —
    never to exit 3 or an ``AttributeError`` traceback (F7/A16).
    """
    root = Path(root)
    path = root / label / "summary.json"
    record: dict[str, Any] = {
        "label": label,
        "source": _rel(path),
        "loadStatus": "missing",
        "error": None,
        "mediaKind": None,
        "checks": None,
        "checkKeys": [],
        "counts": {},
    }
    if not path.is_file():
        record["error"] = "summary.json not found"
        return record
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        record["loadStatus"] = "corrupt"
        record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return record
    if not isinstance(data, dict):
        record["loadStatus"] = "corrupt"
        record["error"] = f"summary.json is {type(data).__name__}, expected object"
        return record

    typed, problem = _typed_sections(data)
    if problem is not None:
        record["loadStatus"] = "corrupt"
        record["error"] = problem
        return record
    counts, db, resources = typed["counts"], typed["jobDb"], typed["resources"]

    media_kind = _media_kind(data, label)
    record.update({
        "loadStatus": "ok",
        "mediaKind": media_kind,
        "jobId": typed["upload"].get("jobId") or data.get("jobId"),
        "status": db.get("status"),
        "attempt": db.get("attempt"),
        "createdAt": db.get("created_at"),
        "completedAt": db.get("completed_at"),
        "mediaSeconds": typed["metadata"].get("mediaDurationS"),
        "sampledFrames": db.get("sampled_frame_count"),
        "effectiveSampleIntervalSec": db.get("effective_sample_interval_sec"),
        "truncated": db.get("truncated"),
        "windows": counts.get("windowFeatures"),
        "observations": counts.get("behaviorObservations"),
        "evidenceFrames": counts.get("evidenceFrames"),
        "behaviorModelFrames": counts.get("behaviorModelFrames"),
        "behaviorModelDigestFrames": counts.get("behaviorModelDigestFrames"),
        "speechWindows": counts.get("speechWindows"),
        "transcript": data.get("transcript"),
        "actionModelCheck": (
            {"applicable": False, "reason": "ASR-only run has no action model"}
            if data.get("transcript") is not None
            and not counts.get("behaviorModelFrames")
            else data.get("actionModelCheck")),
        "checks": data.get("checks"),
        "checkKeys": sorted((data.get("checks") or {}).keys())
        if isinstance(data.get("checks"), dict) else [],
        "counts": counts,
        "resources": resources,
    })
    return record


def _typed_sections(data: dict) -> tuple[dict[str, dict], str | None]:
    """Return the five object-valued sections, or a corruption reason.

    A section may be absent/``null`` (treated as an empty object, which the
    downstream judgement already reports as missing data) but must never be a
    scalar or list: ``(data.get("metadata") or {}).get(...)`` would raise
    ``AttributeError`` and be misreported as an execution error.
    """
    sections: dict[str, dict] = {}
    for name in ("metadata", "counts", "jobDb", "resources", "upload"):
        value = data.get(name)
        if value is None:
            sections[name] = {}
            continue
        if not isinstance(value, dict):
            return {}, (
                f"summary.json field {name!r} is {type(value).__name__}, "
                f"expected object")
        sections[name] = value
    return sections, None


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _count(query_fn: Callable[[str], Any], sql: str) -> int:
    rows = query_fn(sql)
    if not rows:
        return 0
    row = rows[0]
    value = row[0] if isinstance(row, (list, tuple)) else row
    if isinstance(value, bool):
        raise ValueError(f"unexpected boolean count: {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text or not text.lstrip("-").isdigit():
        raise ValueError(f"unparsable count: {text[:80]!r}")
    return int(text)


def classify_sql_error(exc: BaseException) -> str:
    """Classify a psql failure as ``missing`` (schema), ``connection`` or ``other``.

    Only ``missing`` may be downgraded to ``not_applicable`` and only for fields
    explicitly listed in ``LEGACY_OPTIONAL``.
    """
    text = f"{exc}".lower()
    missing_markers = (
        "does not exist", "不存在", "undefinedtable", "undefinedcolumn",
        "42p01", "42703", "no such table", "no such column",
    )
    if any(marker in text for marker in missing_markers):
        return "missing"
    connection_markers = (
        "connection refused", "could not connect", "no such container",
        "cannot connect", "connection reset", "timed out", "timeout",
        "无法连接", "连接被拒绝", "超时", "error response from daemon",
        "permission denied", "access denied",
    )
    if any(marker in text for marker in connection_markers):
        return "connection"
    return "other"


def _hit_sql(table: str, column: str) -> str:
    conditions = " or ".join(f"{column} like {pattern}"
                             for pattern in SIGNED_URL_PATTERNS)
    return (f"select count(*) from {table} where {column} is not null "
            f"and ({conditions})")


def audit_signed_urls(query_fn: Callable[[str], Any],
                      legacy_optional: dict[str, str] | None = None) -> dict:
    """Count presigned-URL markers in the six persisted report/job fields.

    Every query is ``select count(*)``; matched content is never selected or
    printed (it may contain signature strings). Each field gets an explicit
    status: ``ok`` | ``hit`` | ``error`` | ``not_applicable`` | ``empty``.
    """
    allow = LEGACY_OPTIONAL if legacy_optional is None else legacy_optional
    fields: dict[str, dict] = {}
    for field in AUDIT_FIELDS:
        table, column = field.split(".", 1)
        record = {
            "status": "error",
            "required": field not in allow,
            "rowsScanned": 0,
            "hits": 0,
            "error": None,
            "note": None,
        }
        fields[field] = record
        try:
            scanned = _count(query_fn,
                             f"select count(*) from {table} where {column} is not null")
            hits = _count(query_fn, _hit_sql(table, column))
        except Exception as exc:  # noqa: BLE001 - classified below, never swallowed
            kind = classify_sql_error(exc)
            message = f"{type(exc).__name__}: {str(exc)[:200]}"
            if kind == "missing" and field in allow:
                record["status"] = "not_applicable"
                record["note"] = f"旧库缺表/缺列，已声明不适用：{allow[field]}"
                record["error"] = None
            else:
                record["status"] = "error"
                record["error"] = f"{kind}: {message}"
            continue
        record["rowsScanned"] = scanned
        record["hits"] = hits
        if hits > 0:
            record["status"] = "hit"
        elif scanned == 0:
            record["status"] = "empty"
            record["note"] = "no rows to audit"
        else:
            record["status"] = "ok"

    statuses = [record["status"] for record in fields.values()]
    rows_total = sum(record["rowsScanned"] for record in fields.values())
    hits_total = sum(record["hits"] for record in fields.values())
    # 「未覆盖」只指「扫了 0 行、且字段本身没有其它明确结论」。hit/error 已有各自
    # 的失败原因，not_applicable 是显式声明不适用（LEGACY_OPTIONAL），都不该再
    # 触发一次 incomplete（F8/A18）。
    uncovered = [
        field for field in RELEASE_COVERAGE_FIELDS
        if fields.get(field, {}).get("rowsScanned", 0) == 0
        and fields.get(field, {}).get("status") not in _COVERED_STATUSES
    ]
    release_snapshot, release_covered = _release_snapshot_state(fields)
    if "error" in statuses:
        status, note = "error", "至少一个字段查询失败，不能视为已审计"
    elif "hit" in statuses:
        status, note = "hit", "存在落库的签名 URL 痕迹"
    elif rows_total == 0:
        status, note = "incomplete", "全部字段扫描行数为 0，没有任何实际审计数据"
    elif uncovered:
        status, note = "incomplete", (
            "任务结果字段未覆盖到实际数据：" + ", ".join(uncovered))
    else:
        status, note = "ok", "六个字段均实际扫描且无签名 URL 痕迹"
    if status == "ok" and not release_covered:
        note += (
            "；发布快照缺失（" + release_snapshot + "），本次归档不构成发布闭环"
            "隐私验收，只覆盖任务结果字段")
    return {
        "status": status,
        "passed": status == "ok",
        "requiredFields": [f for f in AUDIT_FIELDS if f not in allow],
        "legacyOptional": dict(allow),
        "fields": fields,
        # 发布快照单独判定：有数据就必须 0 命中；没有数据时显式标注为缺失，
        # 而不是让容量归档整批变成 incomplete（F8）。
        "releaseSnapshot": release_snapshot,
        "releaseSnapshotCovered": release_covered,
        # 兼容旧键（下游报告仍读取这三项）。
        "hits": {field: record["hits"] for field, record in fields.items()},
        "rowsAudited": {field: record["rowsScanned"] for field, record in fields.items()},
        "rowsScanned": rows_total,
        "hitsTotal": hits_total,
        "error": next((record["error"] for record in fields.values()
                       if record["status"] == "error"), None),
        "note": ("Counts presigned URL markers in report snapshots and job metadata; "
                 "rowsScanned shows the check is not vacuous. " + note),
    }


def _release_snapshot_state(fields: dict) -> tuple[str, bool]:
    """Describe the release-snapshot field independently of the hard coverage set.

    Returns ``(state, covered)``. ``covered`` is true only when the snapshot
    column actually held rows to audit; a capacity-only archive has no report
    versions at all, which is a *documented gap*, not a failure.
    """
    record = fields.get(RELEASE_SNAPSHOT_FIELD) or {}
    status = record.get("status")
    if status in ("ok", "hit"):
        hits = record.get("hits") or 0
        return (f"present ({record.get('rowsScanned')} rows, {hits} hits)", True)
    if status == "empty":
        return RELEASE_SNAPSHOT_ABSENT, False
    if status == "not_applicable":
        return f"not_applicable ({record.get('note') or 'declared'})", False
    if status == "error":
        return f"error: {record.get('error')}", False
    return f"unknown ({status!r})", False


def _judge_run(label: str, run: dict | None, prefix: str) -> tuple[dict, list[str]]:
    """Judge one run. Returns ``(record, reasons)`` with an explicit status."""
    record = {
        "label": label,
        "status": "incomplete",
        "source": None,
        "mediaKind": None,
        "loadStatus": "missing",
        "requiredChecks": [],
        "missingChecks": [],
        "failedChecks": [],
        "invalidChecks": [],
        "unknownKeys": [],
        "detail": "",
    }
    reasons: list[str] = []
    if run is None:
        record["detail"] = "summary.json not found"
        reasons.append(f"{prefix}:missing_run:{label}")
        return record, reasons

    record["source"] = run.get("source")
    record["mediaKind"] = run.get("mediaKind")
    record["loadStatus"] = run.get("loadStatus", "ok")
    if record["loadStatus"] == "missing":
        record["detail"] = run.get("error") or "summary.json not found"
        reasons.append(f"{prefix}:missing_run:{label}")
        return record, reasons
    if record["loadStatus"] == "corrupt":
        record["status"] = "incomplete"
        record["detail"] = run.get("error") or "summary.json corrupt"
        reasons.append(f"{prefix}:corrupt_summary:{label}")
        return record, reasons

    media_kind = run.get("mediaKind") or "video"
    required = list(required_check_keys(media_kind))
    record["requiredChecks"] = required
    checks = run.get("checks")
    if not isinstance(checks, dict) or not checks:
        record["detail"] = "checks missing or empty"
        reasons.append(f"{prefix}:missing_checks:{label}")
        return record, reasons

    missing = [key for key in required if key not in checks]
    failed = [key for key in required if checks.get(key) is False]
    invalid = [key for key in required
               if key in checks and checks.get(key) is not True
               and checks.get(key) is not False]
    unknown = sorted(set(checks) - set(KNOWN_CHECK_KEYS))
    record["missingChecks"] = missing
    record["failedChecks"] = failed
    record["invalidChecks"] = invalid
    record["unknownKeys"] = unknown

    for key in failed:
        reasons.append(f"{prefix}:failed_check:{label}:{key}")
    for key in invalid:
        reasons.append(
            f"{prefix}:invalid_check_value:{label}:{key}={checks.get(key)!r}")
    for key in missing:
        reasons.append(f"{prefix}:missing_check:{label}:{key}")

    if failed or invalid:
        record["status"] = "failed"
        record["detail"] = "required check failed or non-boolean value"
    elif missing:
        record["status"] = "incomplete"
        record["detail"] = "required check key missing"
    else:
        record["status"] = "ok"
        record["detail"] = "all required checks are boolean True"
    return record, reasons


def _judge_faults(faults: dict | None,
                  required_scenarios: Iterable[str]) -> tuple[dict, list[str]]:
    record: dict[str, Any] = {"status": "failed", "scenarios": {},
                              "missing": [], "failed": []}
    reasons: list[str] = []
    for scenario in required_scenarios:
        entry = faults.get(scenario) if isinstance(faults, dict) else None
        if not isinstance(entry, dict):
            record["scenarios"][scenario] = {"status": "missing", "pass": None}
            record["missing"].append(scenario)
            reasons.append(f"fault:missing_scenario:{scenario}")
            continue
        passed = entry.get("pass")
        if passed is True:
            record["scenarios"][scenario] = {"status": "ok", "pass": passed}
        else:
            record["scenarios"][scenario] = {
                "status": "failed",
                "pass": passed,
                "detail": f"pass must be boolean True, got {passed!r}",
            }
            record["failed"].append(scenario)
            reasons.append(f"fault:failed_scenario:{scenario}={passed!r}")
    if record["missing"] or record["failed"]:
        # 任务书要求：必需场景缺失或 pass 非 True 都属于判定失败。
        record["status"] = "failed"
    else:
        record["status"] = "ok"
    return record, reasons


def _judge_audit(audit: dict | None) -> tuple[dict, list[str]]:
    if not isinstance(audit, dict):
        return ({"status": "incomplete", "detail": "audit missing"},
                ["audit:missing"])
    status = audit.get("status")
    reasons: list[str] = []
    if status == "hit":
        # 命中签名串与查询出错都是判定失败，而不是「通过但有点小问题」。
        reasons.append("audit:hit")
        verdict = "failed"
    elif status == "error":
        reasons.append("audit:error")
        verdict = "failed"
    elif status == "incomplete":
        reasons.append("audit:incomplete")
        verdict = "incomplete"
    elif status == "ok":
        verdict = "ok"
    else:
        reasons.append(f"audit:unexpected_status:{status}")
        verdict = "incomplete"
    return ({"status": verdict, "auditStatus": status,
             "hitsTotal": audit.get("hitsTotal"),
             "rowsScanned": audit.get("rowsScanned"),
             "detail": audit.get("note")}, reasons)


def evaluate(runs: dict, concurrent: dict, faults: dict | None, audit: dict | None,
             required_final: Iterable[str] = FINAL,
             required_concurrent: Iterable[str] = CONCURRENT,
             required_scenarios: Iterable[str] = REQUIRED_SCENARIOS) -> dict:
    """Judge the whole deliverable. Pure function: no I/O, no exceptions."""
    reasons: list[str] = []
    per_run: dict[str, dict] = {}
    for label in required_final:
        record, run_reasons = _judge_run(label, runs.get(label), "capacity")
        per_run[label] = record
        reasons.extend(run_reasons)
    for label in required_concurrent:
        record, run_reasons = _judge_run(label, concurrent.get(label), "concurrent")
        per_run[label] = record
        reasons.extend(run_reasons)

    faults_record, fault_reasons = _judge_faults(faults, required_scenarios)
    audit_record, audit_reasons = _judge_audit(audit)
    reasons.extend(fault_reasons)
    reasons.extend(audit_reasons)

    statuses = [record["status"] for record in per_run.values()]
    statuses += [faults_record["status"], audit_record["status"]]
    if "failed" in statuses:
        status = "failed"
    elif "incomplete" in statuses:
        status = "incomplete"
    else:
        status = "ok"
    return {
        "ok": status == "ok",
        "status": status,
        "exitCode": EXIT_OK if status == "ok" else EXIT_JUDGEMENT,
        "reasons": reasons,
        "perRun": per_run,
        "faults": faults_record,
        "audit": audit_record,
    }


def copy_curve(label: str, suffix: str, root: Path | str,
               evidence: Path | str) -> str | None:
    source = Path(root) / label / "resource-curve.png"
    if not source.exists():
        return None
    target = Path(evidence) / f"2026-10-26-capacity-{suffix}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    csv_source = Path(root) / label / "resource-samples.csv"
    if csv_source.exists():
        shutil.copy2(csv_source, target.with_suffix(".csv"))
    return _rel(target)


def latest_fault_summary(root: Path | str) -> tuple[dict | None, str | None, str]:
    """Return ``(scenarios, source, status)`` for the newest fault summary."""
    candidates = sorted(
        (p for p in (Path(root) / "faults").glob("all-*/summary.json")),
        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, None, "missing"
    path = candidates[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, _rel(path), "corrupt"
    if not isinstance(data, dict):
        return None, _rel(path), "corrupt"
    return data, _rel(path), "ok"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _build_capacity_payload(root: Path, runs: dict,
                            concurrent: dict, defects: dict, audit: dict,
                            judgement: dict, curves: dict,
                            concurrency_curves: dict, fault_source: str | None,
                            faults_status: str) -> dict:
    failed_checks = {
        label: record["failedChecks"] + record["invalidChecks"]
        for label, record in judgement["perRun"].items()
        if record["failedChecks"] or record["invalidChecks"]
    }
    generated = _now()
    return {
        # 兼容旧键：capturedAt 保留，语义与 generatedAt 相同。
        "capturedAt": generated,
        "generatedAt": generated,
        "environment": {
            "host": "Windows 11, i9-13900HX, 24C/32T, 16 GB RAM, RTX 4060 Laptop 8 GB",
            "isolation": "mact-cap-postgres:15532, mact-cap-redis:6380, mact-cap-minio:9100; backend:18081; ai-service:18082",
            "note": "Host-native CUDA service; Docker Compose CPU/int8 profile was not measured.",
        },
        "actionCheckpoint": {
            "path": "docs/experiments/runs/scb5-yolo/train/weights/best.pt",
            "sha256": "c356aac1fc8cab0cd478355118ece9bb16441b221b155ddd8b08327c7dd0b738",
            "classes": ["hand-raising", "reading", "writing", "discussing", "bow-head", "turn-head"],
            "labels": ["举手", "阅读", "书写", "讨论", "低头", "转头"],
            "notSupported": ["离座", "站立", "睡觉", "玩手机"],
        },
        "sources": {
            "capacityRoot": _rel(root),
            "runs": {label: run.get("source") for label, run in runs.items()},
            "concurrentRuns": {label: run.get("source")
                               for label, run in concurrent.items()},
            "legacyZeroWindowRuns": {label: run.get("source")
                                     for label, run in defects.items()},
            "faultSummary": fault_source,
            "faultSummaryStatus": faults_status,
        },
        "runs": runs,
        "concurrentRuns": concurrent,
        "legacyZeroWindowRuns": defects,
        "curves": curves,
        "concurrencyCurves": concurrency_curves,
        "failedChecks": failed_checks,
        "signedUrlAudit": audit,
        "judgement": judgement,
        "requiredChecks": {
            label: list(required_check_keys(run.get("mediaKind") or "video"))
            for label, run in list(runs.items()) + list(concurrent.items())
            if run is not None
        },
        "requiredScenarios": list(REQUIRED_SCENARIOS),
        "runIndex": {
            label: {
                "status": record["status"],
                "source": record["source"],
                "loadStatus": record["loadStatus"],
                "mediaKind": record["mediaKind"],
                "requiredChecks": record["requiredChecks"],
                "missingChecks": record["missingChecks"],
                "failedChecks": record["failedChecks"],
                "invalidChecks": record["invalidChecks"],
                "unknownKeys": record["unknownKeys"],
                "detail": record["detail"],
            }
            for label, record in judgement["perRun"].items()
        },
        "notes": _capacity_notes(audit),
    }


def _capacity_notes(audit: dict | None) -> list[str]:
    """Stable note list; adds an explicit line when the release snapshot is absent."""
    notes = [
        "80/120-minute workloads are repeated concatenations of the same 40-minute 480p lesson; they are capacity workloads, not independent lessons.",
        "The 40/80/120 runs are truncated to the adaptive cap (10s effective interval, 5-6 samples/window), not the requested 2s interval; this is explicit in sampledFrames/effectiveSampleIntervalSec.",
        "Evidence-frame counts are selected images, not action events or student counts.",
        "subjectId is frame-local (target-N), not a name or cross-frame identity.",
        "ASR is faster-whisper small/float16 on CUDA; CER/accuracy was not measured here.",
        "Raw summary.json/resource-samples.csv are never rewritten; this index only records their paths and derived status.",
    ]
    if isinstance(audit, dict) and audit.get("releaseSnapshotCovered") is False:
        notes.append(
            "发布快照（evaluation_report_versions.snapshot_json）本次未覆盖："
            f"{audit.get('releaseSnapshot')}。本次归档不构成发布闭环隐私验收，"
            "只证明任务结果字段（analysis_jobs.result_metadata）无签名 URL 痕迹。")
    return notes


def _build_fault_payload(fault: dict | None, fault_source: str | None,
                         fault_status: str, judgement: dict,
                         generated: str) -> dict:
    scenarios = {k: v for k, v in (fault or {}).items() if k != "manifest"}
    return {
        "capturedAt": generated,
        "generatedAt": generated,
        "source": fault_source,
        "status": fault_status,
        "scenarios": scenarios,
        "manifest": (fault or {}).get("manifest"),
        "notes": (fault or {}).get("notes"),
        "judgement": judgement["faults"],
        "allRequiredScenariosPassed": judgement["faults"]["status"] == "ok",
    }


class _ArgumentParser(argparse.ArgumentParser):
    """argparse 默认以退出码 2 结束；本脚本保留 2 给「判定失败」，参数错误必须是 3。"""

    def error(self, message: str):  # noqa: D401 - argparse contract
        raise ValueError(message)


def main(argv: list[str] | None = None,
         query_fn: Callable[[str], Any] | None = None) -> int:
    parser = _ArgumentParser(
        description="Aggregate capacity/fault evidence with an explicit verdict.")
    parser.add_argument("--root", default=str(ISO_DIR),
                        help="capacity archive root (default: data/tmp/capacity-2026-10-26)")
    parser.add_argument("--evidence", default=str(EVIDENCE),
                        help="evidence output directory (default: docs/reviews/evidence)")
    try:
        args = parser.parse_args(argv)
    except ValueError as exc:
        print(json.dumps({
            "status": "error",
            "exitCode": EXIT_ERROR,
            "error": f"invalid arguments: {exc}",
        }, ensure_ascii=False, indent=2))
        return EXIT_ERROR

    root = Path(args.root)
    evidence = Path(args.evidence)
    auditor = query_fn or psql
    try:
        if not root.is_dir():
            raise FileNotFoundError(f"capacity root not found: {root}")
        evidence.mkdir(parents=True, exist_ok=True)
        runs = {label: load_run(label, root) for label in FINAL}
        concurrent = {label: load_run(label, root) for label in CONCURRENT}
        defects = {label: load_run(label, root) for label in DEFECT}
        audit = audit_signed_urls(auditor)
        fault, fault_source, fault_status = latest_fault_summary(root)
        judgement = evaluate(runs, concurrent, fault, audit)

        curves = {label: copy_curve(label, label, root, evidence)
                  for label, run in runs.items() if run.get("loadStatus") == "ok"}
        concurrency_curves = {
            label: copy_curve(label, label, root, evidence)
            for label, run in concurrent.items() if run.get("loadStatus") == "ok"}

        payload = _build_capacity_payload(
            root, runs, concurrent, defects, audit, judgement,
            curves, concurrency_curves, fault_source, fault_status)
        out = evidence / "2026-10-26-long-session-capacity.json"
        write_json(out, payload)
        fault_out = evidence / "2026-10-26-fault-injection.json"
        write_json(fault_out, _build_fault_payload(
            fault, fault_source, fault_status, judgement, payload["generatedAt"]))
    except Exception as exc:  # noqa: BLE001 - execution error must exit 3, not be silent
        print(json.dumps({
            "status": "error",
            "exitCode": EXIT_ERROR,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, indent=2))
        return EXIT_ERROR

    print(json.dumps({
        "capacity": _rel(out),
        "faultInjection": _rel(fault_out),
        "status": judgement["status"],
        "exitCode": judgement["exitCode"],
        "reasons": judgement["reasons"],
        "runs": {label: record["status"]
                 for label, record in judgement["perRun"].items()},
        "faults": judgement["faults"]["status"],
        "signedUrlAudit": {
            "status": audit["status"],
            "rowsScanned": audit["rowsScanned"],
            "hitsTotal": audit["hitsTotal"],
        },
        "curves": curves,
        "concurrencyCurves": concurrency_curves,
        "faultSummary": fault_source,
    }, ensure_ascii=False, indent=2))
    return judgement["exitCode"]


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
