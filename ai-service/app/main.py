"""
MACT-Eval AI Service - Main Application
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core import readiness
from app.core.config import settings
from app.api.routes import router as api_router
from app.workers.base import consume_jobs

logger = logging.getLogger(__name__)

#: Consecutive consumer failures before the process gives up and lets Docker's
#: restart policy take over. A long healthy run resets the counter.
MAX_CONSECUTIVE_CONSUMER_FAILURES = 5
HEALTHY_RUN_RESET_SECONDS = 60.0


def _configure_logging() -> None:
    """Make worker logs visible.

    Uvicorn configures only its own loggers, so without a root handler every
    ``app.*`` INFO line (job picked up, analysis finished, why it failed) is
    dropped and a dead consumer looks like a silent but healthy service.
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


_configure_logging()

_supervisor_task: Optional[asyncio.Task] = None
_consumer_task: Optional[asyncio.Task] = None
#: Single source of truth for the consumer state. ``readiness`` reads this exact
#: variable through ``_read_consumer_state`` below, so probes and the supervisor
#: can never disagree; ``_set_consumer_state`` only keeps the diagnostic clock.
_consumer_state: str = "stopped"
_consumer_restarts: int = 0


def _read_consumer_state() -> str:
    return _consumer_state


def _write_consumer_state(state: str) -> None:
    """Mirror ``readiness.note_consumer_state`` into the authoritative variable.

    Bound as the writer so the worker's own transitions ("starting", "running"
    only after the group join, "failed") reach the same value
    ``_read_consumer_state`` returns — otherwise a supervisor-set value would
    keep answering /ready while the consumer is still connecting (R06).
    """
    global _consumer_state
    _consumer_state = state


def _set_consumer_state(state: str) -> None:
    global _consumer_state
    _consumer_state = state
    readiness.note_consumer_state(state, _consumer_restarts)


readiness.bind_consumer_state(_read_consumer_state, _write_consumer_state)


def _consumer_done(task: asyncio.Task) -> None:
    """Surface a dead consumer instead of leaving the service silently idle.

    Kept as a small, directly testable helper: a crashed ``consume_jobs`` task
    must never be invisible to health checks.
    """
    if task.cancelled():
        _set_consumer_state("stopped")
        logger.info("Job consumer stopped")
        return
    _set_consumer_state("crashed")
    exc = task.exception()
    if exc is None:
        logger.error(
            "Job consumer exited without an exception; queued jobs will not be "
            "consumed until the service is restarted"
        )
    else:
        logger.error(
            "Job consumer crashed; queued jobs will not be consumed until the "
            "service is restarted: %s",
            exc,
            exc_info=exc,
        )


async def _supervise_consumer() -> None:
    """Restart the consumer after unexpected exits.

    ``consume_jobs`` normally loops forever; this supervisor exists for the
    cases where it returns/raises anyway (programming error, unrecoverable
    dependency loss). After repeated failures the process exits so the
    container restart policy can recreate a clean process.
    """
    global _consumer_task, _consumer_restarts
    failures = 0
    while True:
        started = time.monotonic()
        _consumer_task = asyncio.create_task(consume_jobs())
        # "starting", never "running": the task exists but has not necessarily
        # reached Redis or joined the consumer group yet. ``consume_jobs`` sets
        # "running" itself after the group join succeeds and "failed" when it
        # does not, so /ready cannot be satisfied by a consumer that only
        # exists (R06).
        _set_consumer_state("starting")
        logger.info("Job consumer supervisor started stream '%s'", settings.job_stream)
        try:
            await _consumer_task
            _set_consumer_state("crashed")
            logger.error(
                "Job consumer exited unexpectedly; queued jobs are not being consumed"
            )
        except asyncio.CancelledError:
            _set_consumer_state("stopped")
            logger.info("Job consumer supervisor cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - supervisor must observe failures
            _set_consumer_state("crashed")
            logger.error("Job consumer crashed: %s", exc, exc_info=exc)

        if time.monotonic() - started >= HEALTHY_RUN_RESET_SECONDS:
            failures = 0
        failures += 1
        _consumer_restarts += 1
        readiness.note_consumer_state(_consumer_state, _consumer_restarts)
        if failures >= MAX_CONSECUTIVE_CONSUMER_FAILURES:
            _set_consumer_state("failed")
            logger.critical(
                "Job consumer failed %s times consecutively; exiting so the "
                "container restart policy can recreate the process",
                failures,
            )
            logging.shutdown()
            # Returning from the supervisor would leave uvicorn serving
            # /live forever; Docker restart policies only react to process exit.
            # Under pytest we must not kill the test runner; the test asserts
            # state via /ready instead.
            if os.environ.get("PYTEST_CURRENT_TEST"):
                return
            os._exit(1)
        backoff = min(30, 2 ** failures)
        logger.warning(
            "Restarting job consumer in %ss (failure %s/%s)",
            backoff, failures, MAX_CONSECUTIVE_CONSUMER_FAILURES,
        )
        await asyncio.sleep(backoff)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _supervisor_task
    _supervisor_task = asyncio.create_task(_supervise_consumer())
    yield
    if _supervisor_task is not None:
        _supervisor_task.cancel()
        try:
            await _supervisor_task
        except asyncio.CancelledError:
            pass


def _log_readiness_degraded(reason: str, details: dict) -> None:
    """Log why /ready answered 503, without echoing secrets.

    ``details`` only carries states, ages and counters (see
    ``readiness.snapshot``); the DSN, service token and signed URLs are never
    part of it. ``consumer_*`` reasons are already logged by the supervisor, so
    they are reported at INFO to avoid duplicating an ERROR line per probe.
    """
    level = logger.info if str(reason).startswith("consumer_") else logger.warning
    level(
        "Readiness degraded (%s): consumer=%s restarts=%s redis_last_ok_age_s=%s "
        "queued=%s pending=%s inference_in_progress=%s",
        reason, details.get("consumer_state"), details.get("consumer_restarts"),
        details.get("redis_last_ok_age_s"), details.get("queued"),
        details.get("pending"), details.get("inference_in_progress"),
    )


app = FastAPI(
    title="MACT-Eval AI Service",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/live")
async def live():
    """Liveness: the HTTP process is up. Never depends on Redis or models.

    A failed dependency must never trigger a restart storm; only /ready may
    report 503 for a Redis outage.
    """
    return {"status": "ok", "service": settings.service_name}


@app.get("/health")
async def health():
    """Backward-compatible health endpoint with an explicit readiness flag.

    Stays 200 even when the consumer is down so liveness probes do not restart
    a service that is merely waiting for Redis. ``ready`` here is the passive
    view (consumer task running) and performs no network I/O; ``/ready`` is the
    authoritative probe and also checks recent queue communication.
    """
    details = readiness.snapshot()
    state = details["consumer_state"]
    return {
        "status": "ok" if state == "running" else "degraded",
        "ready": state == "running",
        "service": settings.service_name,
        "job_consumer": state,
    }


@app.get("/ready")
async def ready():
    """Readiness: can this instance actually consume analysis jobs right now?

    200 only when the consumer task is running *and* Redis was contacted
    recently (or an active PING succeeds). See protocol §7.
    """
    ready_flag, reason, details = await readiness.evaluate()
    if not ready_flag:
        _log_readiness_degraded(reason, details)
    body = {
        "status": "ok" if ready_flag else "degraded",
        "ready": ready_flag,
        "service": settings.service_name,
        "job_consumer": details.get("consumer_state"),
        "reason": reason,
        "details": details,
    }
    return JSONResponse(status_code=200 if ready_flag else 503, content=body)
