"""Real readiness for the job consumer (protocol §7).

``/live`` only proves the process is alive; ``/ready`` must also prove that this
instance can still talk to Redis. A consumer whose stream reads started failing
used to keep answering 200 forever because the state was still ``"running"``.

The worker calls the ``note_*`` functions on the paths that already talk to
Redis; probes read :func:`snapshot` without any network I/O, and only
:func:`evaluate` performs an active ``PING`` (off the event loop) when the
passive signal is stale.

Frozen shared seam — see docs/operations/result-delivery-and-recovery-protocol.md
§8. Keep the signatures stable; other agents call these functions read-only.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from typing import Any, Callable

from app.core.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()

#: Last successful Redis communication (``time.monotonic()``), or ``None``.
_last_redis_ok: float | None = None
#: Last Redis failure class name; never the exception text (it can carry a DSN).
_last_redis_error: str | None = None
#: Last consumed stream message (``time.monotonic()``), or ``None``.
_last_message_consumed: float | None = None
#: Total messages consumed since process start (diagnostics only).
_messages_consumed: int = 0
#: True while a job handler is running; a busy consumer is not a dead consumer.
_inference_in_progress: bool = False
_last_inference_started: float | None = None
_last_inference_finished: float | None = None
_consumer_state: str = "stopped"
_consumer_restarts: int = 0
_queued: int | None = None
_pending: int | None = None
#: Optional live reader owned by ``app.main``. When bound, it is the single
#: source of truth for the consumer state so /ready and /health cannot disagree.
_state_reader: Callable[[], str] | None = None
#: Optional writer that mirrors :func:`note_consumer_state` into the same
#: authoritative variable. Without it the worker's own transitions ("starting",
#: "running" after the group join, "failed") would be *overridden* by the bound
#: reader and /ready could report a state nobody set (R06).
_state_writer: Callable[[str], None] | None = None


def bind_consumer_state(reader: Callable[[], str] | None,
                        writer: Callable[[str], None] | None = None) -> None:
    """Bind the authoritative consumer-state reader (``app.main`` owns the var).

    ``writer`` (optional) is the matching setter, so
    :func:`note_consumer_state` reaches the same variable the reader returns.
    Passing ``None`` for ``reader`` unbinds both and falls back to the value
    recorded through :func:`note_consumer_state`.
    """
    global _state_reader, _state_writer
    with _lock:
        _state_reader = reader
        _state_writer = writer if reader is not None else None


def _current_state() -> str:
    reader = _state_reader
    if reader is not None:
        try:
            return str(reader() or "unknown")
        except Exception:  # noqa: BLE001 - a broken reader must not break probes
            logger.exception("Consumer state reader failed")
    return _consumer_state


def _age_s(then: float | None, now: float) -> float | None:
    if then is None:
        return None
    return round(max(0.0, now - then), 3)


def note_redis_ok() -> None:
    """Record a successful Redis round-trip (connect / stream read / claim)."""
    global _last_redis_ok, _last_redis_error
    with _lock:
        _last_redis_ok = time.monotonic()
        _last_redis_error = None


def note_redis_error(exc: BaseException | str | None = None) -> None:
    """Record a Redis failure.

    The fresh-success signal is invalidated so the next probe performs an
    active PING instead of trusting a timestamp from before the failure.
    Only the exception class is retained: the message may contain credentials.
    """
    global _last_redis_ok, _last_redis_error
    name = type(exc).__name__ if isinstance(exc, BaseException) else (str(exc) or "error")
    with _lock:
        _last_redis_ok = None
        _last_redis_error = name


def note_message_consumed(n: int = 1) -> None:
    """Record that ``n`` stream messages were read (XREADGROUP / XAUTOCLAIM).

    Reading from the stream is itself a successful Redis round-trip, so this
    also refreshes the passive readiness signal.
    """
    global _last_message_consumed, _messages_consumed, _last_redis_ok, _last_redis_error
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = 1
    if count <= 0:
        return
    now = time.monotonic()
    with _lock:
        _last_message_consumed = now
        _messages_consumed += count
        _last_redis_ok = now
        _last_redis_error = None


def note_inference_started() -> None:
    global _inference_in_progress, _last_inference_started
    with _lock:
        _inference_in_progress = True
        _last_inference_started = time.monotonic()


def note_inference_finished() -> None:
    global _inference_in_progress, _last_inference_finished
    with _lock:
        _inference_in_progress = False
        _last_inference_finished = time.monotonic()


def note_consumer_state(state: str, restarts: int | None = None) -> None:
    """Set the consumer state (and optionally the restart counter).

    When ``app.main`` bound a writer, the value is mirrored there so the
    authoritative reader returns it: the worker's "starting"/"running"/"failed"
    transitions must not be shadowed by a stale supervisor value (R06).
    """
    global _consumer_state, _consumer_restarts
    value = str(state or "unknown")
    with _lock:
        _consumer_state = value
        if restarts is not None:
            try:
                _consumer_restarts = max(_consumer_restarts, int(restarts))
            except (TypeError, ValueError):
                pass
        writer = _state_writer
    if writer is not None:
        try:
            writer(value)
        except Exception:  # noqa: BLE001 - a broken writer must not break probes
            logger.exception("Consumer state writer failed")


def note_queue_depth(queued: int | None, pending: int | None) -> None:
    """Best-effort XLEN / XPENDING counts; ``None`` means unknown, not zero."""
    global _queued, _pending
    with _lock:
        _queued = _coerce_optional_int(queued)
        _pending = _coerce_optional_int(pending)


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def snapshot(now: float | None = None) -> dict:
    """Thread-safe, network-free view of the readiness inputs."""
    moment = time.monotonic() if now is None else now
    state = _current_state()
    with _lock:
        return {
            "consumer_state": state,
            "consumer_restarts": _consumer_restarts,
            "redis_last_ok_age_s": _age_s(_last_redis_ok, moment),
            "queued": _queued,
            "pending": _pending,
            "inference_in_progress": _inference_in_progress,
            "last_message_consumed_age_s": _age_s(_last_message_consumed, moment),
        }


async def _default_ping() -> None:
    """Active Redis PING using a short-lived client.

    Timeouts are bounded by the readiness setting so one probe cannot queue
    behind a blackholed connection.
    """
    import redis.asyncio as aioredis

    timeout = float(settings.ready_ping_timeout_seconds)
    client = aioredis.from_url(
        settings.redis_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    try:
        await client.ping()
    finally:
        closer = getattr(client, "aclose", None)
        try:
            if closer is not None:
                await closer()
            else:  # pragma: no cover - redis-py < 5
                await client.close()
        except Exception:  # noqa: BLE001 - closing a broken client is best effort
            pass


#: Module-level seam so tests can replace the ping without a real Redis.
_ping: Callable[..., Any] = _default_ping


def _invoke_ping() -> None:
    """Run the injected ping inside a worker thread, with its own event loop."""
    result = _ping()
    if inspect.isawaitable(result):
        asyncio.run(_await_result(result))


async def _await_result(awaitable: Any) -> Any:
    return await awaitable


def _stale_seconds() -> float:
    try:
        return float(settings.ready_stale_seconds)
    except (TypeError, ValueError):
        return 15.0


def _ping_timeout_seconds() -> float:
    try:
        timeout = float(settings.ready_ping_timeout_seconds)
    except (TypeError, ValueError):
        timeout = 2.0
    return timeout + 1


async def evaluate(now: float | None = None) -> tuple[bool, str, dict]:
    """Decide whether this instance can consume jobs right now.

    Returns ``(ready, reason, details)``; ``details`` never contains a DSN,
    password, service token or signed URL.
    """
    moment = time.monotonic() if now is None else now

    state = _current_state()
    with _lock:
        last_ok = _last_redis_ok

    details = snapshot(moment)

    if state != "running":
        return False, f"consumer_{state}", details

    age = None if last_ok is None else moment - last_ok
    if age is not None and age <= _stale_seconds():
        return True, "recent_activity", details

    # Passive signal is stale (or invalidated by a recorded error): prove the
    # connection with one bounded PING, always off the event loop. A long
    # inference keeps the consumer busy but does not make Redis unreachable,
    # so it is deliberately not treated as a failure here.
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_invoke_ping),
            timeout=_ping_timeout_seconds(),
        )
    except Exception as exc:  # noqa: BLE001 - any ping failure is "not ready"
        note_redis_error(exc)
        logger.warning("Readiness ping failed: %s", type(exc).__name__)
        return False, "redis_unreachable", snapshot(moment)

    note_redis_ok()
    return True, "ping_ok", snapshot(moment)


def _reset_for_tests(
    *,
    consumer_state: str = "stopped",
    consumer_restarts: int = 0,
    redis_last_ok_age_s: float | None = None,
    message_consumed_age_s: float | None = None,
    inference_in_progress: bool = False,
    queued: int | None = None,
    pending: int | None = None,
    redis_error: str | None = None,
) -> None:
    """Reset the module state between tests (never called by production code).

    When ``app.main`` has bound its reader (the normal case), that reader stays
    authoritative; ``consumer_state`` here only seeds the fallback used when no
    reader is bound.
    """
    global _last_redis_ok, _last_redis_error, _last_message_consumed
    global _messages_consumed, _inference_in_progress, _last_inference_started
    global _last_inference_finished, _consumer_state, _consumer_restarts
    global _queued, _pending

    current = time.monotonic()
    with _lock:
        _consumer_state = consumer_state
        _consumer_restarts = consumer_restarts
        _last_redis_ok = (
            None if redis_last_ok_age_s is None else current - redis_last_ok_age_s
        )
        _last_redis_error = redis_error
        _last_message_consumed = (
            None if message_consumed_age_s is None else current - message_consumed_age_s
        )
        _messages_consumed = 0
        _inference_in_progress = inference_in_progress
        _last_inference_started = current if inference_in_progress else None
        _last_inference_finished = None
        _queued = queued
        _pending = pending
