"""@file 导出工具导出容量/故障证据的判定逻辑测试（不连接真实数据库）。

覆盖 R01（退出码真实反映成功/失败）与 R02（签名 URL 审计不能误报通过）。
所有数据都在临时目录里构造，数据库查询用假 ``query_fn`` 注入。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

OPS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR))

import export_capacity_evidence as exporter  # noqa: E402

ALL_CHECK_KEYS = [
    "terminalSucceeded",
    "videoProducedWindows",
    "videoProducedObservations",
    "videoWindowCoverage",
    "actionModelMatchesCheckpoint",
    "actionModelDigestMatches",
    "behaviorModelFramesPresent",
    "behaviorModelFramesCarryExpectedDigest",
    "audioTranscriptPresent",
    "audioSpeechWindowsPresent",
]


# ---------------------------------------------------------------------------
# 夹具构造
# ---------------------------------------------------------------------------

def write_summary(root: Path, label: str, *, media_type: str = "video",
                  checks=None, counts=None, terminal: str = "SUCCEEDED") -> Path:
    """写一份与真实 driver 结构一致的 summary.json。``checks=None`` 表示不写该键。"""
    run_dir = root / label
    run_dir.mkdir(parents=True, exist_ok=True)
    if checks is None:
        checks = {key: True for key in ALL_CHECK_KEYS}
    payload = {
        "metadata": {"label": label, "mediaType": media_type, "mediaDurationS": 2400},
        "upload": {"jobId": f"job-{label}"},
        "jobDb": {"status": terminal, "attempt": "1"},
        "transcript": {"chars": 100} if media_type == "audio" else None,
        "counts": counts or {"windowFeatures": 40, "behaviorObservations": 41},
        "checks": checks,
    }
    path = run_dir / "summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def write_fault_summary(root: Path, *, passes=None) -> Path:
    passes = passes or {name: True for name in exporter.REQUIRED_SCENARIOS}
    payload = {name: {"scenario": name, "pass": value}
               for name, value in passes.items()}
    payload["manifest"] = {"capturedAt": "2026-10-26T03:00:00"}
    fault_dir = root / "faults" / "all-final-test"
    fault_dir.mkdir(parents=True, exist_ok=True)
    path = fault_dir / "summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def make_root(tmp_path: Path, *, media_types=None, checks=None,
              fault_passes=None) -> Path:
    """构造 A1 判定所需的五个容量运行 + 两个并发运行 + 故障汇总。"""
    root = tmp_path / "capacity"
    root.mkdir(parents=True, exist_ok=True)
    media_types = media_types or {"final-40min-asr": "audio"}
    for label in exporter.FINAL + exporter.CONCURRENT:
        write_summary(root, label, media_type=media_types.get(label, "video"),
                      checks=checks if checks is not None else None)
    write_fault_summary(root, passes=fault_passes)
    return root


def fake_query(*, hits: dict | None = None, rows: dict | None = None,
               raise_for: set[str] | None = None, default_rows: int = 5):
    """构造假的 psql 查询函数：按 SQL 中的表字段返回 (扫描行数, 命中数)。"""
    hits = hits or {}
    rows = rows or {}
    raise_for = raise_for or set()

    def query(sql: str):
        for field in exporter.AUDIT_FIELDS:
            table, column = field.split(".", 1)
            if f"from {table}" not in sql or column not in sql:
                continue
            if field in raise_for:
                raise_for.pop()
                raise RuntimeError("psql failed: ERROR: relation does not exist")
            if sql.strip().startswith("select count(*)") and "like" in sql:
                return [[str(hits.get(field, 0))]]
            return [[str(rows.get(field, default_rows))]]
        raise AssertionError(f"unexpected SQL: {sql}")

    return query


def audit_for(tmp_path: Path, **kwargs) -> dict:
    return exporter.audit_signed_urls(fake_query(**kwargs))


def runs_from(root: Path, labels) -> dict:
    return {label: exporter.load_run(label, root) for label in labels}


def judge(root: Path, audit: dict | None, **kwargs) -> dict:
    runs = runs_from(root, exporter.FINAL)
    concurrent = runs_from(root, exporter.CONCURRENT)
    faults = None
    if audit is None:
        audit = audit_for(root)
    fault, _source, status = exporter.latest_fault_summary(root)
    if status == "ok":
        faults = fault
    return exporter.evaluate(runs, concurrent, faults, audit, **kwargs)


# ---------------------------------------------------------------------------
# R01 判定
# ---------------------------------------------------------------------------

def test_all_checks_pass_returns_exit_zero(tmp_path):
    root = make_root(tmp_path)
    result = judge(root, None)
    assert result["status"] == "ok"
    assert result["exitCode"] == 0
    assert result["reasons"] == []
    assert all(record["status"] == "ok" for record in result["perRun"].values())


def test_single_required_check_failure_fails(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-80min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"]["videoWindowCoverage"] = False
    path.write_text(json.dumps(data), encoding="utf-8")

    result = judge(root, None)
    assert result["status"] == "failed"
    assert result["exitCode"] == 2
    record = result["perRun"]["final-80min-480p"]
    assert record["failedChecks"] == ["videoWindowCoverage"]
    assert any("failed_check:final-80min-480p:videoWindowCoverage" in reason
               for reason in result["reasons"])


def test_missing_run_is_incomplete(tmp_path):
    root = make_root(tmp_path)
    (root / "final-120min-480p" / "summary.json").unlink()

    result = judge(root, None)
    assert result["status"] == "incomplete"
    assert result["exitCode"] == 2
    assert result["perRun"]["final-120min-480p"]["status"] == "incomplete"
    assert "capacity:missing_run:final-120min-480p" in result["reasons"]


def test_missing_checks_key_is_incomplete(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-40min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("checks")
    path.write_text(json.dumps(data), encoding="utf-8")

    result = judge(root, None)
    assert result["perRun"]["final-40min-480p"]["status"] == "incomplete"
    assert "capacity:missing_checks:final-40min-480p" in result["reasons"]
    assert result["exitCode"] == 2


def test_empty_checks_is_incomplete(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-41min-1080p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"] = {}
    path.write_text(json.dumps(data), encoding="utf-8")

    result = judge(root, None)
    assert result["perRun"]["final-41min-1080p"]["status"] == "incomplete"
    assert "capacity:missing_checks:final-41min-1080p" in result["reasons"]
    assert result["exitCode"] == 2


def test_corrupt_json_is_incomplete(tmp_path):
    root = make_root(tmp_path)
    (root / "final-40min-asr" / "summary.json").write_text("{not json", encoding="utf-8")

    run = exporter.load_run("final-40min-asr", root)
    assert run["loadStatus"] == "corrupt"
    result = judge(root, None)
    assert result["perRun"]["final-40min-asr"]["status"] == "incomplete"
    assert "capacity:corrupt_summary:final-40min-asr" in result["reasons"]
    assert result["exitCode"] == 2


def test_missing_required_check_key_cannot_be_replaced_by_unknown_key(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-40min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"].pop("videoProducedWindows")
    data["checks"]["forgedAllGood"] = True
    path.write_text(json.dumps(data), encoding="utf-8")

    result = judge(root, None)
    record = result["perRun"]["final-40min-480p"]
    assert record["status"] == "incomplete"
    assert record["missingChecks"] == ["videoProducedWindows"]
    assert record["unknownKeys"] == ["forgedAllGood"]
    assert any("missing_check:final-40min-480p:videoProducedWindows" in reason
               for reason in result["reasons"])


def test_string_false_is_not_a_pass(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-40min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"]["videoProducedObservations"] = "false"
    path.write_text(json.dumps(data), encoding="utf-8")

    result = judge(root, None)
    record = result["perRun"]["final-40min-480p"]
    assert record["status"] == "failed"
    assert record["invalidChecks"] == ["videoProducedObservations"]
    assert result["exitCode"] == 2


@pytest.mark.parametrize("value", ["true", 1, 0, None])
def test_non_boolean_check_values_are_treated_as_failure(tmp_path, value):
    root = make_root(tmp_path)
    path = root / "final-40min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"]["terminalSucceeded"] = value
    path.write_text(json.dumps(data), encoding="utf-8")

    result = judge(root, None)
    record = result["perRun"]["final-40min-480p"]
    assert record["status"] == "failed"
    assert record["invalidChecks"] == ["terminalSucceeded"]
    assert result["exitCode"] == 2


def test_audio_run_does_not_require_video_keys(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-40min-asr" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"] = {key: True for key in exporter.AUDIO_CHECK_KEYS}
    path.write_text(json.dumps(data), encoding="utf-8")

    result = judge(root, None)
    assert result["perRun"]["final-40min-asr"]["status"] == "ok"
    assert exporter.required_check_keys("audio") == exporter.AUDIO_CHECK_KEYS
    assert "videoProducedWindows" not in exporter.required_check_keys("audio")


def test_video_run_requires_video_keys(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-40min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"] = {key: True for key in exporter.AUDIO_CHECK_KEYS}
    path.write_text(json.dumps(data), encoding="utf-8")

    result = judge(root, None)
    assert result["perRun"]["final-40min-480p"]["status"] == "incomplete"
    assert "videoWindowCoverage" in result["perRun"]["final-40min-480p"]["missingChecks"]


def test_missing_concurrent_run_is_incomplete(tmp_path):
    root = make_root(tmp_path)
    (root / "final-concurrent-b-40min" / "summary.json").unlink()

    result = judge(root, None)
    assert result["perRun"]["final-concurrent-b-40min"]["status"] == "incomplete"
    assert "concurrent:missing_run:final-concurrent-b-40min" in result["reasons"]
    assert result["exitCode"] == 2


def test_failed_required_fault_scenario_fails(tmp_path):
    root = make_root(tmp_path, fault_passes={
        "redis-outage": True,
        "redis-loss": True,
        "group-loss": False,
        "worker-kill": True,
        "callback-down": True,
    })
    result = judge(root, None)
    assert result["faults"]["status"] == "failed"
    assert result["faults"]["failed"] == ["group-loss"]
    assert "fault:failed_scenario:group-loss=False" in result["reasons"]
    assert result["exitCode"] == 2


def test_missing_required_fault_scenario_fails(tmp_path):
    """任务书：故障必需场景缺失 ⇒ 判定失败，而不是勉强算通过。"""
    root = make_root(tmp_path, fault_passes={
        "redis-outage": True,
        "redis-loss": True,
        "worker-kill": True,
        "callback-down": True,
    })
    result = judge(root, None)
    assert result["faults"]["status"] == "failed"
    assert result["faults"]["missing"] == ["group-loss"]
    assert result["status"] == "failed"
    assert "fault:missing_scenario:group-loss" in result["reasons"]
    assert result["exitCode"] == 2


def test_fault_pass_must_be_boolean_true(tmp_path):
    root = make_root(tmp_path, fault_passes={
        "redis-outage": "true",
        "redis-loss": True,
        "group-loss": True,
        "worker-kill": True,
        "callback-down": 1,
    })
    result = judge(root, None)
    assert result["faults"]["status"] == "failed"
    assert set(result["faults"]["failed"]) == {"redis-outage", "callback-down"}


# ---------------------------------------------------------------------------
# R02 签名 URL 审计
# ---------------------------------------------------------------------------

def test_audit_ok_when_all_fields_scanned_without_hits(tmp_path):
    audit = audit_for(tmp_path)
    assert audit["status"] == "ok"
    assert audit["passed"] is True
    assert audit["hitsTotal"] == 0
    assert all(record["status"] == "ok" for record in audit["fields"].values())


def test_audit_hit_fails_the_export(tmp_path):
    root = make_root(tmp_path)
    audit = audit_for(tmp_path, hits={"analysis_jobs.result_metadata": 2})
    assert audit["status"] == "hit"
    assert audit["passed"] is False
    assert audit["fields"]["analysis_jobs.result_metadata"]["status"] == "hit"

    result = judge(root, audit)
    assert result["status"] == "failed"
    assert "audit:hit" in result["reasons"]
    assert result["exitCode"] == 2


def test_audit_database_unreachable_is_error_not_not_applicable(tmp_path):
    def broken(_sql):
        raise RuntimeError("Error response from daemon: No such container: mact-cap-postgres")

    audit = exporter.audit_signed_urls(broken)
    assert audit["status"] == "error"
    assert audit["passed"] is False
    assert all(record["status"] == "error" for record in audit["fields"].values())
    assert audit["fields"]["evaluation_reports.raw_report"]["error"]

    root = make_root(tmp_path)
    result = judge(root, audit)
    assert result["status"] == "failed"
    assert "audit:error" in result["reasons"]
    assert result["exitCode"] == 2


def test_audit_required_column_missing_is_error(tmp_path):
    def broken(_sql):
        raise RuntimeError(
            'psql failed: ERROR: column "result_metadata" does not exist')

    audit = exporter.audit_signed_urls(broken)
    assert audit["status"] == "error"
    assert audit["fields"]["analysis_jobs.result_metadata"]["status"] == "error"
    assert "missing" in audit["fields"]["analysis_jobs.result_metadata"]["error"]


def test_audit_legacy_optional_field_can_be_not_applicable(tmp_path):
    legacy = {"evaluation_reports.suggestions_json": "V1 建表后该列在旧快照库缺失"}

    def query(sql: str):
        if "suggestions_json" in sql:
            raise RuntimeError('psql failed: ERROR: column "suggestions_json" does not exist')
        return [["0"]] if "like" in sql else [["3"]]

    audit = exporter.audit_signed_urls(query, legacy_optional=legacy)
    record = audit["fields"]["evaluation_reports.suggestions_json"]
    assert record["status"] == "not_applicable"
    assert record["required"] is False
    assert record["error"] is None
    # 其余字段仍有实际数据 → 整体可通过，且兼容旧键结构保持。
    assert audit["status"] == "ok"
    assert audit["legacyOptional"] == legacy

    # 但若把同一字段移出白名单，缺列必须变成 error。
    strict = exporter.audit_signed_urls(query, legacy_optional={})
    assert strict["fields"]["evaluation_reports.suggestions_json"]["status"] == "error"
    assert strict["status"] == "error"


def test_audit_without_any_rows_is_incomplete(tmp_path):
    audit = audit_for(tmp_path, default_rows=0)
    assert audit["status"] == "incomplete"
    assert audit["passed"] is False
    assert "没有任何实际审计数据" in audit["note"]

    root = make_root(tmp_path)
    result = judge(root, audit)
    assert result["status"] == "incomplete"
    assert "audit:incomplete" in result["reasons"]
    assert result["exitCode"] == 2


def test_audit_skips_release_coverage_fields_is_incomplete(tmp_path):
    """只扫到次要字段、任务结果 0 行时不得判 passed。"""
    rows = {"evaluation_report_versions.snapshot_json": 0,
            "analysis_jobs.result_metadata": 0}
    audit = audit_for(tmp_path, rows=rows)
    assert audit["status"] == "incomplete"
    assert audit["rowsScanned"] > 0
    assert "任务结果字段未覆盖到实际数据" in audit["note"]
    assert "analysis_jobs.result_metadata" in audit["note"]


def test_audit_not_applicable_field_does_not_make_it_incomplete(tmp_path):
    """F8/A18: a declared not_applicable field is not an uncovered field."""
    legacy = {"analysis_jobs.result_metadata": "旧库缺该列"}

    def query(sql: str):
        if "result_metadata" in sql:
            raise RuntimeError('psql failed: ERROR: column "result_metadata" does not exist')
        if "from evaluation_report_versions" in sql:
            return [["0"]] if "like" in sql else [["0"]]
        return [["0"]] if "like" in sql else [["5"]]

    audit = exporter.audit_signed_urls(query, legacy_optional=legacy)
    record = audit["fields"]["analysis_jobs.result_metadata"]
    assert record["status"] == "not_applicable"
    assert record["required"] is False
    # 该字段已是显式 not_applicable，不再作为「未覆盖」重复判 incomplete。
    assert "未覆盖到实际数据" not in audit["note"]
    assert audit["status"] == "ok"
    assert audit["releaseSnapshotCovered"] is False


@pytest.mark.parametrize("status", ["hit", "error"])
def test_audit_hit_or_error_field_is_not_reported_as_uncovered(tmp_path, status):
    """hit/error 已有各自失败原因，不应再被当作「未覆盖」。"""
    if status == "hit":
        audit = audit_for(tmp_path, hits={"analysis_jobs.result_metadata": 1})
        assert audit["status"] == "hit"
    else:
        def query(sql: str):
            if "result_metadata" in sql:
                raise RuntimeError("psql failed: ERROR: connection refused")
            return [["0"]]

        audit = exporter.audit_signed_urls(query)
        assert audit["status"] == "error"
    assert "未覆盖到实际数据" not in audit["note"]


def test_release_snapshot_absent_is_ok_but_explicitly_flagged(tmp_path):
    """F8: capacity-only archives have no report versions; that must be ok+flagged."""
    rows = {"evaluation_report_versions.snapshot_json": 0}
    audit = audit_for(tmp_path, rows=rows)

    assert audit["status"] == "ok"
    assert audit["releaseSnapshotCovered"] is False
    assert audit["releaseSnapshot"] == exporter.RELEASE_SNAPSHOT_ABSENT
    assert "不构成发布闭环隐私验收" in audit["note"]


def test_release_snapshot_present_is_covered_and_must_have_no_hits(tmp_path):
    rows = {"evaluation_report_versions.snapshot_json": 7}
    audit = audit_for(tmp_path, rows=rows)

    assert audit["status"] == "ok"
    assert audit["releaseSnapshotCovered"] is True
    assert "7 rows" in audit["releaseSnapshot"]

    hit = audit_for(tmp_path, rows=rows,
                    hits={"evaluation_report_versions.snapshot_json": 1})
    assert hit["status"] == "hit"
    assert hit["passed"] is False


def test_main_index_records_release_snapshot_absence_in_notes(tmp_path):
    root = make_root(tmp_path)
    evidence = tmp_path / "evidence"
    rows = {"evaluation_report_versions.snapshot_json": 0}
    assert exporter.main(["--root", str(root), "--evidence", str(evidence)],
                         query_fn=fake_query(rows=rows)) == 0

    payload = json.loads(
        (evidence / "2026-10-26-long-session-capacity.json").read_text(encoding="utf-8"))
    assert payload["signedUrlAudit"]["releaseSnapshotCovered"] is False
    assert payload["signedUrlAudit"]["releaseSnapshot"] == exporter.RELEASE_SNAPSHOT_ABSENT
    assert any("不构成发布闭环隐私验收" in note for note in payload["notes"])


def test_main_still_requires_job_result_metadata_rows(tmp_path):
    """任务结果字段仍是硬覆盖：0 行 ⇒ incomplete/exit 2，与发布快照无关。"""
    root = make_root(tmp_path)
    evidence = tmp_path / "evidence"
    rows = {"evaluation_report_versions.snapshot_json": 0,
            "analysis_jobs.result_metadata": 0}
    assert exporter.main(["--root", str(root), "--evidence", str(evidence)],
                         query_fn=fake_query(rows=rows)) == 2
    payload = json.loads(
        (evidence / "2026-10-26-long-session-capacity.json").read_text(encoding="utf-8"))
    assert payload["signedUrlAudit"]["status"] == "incomplete"
    assert "analysis_jobs.result_metadata" in payload["signedUrlAudit"]["note"]


# ---------------------------------------------------------------------------
# F7 类型损坏的 summary.json 必须走判定（exit 2）并仍然写索引
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["metadata", "jobDb", "counts", "resources", "upload"])
def test_type_damaged_summary_is_corrupt_not_an_execution_error(tmp_path, field):
    root = make_root(tmp_path)
    path = root / "final-40min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = "not-an-object"
    path.write_text(json.dumps(data), encoding="utf-8")

    run = exporter.load_run("final-40min-480p", root)
    assert run["loadStatus"] == "corrupt"
    assert field in run["error"]

    result = judge(root, None)
    assert result["perRun"]["final-40min-480p"]["status"] == "incomplete"
    assert "capacity:corrupt_summary:final-40min-480p" in result["reasons"]
    assert result["exitCode"] == 2


def test_type_damaged_summary_still_writes_the_index(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-40min-asr" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["metadata"] = ["wrong", "type"]
    path.write_text(json.dumps(data), encoding="utf-8")
    evidence = tmp_path / "evidence"

    code = exporter.main(["--root", str(root), "--evidence", str(evidence)],
                         query_fn=fake_query())
    assert code == 2
    payload = json.loads(
        (evidence / "2026-10-26-long-session-capacity.json").read_text(encoding="utf-8"))
    record = payload["runIndex"]["final-40min-asr"]
    assert record["status"] == "incomplete"
    assert record["loadStatus"] == "corrupt"
    assert "metadata" in record["detail"]
    assert payload["sources"]["runs"]["final-40min-asr"].endswith("summary.json")


def test_audit_sql_never_selects_row_content():
    seen = []

    def query(sql: str):
        seen.append(sql)
        return [["0"]]

    exporter.audit_signed_urls(query)
    assert seen, "audit must issue queries"
    for sql in seen:
        assert sql.strip().lower().startswith("select count(*)")
        assert "select *" not in sql.lower()


def test_audit_accepts_plain_scalar_count():
    """psql 的 -t 输出经封装后可能是标量或单元素列表，两种都要能解析。"""
    def query(sql: str):
        return ["0"] if "like" in sql else ["7"]

    audit = exporter.audit_signed_urls(query)
    assert audit["status"] == "ok"
    assert audit["fields"]["evaluation_reports.raw_report"]["rowsScanned"] == 7
    assert audit["fields"]["evaluation_reports.raw_report"]["hits"] == 0


def test_corrupt_fault_summary_fails(tmp_path):
    root = make_root(tmp_path)
    (root / "faults" / "all-final-test" / "summary.json").write_text(
        "{broken", encoding="utf-8")
    fault, source, status = exporter.latest_fault_summary(root)
    assert status == "corrupt"
    assert fault is None
    assert source.endswith("summary.json")

    result = judge(root, None)
    assert result["faults"]["status"] == "failed"
    assert result["faults"]["missing"] == list(exporter.REQUIRED_SCENARIOS)
    assert result["exitCode"] == 2


def test_missing_fault_summary_fails(tmp_path):
    root = make_root(tmp_path)
    for path in (root / "faults").rglob("summary.json"):
        path.unlink()
    result = judge(root, None)
    assert result["faults"]["missing"] == list(exporter.REQUIRED_SCENARIOS)
    assert result["status"] == "failed"


def test_missing_audit_object_is_incomplete(tmp_path):
    root = make_root(tmp_path)
    result = judge(root, None)
    assert result["audit"]["status"] == "ok"
    missing = exporter.evaluate(runs_from(root, exporter.FINAL),
                                runs_from(root, exporter.CONCURRENT),
                                exporter.latest_fault_summary(root)[0], None)
    assert missing["audit"]["status"] == "incomplete"
    assert "audit:missing" in missing["reasons"]


def test_main_index_carries_status_and_sources_for_every_field(tmp_path):
    root = make_root(tmp_path)
    evidence = tmp_path / "evidence"
    assert exporter.main(["--root", str(root), "--evidence", str(evidence)],
                         query_fn=fake_query()) == 0
    payload = json.loads(
        (evidence / "2026-10-26-long-session-capacity.json").read_text(encoding="utf-8"))

    assert "status" in payload["signedUrlAudit"]
    assert set(payload["signedUrlAudit"]["fields"]) == set(exporter.AUDIT_FIELDS)
    for record in payload["signedUrlAudit"]["fields"].values():
        assert {"status", "required", "rowsScanned", "hits", "error", "note"} <= set(record)
    for label, record in payload["runIndex"].items():
        assert record["status"] == "ok"
        assert record["source"].endswith(f"{label}\\summary.json") or \
            record["source"].endswith(f"{label}/summary.json")
        assert record["requiredChecks"]
    assert payload["judgement"]["perRun"]["final-40min-asr"]["mediaKind"] == "audio"
    assert "generatedAt" in payload["sources"] or payload.get("generatedAt")


def test_main_writes_fault_index_even_when_scenarios_missing(tmp_path):
    root = make_root(tmp_path)
    for path in (root / "faults").rglob("summary.json"):
        path.unlink()
    evidence = tmp_path / "evidence"
    assert exporter.main(["--root", str(root), "--evidence", str(evidence)],
                         query_fn=fake_query()) == 2
    fault_payload = json.loads(
        (evidence / "2026-10-26-fault-injection.json").read_text(encoding="utf-8"))
    assert fault_payload["status"] == "missing"
    assert fault_payload["allRequiredScenariosPassed"] is False
    assert fault_payload["judgement"]["missing"] == list(exporter.REQUIRED_SCENARIOS)


def test_legacy_zero_window_runs_do_not_participate_in_judgement(tmp_path):
    """DEFECT 运行是历史缺陷证据：缺失不得影响判定，存在也不得替代必检运行。"""
    root = make_root(tmp_path)
    assert not (root / "80min-current-code").exists()
    result = judge(root, None)
    assert result["status"] == "ok"
    assert all(label not in result["perRun"] for label in exporter.DEFECT)

    write_summary(root, "80min-current-code",
                  checks={key: False for key in ALL_CHECK_KEYS})
    still_ok = judge(root, None)
    assert still_ok["status"] == "ok"
    assert all(label not in still_ok["perRun"] for label in exporter.DEFECT)


# ---------------------------------------------------------------------------
# main()/CLI 与真实进程退出码
# ---------------------------------------------------------------------------

def test_main_writes_index_and_returns_zero_on_full_pass(tmp_path):
    root = make_root(tmp_path)
    evidence = tmp_path / "evidence"
    code = exporter.main(["--root", str(root), "--evidence", str(evidence)],
                         query_fn=fake_query())
    assert code == 0
    payload = json.loads(
        (evidence / "2026-10-26-long-session-capacity.json").read_text(encoding="utf-8"))
    assert payload["judgement"]["status"] == "ok"
    assert payload["judgement"]["exitCode"] == 0
    assert payload["failedChecks"] == {}
    assert payload["generatedAt"] and payload["capturedAt"]
    assert payload["sources"]["capacityRoot"].endswith("capacity")
    assert all(record["source"] for record in payload["runs"].values())
    fault_payload = json.loads(
        (evidence / "2026-10-26-fault-injection.json").read_text(encoding="utf-8"))
    assert fault_payload["allRequiredScenariosPassed"] is True
    assert fault_payload["scenarios"]["redis-outage"]["pass"] is True

    # 原始 summary.json 未被改写。
    raw = json.loads((root / "final-40min-480p" / "summary.json").read_text(encoding="utf-8"))
    assert raw["checks"]["terminalSucceeded"] is True


def test_main_returns_two_and_still_writes_reasons(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-120min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"]["terminalSucceeded"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    evidence = tmp_path / "evidence"

    code = exporter.main(["--root", str(root), "--evidence", str(evidence)],
                         query_fn=fake_query())
    assert code == 2
    payload = json.loads(
        (evidence / "2026-10-26-long-session-capacity.json").read_text(encoding="utf-8"))
    assert payload["judgement"]["status"] == "failed"
    assert payload["failedChecks"] == {"final-120min-480p": ["terminalSucceeded"]}


def test_main_missing_root_returns_three(tmp_path):
    code = exporter.main(["--root", str(tmp_path / "nope"),
                          "--evidence", str(tmp_path / "evidence")],
                         query_fn=fake_query())
    assert code == 3


def test_main_db_error_returns_two_not_zero(tmp_path):
    root = make_root(tmp_path)
    evidence = tmp_path / "evidence"

    def broken(_sql):
        raise RuntimeError("Error response from daemon: No such container")

    code = exporter.main(["--root", str(root), "--evidence", str(evidence)],
                         query_fn=broken)
    assert code == 2
    payload = json.loads(
        (evidence / "2026-10-26-long-session-capacity.json").read_text(encoding="utf-8"))
    assert payload["signedUrlAudit"]["status"] == "error"
    assert payload["judgement"]["status"] == "failed"


def test_real_process_exit_code_zero(tmp_path):
    """真实子进程：注入假查询后运行 main，断言操作系统退出码。"""
    root = make_root(tmp_path)
    evidence = tmp_path / "evidence"
    runner = tmp_path / "runner_ok.py"
    # 假查询：like 查询返回 0 命中，扫描查询返回 1 行（非空数据）。
    runner.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(OPS_DIR)!r})
        import export_capacity_evidence as mod
        def query(sql):
            return [["0"]] if "like" in sql else [["1"]]
        sys.exit(mod.main(["--root", {str(root)!r}, "--evidence", {str(evidence)!r}],
                          query_fn=query))
    """), encoding="utf-8")

    completed = subprocess.run([sys.executable, "-I", str(runner)],
                               capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr
    assert '"status": "ok"' in completed.stdout


def test_real_process_exit_code_two_for_failed_check(tmp_path):
    root = make_root(tmp_path)
    path = root / "final-40min-480p" / "summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"]["videoProducedWindows"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    runner = tmp_path / "runner_fail.py"
    runner.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(OPS_DIR)!r})
        import export_capacity_evidence as mod
        def query(sql):
            return [["0"]] if "like" in sql else [["1"]]
        sys.exit(mod.main(["--root", {str(root)!r},
                          "--evidence", {str(tmp_path / "evidence")!r}],
                          query_fn=query))
    """), encoding="utf-8")

    completed = subprocess.run([sys.executable, "-I", str(runner)],
                               capture_output=True, text=True, timeout=180)
    assert completed.returncode == 2, completed.stderr
    assert "failed_check:final-40min-480p:videoProducedWindows" in completed.stdout


def test_bad_argument_returns_three_not_two(tmp_path):
    """argparse 默认用 2 表示参数错误；本脚本 2 表示判定失败，必须区分。"""
    assert exporter.main(["--no-such-flag"], query_fn=fake_query()) == 3


def test_real_script_entrypoint_returns_three_for_bad_root(tmp_path):
    """直接跑脚本本体（未注入查询）：根目录不存在时必须 exit 3，且不是 0。"""
    completed = subprocess.run(
        [sys.executable, "-I", str(OPS_DIR / "export_capacity_evidence.py"),
         "--root", str(tmp_path / "definitely-missing"),
         "--evidence", str(tmp_path / "evidence")],
        capture_output=True, text=True, timeout=180)
    assert completed.returncode == 3, completed.stderr + completed.stdout
    assert '"status": "error"' in completed.stdout
