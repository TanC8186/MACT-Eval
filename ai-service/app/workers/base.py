"""Job queue worker base for consuming Redis Streams.

Message lifecycle (docs/operations/result-delivery-and-recovery-protocol.md §4)::

    replay stored result -> claim -> inference -> save result (MinIO)
    -> index (Redis) -> backend callback -> ACK

The invariant that matters most is **never ACK before the result is durable**:
an ACKed message can vanish together with Redis, so the object in MinIO is the
only copy a recovery path can use. The ACK is the last step of a message, issued
after the callback outcome is known. A ``stale_attempt`` / ``not_running`` /
``invalid_attempt`` rejection must leave the message in the PEL (it may be the
only copy for a newer attempt), and that is impossible once the message has been
XDELed. This is stricter than, and compatible with, the durability rule §4
states first; the protocol's own "stale_attempt 不 ACK、不删当前消息" row requires
it.

Redis only holds a *rebuildable* index of stored results
(:mod:`app.workers.result_store`); the database plus the MinIO objects stay
authoritative. ``analysis:pending_callback:*`` keys written by the pre-R05
worker are still retried as a transitional second path.

Fault-drill hooks (never set in a normal deployment; used by the F06 scenario):

* ``MACT_AI_FAULT_STOP_AFTER_RESULT_PUT=1`` — the process exits (``os._exit``)
  right after ``save_result`` succeeded and before ``mark_pending``/ACK, i.e. the
  "object written, index missing, message still pending" crash window.
* ``MACT_AI_FAULT_STOP_AFTER_MARK_PENDING=1`` — the same crash one step later,
  after the Redis index exists but before the callback/ACK.

Both hooks route through the module-level ``_fault_exit`` seam so unit tests can
observe the call without killing the test process.
"""
from __future__ import annotations

import json
import os
import asyncio
import itertools
import logging
import tempfile
import time
from datetime import datetime
from typing import Any, Callable, Awaitable

import httpx
import redis.asyncio as aioredis

from app.core import readiness
from app.core.config import settings
from app.core.paths import media_work_dir
from app.workers import result_store
from app.workers.courseware import parse_courseware
from app.workers.vision import analyze_video
from app.workers.grading import grade_submission
from app.workers.prep import analyze_prep

logger = logging.getLogger(__name__)

JobHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


#: How many times to retry joining the consumer group before giving up.
REDIS_CONNECT_ATTEMPTS: int = 10
#: Seconds to wait between consumer-group join attempts.
REDIS_CONNECT_BACKOFF_SECONDS: float = 3.0
#: Execution lease and heartbeat interval. A long inference must renew its
#: lease so another consumer cannot take over while it is still running.
JOB_LEASE_SECONDS: int = 120
JOB_HEARTBEAT_INTERVAL_SECONDS: int = 30
#: Legacy (pre-R05) retry path: failed backend callbacks were stored in Redis
#: and retried without re-running the (expensive) inference.
PENDING_CALLBACK_SET: str = "analysis:pending_callbacks"
PENDING_CALLBACK_KEY: str = "analysis:pending_callback:{}"
TERMINAL_JOB_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}

#: Inline HTTP attempts for one callback before the result is left to the
#: background retry / database recovery.
CALLBACK_HTTP_ATTEMPTS: int = 3
#: Linear backoff unit between inline callback attempts (seconds).
CALLBACK_RETRY_BACKOFF_SECONDS: float = 1.0
#: Background job timers, decoupled from the loop-iteration count so a long
#: inference does not starve callback retries or reclaim (protocol §5 rule 8).
CALLBACK_RETRY_INTERVAL_SECONDS: float = 10.0
RECLAIM_INTERVAL_SECONDS: float = 30.0
QUEUE_DEPTH_INTERVAL_SECONDS: float = 10.0
#: Resolution of the independent maintenance task (R05). Small enough that a
#: due callback retry starts promptly, large enough to stay idle-cheap.
MAINTENANCE_TICK_SECONDS: float = 1.0
#: Idle time before another consumer's message can be reclaimed.
RECLAIM_MIN_IDLE_MS: int = 60_000
#: Upper bound on indexed results delivered per background sweep. The consumer
#: loop is serial (one worker, no parallel inference), so this stays bounded.
PENDING_RESULT_SWEEP_LIMIT: int = 5

#: Backend rejections that must keep the stream message pending.
CALLBACK_NO_ACK_REASONS = frozenset({"stale_attempt", "not_running", "invalid_attempt"})
#: Rejections that prove the *indexed attempt* was superseded (or never ran), so
#: that attempt's index entry is dead weight and may be dropped before claiming.
#: ``invalid_attempt`` is deliberately absent: the callback body itself carries a
#: malformed attempt, i.e. the stored object is bad — the backend JobResultStore
#: must be the one to mark it INVALID, so the index is kept (F13 semantics).
STALE_INDEX_DROP_REASONS = frozenset({"stale_attempt", "not_running"})

#: Consecutive backend rejections of the *indexed* attempt after which every
#: index entry of that job is dropped and the message is claimed normally. It
#: bounds the A12 livelock: without a cap, one permanently stale index key makes
#: the worker replay forever and never claim.
STALE_REPLAY_LIMIT: int = 3
#: Per-job consecutive stale-replay counter (process-local; reset on success).
_STALE_REPLAY_COUNTS: dict[str, int] = {}
#: Process-local rotation cursor for the background pending-result sweep.
_SWEEP_CURSOR: str | None = None

FAULT_STOP_AFTER_RESULT_PUT_ENV = "MACT_AI_FAULT_STOP_AFTER_RESULT_PUT"
FAULT_STOP_AFTER_MARK_PENDING_ENV = "MACT_AI_FAULT_STOP_AFTER_MARK_PENDING"
#: Test seam: the fault hooks call this instead of ``os._exit`` directly.
_fault_exit: Callable[[int], None] = os._exit


def _is_terminal_status(status: str | None) -> bool:
    return str(status or "").upper() in TERMINAL_JOB_STATUSES


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on", "y")


def _fault_stop(env_name: str, stage: str, job_id: str, attempt: int | None) -> None:
    """Trigger a drill crash when ``env_name`` is set (no-op otherwise).

    Only for the fault-injection scenario: a normal deployment must never set
    these variables, because this exits the process without cleanup.
    """
    if not _truthy_env(env_name):
        return
    logger.error(
        "FAULT INJECTION %s=1: exiting during %s for job %s attempt %s "
        "(this is a drill hook; unset it in any real deployment)",
        env_name, stage, job_id, attempt,
    )
    _fault_exit(137)


async def _close_redis(client: Any) -> None:
    """Close an async Redis client; never raise (shutdown is best effort).

    Without an explicit ``aclose`` the sockets are only released by the garbage
    collector, which then runs ``AbstractConnection.__del__`` on a closed event
    loop and prints "Event loop is closed" (L01).
    """
    if client is None:
        return
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as exc:  # noqa: BLE001 - closing must not mask shutdown
        logger.debug("Closing the Redis client failed: %s", exc)


async def create_redis_pool() -> aioredis.Redis:
    """Create the consumer's Redis client and prove it actually works.

    ``from_url`` is lazy: it builds a client without opening a connection, so
    recording a successful round-trip there made /ready answer 200 for a
    consumer that had never talked to Redis (R06). The explicit ``PING`` is the
    only acceptable proof at this point; on failure the error is recorded and
    re-raised so the supervisor restarts the consumer instead of advertising a
    dead one.

    A paused/blackholed Redis must not block the consumer forever; the socket
    timeouts below bound the handshake.
    """
    client = None
    # A paused/blackholed Redis must not block the consumer forever. The
    # consumer loop already retries with backoff; these bounds make the failure
    # observable instead of leaving /ready green while no job can be consumed.
    try:
        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=30,
            health_check_interval=30,
        )
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - surfaced to /ready, then re-raised
        readiness.note_redis_error(exc)
        logger.error(
            "Redis is not reachable at startup (%s); /ready stays 503 until a "
            "real round-trip succeeds", type(exc).__name__)
        await _close_redis(client)
        raise
    readiness.note_redis_ok()
    return client


def _get_field(job: dict, name: str, default: Any = None) -> Any:
    """
    Read a job field either from the top-level message (flat contract)
    or from the nested `params` JSON string (legacy contract).
    """
    if name in job and job[name] not in (None, ""):
        return job[name]
    params = job.get("params")
    if isinstance(params, str) and params.strip().startswith("{"):
        try:
            parsed = json.loads(params)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and parsed.get(name) not in (None, ""):
            return parsed.get(name)
    return default


def _to_bool(value: Any, default: bool = False) -> bool:
    """Coerce a string/primitive value into a boolean safely."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _sanitize_object_name(object_name: str) -> str:
    """Keep only the basename; reject path traversal in queue payloads."""
    from app.core.paths import safe_basename

    cleaned = safe_basename(object_name)
    if not cleaned:
        raise ValueError(f"unsafe objectName: {object_name!r}")
    return cleaned


def _is_temp_download(path: str | None) -> bool:
    """True when ``path`` is a media copy this service downloaded itself.

    Cleanup must only ever delete those copies, never a business upload.
    The directory comes from settings (MACT_AI_WORK_DIR) so that moving the
    work dir off the system drive cannot silently disable cleanup -- a
    hard-coded "mact-eval" substring check would do exactly that.
    """
    if not path:
        return False
    try:
        root = os.path.abspath(str(media_work_dir()))
        candidate = os.path.abspath(str(path))
        return os.path.commonpath([candidate, root]) == root
    except (OSError, ValueError):
        return False


def _cleanup_local(path: str | None) -> None:
    """Best-effort delete of a temp download after the job finishes."""
    if not path:
        return
    try:
        if os.path.isfile(path) and _is_temp_download(path):
            os.unlink(path)
    except Exception as e:
        logger.debug("Temp cleanup skipped for %s: %s", path, e)


def _download_from_minio(bucket: str, object_name: str) -> str | None:
    """Download a courseware object from MinIO to a local temp file."""
    try:
        from minio import Minio

        safe_name = _sanitize_object_name(object_name)
        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        local_dir = str(media_work_dir())
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, safe_name.replace("/", "_"))
        client.fget_object(bucket, object_name, local_path)
        logger.info("Downloaded %s/%s from MinIO to %s", bucket, object_name, local_path)
        return local_path
    except Exception as e:
        logger.warning("MinIO download failed for %s/%s: %s", bucket, object_name, e)
        return None


def _resolve_courseware_file(job: dict) -> str:
    """Resolve the local file path for a courseware job."""
    from app.core.paths import resolve_under_roots

    explicit = _get_field(job, "file_path")
    if explicit:
        safe = resolve_under_roots(str(explicit))
        if safe:
            return str(safe)
        logger.warning("Rejecting file_path outside allowed roots: %s", explicit)

    object_name = str(_get_field(job, "objectName", ""))
    bucket = str(_get_field(job, "bucket", "teaching-resources"))
    try:
        safe_name = _sanitize_object_name(object_name)
    except ValueError:
        return ""

    # Local upload fallback paths used by the backend (UploadController).
    for base in ("data/uploads", "../data/uploads", "data"):
        candidate = os.path.join(base, safe_name)
        resolved = resolve_under_roots(candidate)
        if resolved:
            return str(resolved)

    # Try to fetch from MinIO.
    downloaded = _download_from_minio(bucket, object_name)
    if downloaded:
        return downloaded

    # Best-guess local path; handler will report FILE_NOT_FOUND if missing.
    return os.path.join("data/uploads", safe_name)


async def _resolve_courseware_file_async(job: dict) -> str:
    """Resolve courseware path; MinIO download runs off the event loop."""
    from app.core.paths import resolve_under_roots

    explicit = _get_field(job, "file_path")
    if explicit:
        safe = resolve_under_roots(str(explicit))
        if safe:
            return str(safe)
        logger.warning("Rejecting file_path outside allowed roots: %s", explicit)

    object_name = str(_get_field(job, "objectName", ""))
    bucket = str(_get_field(job, "bucket", "teaching-resources"))
    if not object_name:
        return ""
    downloaded = await asyncio.to_thread(_download_from_minio, bucket, object_name)
    return downloaded or ""


def _resolve_media_file(job: dict) -> str:
    """Resolve a classroom media object (video/audio) from MinIO to a local temp file."""
    object_name = str(_get_field(job, "objectName", ""))
    bucket = str(_get_field(job, "bucket", "classroom-raw-media"))
    if not object_name:
        return ""
    downloaded = _download_from_minio(bucket, object_name)
    return downloaded or ""


async def _resolve_media_file_async(job: dict) -> str:
    """Async media resolve — MinIO download must not block the event loop."""
    object_name = str(_get_field(job, "objectName", ""))
    bucket = str(_get_field(job, "bucket", "classroom-raw-media"))
    if not object_name:
        return ""
    downloaded = await asyncio.to_thread(_download_from_minio, bucket, object_name)
    return downloaded or ""


async def _handle_courseware(job: dict) -> dict:
    resolved = await _resolve_courseware_file_async(job)
    params = {
        "file_path": resolved,
        "original_name": str(_get_field(job, "originalFileName", _get_field(job, "original_name", "unknown"))),
        "lesson_session_id": str(_get_field(job, "lesson_session_id", _get_field(job, "id", "unknown"))),
        "allow_demo": _to_bool(_get_field(job, "allow_demo", None), settings.demo_enabled),
    }
    try:
        return await asyncio.to_thread(parse_courseware, params)
    finally:
        # 仅清理 MinIO 下载到临时目录的副本，不删业务上传目录
        if _is_temp_download(resolved):
            _cleanup_local(resolved)


async def _handle_prep(job: dict) -> dict:
    params = {
        "full_text": str(_get_field(job, "fullText", _get_field(job, "full_text", "")))[:50000],
        "courseName": str(_get_field(job, "courseName", "")),
        "courseDescription": str(_get_field(job, "courseDescription", "")),
        "department": str(_get_field(job, "department", "")),
        "originalFileName": str(_get_field(job, "originalFileName", "")),
        "objectives": str(_get_field(job, "objectives", "[]")),
        "keyPoints": str(_get_field(job, "keyPoints", "[]")),
    }
    return await analyze_prep(params)


async def _handle_video(job: dict) -> dict:
    from app.core.paths import resolve_under_roots

    raw_path = str(_get_field(job, "video_path", ""))
    if raw_path:
        safe = resolve_under_roots(raw_path)
        video_path = str(safe) if safe else ""
    else:
        video_path = await _resolve_media_file_async(job)
    params = {
        "video_path": video_path,
        "job_id": str(_get_field(job, "id", "")),
        "lesson_session_id": str(_get_field(job, "lessonSessionId",
            _get_field(job, "lesson_session_id", _get_field(job, "id", "unknown")))),
        "total_seats": int(_get_field(job, "totalSeats", 45)),
        "sample_interval": int(_get_field(job, "sampleInterval", 2)),
        "window_duration": int(_get_field(job, "windowDuration", 60)),
        "min_observations": int(_get_field(job, "minObservations", 5)),
        "scene_gate": _to_bool(_get_field(job, "sceneGate", None), True),
        "allow_demo": _to_bool(_get_field(job, "allow_demo", None), settings.demo_enabled),
    }
    try:
        return await asyncio.to_thread(analyze_video, params)
    finally:
        if _is_temp_download(video_path):
            _cleanup_local(video_path)


async def _handle_audio(job: dict) -> dict:
    from app.workers.asr import HAS_WHISPER, transcribe_audio

    audio_path = await _resolve_media_file_async(job)
    if not audio_path:
        return {"status": "failed", "error": "音频文件下载失败"}
    if not HAS_WHISPER:
        _cleanup_local(audio_path)
        return {"status": "not_implemented", "error": "faster-whisper is not installed"}
    try:
        return await asyncio.to_thread(
            transcribe_audio, audio_path, settings.whisper_model_size,
            settings.whisper_device, settings.whisper_compute_type)
    finally:
        if _is_temp_download(audio_path):
            _cleanup_local(audio_path)


async def dispatch_job(job: dict) -> dict:
    """Dispatch a job to the worker handler for its type."""
    job_type = str(_get_field(job, "type", "")).upper()

    if job_type == "COURSEWARE_ANALYSIS":
        return await _handle_courseware(job)
    if job_type == "PREP_ANALYSIS":
        return await _handle_prep(job)
    if job_type == "VIDEO_ANALYSIS":
        return await _handle_video(job)
    if job_type == "AUDIO_ANALYSIS":
        return await _handle_audio(job)
    if job_type == "GRADING":
        return await grade_submission(job)

    logger.warning("Unsupported job type %r for job %s", job_type, job.get("id"))
    return {"status": "failed", "error": f"Unsupported job type: {job_type}"}


async def _notify_backend(job_id: str, result: dict) -> dict:
    """POST the worker result and return the backend's applied/status payload."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.backend_url}/jobs/{job_id}/complete",
            json=result,
            headers={"X-Service-Token": settings.service_token},
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("data") or {}


async def _job_status(job_id: str) -> dict | None:
    """Ask the backend for the authoritative status of one job (R07).

    Used only when a stored result object is corrupt: whether that message may
    be cleaned up depends on the *database*, never on the broken object. The
    contract (implemented by the backend agent, path frozen) is::

        GET {backend_url}/jobs/{job_id}/internal-status
        X-Service-Token: <service token>
        -> {"code":200,"message":"success","data":{"status":"FAILED",
                                                    "attempt":1,"terminal":true}}

    An unknown job answers HTTP 200 with ``code=404`` (``ApiResponse.error``) or
    a plain HTTP 404; both are reported as ``{"exists": False}``. Any transport
    or parse failure returns ``None`` — "unknown", which must never be treated
    as terminal (it would ACK a message whose result is still undelivered).
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.backend_url}/jobs/{job_id}/internal-status",
                headers={"X-Service-Token": settings.service_token},
            )
    except Exception as e:  # noqa: BLE001 - backend outage is "unknown"
        logger.warning(
            "Could not read the backend status of job %s (%s); treating it as "
            "unknown", job_id, type(e).__name__)
        return None

    if resp.status_code == 404:
        return {"exists": False}
    try:
        payload = resp.json() or {}
    except Exception as e:  # noqa: BLE001 - malformed body is "unknown"
        logger.warning(
            "Backend status for job %s is not JSON (%s); treating it as unknown",
            job_id, type(e).__name__)
        return None

    code = payload.get("code")
    if isinstance(code, int) and code == 404:
        return {"exists": False}
    if resp.status_code >= 400 or (isinstance(code, int) and code >= 400):
        logger.warning(
            "Backend status for job %s answered code=%s http=%s; treating it as "
            "unknown", job_id, code, resp.status_code)
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        logger.warning(
            "Backend status for job %s carries no data object; treating it as "
            "unknown", job_id)
        return None
    status = data.get("status")
    terminal = data.get("terminal")
    return {
        "exists": True,
        "status": str(status) if status is not None else None,
        # The backend owns the terminal decision; fall back to the frozen
        # status set only when the flag is missing.
        "terminal": bool(terminal) if terminal is not None else _is_terminal_status(status),
        "attempt": data.get("attempt"),
    }


async def _claim_job(job_id: str) -> dict:
    """Atomically claim a job before running inference.

    Backend sets RUNNING + lease. ``claimed=False`` means the job is already
    terminal or another live worker holds the lease; see :func:`_claim_outcome`
    for the ACK decision per reason (protocol §1.1).
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.backend_url}/jobs/{job_id}/claim",
            json={"leaseSeconds": JOB_LEASE_SECONDS},
            headers={"X-Service-Token": settings.service_token},
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("data") or {}


async def _heartbeat_job(job_id: str, attempt: int | None = None) -> dict:
    payload = {"leaseSeconds": JOB_LEASE_SECONDS}
    if attempt is not None:
        payload["attempt"] = attempt
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.backend_url}/jobs/{job_id}/heartbeat",
            json=payload,
            headers={"X-Service-Token": settings.service_token},
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("data") or {}


async def _heartbeat_loop(job_id: str, attempt: int, stop: asyncio.Event,
                          lease_lost: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            payload = await _heartbeat_job(job_id, attempt)
            if payload.get("extended") is False:
                # A newer attempt owns the row. Do not ACK/XDEL the message:
                # doing so can delete the only copy the new worker is using.
                logger.warning(
                    "Lease for job %s is no longer held by attempt %s (%s); "
                    "stopping heartbeat without ACK",
                    job_id, attempt, payload.get("reason", "not_held"))
                lease_lost.set()
                return
        except Exception as e:  # noqa: BLE001 - lease renewal is best effort
            logger.warning("Heartbeat failed for job %s: %s", job_id, e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=JOB_HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Callback classification
# ---------------------------------------------------------------------------

def _callback_outcome(response: dict | None) -> str:
    """Classify a ``/jobs/{id}/complete`` payload (protocol §1.2).

    Returns ``applied``, ``already_terminal``, ``no_ack`` (the message must stay
    in the PEL) or ``rejected`` (an unexpected refusal).
    """
    data = response if isinstance(response, dict) else {}
    if data.get("applied") is not False:
        return "applied"
    reason = str(data.get("reason") or "")
    if reason == "already_terminal":
        return "already_terminal"
    if reason in CALLBACK_NO_ACK_REASONS:
        return "no_ack"
    return "rejected"


def _claim_outcome(claim: dict | None) -> str:
    """Map a claim response to one of ``claimed|terminal|leased|recovery_pending|other``.

    ``recovery_pending`` must never be ACKed: the message may be the only copy
    and the database recovery service owns the decision (protocol §4 last row).
    """
    data = claim if isinstance(claim, dict) else {}
    if data.get("claimed"):
        return "claimed"
    reason = str(data.get("reason") or "")
    if reason == "terminal":
        return "terminal"
    if reason == "leased":
        return "leased"
    if reason == "recovery_pending":
        return "recovery_pending"
    if _is_terminal_status(data.get("status")):
        return "terminal"
    return "other"


async def _notify_with_retries(job_id: str, result: dict) -> str:
    """Deliver once inline with bounded retries; returns a callback outcome.

    ``undeliverable`` means the backend could not be reached at all; the caller
    then keeps the stored result for the background retry / recovery service.
    """
    outcome = "undeliverable"
    for notify_attempt in range(CALLBACK_HTTP_ATTEMPTS):
        try:
            response = await _notify_backend(job_id, result) or {}
            return _callback_outcome(response)
        except Exception as e:  # noqa: BLE001 - backend outage is expected
            outcome = "undeliverable"
            logger.warning(
                "Backend notify failed for job %s (attempt %s/%s): %s",
                job_id, notify_attempt + 1, CALLBACK_HTTP_ATTEMPTS, e)
            if notify_attempt + 1 < CALLBACK_HTTP_ATTEMPTS:
                await asyncio.sleep(CALLBACK_RETRY_BACKOFF_SECONDS * (notify_attempt + 1))
    return outcome


# ---------------------------------------------------------------------------
# Durable result index helpers (read-only views over result_store)
# ---------------------------------------------------------------------------

def _pending_attempts(job_id: str) -> list[int]:
    """Attempts of ``job_id`` that have a pending index hash (newest first).

    Read-only helper: it scans the keys ``result_store.mark_pending`` writes and
    never mutates the store, so the frozen API does not need to grow.

    The SET membership check first keeps the common "nothing stored" case at one
    O(1) round-trip; only jobs that own a pending result pay for the SCAN (SCAN
    with MATCH still walks the whole keyspace, which per message would be far
    too expensive on a busy instance).
    """
    client = result_store._get_redis()
    if not client.sismember(result_store.PENDING_SET_KEY, job_id):
        return []
    pattern = result_store.PENDING_HASH_PATTERN.format(
        job_id=result_store._escape_glob(job_id))
    prefix = result_store.PENDING_HASH_TEMPLATE.format(job_id=job_id, attempt="")
    attempts: set[int] = set()
    for key in client.scan_iter(match=pattern, count=100):
        text = key.decode() if isinstance(key, bytes) else str(key)
        if not text.startswith(prefix):
            continue
        suffix = text[len(prefix):]
        if suffix.isdigit():
            attempts.add(int(suffix))
    return sorted(attempts, reverse=True)


def _pending_job_ids() -> set[str]:
    """Job ids recorded in the pending-result SET (read-only)."""
    client = result_store._get_redis()
    members = client.smembers(result_store.PENDING_SET_KEY) or set()
    return {
        m.decode() if isinstance(m, bytes) else str(m)
        for m in members
    }


async def _pending_attempts_async(job_id: str) -> list[int]:
    return await asyncio.to_thread(_pending_attempts, job_id)


async def _clear_pending_best_effort(job_id: str, attempt: int) -> None:
    """Clear one attempt's pending index; never fail the delivery on it."""
    try:
        await result_store.clear_pending_async(job_id, attempt)
    except Exception as e:  # noqa: BLE001 - index is rebuildable
        logger.warning(
            "Could not clear pending result index for job %s attempt %s: %s",
            job_id, attempt, e)


async def _replay_stored_result(job_id: str, msg_id: str | None = None) -> str:
    """Deliver an already stored result before claiming (protocol §4 window 1).

    Returns ``acked`` (caller ACKs), ``leave_pending`` (caller returns without
    ACK) or ``skip`` (no stored result left to deliver; continue with the
    claim).

    A backend rejection of the *indexed attempt* (``stale_attempt`` /
    ``not_running`` / ``invalid_attempt``) and an index pointing at an object
    that was never committed are **not** reasons to block the job forever: the
    index entry for that one attempt is dropped (with an explicit log line) and
    the caller continues to the claim step. The stream message itself is never
    ACKed from here — it may be the only copy of a newer attempt (protocol §4),
    where the same index entry would be the stale artifact reviewed as A12.

    The one exception is a corrupt object for a job the backend reports as
    terminal: the object is kept for audit, only that attempt's index is
    dropped and ``acked`` is returned, so the PEL and the index converge
    instead of retrying forever (R07).
    """
    try:
        attempts = await _pending_attempts_async(job_id)
    except Exception as e:  # noqa: BLE001 - Redis/scan failure
        logger.error(
            "Could not read the stored-result index for job %s; leaving message "
            "pending (store state unknown): %s", job_id, e)
        return "leave_pending"
    if not attempts:
        _STALE_REPLAY_COUNTS.pop(job_id, None)
        return "skip"

    logger.info("Stored result(s) found for job %s (message %s, attempts %s); "
                "delivering before claim", job_id, msg_id, attempts)
    for attempt in attempts:
        try:
            loaded = await result_store.load_result_async(job_id, attempt)
        except result_store.ResultCorruptError as e:
            # The object is reachable but provably wrong (bad digest/envelope/
            # JSON). Never re-run inference. Whether the message may be cleaned
            # up depends on the *database*, not on the broken object: a job the
            # backend already finished must not keep its message in the PEL and
            # its index in Redis forever (R07).
            state = await _job_status(job_id)
            if state is not None and state.get("terminal"):
                # Bad object kept in MinIO for audit; only this attempt's
                # rebuildable index is dropped so a newer attempt's index (or a
                # re-queued attempt) is never touched.
                logger.error(
                    "Stored result %s/%s is corrupt (%s) but job %s is already "
                    "terminal (%s); clearing that attempt's index and ACKing the "
                    "message; the corrupt object is kept for audit",
                    job_id, attempt, e, job_id, state.get("status"))
                await _drop_stale_index(job_id, attempt, "corrupt_result_terminal_job")
                return "acked"
            if state is None:
                logger.error(
                    "Stored result %s/%s is corrupt (%s) and the backend status is "
                    "unavailable; keeping the index and leaving the message "
                    "pending (never ACK on an unknown status)",
                    job_id, attempt, e)
            elif not state.get("exists"):
                logger.error(
                    "Stored result %s/%s is corrupt (%s) and job %s does not exist "
                    "in the backend; keeping the index and leaving the message "
                    "pending for manual review", job_id, attempt, e, job_id)
            else:
                logger.error(
                    "Stored result %s/%s is corrupt (%s) but job %s is still %s; "
                    "keeping the index and leaving the message pending so the "
                    "backend can mark the result INVALID",
                    job_id, attempt, e, job_id, state.get("status"))
            return "leave_pending"
        except result_store.ResultStoreError as e:
            # Unknown store state: re-running inference could duplicate work and
            # overwrite a recoverable result.
            logger.error(
                "Stored result %s/%s is unreadable (%s); leaving message pending "
                "instead of re-running inference", job_id, attempt, e)
            return "leave_pending"
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Unexpected failure loading stored result %s/%s: %s; leaving "
                "message pending", job_id, attempt, e)
            return "leave_pending"
        if loaded is None:
            logger.warning(
                "Index for job %s attempt %s has no committed result object "
                "(unfinished write); dropping that index entry and falling back "
                "to claim", job_id, attempt)
            await _drop_stale_index(job_id, attempt, "no_committed_object")
            continue

        result, ref = loaded
        result.setdefault("attempt", ref.attempt)
        try:
            response = await _notify_backend(job_id, result) or {}
        except Exception as e:  # noqa: BLE001 - backend outage
            logger.warning(
                "Could not deliver stored result for job %s attempt %s; leaving "
                "message pending for the next attempt: %s", job_id, ref.attempt, e)
            return "leave_pending"

        outcome = _callback_outcome(response)
        if outcome in ("applied", "already_terminal"):
            await _clear_pending_best_effort(job_id, ref.attempt)
            _STALE_REPLAY_COUNTS.pop(job_id, None)
            logger.info(
                "Delivered stored result for job %s attempt %s (%s); ACK without inference",
                job_id, ref.attempt, outcome)
            return "acked"
        if outcome == "no_ack":
            reason = str(response.get("reason") or "unknown")
            if reason not in STALE_INDEX_DROP_REASONS:
                # The object itself is what the backend refused (e.g. a malformed
                # attempt). Keep the index: the backend JobResultStore must mark
                # it INVALID, and dropping it here would hide that verdict.
                logger.warning(
                    "Backend refused stored result for job %s attempt %s (reason=%s); "
                    "keeping the index for the recovery service and leaving the "
                    "message pending", job_id, ref.attempt, reason)
                return "leave_pending"
            # This specific attempt is dead as far as the backend is concerned.
            # Remove only its index and keep going: the message may belong to a
            # newer attempt and must not be ACKed here.
            logger.warning(
                "Backend refused stored result for job %s attempt %s (reason=%s); "
                "dropping that attempt's index and continuing to claim without ACK",
                job_id, ref.attempt, reason)
            await _drop_stale_index(job_id, ref.attempt, reason)
            if _note_stale_replay(job_id, attempts, reason):
                await _clear_all_pending_attempts(job_id)
                break
            continue
        logger.warning(
            "Backend did not apply stored result for job %s attempt %s (reason=%s); "
            "leaving message pending", job_id, ref.attempt,
            response.get("reason", "unknown"))
        return "leave_pending"
    return "skip"


async def _drop_stale_index(job_id: str, attempt: int, reason: str) -> None:
    """Delete one attempt's index entry, logging jobId/attempt/reason."""
    try:
        await result_store.clear_pending_async(job_id, attempt)
    except Exception as e:  # noqa: BLE001 - index is rebuildable
        logger.warning(
            "Could not drop stale pending index for job %s attempt %s (reason=%s): %s",
            job_id, attempt, reason, e)
        return
    logger.warning(
        "Dropped stale pending index for job %s attempt %s (reason=%s)",
        job_id, attempt, reason)


def _note_stale_replay(job_id: str, attempts: list[int], reason: str) -> bool:
    """Count consecutive rejected replays; ``True`` means the cap was reached.

    A job whose backend keeps answering ``stale_attempt`` while several index
    entries linger would otherwise spin forever without claiming. After
    :data:`STALE_REPLAY_LIMIT` consecutive rejections the caller drops every
    indexed attempt for the job and proceeds to the claim step anyway.
    """
    count = _STALE_REPLAY_COUNTS.get(job_id, 0) + 1
    _STALE_REPLAY_COUNTS[job_id] = count
    if count < STALE_REPLAY_LIMIT:
        return False
    logger.warning(
        "Job %s hit %s consecutive stale replays (attempts %s, last reason=%s); "
        "clearing every indexed attempt for this job and proceeding to claim",
        job_id, count, attempts, reason)
    _STALE_REPLAY_COUNTS.pop(job_id, None)
    return True


async def _clear_all_pending_attempts(job_id: str) -> None:
    """Remove every attempt index of one job (livelock cap / superseded job)."""
    try:
        attempts = await _pending_attempts_async(job_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Could not enumerate stale indexes for job %s while applying the "
            "replay cap: %s", job_id, e)
        return
    for attempt in attempts:
        await _drop_stale_index(job_id, attempt, "stale_replay_cap")


async def _retry_pending_results(limit: int = PENDING_RESULT_SWEEP_LIMIT) -> int:
    """Background sweep: deliver indexed results that were never confirmed.

    Bounded on purpose — it runs on the same serial consumer loop, so a wide
    sweep would delay new jobs. The database recovery service remains the
    authority for everything this sweep does not reach.

    Selection rotates instead of always taking the first ``limit`` job ids: a
    permanently pending job (e.g. one stuck on a backend rejection) would
    otherwise occupy a slot in every round and starve the rest. The cursor is
    process-local, so a restart restarts the rotation from the beginning; that
    is acceptable because the sweep is a latency optimisation, not the
    delivery authority.
    """
    global _SWEEP_CURSOR
    try:
        job_ids = await asyncio.to_thread(_pending_job_ids)
    except Exception as e:  # noqa: BLE001 - Redis may be down
        logger.debug("Could not read pending result index: %s", e)
        return 0
    ordered = sorted(job_ids)
    batch, _SWEEP_CURSOR = _next_sweep_batch(ordered, limit, _SWEEP_CURSOR)
    delivered = 0
    attempted: set[str] = set()
    for job_id in batch:
        if job_id in attempted:
            continue
        attempted.add(job_id)
        try:
            state = await _replay_stored_result(job_id)
        except Exception as e:  # noqa: BLE001 - never kill the loop
            logger.warning("Pending result sweep failed for job %s: %s", job_id, e)
            continue
        if state == "acked":
            delivered += 1
    return delivered


def _next_sweep_batch(ordered: list[str], limit: int,
                      cursor: str | None) -> tuple[list[str], str | None]:
    """Return the next rotating window of ``ordered`` and the new cursor.

    ``ordered`` is the sorted job-id list; the window starts strictly after the
    cursor (wrapping around), so consecutive rounds cover every job even when
    the number of pending jobs exceeds ``limit``. The returned cursor is the
    last id handed out, so the next round resumes right after it.
    """
    if not ordered or limit <= 0:
        return [], cursor
    if len(ordered) <= limit:
        # Everything fits in one round: no rotation state is needed.
        return ordered, cursor
    # Resume strictly after the last id handed out; wrap to the front.
    start = next((i for i, job_id in enumerate(ordered) if job_id > cursor), 0) \
        if cursor is not None else 0
    rotated = ordered[start:] + ordered[:start]
    batch = list(itertools.islice(rotated, limit))
    return batch, (batch[-1] if batch else cursor)


# ---------------------------------------------------------------------------
# Legacy (pre-R05) pending-callback retry path
# ---------------------------------------------------------------------------

async def _store_pending_callback(redis: aioredis.Redis, job_id: str, result: dict) -> None:
    await redis.set(
        PENDING_CALLBACK_KEY.format(job_id),
        json.dumps(result, ensure_ascii=False),
        ex=24 * 3600,
    )
    await redis.sadd(PENDING_CALLBACK_SET, job_id)


async def _store_pending_callback_best_effort(
        redis: aioredis.Redis, job_id: str, result: dict) -> None:
    """Keep the legacy retry copy without letting its failure change the ACK."""
    try:
        await _store_pending_callback(redis, job_id, result)
        logger.info("Legacy pending callback stored for job %s", job_id)
    except Exception as e:  # noqa: BLE001 - the durable index already exists
        logger.warning(
            "Could not store legacy pending callback for job %s (durable index "
            "remains authoritative): %s", job_id, e)


async def _deliver_pending_callback(redis: aioredis.Redis, job_id: str) -> str:
    """Deliver and remove one stored callback.

    Returns ``absent`` when there is no stored result, ``delivered`` after the
    backend accepted it, and ``failed`` when the backend is still unreachable.
    A failed delivery must leave the stream message pending rather than claim and
    re-run inference.
    """
    key = PENDING_CALLBACK_KEY.format(job_id)
    try:
        raw = await redis.get(key)
    except Exception as e:  # noqa: BLE001 - Redis itself is unavailable
        logger.warning("Could not read pending callback for job %s: %s", job_id, e)
        return "failed"
    if not raw:
        return "absent"
    try:
        response = await _notify_backend(job_id, json.loads(raw)) or {}
    except Exception as e:  # noqa: BLE001 - keep for the next round
        logger.warning("Pending callback retry failed for job %s: %s", job_id, e)
        return "failed"
    if response.get("applied") is False:
        reason = str(response.get("reason") or "")
        if reason == "stale_attempt":
            # The stored result belongs to an older attempt. Drop it so the
            # current message can be claimed and executed, but do not ACK here.
            logger.warning("Dropping stale pending callback for job %s (reason=%s)",
                           job_id, reason)
            try:
                await redis.delete(key)
                await redis.srem(PENDING_CALLBACK_SET, job_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not clear stale pending callback for job %s: %s",
                               job_id, e)
            return "stale"
        if reason != "already_terminal":
            logger.warning("Backend did not apply pending callback for job %s (reason=%s)",
                           job_id, reason or "unknown")
            return "failed"
    try:
        await redis.delete(key)
        await redis.srem(PENDING_CALLBACK_SET, job_id)
    except Exception as e:  # noqa: BLE001 - backend already has the result
        logger.warning("Could not clear pending callback for job %s: %s", job_id, e)
    return "delivered"


async def _retry_pending_callbacks(redis: aioredis.Redis) -> int:
    """Retry callbacks whose inference already finished (legacy keys)."""
    retried = 0
    try:
        job_ids = await redis.smembers(PENDING_CALLBACK_SET)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read pending callbacks: %s", e)
        return 0
    for job_id in list(job_ids or []):
        state = await _deliver_pending_callback(redis, job_id)
        if state == "delivered":
            retried += 1
            logger.info("Retried pending callback for job %s", job_id)
        elif state == "absent":
            try:
                await redis.srem(PENDING_CALLBACK_SET, job_id)
            except Exception:
                pass
    return retried


async def join_consumer_group(redis: aioredis.Redis) -> None:
    """Create/join the consumer group, retrying while Redis is not ready.

    ``docker compose`` starts ai-service before redis finishes booting. Without
    this retry the join raises on the first attempt, the ``consume_jobs`` task
    dies, and every job then sits in QUEUED while the HTTP layer keeps
    answering 200 — the exact failure that broke several E2E runs.
    """
    for attempt in range(1, REDIS_CONNECT_ATTEMPTS + 1):
        try:
            await redis.xgroup_create(
                settings.job_stream, settings.job_group, id="0", mkstream=True
            )
            readiness.note_redis_ok()
            return
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                readiness.note_redis_ok()
                return
            readiness.note_redis_error(e)
            raise
        except Exception as e:  # noqa: BLE001 - connection errors are retryable
            readiness.note_redis_error(e)
            if attempt >= REDIS_CONNECT_ATTEMPTS:
                logger.error(
                    "Cannot join job stream after %s attempts: %s", attempt, e
                )
                raise
            logger.warning(
                "Job stream unavailable (attempt %s/%s): %s; retrying in %ss",
                attempt, REDIS_CONNECT_ATTEMPTS, e, REDIS_CONNECT_BACKOFF_SECONDS,
            )
            await asyncio.sleep(REDIS_CONNECT_BACKOFF_SECONDS)

class _Maintenance:
    """Time-driven background work for the consumer loop (protocol §5 rule 8).

    Timers are wall-clock (``time.monotonic``) instead of loop-iteration counts,
    so a slow inference or an idle stream cannot postpone callback retries.

    Since R05 these timers are driven by an independent maintenance coroutine
    (:meth:`run`, started with ``asyncio.create_task``), so a long inference no
    longer postpones callback retries, reclaim or queue diagnostics.

    Parallel inference stays impossible by construction: the maintenance task
    never calls :meth:`_Consumer.process`. Everything it reclaims from the PEL is
    pushed onto :attr:`reclaimed` and the *main* loop drains that queue and
    processes the messages serially, exactly like messages read by XREADGROUP.
    """

    def __init__(self, redis: aioredis.Redis,
                 consumer_id: str | None = None,
                 callback_interval: float | None = None,
                 reclaim_interval: float | None = None,
                 depth_interval: float | None = None,
                 tick_interval: float | None = None,
                 reclaimed: asyncio.Queue | None = None) -> None:
        self.redis = redis
        self.consumer_id = consumer_id
        # Resolved at construction time (not at import time) so the module-level
        # tunables stay overridable from configuration and tests.
        self.callback_interval = (
            CALLBACK_RETRY_INTERVAL_SECONDS if callback_interval is None
            else callback_interval)
        self.reclaim_interval = (
            RECLAIM_INTERVAL_SECONDS if reclaim_interval is None
            else reclaim_interval)
        self.depth_interval = (
            QUEUE_DEPTH_INTERVAL_SECONDS if depth_interval is None
            else depth_interval)
        self.tick_interval = (
            MAINTENANCE_TICK_SECONDS if tick_interval is None else tick_interval)
        #: Reclaimed ``(msg_id, job)`` pairs waiting for the serial main loop.
        self.reclaimed: asyncio.Queue = (
            reclaimed if reclaimed is not None else asyncio.Queue())
        #: Completed maintenance rounds (diagnostics/tests only).
        self.rounds = 0
        now = time.monotonic()
        self._next_callbacks = now
        self._next_reclaim = now
        self._next_depth = now

    def _due(self, attr: str, interval: float, now: float) -> bool:
        if now < getattr(self, attr):
            return False
        setattr(self, attr, now + interval)
        return True

    def drain_reclaimed(self) -> list[tuple[str, dict]]:
        """Take every message reclaimed since the last drain (FIFO, never blocks)."""
        pairs: list[tuple[str, dict]] = []
        while True:
            try:
                pairs.append(self.reclaimed.get_nowait())
            except asyncio.QueueEmpty:
                return pairs

    async def run(self) -> None:
        """Maintenance loop; runs as its own task and only exits on cancellation.

        One failing round is logged and the loop continues: maintenance must
        never take the consumer down. Cancellation is re-raised so the caller
        can ``await`` the task in its ``finally`` and know it has stopped.
        """
        logger.info("Maintenance task started (tick=%ss)", self.tick_interval)
        try:
            while True:
                now = time.monotonic()
                try:
                    await self.maybe_retry_callbacks(now)
                    await self.maybe_note_depth(now)
                    if self.consumer_id is not None:
                        for pair in await self.maybe_reclaim(self.consumer_id, now):
                            self.reclaimed.put_nowait(pair)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - maintenance must survive
                    logger.warning("Maintenance round failed: %s", e)
                self.rounds += 1
                await asyncio.sleep(self.tick_interval)
        except asyncio.CancelledError:
            logger.info("Maintenance task cancelled after %s rounds", self.rounds)
            raise
        finally:
            logger.debug("Maintenance task leaving after %s rounds", self.rounds)

    async def maybe_retry_callbacks(self, now: float) -> None:
        if not self._due("_next_callbacks", self.callback_interval, now):
            return
        try:
            await _retry_pending_results()
        except Exception as e:  # noqa: BLE001 - maintenance must never kill the loop
            logger.warning("Pending result sweep failed: %s", e)
        try:
            await _retry_pending_callbacks(self.redis)
        except Exception as e:  # noqa: BLE001
            logger.warning("Legacy pending callback retry failed: %s", e)

    async def maybe_reclaim(self, consumer_id: str, now: float) -> list[tuple[str, dict]]:
        """Take over idle PEL messages; returns ``(msg_id, job)`` pairs.

        The messages are handed back to the caller, which processes them through
        :meth:`_Consumer.process` so the same ACK rules apply as for new reads.
        """
        if not self._due("_next_reclaim", self.reclaim_interval, now):
            return []
        claimed = await self.redis.xautoclaim(
            settings.job_stream,
            settings.job_group,
            consumer_id,
            min_idle_time=RECLAIM_MIN_IDLE_MS,
            start_id="0-0",
            count=10,
        )
        # redis-py returns (next_start, messages, deleted_ids) or (next, messages)
        messages = claimed[1] if isinstance(claimed, (tuple, list)) and len(claimed) > 1 else []
        pairs = [
            (str(msg_id), {k: v for k, v in msg_data.items()})
            for msg_id, msg_data in messages or []
            if msg_data
        ]
        readiness.note_redis_ok()
        if pairs:
            readiness.note_message_consumed(len(pairs))
        return pairs

    async def maybe_note_depth(self, now: float) -> None:
        if not self._due("_next_depth", self.depth_interval, now):
            return
        queued = pending = None
        try:
            queued = int(await self.redis.xlen(settings.job_stream))
        except Exception as e:  # noqa: BLE001 - diagnostics only
            logger.debug("Could not read stream length: %s", e)
        try:
            summary = await self.redis.xpending(settings.job_stream, settings.job_group)
            if isinstance(summary, dict):
                pending = int(summary.get("pending") or 0)
            elif isinstance(summary, (tuple, list)) and summary:
                pending = int(summary[0] or 0)
        except Exception as e:  # noqa: BLE001 - diagnostics only
            logger.debug("Could not read pending count: %s", e)
        readiness.note_queue_depth(queued, pending)


#: Handler used by :class:`_Consumer`; set by :func:`consume_jobs`
#: (``dispatch_job`` by default). Module level so tests can inject a fake.
_HANDLER: JobHandler | None = None
#: The running maintenance object while :func:`consume_jobs` is active
#: (diagnostics in tests; ``None`` otherwise).
_MAINTENANCE: "_Maintenance | None" = None


class _Consumer:
    """One stream consumer: reads messages and applies the ACK protocol."""

    def __init__(self, redis: aioredis.Redis, consumer_id: str) -> None:
        self.redis = redis
        self.consumer_id = consumer_id

    async def ack(self, msg_id: str) -> None:
        await self.redis.xack(settings.job_stream, settings.job_group, msg_id)
        await self.redis.xdel(settings.job_stream, msg_id)

    async def _ack_or_log(self, msg_id: str, job_id: str, reason: str) -> None:
        """ACK with an explicit reason log; a Redis failure is never silent."""
        try:
            await self.ack(msg_id)
        except Exception as e:  # noqa: BLE001 - message stays pending and is retried
            readiness.note_redis_error(e)
            logger.error(
                "Could not ACK message %s for job %s (intended because %s): %s",
                msg_id, job_id, reason, e)
            return
        logger.info("ACKed message %s for job %s (%s)", msg_id, job_id, reason)

    async def _leave_pending(self, msg_id: str, job_id: str, attempt: int | None,
                             reason: str, detail: str = "") -> None:
        """Return without ACK, always with an explicit reason in the log."""
        logger.warning(
            "Leaving message %s pending for job %s (attempt=%s): %s%s",
            msg_id, job_id, attempt, reason, f" - {detail}" if detail else "")

    async def process(self, msg_id: str, job: dict) -> None:
        """Handle one stream message end to end (protocol §4).

        ACK branches::

            situation                       ACK?  why
            stored result delivered          yes  backend already has it
            stored result rejected by backend no  message may be the newer copy;
                                                  only that attempt's index is dropped
                                                  and the claim step still runs
            stored result corrupt             no  backend will mark it INVALID/FAILED
            claim terminal                   yes  nothing left to run
            claim leased                      no  another worker owns it
            claim recovery_pending            no  message may be the only copy
            lease lost during inference       no  a newer attempt owns the row
            result save failed                no  no durable copy exists yet
            pending index write failed        no  ACK would drop the only pointer
            callback applied/terminal        yes  durable and applied
            callback unreachable             yes  durable; recovery delivers it
            callback rejected (stale/...)     no  may be the newer attempt's copy
        """
        job_id = str(job.get("id", msg_id))
        logger.info("Processing job %s (%s)", job_id, job.get("type", "unknown"))

        replay = await _replay_stored_result(job_id, msg_id)
        if replay == "acked":
            await self._ack_or_log(msg_id, job_id, "stored result already delivered")
            return
        if replay == "leave_pending":
            await self._leave_pending(msg_id, job_id, None,
                                      "stored-result replay did not complete")
            return

        try:
            claim = await _claim_job(job_id)
        except Exception as e:  # noqa: BLE001 - backend unreachable
            await self._leave_pending(msg_id, job_id, None, "claim request failed", str(e))
            return

        claim_outcome = _claim_outcome(claim)
        if claim_outcome == "terminal":
            logger.info("Job %s already terminal (%s); ACK without inference",
                        job_id, (claim or {}).get("status", "unknown"))
            await self._ack_or_log(msg_id, job_id, "job already terminal")
            return
        if claim_outcome == "leased":
            await self._leave_pending(
                msg_id, job_id, (claim or {}).get("attempt"),
                "job is leased by another live worker; waiting for reclaim")
            return
        if claim_outcome == "recovery_pending":
            logger.info(
                "job %s awaiting database recovery (attempt %s); not ACKing "
                "message %s, not running inference",
                job_id, (claim or {}).get("attempt"), msg_id)
            return
        if claim_outcome != "claimed":
            await self._leave_pending(
                msg_id, job_id, (claim or {}).get("attempt"),
                "claim rejected", f"reason={(claim or {}).get('reason', 'unknown')}")
            return

        attempt_number = int((claim or {}).get("attempt") or 0)
        result = await self._run_inference(job_id, job, attempt_number)
        if result is None:
            await self._leave_pending(
                msg_id, job_id, attempt_number,
                "lease lost during inference; a newer attempt owns the job")
            return
        result["attempt"] = attempt_number

        saved = await self._save_result(job_id, attempt_number, result)
        if saved is None:
            await self._leave_pending(
                msg_id, job_id, attempt_number,
                "result could not be stored durably")
            return
        result_ref, conflict, deliverable = saved

        _fault_stop(FAULT_STOP_AFTER_RESULT_PUT_ENV, "after result put",
                    job_id, attempt_number)
        if not await self._mark_pending(result_ref, conflict):
            await self._leave_pending(
                msg_id, job_id, attempt_number,
                "stored result could not be indexed in Redis")
            return
        _fault_stop(FAULT_STOP_AFTER_MARK_PENDING_ENV, "after mark_pending",
                    job_id, attempt_number)

        # On a result conflict the durable first object is the single copy of
        # this attempt, so the callback must carry *its* content (F6/A11), not
        # the freshly computed body that was never stored.
        outcome = await _notify_with_retries(job_id, deliverable)
        if outcome in ("applied", "already_terminal"):
            await _clear_pending_best_effort(job_id, attempt_number)
            await self._ack_or_log(msg_id, job_id, f"result delivered ({outcome})")
            return
        if outcome == "undeliverable":
            # The result is durable and indexed; the background sweep and the
            # database recovery service own delivery from here.
            await _store_pending_callback_best_effort(
                self.redis, job_id, deliverable)
            await self._ack_or_log(
                msg_id, job_id,
                "result stored but backend unreachable; recovery owns delivery")
            return

        # no_ack / rejected: keep the message (it may be the newer attempt's
        # only copy) and keep the pending index for the recovery service.
        await self._leave_pending(
            msg_id, job_id, attempt_number,
            "backend refused the result; message may be the only copy",
            f"outcome={outcome}")

    async def _run_inference(self, job_id: str, job: dict, attempt: int) -> dict | None:
        """Run the handler under a heartbeat; ``None`` when the lease was lost."""
        stop_heartbeat = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(job_id, attempt, stop_heartbeat, lease_lost))
        handler = _HANDLER or dispatch_job
        readiness.note_inference_started()
        try:
            result = await handler(job)
        except Exception as e:  # noqa: BLE001 - failures are results too
            logger.error("Handler failed for job %s: %s", job_id, e, exc_info=True)
            result = {"status": "failed", "error": str(e)}
        finally:
            readiness.note_inference_finished()
            stop_heartbeat.set()
            try:
                await asyncio.wait_for(heartbeat_task, timeout=5)
            except Exception:  # noqa: BLE001
                heartbeat_task.cancel()
        if lease_lost.is_set():
            logger.warning(
                "Attempt %s for job %s lost its lease during inference; not saving "
                "or ACKing", attempt, job_id)
            return None
        return result if isinstance(result, dict) else {"status": "failed", "result": result}

    async def _save_result(self, job_id: str, attempt: int,
                           result: dict) -> tuple[result_store.ResultRef, bool, dict] | None:
        """Persist the result; ``None`` means the message must stay pending.

        Returns ``(ref, conflict, deliverable)`` where ``deliverable`` is the
        body the callback must send. For a fresh write that is ``result``
        itself; on a :class:`ResultConflictError` it is the *stored* first
        object read back from the store, so the indexed object and the payload
        delivered to the backend are always the same bytes (F6/A11).
        """
        try:
            ref = await result_store.save_result_async(job_id, attempt, result)
            return ref, False, result
        except result_store.ResultConflictError as e:
            if e.ref is None:  # pragma: no cover - defensive
                logger.error(
                    "Result conflict for job %s attempt %s without a stored ref; "
                    "leaving message pending", job_id, attempt)
                return None
            logger.warning(
                "Result conflict for job %s attempt %s: a different digest is "
                "already stored; keeping the first object and delivering it (%s)",
                job_id, attempt, e)
            stored = await self._load_conflicting_result(job_id, e.ref.attempt)
            if stored is None:
                return None
            return e.ref, True, stored
        except result_store.ResultSerializationError as e:
            # Unserialisable payload (non-finite float): storing it would create
            # an object the backend's JSON parser rejects, i.e. a guaranteed
            # INVALID result. Leave the message pending instead.
            logger.error(
                "Result for job %s attempt %s cannot be serialised (%s); leaving "
                "message pending instead of storing bytes the backend cannot parse",
                job_id, attempt, e)
            return None
        except result_store.ResultStoreError as e:
            logger.error(
                "Could not store result for job %s attempt %s (%s); leaving message "
                "pending so the result is not lost", job_id, attempt, e)
            return None
        except Exception as e:  # noqa: BLE001 - unexpected store bug
            logger.error(
                "Unexpected failure storing result for job %s attempt %s: %s; "
                "leaving message pending", job_id, attempt, e, exc_info=True)
            return None

    async def _load_conflicting_result(self, job_id: str, attempt: int) -> dict | None:
        """Read back the first stored object for a conflicting attempt."""
        try:
            loaded = await result_store.load_result_async(job_id, attempt)
        except result_store.ResultStoreError as e:
            logger.error(
                "Conflicting result %s/%s could not be read back (%s); leaving "
                "message pending", job_id, attempt, e)
            return None
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Unexpected failure reading back conflicting result %s/%s: %s; "
                "leaving message pending", job_id, attempt, e, exc_info=True)
            return None
        if loaded is None:
            logger.error(
                "Conflicting result %s/%s has no committed object; leaving message "
                "pending", job_id, attempt)
            return None
        stored_result, stored_ref = loaded
        result = dict(stored_result)
        result["attempt"] = stored_ref.attempt
        logger.warning(
            "Delivering the stored first object for conflicting result %s/%s "
            "(sha256=%s); the in-memory recomputation is discarded",
            job_id, attempt, stored_ref.sha256)
        return result

    async def _mark_pending(self, ref: result_store.ResultRef, conflict: bool) -> bool:
        """Write the Redis index; ``False`` means the message must stay pending."""
        meta = {"lastError": "RESULT_CONFLICT"} if conflict else {}
        try:
            await result_store.mark_pending_async(ref, **meta)
            return True
        except Exception as e:  # noqa: BLE001 - index write must not be silent
            logger.error(
                "Could not index stored result %s/%s (%s); leaving message pending "
                "(the object exists and will be reused idempotently on replay)",
                ref.job_id, ref.attempt, e)
            return False


async def consume_jobs(handler: JobHandler | None = None):
    """Consume jobs from the Redis stream until cancelled.

    The default handler dispatches jobs by their ``type`` field (courseware /
    vision / asr / grading). Every message goes through
    :meth:`_Consumer.process`, which owns the ACK decision.

    Since R05 an independent maintenance task (callback retries, reclaim, queue
    diagnostics) runs next to the read loop, so a long inference no longer
    blocks them. Inference itself stays serial: the maintenance task only
    *queues* reclaimed messages and this loop processes them one by one.
    """
    global _HANDLER, _MAINTENANCE
    _HANDLER = handler or dispatch_job

    # "starting" while the Redis connection is still being established: /ready
    # must not answer 200 for a consumer that cannot read the stream yet.
    readiness.note_consumer_state("starting")
    r = None
    maintenance_task: asyncio.Task | None = None
    try:
        try:
            r = await create_redis_pool()
            await join_consumer_group(r)
        except Exception as e:  # noqa: BLE001 - the supervisor restarts us
            readiness.note_redis_error(e)
            readiness.note_consumer_state("failed")
            raise
        # Only a consumer that really joined the group may report "running".
        readiness.note_consumer_state("running")

        consumer_id = f"worker-{datetime.now().timestamp()}"
        logger.info(
            "Job consumer %s joined stream %s (group %s)",
            consumer_id, settings.job_stream, settings.job_group,
        )
        consumer = _Consumer(r, consumer_id)
        maintenance = _Maintenance(r, consumer_id=consumer_id)
        _MAINTENANCE = maintenance
        maintenance_task = asyncio.create_task(
            maintenance.run(), name="ai-maintenance")

        while True:
            try:
                # Drain whatever the maintenance task reclaimed from the PEL
                # before reading new work: same serial path, same ACK rules.
                for msg_id, job in maintenance.drain_reclaimed():
                    await consumer.process(msg_id, job)

                try:
                    results = await r.xreadgroup(
                        groupname=settings.job_group,
                        consumername=consumer_id,
                        streams={settings.job_stream: ">"},
                        count=1,
                        block=1000,
                    )
                except Exception as e:  # noqa: BLE001
                    readiness.note_redis_error(e)
                    raise
                readiness.note_redis_ok()

                if not results:
                    await asyncio.sleep(settings.poll_interval)
                    continue

                messages = [
                    (str(msg_id), {k: v for k, v in msg_data.items()})
                    for _stream, entries in results
                    for msg_id, msg_data in entries
                ]
                if messages:
                    readiness.note_message_consumed(len(messages))
                for msg_id, job in messages:
                    await consumer.process(msg_id, job)

            except asyncio.CancelledError:
                # Propagate cancellation: the supervisor relies on it to stop
                # restarting the consumer during service shutdown.
                logger.info("Job consumer cancelled")
                raise
            except Exception as e:  # noqa: BLE001 - loop must survive
                readiness.note_redis_error(e)
                logger.error(f"Consumer error: {e}")
                if "NOGROUP" in str(e).upper():
                    # Redis restarted without the group (data loss or FLUSHALL).
                    # Recreate it so later jobs can still be consumed; a lost
                    # message itself still needs outbox reconciliation.
                    logger.warning("Consumer group is missing; recreating it")
                    try:
                        await join_consumer_group(r)
                    except Exception as join_error:  # noqa: BLE001
                        readiness.note_redis_error(join_error)
                        logger.error("Could not recreate consumer group: %s", join_error)
                await asyncio.sleep(5)
    finally:
        # Order matters: stop the maintenance task before touching the client
        # it uses, then release the sockets explicitly (L01) so no connection is
        # closed by the garbage collector after the event loop is gone.
        _MAINTENANCE = None
        if maintenance_task is not None:
            maintenance_task.cancel()
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001 - shutdown must not raise
                logger.warning("Maintenance task ended with an error: %s", e)
        await _close_redis(r)
        readiness.note_inference_finished()
        # "failed" was recorded on the startup failure path and must survive the
        # cleanup: overwriting it with "stopped" would hide a consumer that never
        # joined the group (R06).
        if readiness.snapshot()["consumer_state"] != "failed":
            readiness.note_consumer_state("stopped")
