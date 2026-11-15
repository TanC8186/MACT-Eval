"""Independent maintenance task, startup readiness and Redis shutdown (R05/R06/L01).

Pinned regressions (docs/reviews/2026-10-27-post-execution-review.md):

* R05 — a long inference must not postpone callback retries / reclaim: since
  the maintenance work runs in its own task, a blocked handler is no longer a
  blocked maintenance round. Inference concurrency stays 1 because the
  maintenance task only *queues* reclaimed messages for the serial main loop.
* R06 — a consumer that has not really talked to Redis and joined the group
  must never look ready. ``create_redis_pool`` therefore performs a real PING
  and the supervisor no longer forces "running".
* L01 — the Redis client is closed explicitly on shutdown, so no socket is
  finalized on a dead event loop.

No test opens a real connection: every Redis/backend/HTTP seam is faked.
"""
from __future__ import annotations

import asyncio

import pytest

from app import main as main_module
from app.core import readiness
from app.workers import base as base_module


# ---------------------------------------------------------------------------
# R06: startup readiness needs a real round-trip and a real group join
# ---------------------------------------------------------------------------

class _PingRedis:
    """Minimal async Redis client that records PING calls and can fail them."""

    def __init__(self, ping_error: BaseException | None = None,
                 group_error: BaseException | None = None) -> None:
        self.ping_calls = 0
        self.group_calls = 0
        self.closed = 0
        self._ping_error = ping_error
        self._group_error = group_error

    async def ping(self):
        self.ping_calls += 1
        if self._ping_error is not None:
            raise self._ping_error
        return True

    async def xgroup_create(self, *args, **kwargs):
        self.group_calls += 1
        if self._group_error is not None:
            raise self._group_error
        return True

    async def aclose(self):
        self.closed += 1


def _install_fake_redis(monkeypatch, client: _PingRedis) -> None:
    monkeypatch.setattr(base_module.aioredis, "from_url", lambda *a, **kw: client)


@pytest.fixture(autouse=True)
def isolated_readiness():
    readiness._reset_for_tests()
    yield
    readiness._reset_for_tests()
    base_module._MAINTENANCE = None


@pytest.fixture
def consumer_state(monkeypatch):
    """Set the canonical consumer state (owned by ``app.main``) for one test."""
    def _set(state: str) -> None:
        monkeypatch.setattr(main_module, "_consumer_state", state)

    return _set


def test_create_redis_pool_requires_a_real_ping(monkeypatch):
    """``from_url`` is lazy: only a real PING may be recorded as success."""
    client = _PingRedis()
    _install_fake_redis(monkeypatch, client)

    pool = asyncio.run(base_module.create_redis_pool())

    assert pool is client
    assert client.ping_calls == 1, "from_url alone must not count as connected"
    assert readiness.snapshot()["redis_last_ok_age_s"] is not None


def test_create_redis_pool_fails_loudly_when_redis_is_unreachable(monkeypatch):
    client = _PingRedis(ping_error=ConnectionError("redis down"))
    _install_fake_redis(monkeypatch, client)

    with pytest.raises(ConnectionError):
        asyncio.run(base_module.create_redis_pool())

    assert client.ping_calls == 1
    assert client.closed == 1, "a client that never connected must be released"
    assert readiness.snapshot()["redis_last_ok_age_s"] is None


def test_startup_with_unreachable_redis_returns_503_never_false_ready(monkeypatch):
    """Review repro ``{"ready":true,"reason":"recent_activity","pingCalls":0}``.

    Startup runs the *real* ``consume_jobs`` against a Redis whose PING fails.
    Afterwards /ready must be 503 with a consumer-state reason, the passive
    signal must stay empty, and the startup PING must really have been issued
    (``pingCalls > 0``) — otherwise "no activity" and "never tried" would look
    the same.
    """
    startup_redis = _PingRedis(ping_error=ConnectionError("no route to redis"))
    _install_fake_redis(monkeypatch, startup_redis)
    readiness.bind_consumer_state(None)

    def must_not_ping() -> None:
        raise AssertionError(
            "a consumer that failed to start must be 503 by state, no probe")

    monkeypatch.setattr(readiness, "_ping", must_not_ping)

    try:
        with pytest.raises(ConnectionError):
            asyncio.run(base_module.consume_jobs())

        ready, reason, details = asyncio.run(readiness.evaluate())

        assert startup_redis.ping_calls > 0, (
            "startup must have attempted a real PING, not trusted from_url()")
        assert ready is False
        assert reason == "consumer_failed"
        assert details["redis_last_ok_age_s"] is None
    finally:
        readiness.bind_consumer_state(main_module._read_consumer_state,
                                      main_module._write_consumer_state)


def test_ready_pings_for_real_when_the_passive_signal_is_missing(
        monkeypatch, client, consumer_state):
    """A "running" consumer with no proven round-trip must be judged by a PING.

    This is the second half of the review repro: readiness may still answer
    ready, but only after an actual PING counted ``pingCalls > 0``.
    """
    pings: list[int] = []

    async def ok_ping() -> None:
        pings.append(1)

    monkeypatch.setattr(readiness, "_ping", ok_ping)
    consumer_state("running")
    readiness._reset_for_tests(consumer_state="running")
    # from_url() alone must never have produced a passive success signal.
    assert readiness.snapshot()["redis_last_ok_age_s"] is None

    response = client.get("/ready")
    body = response.json()

    assert response.status_code == 200
    assert body["reason"] == "ping_ok"
    assert pings == [1], "readiness must have performed exactly one real PING"
    assert body["details"]["redis_last_ok_age_s"] is not None


def test_consume_jobs_marks_failed_when_redis_is_unreachable(monkeypatch):
    """A failed pool must set ``failed`` and re-raise, never ``running``."""
    seen: list[str] = []

    async def failing_pool():
        seen.append(readiness.snapshot()["consumer_state"])
        raise ConnectionError("redis down")

    monkeypatch.setattr(base_module, "create_redis_pool", failing_pool)
    readiness.bind_consumer_state(None)

    try:
        with pytest.raises(ConnectionError):
            asyncio.run(base_module.consume_jobs())
        assert seen == ["starting"]
        assert readiness.snapshot()["consumer_state"] == "failed"
    finally:
        readiness.bind_consumer_state(main_module._read_consumer_state,
                                      main_module._write_consumer_state)


def test_consume_jobs_marks_failed_when_group_join_fails(monkeypatch):
    """Connected but not joined is still not able to consume: state is failed."""

    class Redis:
        async def ping(self):
            return True

        async def aclose(self):
            return None

    async def fake_pool():
        return Redis()

    async def failing_join(redis):
        raise base_module.aioredis.ResponseError("ERR unknown command")

    monkeypatch.setattr(base_module, "create_redis_pool", fake_pool)
    monkeypatch.setattr(base_module, "join_consumer_group", failing_join)
    readiness.bind_consumer_state(None)

    try:
        with pytest.raises(base_module.aioredis.ResponseError):
            asyncio.run(base_module.consume_jobs())
        assert readiness.snapshot()["consumer_state"] == "failed"
    finally:
        readiness.bind_consumer_state(main_module._read_consumer_state,
                                      main_module._write_consumer_state)


def test_group_join_failure_makes_ready_503(monkeypatch):
    """Connected but not joined must be 503 through the real HTTP probe."""
    ready_states: list[str] = []

    class Redis:
        async def ping(self):
            return True

        async def aclose(self):
            return None

    async def fake_pool():
        return Redis()

    async def failing_join(redis):
        raise base_module.aioredis.ResponseError(
            "BUSYGROUP is not this error; joining genuinely failed")

    monkeypatch.setattr(base_module, "create_redis_pool", fake_pool)
    monkeypatch.setattr(base_module, "join_consumer_group", failing_join)
    readiness.bind_consumer_state(None)

    def must_not_ping() -> None:
        raise AssertionError("a failed join is 503 by state; no probe needed")

    monkeypatch.setattr(readiness, "_ping", must_not_ping)

    try:
        with pytest.raises(base_module.aioredis.ResponseError):
            asyncio.run(base_module.consume_jobs())

        ready, reason, _ = asyncio.run(readiness.evaluate())
        ready_states.append(reason)

        assert ready is False
        assert reason == "consumer_failed"
    finally:
        readiness.bind_consumer_state(main_module._read_consumer_state,
                                      main_module._write_consumer_state)


def test_successful_join_makes_ready_true(monkeypatch):
    """The happy path: a real PING + a real group join -> state running -> 200."""
    observed: dict = {}

    class Redis:
        def __init__(self) -> None:
            self.group_joined = False

        async def ping(self):
            return True

        async def xgroup_create(self, *args, **kwargs):
            self.group_joined = True
            return True

        async def aclose(self):
            return None

        async def xreadgroup(self, **kwargs):
            # Sample the state from inside the loop, i.e. after the join.
            observed["state"] = readiness.snapshot()["consumer_state"]
            raise asyncio.CancelledError

    redis = Redis()
    _install_fake_redis(monkeypatch, redis)
    readiness.bind_consumer_state(None)

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(base_module.consume_jobs())

        assert redis.group_joined is True
        assert observed["state"] == "running", (
            "only a successful join may promote the consumer to running")
    finally:
        readiness.bind_consumer_state(main_module._read_consumer_state,
                                      main_module._write_consumer_state)


def test_starting_state_is_not_ready_and_does_not_ping(monkeypatch, consumer_state):
    pings: list[int] = []
    monkeypatch.setattr(readiness, "_ping", lambda: pings.append(1))
    consumer_state("starting")
    readiness._reset_for_tests(consumer_state="starting")
    # A stale-but-true Redis signal must not rescue a consumer that has not
    # joined the group yet.
    readiness.note_redis_ok()

    ready, reason, details = asyncio.run(readiness.evaluate())

    assert ready is False
    assert reason == "consumer_starting"
    assert details["consumer_state"] == "starting"
    assert pings == []


def test_supervisor_no_longer_forces_running(monkeypatch):
    """The supervisor must leave the state to ``consume_jobs`` (R06).

    It starts the consumer task and immediately reports "starting"; only the
    group join inside ``consume_jobs`` may promote it to "running".
    """
    observed: list[str] = []

    async def fake_consumer() -> None:
        observed.append(main_module._read_consumer_state())
        main_module._set_consumer_state("running")
        await asyncio.sleep(3600)

    monkeypatch.setattr(main_module, "consume_jobs", fake_consumer)
    monkeypatch.setattr(main_module, "_consumer_state", "stopped")
    monkeypatch.setattr(main_module, "HEALTHY_RUN_RESET_SECONDS", 3600.0)

    async def scenario() -> None:
        task = asyncio.create_task(main_module._supervise_consumer())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if observed:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert observed == ["starting"], (
        "the consumer must observe 'starting', not a supervisor-forced 'running'")


def test_note_consumer_state_reaches_the_authoritative_reader(monkeypatch):
    """Worker state transitions must not be shadowed by the bound reader."""
    monkeypatch.setattr(main_module, "_consumer_state", "running")
    readiness.bind_consumer_state(main_module._read_consumer_state,
                                  main_module._write_consumer_state)
    try:
        readiness.note_consumer_state("starting")
        assert main_module._read_consumer_state() == "starting"
        assert readiness.snapshot()["consumer_state"] == "starting"

        readiness.note_consumer_state("running")
        assert main_module._read_consumer_state() == "running"
    finally:
        readiness.note_consumer_state("stopped")


# ---------------------------------------------------------------------------
# R05: maintenance runs independently of a long inference, inference stays serial
# ---------------------------------------------------------------------------

class _LoopRedis:
    """Async Redis stub whose XREADGROUP blocks until the test cancels it."""

    def __init__(self) -> None:
        self.closed = 0
        self.reads = 0

    async def ping(self):
        return True

    async def xreadgroup(self, **kwargs):
        self.reads += 1
        await asyncio.sleep(0.01)
        return []

    async def xautoclaim(self, *args, **kwargs):
        return ("0-0", [], [])

    async def xlen(self, stream):
        return 0

    async def xpending(self, stream, group):
        return {"pending": 0}

    async def xack(self, stream, group, msg_id):
        return 1

    async def xdel(self, stream, msg_id):
        return 1

    async def aclose(self):
        self.closed += 1


def test_maintenance_retries_callbacks_while_an_inference_is_blocked(monkeypatch):
    """The review's core R05 symptom: callbacks wait for the whole inference."""
    callback_rounds: list[int] = []
    inference_started = asyncio.Event()
    release_inference = asyncio.Event()
    concurrency = {"now": 0, "max": 0}

    async def fake_retry_results(limit=base_module.PENDING_RESULT_SWEEP_LIMIT):
        callback_rounds.append(1)
        return 0

    async def fake_retry_legacy(redis):
        return 0

    async def slow_handler(job):
        concurrency["now"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["now"])
        inference_started.set()
        try:
            await release_inference.wait()
        finally:
            concurrency["now"] -= 1
        return {"status": "succeeded"}

    async def fake_claim(job_id):
        return {"claimed": True, "attempt": 1, "status": "RUNNING"}

    async def fake_save(job_id, attempt, result, **kwargs):
        from app.workers.result_store import ResultRef

        return ResultRef(job_id=job_id, attempt=attempt, bucket="b",
                         result_key="k", envelope_key="e",
                         sha256="a" * 64, bytes=1)

    async def fake_mark(ref, **meta):
        return None

    async def fake_notify(job_id, result):
        return {"applied": True, "status": "SUCCEEDED"}

    async def fake_heartbeat(job_id, attempt):
        return {"extended": True}

    monkeypatch.setattr(base_module, "_retry_pending_results", fake_retry_results)
    monkeypatch.setattr(base_module, "_retry_pending_callbacks", fake_retry_legacy)
    monkeypatch.setattr(base_module, "_claim_job", fake_claim)
    monkeypatch.setattr(base_module, "_notify_backend", fake_notify)
    monkeypatch.setattr(base_module, "_heartbeat_job", fake_heartbeat)
    monkeypatch.setattr(base_module, "CALLBACK_RETRY_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(base_module, "MAINTENANCE_TICK_SECONDS", 0.01)
    monkeypatch.setattr(base_module, "QUEUE_DEPTH_INTERVAL_SECONDS", 0.0)
    from app.workers import result_store

    monkeypatch.setattr(result_store, "save_result_async", fake_save)
    monkeypatch.setattr(result_store, "mark_pending_async", fake_mark)

    redis = _LoopRedis()
    job = {"id": "job-long", "type": "VIDEO_ANALYSIS"}

    async def fake_pool():
        return redis

    async def fake_join(r):
        return None

    monkeypatch.setattr(base_module, "create_redis_pool", fake_pool)
    monkeypatch.setattr(base_module, "join_consumer_group", fake_join)

    readiness.bind_consumer_state(None)
    try:
        async def scenario():
            real_xread = redis.xreadgroup
            call = {"n": 0}

            async def xreadgroup(**kwargs):
                call["n"] += 1
                if call["n"] == 1:
                    return [[base_module.settings.job_stream, [("1-0", dict(job))]]]
                return await real_xread(**kwargs)

            redis.xreadgroup = xreadgroup
            task = asyncio.create_task(base_module.consume_jobs(slow_handler))
            try:
                await asyncio.wait_for(inference_started.wait(), timeout=5)
                # The handler is still blocked; maintenance must already have
                # completed at least one callback-retry round.
                for _ in range(300):
                    if callback_rounds:
                        break
                    await asyncio.sleep(0.01)
                assert callback_rounds, (
                    "maintenance must run while the inference is still blocked")
                assert concurrency["max"] == 1, "no parallel inference"
                assert not release_inference.is_set()
            finally:
                release_inference.set()
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - shutdown path
                    pass

        asyncio.run(scenario())

        assert concurrency["max"] == 1
        assert redis.closed == 1, "the consumer closes its client on shutdown (L01)"
    finally:
        readiness.bind_consumer_state(main_module._read_consumer_state,
                                      main_module._write_consumer_state)


def test_maintenance_queues_reclaimed_messages_for_the_serial_loop(monkeypatch):
    """Reclaimed messages are handed to the main loop, never processed inline."""
    queued: list[tuple[str, dict]] = []
    processed: list[tuple[str, dict]] = []

    class Redis:
        async def xautoclaim(self, *args, **kwargs):
            return ("0-0", [("7-0", {"id": "job-7", "type": "VIDEO_ANALYSIS"})], [])

        async def xlen(self, stream):
            return 1

        async def xpending(self, stream, group):
            return {"pending": 1}

    async def fake_retry_results(limit=base_module.PENDING_RESULT_SWEEP_LIMIT):
        return 0

    async def fake_retry_legacy(redis):
        return 0

    monkeypatch.setattr(base_module, "_retry_pending_results", fake_retry_results)
    monkeypatch.setattr(base_module, "_retry_pending_callbacks", fake_retry_legacy)

    maintenance = base_module._Maintenance(
        Redis(), consumer_id="c1", callback_interval=0.0, reclaim_interval=0.0,
        depth_interval=0.0, tick_interval=0.01)

    async def scenario():
        task = asyncio.create_task(maintenance.run())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not maintenance.reclaimed.empty():
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        queued.extend(maintenance.drain_reclaimed())

    asyncio.run(scenario())

    assert queued == [("7-0", {"id": "job-7", "type": "VIDEO_ANALYSIS"})]
    assert processed == [], "the maintenance task must never process a message itself"


def test_maintenance_loop_survives_a_failing_round(monkeypatch):
    """A raising maintenance step must be logged, not kill the task."""
    calls: list[int] = []

    async def boom(limit=base_module.PENDING_RESULT_SWEEP_LIMIT):
        calls.append(1)
        raise ConnectionError("redis down")

    async def ok(redis):
        return 0

    monkeypatch.setattr(base_module, "_retry_pending_results", boom)
    monkeypatch.setattr(base_module, "_retry_pending_callbacks", ok)

    class Redis:
        async def xlen(self, stream):
            return 0

        async def xpending(self, stream, group):
            return {"pending": 0}

    maintenance = base_module._Maintenance(
        Redis(), callback_interval=0.0, depth_interval=1e9, tick_interval=0.01)

    async def scenario():
        task = asyncio.create_task(maintenance.run())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(calls) >= 3:
                break
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    assert asyncio.run(scenario()) is True
    assert len(calls) >= 3, "the maintenance task must keep running after failures"


# ---------------------------------------------------------------------------
# L01: explicit client shutdown
# ---------------------------------------------------------------------------

def test_close_redis_client_prefers_aclose_and_swallows_errors():
    class Client:
        def __init__(self) -> None:
            self.aclosed = 0

        async def aclose(self):
            self.aclosed += 1

    class LegacyClient:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self):
            self.closed += 1

    class Broken:
        async def aclose(self):
            raise RuntimeError("already closed")

    client = Client()
    legacy = LegacyClient()
    asyncio.run(base_module._close_redis(client))
    asyncio.run(base_module._close_redis(legacy))
    asyncio.run(base_module._close_redis(Broken()))   # must not raise
    asyncio.run(base_module._close_redis(None))

    assert client.aclosed == 1
    assert legacy.closed == 1
