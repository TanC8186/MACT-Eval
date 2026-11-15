"""ACK ordering and result-delivery tests (protocol §4 / R05).

Pinned invariants:

* the result is saved to durable storage and indexed before any callback,
* the stream message is ACKed only when the outcome is unambiguous
  (delivered, or genuinely terminal), never while a newer attempt may own it,
* a crash between "object written" and "ACK" replays the stored result instead
  of re-running inference.

Everything is faked: no test touches a real Redis, MinIO or backend.
"""
from __future__ import annotations

import asyncio

import pytest

from app.workers import base as base_module
from app.workers import result_store
from app.workers.result_store import ResultConflictError, ResultRef, ResultStoreError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _ref(attempt: int = 1, job_id: str = "job-1") -> ResultRef:
    return ResultRef(
        job_id=job_id,
        attempt=attempt,
        bucket="analysis-artifacts",
        result_key=f"analysis-results/{job_id}/{attempt}/result.json",
        envelope_key=f"analysis-results/{job_id}/{attempt}/envelope.json",
        sha256="a" * 64,
        bytes=42,
    )


class FakeRedis:
    """Records every stream/index mutation the consumer performs."""

    def __init__(self, events: list) -> None:
        self.events = events
        self.acked: list[str] = []
        self.deleted_stream_ids: list[str] = []
        self.values: dict[str, str] = {}
        self.sets: dict[str, set] = {}

    async def xack(self, stream, group, msg_id):
        self.events.append("xack")
        self.acked.append(msg_id)
        return 1

    async def xdel(self, stream, msg_id):
        self.events.append("xdel")
        self.deleted_stream_ids.append(msg_id)
        return 1

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)
        return 1


class _DrillExit(BaseException):
    """Stands in for ``os._exit`` inside the fault-injection hooks."""


@pytest.fixture
def env(monkeypatch):
    """Wire the consumer to fully in-memory fakes and record the call order."""
    events: list = []
    recorded: dict = {
        "events": events,
        "claim": {"claimed": True, "attempt": 3, "status": "RUNNING"},
        "notify": {"applied": True, "status": "SUCCEEDED", "attempt": 3},
        "save_error": None,
        "mark_error": None,
        "pending_attempts": [],
        "stored_result": None,
        "handler_calls": [],
        "exits": [],
        "notified": [],
        "cleared": [],
        # Default: the backend is reachable and the job is still RUNNING, so
        # the R07 corrupt-object branch keeps the message pending.
        "job_status": {"exists": True, "status": "RUNNING", "attempt": 3,
                       "terminal": False},
    }

    async def fake_claim(job_id):
        events.append("claim")
        claim = recorded["claim"]
        if isinstance(claim, Exception):
            raise claim
        return claim

    async def fake_job_status(job_id):
        events.append("status")
        status = recorded["job_status"]
        if isinstance(status, Exception):
            raise status
        return status

    async def fake_notify(job_id, result):
        events.append("callback")
        recorded.setdefault("notified", []).append(result)
        notify = recorded["notify"]
        if isinstance(notify, Exception):
            raise notify
        if isinstance(notify, list):
            # Sequence mode: one response per call (last one repeats).
            return notify.pop(0) if len(notify) > 1 else notify[0]
        return notify

    async def fake_save(job_id, attempt, result, **kwargs):
        events.append("save")
        recorded["saved_result"] = result
        if recorded["save_error"] is not None:
            raise recorded["save_error"]
        return _ref(attempt, job_id)

    async def fake_mark(ref, **meta):
        events.append("mark")
        recorded["mark_meta"] = meta
        if recorded["mark_error"] is not None:
            raise recorded["mark_error"]

    async def fake_clear(job_id, attempt):
        events.append("clear")
        recorded["cleared"].append((job_id, attempt))
        if recorded.get("clear_error") is not None:
            raise recorded["clear_error"]
        if attempt in recorded["pending_attempts"]:
            recorded["pending_attempts"].remove(attempt)

    async def fake_pending_attempts(job_id):
        return list(recorded["pending_attempts"])

    async def fake_load(job_id, attempt):
        events.append("load")
        by_attempt = recorded.get("stored_results") or {}
        stored = by_attempt.get(attempt, recorded["stored_result"])
        if isinstance(stored, Exception):
            raise stored
        return stored

    async def fake_handler(job):
        events.append("inference")
        recorded["handler_calls"].append(job)
        return {"status": "succeeded", "windows": []}

    async def fake_heartbeat(job_id, attempt):
        return {"extended": True}

    def fake_exit(code):
        recorded["exits"].append(code)
        raise _DrillExit(code)

    monkeypatch.setattr(base_module, "_claim_job", fake_claim)
    monkeypatch.setattr(base_module, "_job_status", fake_job_status)
    monkeypatch.setattr(base_module, "_notify_backend", fake_notify)
    monkeypatch.setattr(base_module, "_heartbeat_job", fake_heartbeat)
    monkeypatch.setattr(base_module, "_pending_attempts_async", fake_pending_attempts)
    monkeypatch.setattr(base_module, "_HANDLER", fake_handler)
    monkeypatch.setattr(base_module, "_fault_exit", fake_exit)
    monkeypatch.setattr(base_module, "CALLBACK_RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(result_store, "save_result_async", fake_save)
    monkeypatch.setattr(result_store, "mark_pending_async", fake_mark)
    monkeypatch.setattr(result_store, "clear_pending_async", fake_clear)
    monkeypatch.setattr(result_store, "load_result_async", fake_load)
    # Per-job livelock counters are module state shared across tests.
    base_module._STALE_REPLAY_COUNTS.clear()
    base_module._SWEEP_CURSOR = None
    recorded["redis"] = FakeRedis(events)
    recorded["consumer"] = base_module._Consumer(recorded["redis"], "consumer-test")
    return recorded


def _process(recorded, job: dict | None = None) -> None:
    async def _run() -> None:
        await recorded["consumer"].process("1-0", job or {"id": "job-1", "type": "VIDEO_ANALYSIS"})

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 1. Happy path: save -> index -> callback -> clear -> ACK
# ---------------------------------------------------------------------------

def test_result_is_saved_and_indexed_before_callback_and_ack(env):
    _process(env)

    assert env["events"] == ["claim", "inference", "save", "mark", "callback", "clear",
                             "xack", "xdel"]
    assert env["redis"].acked == ["1-0"]
    assert env["handler_calls"], "inference must run once"
    assert env["exits"] == []


# ---------------------------------------------------------------------------
# 2/3. Store failures keep the message pending
# ---------------------------------------------------------------------------

def test_save_failure_leaves_message_pending(env, caplog):
    env["save_error"] = ResultStoreError("minio 503")

    _process(env)

    assert "save" in env["events"]
    assert "callback" not in env["events"]
    assert env["redis"].acked == []
    assert "Leaving message 1-0 pending" in caplog.text


def test_mark_pending_failure_leaves_message_pending(env, caplog):
    env["mark_error"] = ConnectionError("redis gone")

    _process(env)

    assert env["events"] == ["claim", "inference", "save", "mark"]
    assert env["redis"].acked == []
    assert "Could not index stored result" in caplog.text


# ---------------------------------------------------------------------------
# 4/5. Callback outcomes
# ---------------------------------------------------------------------------

def test_unreachable_backend_acks_and_keeps_pending_index(env):
    env["notify"] = ConnectionError("backend down")

    _process(env)

    assert env["redis"].acked == ["1-0"]
    assert "clear" not in env["events"], "pending index must survive for recovery"
    # The legacy retry copy is still written as a second, best-effort path.
    assert "job-1" in env["redis"].sets.get(base_module.PENDING_CALLBACK_SET, set())
    assert ("analysis:pending_callback:job-1" in env["redis"].values)


def test_stale_attempt_callback_is_not_acked(env):
    env["notify"] = {"applied": False, "reason": "stale_attempt", "attempt": 4}

    _process(env)

    assert env["redis"].acked == []
    assert "clear" not in env["events"]


@pytest.mark.parametrize("reason", ["not_running", "invalid_attempt"])
def test_other_fencing_rejections_are_not_acked(env, reason):
    env["notify"] = {"applied": False, "reason": reason}

    _process(env)

    assert env["redis"].acked == []


def test_already_terminal_is_treated_as_delivered(env):
    env["notify"] = {"applied": False, "reason": "already_terminal"}

    _process(env)

    assert env["redis"].acked == ["1-0"]
    assert "clear" in env["events"]


# ---------------------------------------------------------------------------
# 6/7/8. Claim decisions
# ---------------------------------------------------------------------------

def test_recovery_pending_is_not_acked_and_not_inferred(env, caplog):
    env["claim"] = {"claimed": False, "reason": "recovery_pending",
                    "attempt": 4, "status": "RUNNING"}

    _process(env)

    assert env["events"] == ["claim"]
    assert env["redis"].acked == []
    assert "awaiting database recovery" in caplog.text


def test_leased_job_is_not_acked(env):
    env["claim"] = {"claimed": False, "reason": "leased", "attempt": 3, "status": "RUNNING"}

    _process(env)

    assert env["events"] == ["claim"]
    assert env["redis"].acked == []


def test_terminal_job_is_acked_without_inference(env):
    env["claim"] = {"claimed": False, "reason": "terminal", "status": "SUCCEEDED", "attempt": 3}

    _process(env)

    assert env["events"] == ["claim", "xack", "xdel"]
    assert env["handler_calls"] == []


# ---------------------------------------------------------------------------
# 9/10. Replay of an already stored result
# ---------------------------------------------------------------------------

def test_stored_result_is_delivered_without_inference(env):
    env["pending_attempts"] = [3]
    env["stored_result"] = ({"status": "succeeded", "attempt": 3}, _ref(3))

    _process(env)

    assert env["events"] == ["load", "callback", "clear", "xack", "xdel"]
    assert env["handler_calls"] == []
    assert "claim" not in env["events"]


def test_stored_result_rejected_as_stale_clears_index_and_still_claims(env, caplog):
    """F4/A12: a stale index entry must not livelock the worker.

    The rejected attempt's index is dropped (logged with jobId/attempt/reason),
    the stream message is NOT ACKed from the replay path, and the claim step is
    reached so a newer attempt can run.
    """
    env["pending_attempts"] = [3]
    env["stored_result"] = ({"status": "succeeded", "attempt": 3}, _ref(3))
    env["notify"] = [{"applied": False, "reason": "stale_attempt"},
                     {"applied": True, "status": "SUCCEEDED"}]

    _process(env)

    assert env["events"] == ["load", "callback", "clear", "claim", "inference",
                             "save", "mark", "callback", "clear", "xack", "xdel"]
    assert env["cleared"] == [("job-1", 3), ("job-1", 3)]
    assert "Dropped stale pending index for job job-1 attempt 3 (reason=stale_attempt)" \
        in caplog.text

    # The next replay no longer hits the dropped index: the message goes straight
    # to claim instead of replaying the stale result again.
    env["pending_attempts"] = []
    env["events"].clear()
    env["notified"].clear()
    _process(env)
    assert env["events"][0] == "claim"
    assert all(item.get("attempt") == 3 for item in env["notified"])


def test_stale_replay_reaches_the_claim_step_after_the_index_is_gone(env):
    """Second round: the cleaned index means no second stale callback."""
    env["pending_attempts"] = [1]
    env["stored_result"] = ({"status": "succeeded", "attempt": 1}, _ref(1))
    env["notify"] = [{"applied": False, "reason": "stale_attempt"},
                     {"applied": True, "status": "SUCCEEDED"}]

    _process(env)
    assert env["cleared"][0] == ("job-1", 1)
    assert env["redis"].acked == ["1-0"]

    env["notified"].clear()
    env["events"].clear()
    _process(env)

    # No replay callback with attempt=1 any more; only the fresh attempt 3.
    assert [item.get("attempt") for item in env["notified"]] == [3]


def test_invalid_attempt_keeps_the_index_for_the_backend_invalid_verdict(env, caplog):
    """F13: ``invalid_attempt`` means the object is bad, not that it is superseded.

    The index must survive so the backend JobResultStore can mark the result
    INVALID (→ FAILED) instead of the worker silently hiding it.
    """
    env["pending_attempts"] = [3]
    env["stored_result"] = ({"status": "succeeded", "attempt": "three"}, _ref(3))
    env["notify"] = {"applied": False, "reason": "invalid_attempt"}

    _process(env)

    assert env["events"] == ["load", "callback"]
    assert env["cleared"] == []
    assert env["redis"].acked == []
    assert "keeping the index" in caplog.text


def test_stale_attempts_across_multiple_indexed_attempts_clear_only_rejected(env):
    """Only the attempt the backend explicitly refused is cleared."""
    env["pending_attempts"] = [3, 2]
    env["stored_results"] = {
        3: ({"status": "succeeded", "attempt": 3}, _ref(3)),
        2: ({"status": "succeeded", "attempt": 2}, _ref(2)),
    }
    env["notify"] = [{"applied": False, "reason": "not_running"},
                     {"applied": True}]

    _process(env)

    assert env["cleared"][0] == ("job-1", 3)
    # Attempt 2 was then delivered successfully and its own index cleared.
    assert env["cleared"][1] == ("job-1", 2)
    assert "xack" in env["events"]
    assert "claim" not in env["events"]


def test_repeated_stale_replays_clear_every_index_and_proceed(env, monkeypatch):
    """Livelock cap: after STALE_REPLAY_LIMIT consecutive rejections all indexes go."""
    monkeypatch.setattr(base_module, "STALE_REPLAY_LIMIT", 2)
    base_module._STALE_REPLAY_COUNTS.clear()
    env["pending_attempts"] = [1]
    # Re-adding the index each round simulates the backend never clearing it.
    env["stored_result"] = ({"status": "succeeded", "attempt": 1}, _ref(1))
    env["notify"] = [{"applied": False, "reason": "stale_attempt"}]

    _process(env)   # 1st rejection: only attempt 1 cleared, then claim runs
    assert env["cleared"][0] == ("job-1", 1)
    assert "claim" in env["events"]

    env["pending_attempts"] = [1, 2]
    env["events"].clear()
    env["cleared"].clear()
    _process(env)   # 2nd rejection hits the cap: every index entry is cleared
    assert ("job-1", 1) in env["cleared"]
    assert ("job-1", 2) in env["cleared"]
    assert "claim" in env["events"]
    base_module._STALE_REPLAY_COUNTS.clear()


def test_unreadable_stored_result_leaves_message_pending(env):
    """A store outage must never be mistaken for "no result" and re-run inference."""
    env["pending_attempts"] = [3]
    env["stored_result"] = ResultStoreError("minio timeout")

    _process(env)

    assert env["events"] == ["load"]
    assert env["redis"].acked == []
    assert env["handler_calls"] == []


def test_corrupt_stored_result_keeps_index_and_never_reinfers(env, caplog):
    """F13: damage is distinct from unreachability — keep the index, no re-run.

    R07 adds a status lookup before the decision; a job the backend still
    reports as RUNNING must keep both the index and the message so the backend
    can mark the result INVALID.
    """
    env["pending_attempts"] = [3]
    env["stored_result"] = result_store.ResultCorruptError("sha256 mismatch")
    env["job_status"] = {"exists": True, "status": "RUNNING", "attempt": 3,
                         "terminal": False}

    _process(env)

    assert env["events"] == ["load", "status"]
    assert env["redis"].acked == []
    assert env["handler_calls"] == []
    assert env["cleared"] == [], "a corrupt result must keep its index"
    assert "is corrupt" in caplog.text
    assert "INVALID" in caplog.text


# ---------------------------------------------------------------------------
# 10b. R07: a corrupt object must not block a message whose job is terminal
# ---------------------------------------------------------------------------

def test_corrupt_result_for_terminal_job_clears_index_and_acks(env, caplog):
    """R07: backend says FAILED -> clean this attempt's index and ACK the message.

    Before the fix the worker re-read the broken object every round, kept the
    index and the PEL entry forever (review repro: loadCalls=2, ackCalls=0).
    The payload object itself is *not* deleted: it stays in MinIO for audit.
    """
    env["pending_attempts"] = [3]
    env["stored_result"] = result_store.ResultCorruptError("sha256 mismatch")
    env["job_status"] = {"exists": True, "status": "FAILED", "attempt": 3,
                         "terminal": True}

    _process(env)

    assert env["events"] == ["load", "status", "clear", "xack", "xdel"]
    assert env["cleared"] == [("job-1", 3)], "only this attempt's index is cleared"
    assert env["redis"].acked == ["1-0"]
    assert env["handler_calls"] == [], "a corrupt result must never be re-inferred"
    assert "terminal" in caplog.text
    assert "kept for audit" in caplog.text

    # Second round: the index is gone, so the broken object is never read again
    # and the status endpoint is not consulted (review repro: loadCalls==2).
    env["events"].clear()
    env["pending_attempts"] = []
    _process(env)
    assert env["events"][0] == "claim"
    assert "load" not in env["events"], "the corrupt object must not be re-read"
    assert "status" not in env["events"]


def test_corrupt_result_for_terminal_job_only_clears_the_corrupt_attempt(env):
    """A newer attempt's index must survive the cleanup of an older attempt."""
    env["pending_attempts"] = [3, 4]
    env["stored_results"] = {
        3: result_store.ResultCorruptError("sha256 mismatch"),
        4: ({"status": "succeeded", "attempt": 4}, _ref(4)),
    }
    env["job_status"] = {"exists": True, "status": "SUCCEEDED", "attempt": 4,
                         "terminal": True}

    _process(env)

    assert env["cleared"] == [("job-1", 3)]
    assert ("job-1", 4) not in env["cleared"]
    assert env["redis"].acked == ["1-0"]


def test_corrupt_result_for_running_job_stays_pending(env, caplog):
    env["pending_attempts"] = [3]
    env["stored_result"] = result_store.ResultCorruptError("sha256 mismatch")
    env["job_status"] = {"exists": True, "status": "RUNNING", "attempt": 3,
                         "terminal": False}

    _process(env)

    assert env["redis"].acked == []
    assert env["cleared"] == []
    assert "still RUNNING" in caplog.text


def test_corrupt_result_with_unknown_backend_status_stays_pending(env, caplog):
    """Backend unreachable -> status is ``None`` -> never ACK, never re-infer."""
    env["pending_attempts"] = [3]
    env["stored_result"] = result_store.ResultCorruptError("sha256 mismatch")
    env["job_status"] = None

    _process(env)

    assert env["redis"].acked == []
    assert env["cleared"] == []
    assert env["handler_calls"] == []
    assert "status is unavailable" in caplog.text


def test_corrupt_result_for_missing_job_stays_pending(env, caplog):
    """A 404 means the job does not exist; that is not a terminal verdict."""
    env["pending_attempts"] = [3]
    env["stored_result"] = result_store.ResultCorruptError("sha256 mismatch")
    env["job_status"] = {"exists": False}

    _process(env)

    assert env["redis"].acked == []
    assert env["cleared"] == []
    assert "does not exist" in caplog.text


# ---------------------------------------------------------------------------
# 11. Conflict: index and forward the first object
# ---------------------------------------------------------------------------

def test_result_conflict_delivers_the_stored_first_object(env):
    """F6/A11: the callback body must equal the indexed/stored first object."""
    existing = _ref(3)
    env["save_error"] = ResultConflictError("digest differs", ref=existing)
    stored_body = {"status": "succeeded", "windows": [1, 2, 3],
                   "model_version": "OLD_FIRST", "attempt": 3}
    env["stored_result"] = (stored_body, existing)

    _process(env)

    assert env["events"] == ["claim", "inference", "save", "load", "mark",
                             "callback", "clear", "xack", "xdel"]
    assert env["mark_meta"].get("lastError") == "RESULT_CONFLICT"
    assert env["notified"][0] == stored_body
    assert env["notified"][0]["model_version"] == "OLD_FIRST"
    assert env["saved_result"] != stored_body, (
        "the in-memory recomputation must not be the one delivered")
    assert env["redis"].acked == ["1-0"]


def test_result_conflict_readback_failure_leaves_message_pending(env):
    env["save_error"] = ResultConflictError("digest differs", ref=_ref(3))
    env["stored_result"] = ResultStoreError("minio timeout")

    _process(env)

    assert "callback" not in env["events"]
    assert "mark" not in env["events"]
    assert env["redis"].acked == []


def test_result_conflict_without_ref_does_not_ack(env):
    env["save_error"] = ResultConflictError("digest differs", ref=None)

    _process(env)

    assert env["redis"].acked == []


def test_unserialisable_result_leaves_message_pending(env, caplog):
    """F9: non-finite floats must not be stored as backend-rejected bytes."""
    env["save_error"] = result_store.ResultSerializationError(
        "non-finite float in result at key confidence")

    _process(env)

    assert env["events"] == ["claim", "inference", "save"]
    assert env["redis"].acked == []
    assert "cannot be serialised" in caplog.text
    assert "non-finite" in caplog.text


# ---------------------------------------------------------------------------
# 12. Fault-injection hooks (F06 drill)
# ---------------------------------------------------------------------------

def test_fault_hook_after_result_put_stops_before_index(env, monkeypatch):
    monkeypatch.setenv(base_module.FAULT_STOP_AFTER_RESULT_PUT_ENV, "1")

    with pytest.raises(_DrillExit) as excinfo:
        _process(env)

    assert excinfo.value.args[0] == 137
    assert env["events"] == ["claim", "inference", "save"]
    assert env["redis"].acked == []


def test_fault_hook_after_mark_pending_stops_before_callback(env, monkeypatch):
    monkeypatch.setenv(base_module.FAULT_STOP_AFTER_MARK_PENDING_ENV, "1")

    with pytest.raises(_DrillExit):
        _process(env)

    assert env["events"] == ["claim", "inference", "save", "mark"]
    assert env["redis"].acked == []


def test_fault_hooks_are_inert_without_the_environment_variable(env, monkeypatch):
    monkeypatch.delenv(base_module.FAULT_STOP_AFTER_RESULT_PUT_ENV, raising=False)
    monkeypatch.delenv(base_module.FAULT_STOP_AFTER_MARK_PENDING_ENV, raising=False)

    _process(env)

    assert env["exits"] == []
    assert env["redis"].acked == ["1-0"]


# ---------------------------------------------------------------------------
# Readiness reporting from the worker path
# ---------------------------------------------------------------------------

def test_inference_is_reported_to_readiness(env, monkeypatch):
    from app.core import readiness

    readiness._reset_for_tests()
    observed: list[bool] = []

    async def fake_handler(job):
        observed.append(readiness.snapshot()["inference_in_progress"])
        return {"status": "succeeded"}

    monkeypatch.setattr(base_module, "_HANDLER", fake_handler)

    _process(env)

    assert observed == [True]
    assert readiness.snapshot()["inference_in_progress"] is False
    readiness._reset_for_tests()


# ---------------------------------------------------------------------------
# Maintenance timers (protocol §5 rule 8) and the read-only index helpers
# ---------------------------------------------------------------------------

def test_maintenance_timers_are_wall_clock_not_iteration_counted(monkeypatch):
    calls: list[str] = []

    async def fake_results(limit=base_module.PENDING_RESULT_SWEEP_LIMIT):
        calls.append("results")
        return 0

    async def fake_legacy(redis):
        calls.append("legacy")
        return 0

    monkeypatch.setattr(base_module, "_retry_pending_results", fake_results)
    monkeypatch.setattr(base_module, "_retry_pending_callbacks", fake_legacy)

    maintenance = base_module._Maintenance(
        object(), callback_interval=10.0, reclaim_interval=30.0, depth_interval=10.0)
    # The schedule starts at construction time (monotonic clock), so the test
    # moves that same clock forward rather than inventing an arbitrary epoch.
    now = maintenance._next_callbacks

    async def scenario():
        await maintenance.maybe_retry_callbacks(now)
        await maintenance.maybe_retry_callbacks(now + 1)      # not due yet
        await maintenance.maybe_retry_callbacks(now + 10.5)   # due again

    asyncio.run(scenario())

    assert calls == ["results", "legacy", "results", "legacy"]


def test_maintenance_reclaim_is_never_due_twice_within_the_interval():
    class BrokenRedis:
        async def xautoclaim(self, *args, **kwargs):
            raise AssertionError("reclaim must not run twice in one interval")

    maintenance = base_module._Maintenance(BrokenRedis(), reclaim_interval=30.0)
    first = maintenance._next_reclaim

    async def scenario():
        return await maintenance.maybe_reclaim("consumer", first)

    # First call is due and fails on the broken client; the second is not due.
    with pytest.raises(AssertionError):
        asyncio.run(scenario())
    assert asyncio.run(maintenance.maybe_reclaim("consumer", first + 1)) == []


def test_pending_attempts_reads_only_this_jobs_attempt_keys():
    class ScanRedis:
        def sismember(self, key, member):
            return True

        def scan_iter(self, match, count=100):
            yield "analysis:result:job-1:1"
            yield "analysis:result:job-1:12"
            yield "analysis:result:job-1:suffix"
            yield b"analysis:result:job-11:4"   # different job id

    recorded = {}
    original = result_store._get_redis
    result_store._get_redis = lambda: ScanRedis()
    try:
        recorded["attempts"] = base_module._pending_attempts("job-1")
    finally:
        result_store._get_redis = original

    assert recorded["attempts"] == [12, 1]


def test_pending_attempts_skips_the_scan_for_unjobbed_ids():
    """No pending-result SET member -> one O(1) call, no keyspace SCAN."""
    class UnlistedRedis:
        def sismember(self, key, member):
            return False

        def scan_iter(self, match, count=100):
            raise AssertionError("SCAN must not run for a job with no pending result")

    original = result_store._get_redis
    result_store._get_redis = lambda: UnlistedRedis()
    try:
        assert base_module._pending_attempts("job-none") == []
    finally:
        result_store._get_redis = original


def test_pending_attempts_failure_leaves_message_pending(env, monkeypatch):
    async def failing(job_id):
        raise ConnectionError("redis down")

    monkeypatch.setattr(base_module, "_pending_attempts_async", failing)

    _process(env)

    assert env["redis"].acked == []
    assert env["handler_calls"] == []


# ---------------------------------------------------------------------------
# Background sweep rotation (F15)
# ---------------------------------------------------------------------------

def test_sweep_rotation_visits_every_pending_job(monkeypatch):
    """6 pending jobs, limit 5: two rounds must reach all 6, not the first 5 twice."""
    job_ids = {f"job-{i}" for i in range(6)}
    visited: list[str] = []

    def fake_job_ids():
        return set(job_ids)

    async def fake_replay(job_id, msg_id=None):
        visited.append(job_id)
        return "skip"

    monkeypatch.setattr(base_module, "_pending_job_ids", fake_job_ids)
    monkeypatch.setattr(base_module, "_replay_stored_result", fake_replay)
    base_module._SWEEP_CURSOR = None

    first = asyncio.run(base_module._retry_pending_results(limit=5))
    second = asyncio.run(base_module._retry_pending_results(limit=5))

    assert first == 0 and second == 0
    assert len(visited) == 10
    assert set(visited[:5]) != set(visited[5:]), "the window must rotate"
    assert set(visited) == job_ids, "every pending job is reached within two rounds"
    # Each round stays bounded by limit.
    assert len(set(visited[:5])) == 5 and len(set(visited[5:])) == 5
    base_module._SWEEP_CURSOR = None


def test_sweep_rotation_cursor_wraps_around(monkeypatch):
    ordered = ["job-a", "job-b", "job-c", "job-d"]
    first, cursor = base_module._next_sweep_batch(ordered, 2, None)
    second, cursor = base_module._next_sweep_batch(ordered, 2, cursor)
    third, cursor = base_module._next_sweep_batch(ordered, 2, cursor)

    assert first == ["job-a", "job-b"]
    assert second == ["job-c", "job-d"]
    assert third == ["job-a", "job-b"], "the rotation wraps to the front"


def test_sweep_rotation_is_a_no_op_when_everything_fits(monkeypatch):
    assert base_module._next_sweep_batch(["a", "b"], 5, None) == (["a", "b"], None)
    assert base_module._next_sweep_batch([], 5, "x") == ([], "x")
    assert base_module._next_sweep_batch(["a"], 0, "x") == ([], "x")


def test_consume_jobs_reports_starting_running_and_stopped(monkeypatch):
    """The consumer state must be visible to /ready through readiness."""
    from app.core import readiness

    readiness._reset_for_tests()
    observed: list[str] = []

    class Redis:
        async def xreadgroup(self, **kwargs):
            observed.append(readiness.snapshot()["consumer_state"])
            raise asyncio.CancelledError

        async def xlen(self, stream):
            return 0

        async def xpending(self, stream, group):
            return {"pending": 0}

    async def fake_pool():
        observed.append(readiness.snapshot()["consumer_state"])
        return Redis()

    async def fake_join(redis):
        return None

    monkeypatch.setattr(base_module, "create_redis_pool", fake_pool)
    monkeypatch.setattr(base_module, "join_consumer_group", fake_join)
    readiness.bind_consumer_state(None)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(base_module.consume_jobs())

        assert observed == ["starting", "running"]
        assert readiness.snapshot()["consumer_state"] == "stopped"
    finally:
        # Restore the binding app.main installed at import time.
        from app import main as main_module

        readiness.bind_consumer_state(main_module._read_consumer_state)
        readiness._reset_for_tests()
