"""Readiness semantics for /live and /ready (protocol §7).

The regression these tests pin: a consumer whose Redis reads started failing
kept answering 200 forever because readiness only looked at the asyncio task
state. Now /ready must prove recent queue communication and /live must never be
dragged down by a Redis outage.

No test talks to a real Redis: the active PING runs through the module-level
``readiness._ping`` seam.
"""
from __future__ import annotations

import asyncio

import pytest

from app import main as main_module
from app.core import readiness
from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_readiness(monkeypatch):
    """Reset module state and keep the supervisor away from a real Redis."""
    async def _idle_consumer() -> None:
        await asyncio.sleep(3600)

    async def _no_supervisor() -> None:
        return None

    monkeypatch.setattr(main_module, "consume_jobs", _idle_consumer)
    # The supervisor sets "running" on its own; disable it so each test owns the
    # state and /ready can never race with a startup task.
    monkeypatch.setattr(main_module, "_supervise_consumer", _no_supervisor)
    monkeypatch.setattr(main_module, "_consumer_state", "stopped")
    readiness._reset_for_tests()
    yield
    readiness._reset_for_tests()


@pytest.fixture
def consumer_state(monkeypatch):
    """Set the canonical state (owned by app.main) for one test."""
    def _set(state: str) -> None:
        monkeypatch.setattr(main_module, "_consumer_state", state)

    return _set


# --------------------------------------------------------------------------- #
# Unit level: evaluate()
# --------------------------------------------------------------------------- #

def test_crashed_consumer_is_not_ready_without_pinging(consumer_state, monkeypatch):
    pings: list[int] = []

    def fake_ping() -> None:
        pings.append(1)

    monkeypatch.setattr(readiness, "_ping", fake_ping)
    consumer_state("crashed")

    ready, reason, details = asyncio.run(readiness.evaluate())

    assert ready is False
    assert reason == "consumer_crashed"
    assert details["consumer_state"] == "crashed"
    assert pings == [], "a crashed consumer must not trigger a network probe"


def test_running_with_recent_activity_is_ready_without_pinging(consumer_state, monkeypatch):
    def fake_ping() -> None:
        raise AssertionError("fresh passive signal must not trigger a PING")

    monkeypatch.setattr(readiness, "_ping", fake_ping)
    consumer_state("running")
    readiness.note_redis_ok()
    readiness.note_message_consumed()

    ready, reason, details = asyncio.run(readiness.evaluate())

    assert ready is True
    assert reason == "recent_activity"
    assert details["redis_last_ok_age_s"] is not None
    assert details["last_message_consumed_age_s"] is not None


def test_stale_activity_with_failing_ping_is_unreachable(consumer_state, monkeypatch):
    calls: list[int] = []

    def failing_ping() -> None:
        calls.append(1)
        raise ConnectionError(f"redis down: {settings.redis_url}")

    monkeypatch.setattr(readiness, "_ping", failing_ping)
    consumer_state("running")
    readiness._reset_for_tests(
        consumer_state="running", redis_last_ok_age_s=settings.ready_stale_seconds + 5
    )

    ready, reason, details = asyncio.run(readiness.evaluate())

    assert ready is False
    assert reason == "redis_unreachable"
    assert calls == [1]
    assert details["consumer_state"] == "running"


def test_stale_activity_with_successful_ping_refreshes_timestamp(consumer_state, monkeypatch):
    calls: list[int] = []

    async def ok_ping() -> None:
        calls.append(1)
        await asyncio.sleep(0)

    monkeypatch.setattr(readiness, "_ping", ok_ping)
    consumer_state("running")
    readiness._reset_for_tests(
        consumer_state="running", redis_last_ok_age_s=settings.ready_stale_seconds + 60
    )

    ready, reason, details = asyncio.run(readiness.evaluate())

    assert ready is True
    assert reason == "ping_ok"
    assert calls == [1]
    assert details["redis_last_ok_age_s"] is not None
    assert details["redis_last_ok_age_s"] <= 1.0, "ping success must refresh the clock"


def test_long_inference_with_recent_activity_stays_ready(consumer_state, monkeypatch):
    def fake_ping() -> None:
        raise AssertionError("busy consumer with fresh Redis signal must stay ready")

    monkeypatch.setattr(readiness, "_ping", fake_ping)
    consumer_state("running")
    readiness.note_redis_ok()
    readiness.note_message_consumed()
    readiness.note_inference_started()

    ready, reason, details = asyncio.run(readiness.evaluate())

    assert ready is True
    assert reason == "recent_activity"
    assert details["inference_in_progress"] is True


def test_ping_timeout_is_bounded_and_does_not_block_the_event_loop(consumer_state, monkeypatch):
    import threading

    release = threading.Event()

    def slow_ping() -> None:
        release.wait(10)

    monkeypatch.setattr(readiness, "_ping", slow_ping)
    monkeypatch.setattr(settings, "ready_ping_timeout_seconds", 0.05)
    consumer_state("running")
    readiness._reset_for_tests(consumer_state="running")

    ticks: list[int] = []

    async def _ticker() -> None:
        for _ in range(50):
            ticks.append(1)
            await asyncio.sleep(0.01)

    async def _scenario():
        ticker = asyncio.create_task(_ticker())
        try:
            return await readiness.evaluate()
        finally:
            ticker.cancel()

    try:
        ready, reason, _ = asyncio.run(_scenario())
    finally:
        release.set()

    assert ready is False
    assert reason == "redis_unreachable"
    assert len(ticks) >= 5, "the event loop must keep running while the PING is in flight"


def test_recorded_redis_error_invalidates_fresh_success(consumer_state, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(readiness, "_ping", lambda: calls.append(1))
    consumer_state("running")
    readiness.note_redis_ok()
    readiness.note_redis_error(ConnectionError("lost"))

    ready, reason, _ = asyncio.run(readiness.evaluate())

    assert ready is True
    assert reason == "ping_ok"
    assert calls == [1], "a recorded error must force an active PING"


def test_message_consumed_counts_as_recent_queue_communication(consumer_state):
    consumer_state("running")
    readiness._reset_for_tests(consumer_state="running")

    readiness.note_message_consumed(2)

    snap = readiness.snapshot()
    assert snap["last_message_consumed_age_s"] is not None
    assert snap["redis_last_ok_age_s"] is not None


def test_snapshot_reports_queue_and_inference_fields(consumer_state):
    consumer_state("running")
    readiness.note_consumer_state("running", 3)
    readiness.note_queue_depth(7, 2)
    readiness.note_inference_started()
    readiness.note_message_consumed(4)

    snap = readiness.snapshot()

    assert snap["consumer_restarts"] == 3
    assert snap["queued"] == 7
    assert snap["pending"] == 2
    assert snap["inference_in_progress"] is True
    assert snap["last_message_consumed_age_s"] is not None

    readiness.note_inference_finished()
    readiness.note_queue_depth(None, None)
    snap = readiness.snapshot()
    assert snap["inference_in_progress"] is False
    assert snap["queued"] is None, "unknown queue depth must not be reported as 0"
    assert snap["pending"] is None


# --------------------------------------------------------------------------- #
# HTTP level: /live, /ready, /health
# --------------------------------------------------------------------------- #

def test_ready_returns_503_consumer_crashed_with_details(client, consumer_state, monkeypatch):
    consumer_state("crashed")

    response = client.get("/ready")
    body = response.json()

    assert response.status_code == 503
    assert body["ready"] is False
    assert body["job_consumer"] == "crashed"
    assert body["reason"] == "consumer_crashed"
    assert set([
        "consumer_state", "consumer_restarts", "redis_last_ok_age_s",
        "queued", "pending", "inference_in_progress", "last_message_consumed_age_s",
    ]).issubset(body["details"].keys())


def test_ready_returns_200_with_recent_activity(client, consumer_state):
    consumer_state("running")
    readiness.note_redis_ok()

    response = client.get("/ready")
    body = response.json()

    assert response.status_code == 200
    assert body["ready"] is True
    assert body["reason"] == "recent_activity"
    assert body["job_consumer"] == "running"


def test_ready_returns_503_when_redis_unreachable(client, consumer_state, monkeypatch):
    consumer_state("running")
    readiness._reset_for_tests(consumer_state="running")
    monkeypatch.setattr(settings, "ready_ping_timeout_seconds", 0.05)

    def failing_ping() -> None:
        raise ConnectionError("no route to redis")

    monkeypatch.setattr(readiness, "_ping", failing_ping)

    response = client.get("/ready")
    body = response.json()

    assert response.status_code == 503
    assert body["reason"] == "redis_unreachable"
    # The state is still running: /health keeps reporting the process alive.
    assert client.get("/health").status_code == 200


def test_live_stays_200_when_redis_ping_fails(client, consumer_state, monkeypatch):
    consumer_state("running")
    readiness._reset_for_tests(consumer_state="running")

    def failing_ping() -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(readiness, "_ping", failing_ping)

    live = client.get("/live")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert live.json()["service"] == settings.service_name
    assert client.get("/ready").status_code == 503


def test_probe_bodies_never_leak_credentials(client, consumer_state, monkeypatch):
    consumer_state("running")
    readiness._reset_for_tests(consumer_state="running")

    def failing_ping() -> None:
        raise ConnectionError(f"cannot reach {settings.redis_url}")

    monkeypatch.setattr(readiness, "_ping", failing_ping)

    payloads = [
        client.get("/live").text,
        client.get("/ready").text,
        client.get("/health").text,
    ]

    for payload in payloads:
        assert "redis://" not in payload
        assert settings.service_token not in payload
        assert "password" not in payload.lower()
        assert "http://" not in payload


def test_health_and_ready_share_one_consumer_state(client, consumer_state):
    consumer_state("crashed")

    health = client.get("/health").json()
    ready = client.get("/ready").json()

    assert health["job_consumer"] == ready["job_consumer"] == "crashed"
    assert health["ready"] is False
    assert ready["ready"] is False


def test_consumer_done_updates_both_views(monkeypatch):
    """The supervisor's callback and readiness must never disagree."""
    monkeypatch.setattr(main_module, "_consumer_state", "running")

    async def _crash() -> None:
        raise RuntimeError("redis connection lost")

    async def _scenario() -> None:
        task = asyncio.create_task(_crash())
        try:
            await task
        except RuntimeError:
            pass
        main_module._consumer_done(task)

    asyncio.run(_scenario())

    assert main_module._read_consumer_state() == "crashed"
    assert main_module._consumer_state == "crashed"
    assert readiness.snapshot()["consumer_state"] == "crashed"
