#!/usr/bin/env python
"""Unit tests for ``scripts/ops/recovery_faults.py`` (no real environment).

These tests only exercise the pure parts of the harness: target validation,
assertion evaluation, summary merging, JSON shape and timeline ordering. No
container, database, MinIO or AI process is started, and nothing is injected.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recovery_faults as rf  # noqa: E402


# --- isolation guard ------------------------------------------------------
def test_assert_isolated_target_accepts_the_frozen_round2_target():
    target = rf.assert_isolated_target()
    assert target["pgContainer"] == "mact-r2-postgres"
    assert target["redisContainer"] == "mact-cap-redis"
    assert target["minioContainer"] == "mact-r2-minio"
    assert target["pgPort"] == 15532
    assert target["database"] == "macteval_round2"
    assert target["aiPorts"] == [18082, 18083]


@pytest.mark.parametrize("override", [
    {"pgContainer": "personal_design-postgres-1"},
    {"redisContainer": "personal_design-redis-1"},
    {"minioContainer": "mact-cap-minio"},
    {"pgContainer": "mact-cap-postgres"},
    {"pgContainer": "MACT-R2-POSTGRES"},
    {"redisPort": 6379},
    {"pgPort": 5432},
    {"minioPort": 9000},
    {"backendPort": 8000},
    {"backendPort": 18080},
    {"backendPort": "18081"},
    {"aiPorts": [18082, 18084]},
    {"aiPorts": [18082, 6379]},
    {"aiPorts": "18082"},
    {"database": "macteval"},
    {"database": "macteval_capacity"},
    {"database": "postgres"},
])
def test_assert_isolated_target_rejects_forbidden_targets(override):
    with pytest.raises(rf.IsolationError) as excinfo:
        rf.assert_isolated_target(override)
    message = str(excinfo.value)
    # The error must show the actual offending value, not a generic message.
    assert "refusing to touch a non-isolated target" in message
    assert "actual=" in message


def test_assert_isolated_target_reports_every_problem_at_once():
    with pytest.raises(rf.IsolationError) as excinfo:
        rf.assert_isolated_target({"pgContainer": "personal_design-postgres-1",
                                   "redisPort": 6379,
                                   "database": "macteval"})
    message = str(excinfo.value)
    assert message.count("is a forbidden") + message.count("is not an allowed") >= 3


def test_assert_isolated_target_is_stable_when_called_twice():
    first = rf.assert_isolated_target()
    second = rf.assert_isolated_target(first)
    assert first == second


def test_module_constants_match_the_allow_lists():
    assert rf.PG_CONTAINER in rf.ALLOWED_CONTAINERS
    assert rf.REDIS_CONTAINER in rf.ALLOWED_CONTAINERS
    assert rf.MINIO_CONTAINER in rf.ALLOWED_CONTAINERS
    assert rf.PG_DB in rf.ALLOWED_DATABASES
    assert {rf.PG_PORT, rf.REDIS_PORT, rf.MINIO_PORT,
            rf.BACKEND_PORT, rf.AI_A_PORT, rf.AI_B_PORT} == set(rf.ALLOWED_PORTS)
    # The user's live services and the previous round must never be targets.
    for forbidden in (5432, 6379, 9000, 8000, 8001):
        assert forbidden not in rf.ALLOWED_PORTS


def test_port_open_rejects_a_port_outside_the_allow_list():
    with pytest.raises(rf.IsolationError):
        rf.port_open(6379, timeout=0.01)


def test_find_ai_pid_rejects_a_non_test_port():
    with pytest.raises(rf.IsolationError):
        rf.find_ai_pid(8001)


# --- assertion evaluation -------------------------------------------------
def test_derive_pass_requires_all_assertions_true():
    result = rf.new_result("demo")
    rf.add_assertion(result, "a", True, 1, 1)
    rf.add_assertion(result, "b", True, 2, 2)
    assert rf.derive_pass(result) is True


def test_derive_pass_is_false_when_any_assertion_is_false():
    result = rf.new_result("demo")
    rf.add_assertion(result, "a", True, 1, 1)
    rf.add_assertion(result, "b", False, 2, 3)
    assert rf.derive_pass(result) is False


def test_derive_pass_is_false_without_assertions():
    assert rf.derive_pass(rf.new_result("demo")) is False


def test_derive_pass_is_false_for_a_skipped_scenario_even_if_assertions_pass():
    result = rf.new_result("demo")
    rf.add_assertion(result, "a", True, 1, 1)
    result["notRun"] = "backend recovery service missing"
    assert rf.derive_pass(result) is False


def test_add_assertion_coerces_truthiness_but_records_actual_value():
    result = rf.new_result("demo")
    rf.add_assertion(result, "truthy", 1, True, 1)
    entry = result["assertions"][0]
    assert entry == {"name": "truthy", "ok": True, "expected": True, "actual": 1}


def test_pass_is_never_hardcoded_in_a_new_result():
    assert rf.new_result("demo")["pass"] is False


# --- summary merge --------------------------------------------------------
def test_merge_summary_preserves_pass_notrun_and_failures():
    passed = rf.new_result("ok")
    rf.add_assertion(passed, "fine", True, 1, 1)
    passed["pass"] = True

    skipped = rf.new_result("skip")
    skipped["notRun"] = "hook missing"

    failed = rf.new_result("bad")
    rf.add_assertion(failed, "broken", False, 1, 2)
    failed["pass"] = False

    summary = rf.merge_summary({"ok": passed, "skip": skipped, "bad": failed})
    assert summary["ok"]["pass"] is True
    assert summary["ok"]["notRun"] is None
    assert summary["skip"]["pass"] is False
    assert summary["skip"]["notRun"] == "hook missing"
    assert summary["bad"]["failedAssertions"] == ["broken"]
    assert summary["totals"] == {
        "scenarios": 3, "passed": 1, "skipped": 1, "failed": 1,
        "passedNames": ["ok"], "skippedNames": ["skip"], "failedNames": ["bad"],
    }


def test_merge_summary_keeps_manifest_separate_from_scenarios():
    summary = rf.merge_summary({}, {"gitHead": "abc"})
    assert summary["manifest"] == {"gitHead": "abc"}
    assert summary["totals"]["scenarios"] == 0


def test_merge_summary_survives_json_round_trip():
    result = rf.new_result("ok")
    rf.add_assertion(result, "fine", True, 1, 1)
    result["pass"] = True
    summary = rf.merge_summary({"ok": result})
    assert json.loads(json.dumps(summary)) == summary


# --- JSON shape -----------------------------------------------------------
def test_new_result_contains_every_required_key():
    result = rf.new_result("shape")
    for key in rf.REQUIRED_KEYS:
        assert key in result, key
    assert isinstance(result["timeline"], list)
    assert isinstance(result["assertions"], list)
    assert isinstance(result["counts"], dict)
    assert set(result["counts"]) == {"before", "after", "delta"}
    assert set(result["duplicateCheck"]) >= {"ok", "detail"}
    assert isinstance(result["notes"], list) and result["notes"]


def test_every_scenario_result_is_json_serialisable_with_the_required_schema():
    for name in rf.SCENARIOS:
        result = rf.new_result(name)
        rf.add_assertion(result, "placeholder", False, True, None)
        result["pass"] = rf.derive_pass(result)
        encoded = json.dumps(result, ensure_ascii=False)
        decoded = json.loads(encoded)
        for key in rf.REQUIRED_KEYS:
            assert key in decoded, (name, key)
        assert decoded["scenario"] == name


def test_run_scenario_records_a_skip_with_the_full_schema(monkeypatch, tmp_path):
    """A ScenarioSkip must produce ``notRun`` and never ``pass``."""
    def _skip(ctx):
        raise rf.ScenarioSkip("deliberate skip")

    monkeypatch.setitem(rf.SCENARIOS, "synthetic-skip", _skip)
    monkeypatch.setattr(rf.Rig, "stop_ai", lambda self, port, hard=False: None)
    try:
        result = rf.run_scenario("synthetic-skip", api=None, out_dir=tmp_path,
                                 caps={"dummy": True})
    finally:
        rf.SCENARIOS.pop("synthetic-skip", None)

    for key in rf.REQUIRED_KEYS:
        assert key in result, key
    assert result["notRun"] == "deliberate skip"
    assert result["pass"] is False
    assert result["jobId"] is None
    assert any(event["event"] == "scenario_skipped" for event in result["timeline"])


def test_run_scenario_records_an_exception_as_a_failed_assertion(monkeypatch, tmp_path):
    def _boom(ctx):
        raise ValueError("kaboom")

    monkeypatch.setitem(rf.SCENARIOS, "synthetic-boom", _boom)
    monkeypatch.setattr(rf.Rig, "stop_ai", lambda self, port, hard=False: None)
    try:
        result = rf.run_scenario("synthetic-boom", api=None, out_dir=tmp_path,
                                 caps={"dummy": True})
    finally:
        rf.SCENARIOS.pop("synthetic-boom", None)

    assert result["pass"] is False
    assert "kaboom" in result["error"]
    assert result["notRun"] is None
    assert [a["name"] for a in result["assertions"]] == [
        "scenario_completed_without_exception"]
    assert result["assertions"][0]["ok"] is False


def test_run_scenario_derives_pass_from_assertions(monkeypatch, tmp_path):
    def _good(ctx):
        rf.add_assertion(ctx["result"], "fine", True, 1, 1)
        return ctx["result"]

    monkeypatch.setitem(rf.SCENARIOS, "synthetic-good", _good)
    monkeypatch.setattr(rf.Rig, "stop_ai", lambda self, port, hard=False: None)
    try:
        result = rf.run_scenario("synthetic-good", api=None, out_dir=tmp_path,
                                 caps={"dummy": True})
    finally:
        rf.SCENARIOS.pop("synthetic-good", None)
    assert result["pass"] is True


def test_run_scenario_never_swallows_an_isolation_error(monkeypatch, tmp_path):
    def _unsafe(ctx):
        raise rf.IsolationError("bad target")

    monkeypatch.setitem(rf.SCENARIOS, "synthetic-unsafe", _unsafe)
    monkeypatch.setattr(rf.Rig, "stop_ai", lambda self, port, hard=False: None)
    try:
        with pytest.raises(rf.IsolationError):
            rf.run_scenario("synthetic-unsafe", api=None, out_dir=tmp_path,
                            caps={"dummy": True})
    finally:
        rf.SCENARIOS.pop("synthetic-unsafe", None)


# --- timeline -------------------------------------------------------------
def test_timeline_at_s_is_monotonic_and_sorted():
    timeline = rf.Timeline()
    for index in range(25):
        timeline.mark("step", index=index)
    at_values = [entry["at_s"] for entry in timeline.events]
    assert at_values == sorted(at_values)
    assert all(value >= 0 for value in at_values)
    assert [entry["index"] for entry in timeline.events] == list(range(25))


def test_timeline_drops_none_fields_but_keeps_false_and_zero():
    timeline = rf.Timeline()
    entry = timeline.mark("event", keep_zero=0, keep_false=False, drop=None)
    assert "drop" not in entry
    assert entry["keep_zero"] == 0
    assert entry["keep_false"] is False


def test_timeline_elapsed_is_non_negative():
    assert rf.Timeline().elapsed() >= 0


# --- helpers --------------------------------------------------------------
def test_count_delta_handles_missing_keys_on_both_sides():
    delta = rf.count_delta({"a": 1, "b": 5}, {"b": 7, "c": 2})
    assert delta == {"a": -1, "b": 2, "c": 2}


def test_inference_start_marker_is_scoped_to_one_lesson():
    """The no-reinference assertion depends on a per-lesson marker."""
    marker = "Starting video analysis: lesson=lesson-abc"
    log = (marker + "\n" + "Starting video analysis: lesson=lesson-xyz\n" + marker)
    assert log.count(marker) == 2


def test_fault_hook_probe_reports_a_bool():
    assert isinstance(rf.fault_hook_supported(), bool)


def test_capability_matrix_has_the_keys_scenarios_rely_on():
    # Pure file/DB introspection; an unreachable DB yields empty column sets.
    caps = rf.capability_matrix()
    for key in ("hasRecoveryColumns", "resultStoreModule", "readinessModule",
                "aiWorkerUsesResultStore", "backendRecoveryService",
                "faultHookResultPut", "analysisJobColumns"):
        assert key in caps, key


def test_require_records_nothing_and_raises_for_a_missing_capability():
    with pytest.raises(rf.ScenarioSkip) as excinfo:
        rf.require({}, {"faultHookResultPut": False}, "faultHookResultPut", "hook absent")
    assert "hook absent" in str(excinfo.value)
    rf.require({}, {"faultHookResultPut": True}, "faultHookResultPut", "hook absent")


def test_redis_cleanup_never_issues_a_global_flush():
    """No redis_cli call may pass a global destructive command."""
    import ast

    tree = ast.parse(Path(rf.__file__).read_text(encoding="utf-8"))
    forbidden = {"flushall", "flushdb", "config", "shutdown", "swapdb"}
    calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name != "redis_cli" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            calls.append(first.value.lower())
    assert calls, "expected the module to call redis_cli"
    assert not forbidden & set(calls), sorted(forbidden & set(calls))
    assert any(call in {"xgroup", "del"} for call in calls)


def test_drop_job_redis_state_is_scoped_to_one_job(monkeypatch):
    """The Redis cleanup must never degrade into a global flush."""
    issued: list[tuple[str, ...]] = []

    def _fake_redis_cli(*args, **kwargs):
        issued.append(args)
        return "1"

    monkeypatch.setattr(rf, "redis_cli", _fake_redis_cli)
    rf.drop_job_redis_state("job-123", 2)
    flatten = " ".join(" ".join(call) for call in issued).lower()
    assert "flushall" not in flatten and "flushdb" not in flatten
    assert "job-123" in flatten
    assert any(call[0] == "xgroup" and call[1] == "destroy" for call in issued)


def test_cli_list_prints_every_scenario(capsys):
    assert rf.main(["--list"]) == 0
    printed = capsys.readouterr().out.split()
    assert printed == list(rf.SCENARIOS)


def test_cli_rejects_an_unknown_scenario():
    with pytest.raises(SystemExit) as excinfo:
        rf.main(["not-a-scenario"])
    assert excinfo.value.code == 2


def test_cli_requires_a_scenario_when_no_mode_is_given():
    with pytest.raises(SystemExit) as excinfo:
        rf.main([])
    assert excinfo.value.code == 2
