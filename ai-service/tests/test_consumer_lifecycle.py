"""Regression tests for the job-consumer lifecycle.

A crashed consumer used to be invisible: the process kept answering /health
with 200 while every Redis job stayed QUEUED. These tests pin the two fixes —
the done callback that reports the crash, and the retry that survives a Redis
that is not up yet.

The ACK ordering itself (save result -> index -> callback -> ACK) is pinned in
``test_consumer_result_delivery.py``; the cases kept here that touch callback
state cover the legacy ``analysis:pending_callback:*`` retry path, which R05
kept as a transitional second path next to the durable result index.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import main as main_module
from app.workers import base as base_module


def _finished_task(exc: BaseException | None, *, cancelled: bool = False) -> asyncio.Task:
    async def _run() -> None:
        if cancelled:
            raise asyncio.CancelledError
        if exc is not None:
            raise exc

    task = asyncio.get_event_loop().create_task(_run())
    try:
        asyncio.get_event_loop().run_until_complete(task)
    except BaseException:
        pass
    return task


def test_consumer_done_marks_state_running_then_crashed_on_exception(caplog):
    async def _boom() -> None:
        raise RuntimeError("redis connection lost")

    async def _scenario() -> str:
        main_module._consumer_state = "running"
        task = asyncio.create_task(_boom())
        with pytest.raises(RuntimeError):
            await task
        main_module._consumer_done(task)
        return main_module._consumer_state

    state = asyncio.run(_scenario())

    assert state == "crashed"
    assert "Job consumer crashed" in caplog.text
    assert "redis connection lost" in caplog.text


def test_consumer_done_treats_cancellation_as_clean_stop(caplog):
    caplog.set_level(__import__("logging").INFO)

    async def _scenario() -> str:
        main_module._consumer_state = "running"
        task = asyncio.create_task(asyncio.sleep(60))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        main_module._consumer_done(task)
        return main_module._consumer_state

    assert asyncio.run(_scenario()) == "stopped"
    assert "Job consumer stopped" in caplog.text


def test_consumer_done_reports_silent_exit(caplog):
    caplog.set_level(__import__("logging").INFO)

    async def _scenario() -> str:
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        main_module._consumer_done(task)
        return main_module._consumer_state

    assert asyncio.run(_scenario()) == "crashed"
    assert "without an exception" in caplog.text


def test_health_exposes_consumer_state(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["job_consumer"] in {"running", "stopped", "crashed"}


def test_join_consumer_group_retries_until_redis_is_available(monkeypatch, caplog):
    """A Redis that is not up yet must not kill the consumer task."""
    calls: list[int] = []

    class _FlakyRedis:
        async def xgroup_create(self, *args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("redis not ready")
            return None

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(base_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(base_module, "REDIS_CONNECT_ATTEMPTS", 5)
    monkeypatch.setattr(base_module, "REDIS_CONNECT_BACKOFF_SECONDS", 0)

    asyncio.run(base_module.join_consumer_group(_FlakyRedis()))

    assert len(calls) == 3, "should retry twice, then succeed on the third attempt"
    assert "Job stream unavailable" in caplog.text


def test_join_consumer_group_gives_up_after_max_attempts(monkeypatch):
    class _DownRedis:
        async def xgroup_create(self, *args, **kwargs):
            raise ConnectionError("redis down")

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(base_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(base_module, "REDIS_CONNECT_ATTEMPTS", 3)

    with pytest.raises(ConnectionError, match="redis down"):
        asyncio.run(base_module.join_consumer_group(_DownRedis()))


def test_join_consumer_group_tolerates_existing_group(monkeypatch):
    """BUSYGROUP means the group already exists — that is success, not failure."""

    class _BusyRedis:
        async def xgroup_create(self, *args, **kwargs):
            raise base_module.aioredis.ResponseError("BUSYGROUP Consumer Group name already exists")

    asyncio.run(base_module.join_consumer_group(_BusyRedis()))


def test_ready_endpoint_reports_503_when_consumer_crashed(client):
    import app.main as main_module

    original = main_module._consumer_state
    try:
        main_module._consumer_state = "crashed"
        health = client.get("/health")
        ready = client.get("/ready")
        assert health.status_code == 200
        assert health.json()["ready"] is False
        assert ready.status_code == 503
        assert ready.json()["job_consumer"] == "crashed"
    finally:
        main_module._consumer_state = original


def test_ready_endpoint_reports_200_when_consumer_running(client):
    import app.main as main_module
    from app.core import readiness

    original = main_module._consumer_state
    try:
        main_module._consumer_state = "running"
        # /ready now also requires recent queue communication (protocol §7):
        # record a fresh Redis success instead of probing a real server here.
        readiness.note_redis_ok()
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
        assert ready.json()["reason"] == "recent_activity"
    finally:
        main_module._consumer_state = original
        readiness._reset_for_tests()


def test_pending_callback_retry_does_not_rerun_inference(monkeypatch):
    """Legacy path: stored callback is delivered without re-running inference.

    R05 moved the primary replay to the durable result index
    (``analysis:result:{jobId}:{attempt}``, asserted in
    ``test_consumer_result_delivery.py``). The ``analysis:pending_callback:*``
    keys written by an older worker are still retried here, and they likewise
    must never re-run the model.
    """

    class FakeRedis:
        def __init__(self):
            self.values = {"analysis:pending_callback:job-1": json.dumps({"status": "succeeded"})}
            self.set = {"analysis:pending_callbacks": {"job-1"}}
            self.deleted = []

        async def smembers(self, key):
            return self.set.get(key, set())

        async def get(self, key):
            return self.values.get(key)

        async def delete(self, key):
            self.deleted.append(key)
            self.values.pop(key, None)

        async def srem(self, key, value):
            self.set.get(key, set()).discard(value)

    calls = []

    async def fake_notify(job_id, result):
        calls.append((job_id, result))

    monkeypatch.setattr(base_module, "_notify_backend", fake_notify)
    redis = FakeRedis()
    retried = asyncio.run(base_module._retry_pending_callbacks(redis))

    assert retried == 1
    assert calls == [("job-1", {"status": "succeeded"})]
    assert redis.deleted == ["analysis:pending_callback:job-1"]


def test_unclaimed_terminal_job_is_acked_but_live_lease_is_not():
    """Only terminal jobs may be ACKed without inference.

    A message reclaimed while another worker still holds a valid lease must stay
    pending; ACKing it would delete the only copy and leave the job RUNNING.
    The per-reason decision is exercised end to end in
    ``test_consumer_result_delivery.py``.
    """
    assert base_module._is_terminal_status("SUCCEEDED")
    assert base_module._is_terminal_status("partial")
    assert not base_module._is_terminal_status("RUNNING")
    assert not base_module._is_terminal_status("QUEUED")


def test_claim_outcome_maps_every_reason():
    """claim reason -> outcome (protocol §1.1); only ``terminal`` may ACK."""
    assert base_module._claim_outcome({"claimed": True, "attempt": 2}) == "claimed"
    assert base_module._claim_outcome({"claimed": False, "reason": "terminal"}) == "terminal"
    assert base_module._claim_outcome({"claimed": False, "reason": "leased"}) == "leased"
    assert base_module._claim_outcome(
        {"claimed": False, "reason": "recovery_pending"}) == "recovery_pending"
    assert base_module._claim_outcome(
        {"claimed": False, "reason": "not_claimable"}) == "other"
    assert base_module._claim_outcome({}) == "other"


class _PendingRedis:
    def __init__(self, value=None):
        self.value = value
        self.deleted = []
        self.removed = []

    async def get(self, key):
        return self.value

    async def delete(self, key):
        self.deleted.append(key)
        self.value = None

    async def srem(self, key, member):
        self.removed.append((key, member))


def test_deliver_pending_callback_reports_absent(monkeypatch):
    async def scenario():
        return await base_module._deliver_pending_callback(_PendingRedis(), "job-absent")

    assert asyncio.run(scenario()) == "absent"


def test_deliver_pending_callback_delivers_and_clears(monkeypatch):
    calls = []

    async def fake_notify(job_id, result):
        calls.append((job_id, result))

    monkeypatch.setattr(base_module, "_notify_backend", fake_notify)
    redis = _PendingRedis(json.dumps({"status": "succeeded", "attempt": 1}))
    state = asyncio.run(base_module._deliver_pending_callback(redis, "job-ok"))
    assert state == "delivered"
    assert calls == [("job-ok", {"status": "succeeded", "attempt": 1})]
    assert redis.deleted == ["analysis:pending_callback:job-ok"]
    assert redis.removed == [("analysis:pending_callbacks", "job-ok")]


def test_deliver_pending_callback_keeps_result_when_backend_down(monkeypatch):
    async def failing_notify(job_id, result):
        raise RuntimeError("backend down")

    monkeypatch.setattr(base_module, "_notify_backend", failing_notify)
    redis = _PendingRedis(json.dumps({"status": "succeeded"}))
    state = asyncio.run(base_module._deliver_pending_callback(redis, "job-down"))
    assert state == "failed"
    assert redis.deleted == []
    assert redis.value is not None


def test_heartbeat_loop_stops_without_ack_when_attempt_is_stale(monkeypatch):
    calls = []

    async def fake_heartbeat(job_id, attempt):
        calls.append((job_id, attempt))
        return {"extended": False, "reason": "attempt_mismatch_or_not_running"}

    monkeypatch.setattr(base_module, "_heartbeat_job", fake_heartbeat)

    async def scenario():
        stop = asyncio.Event()
        lease_lost = asyncio.Event()
        await base_module._heartbeat_loop("job-stale", 2, stop, lease_lost)
        return lease_lost.is_set(), calls

    lost, observed = asyncio.run(scenario())
    assert lost is True
    assert observed == [("job-stale", 2)]


def test_deliver_pending_callback_drops_stale_attempt(monkeypatch):
    async def stale_notify(job_id, result):
        return {"applied": False, "reason": "stale_attempt", "attempt": 2}

    monkeypatch.setattr(base_module, "_notify_backend", stale_notify)
    redis = _PendingRedis(json.dumps({"status": "succeeded", "attempt": 1}))
    state = asyncio.run(base_module._deliver_pending_callback(redis, "job-stale"))
    assert state == "stale"
    assert redis.deleted == ["analysis:pending_callback:job-stale"]
    assert redis.removed == [("analysis:pending_callbacks", "job-stale")]
