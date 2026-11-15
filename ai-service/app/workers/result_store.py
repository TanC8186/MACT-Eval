"""
Durable result store for AI worker jobs (protocol §2/§3).

An undelivered job result must survive a full Redis loss, so the worker writes
the complete result to a private MinIO prefix *before* it ACKs the stream
message. Redis only keeps a rebuildable index of the stored object; it is never
the single copy of a result.

Layout (bucket ``settings.result_bucket``)::

    {settings.result_prefix}/{jobId}/{attempt}/result.json    # canonical bytes
    {settings.result_prefix}/{jobId}/{attempt}/envelope.json  # commit marker

``result.json`` is uploaded exactly as produced by :func:`canonical_bytes`, so
the SHA-256 recorded in the envelope is the digest of the stored bytes (no
second serialisation step can drift). ``envelope.json`` is written last and is
the commit marker: a result object without an envelope is an unfinished write.

Frozen signatures: docs/operations/result-delivery-and-recovery-protocol.md §8.

This module does not ACK stream messages, does not write backend tables and
does not issue HTTP requests. MinIO/Redis clients are created lazily on first
use, so importing it never touches the network.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Redis SET of job ids that own at least one undelivered result (no TTL).
PENDING_SET_KEY = "analysis:results:pending"
#: Redis HASH per attempt; no TTL by design.
PENDING_HASH_TEMPLATE = "analysis:result:{job_id}:{attempt}"
#: SCAN pattern used to detect sibling attempts of the same job.
PENDING_HASH_PATTERN = "analysis:result:{job_id}:*"

RESULT_OBJECT_NAME = "result.json"
ENVELOPE_OBJECT_NAME = "envelope.json"

#: S3 error codes that mean "object/bucket is not there", nothing else.
_MISSING_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NoSuchVersion", "NotFound"})

#: Lazy singletons. Tests monkeypatch ``_client_factory`` / ``_redis_factory``
#: (or the cached globals) instead of opening real connections.
_minio_client: Any = None
_redis_client: Any = None
_ready_buckets: set[str] = set()


class ResultStoreError(RuntimeError):
    """The result store is unreachable or answered with a non-missing error.

    Callers must treat this as "unknown", never as "no result": retrying the
    inference on a store outage duplicates work and may overwrite a good
    result. Only a confirmed *missing* object may trigger a re-run
    (protocol §2 rule 3 / §5).
    """


class ResultConflictError(ResultStoreError):
    """Same ``(jobId, attempt)`` already holds a different result digest.

    The first object is never overwritten. The existing reference is attached
    as ``ref`` so the caller can index/forward it.
    """

    def __init__(self, message: str, ref: "ResultRef | None" = None) -> None:
        super().__init__(message)
        self.ref = ref


class ResultCorruptError(ResultStoreError):
    """The stored object exists but its content cannot be trusted.

    Raised by :func:`load_result` when the envelope disagrees with the object
    (jobId/attempt), when the bytes do not match the recorded SHA-256/size, or
    when the payload is not valid UTF-8 JSON object. This is *not* the same
    condition as a store outage: the bytes are reachable and provably wrong, so
    the worker must keep the pending index and let the backend
    ``JobResultStore`` mark the job ``INVALID`` (→ FAILED). Re-running inference
    would hide a tampered/truncated result behind a fresh one.
    """


class ResultSerializationError(ResultStoreError):
    """``result`` cannot be serialised into bytes the backend can parse.

    Today the only cause is a non-finite float (``NaN``/``±Infinity``): Python
    writes those as the non-standard tokens ``NaN``/``Infinity``/``-Infinity``,
    which the backend's Jackson parser rejects. Storing such bytes would make the
    result permanently ``INVALID`` on the recovery path, so the write is refused
    before anything reaches the object store.
    """


@dataclass(frozen=True)
class ResultRef:
    """Immutable pointer to one stored result object pair."""

    job_id: str
    attempt: int
    bucket: str
    result_key: str
    envelope_key: str
    sha256: str
    bytes: int


def canonical_bytes(result: dict) -> bytes:
    """Serialise ``result`` to the exact bytes stored and hashed.

    Both the SHA-256 and the uploaded ``result.json`` come from this single
    call, so a mismatching digest cannot be produced by re-serialising.

    Non-finite floats (``NaN``/``±Infinity``) are rejected with
    :class:`ResultSerializationError` *before* anything is written:
    ``json.dumps(allow_nan=False)`` refuses to emit the non-standard tokens
    ``NaN``/``Infinity``/``-Infinity`` that the backend's Jackson parser
    rejects. Storing them would produce an object that the recovery path can
    never deliver, so the caller must leave the message pending instead.

    The current handlers do not produce non-finite values (``vision.py``
    derives its ratios from integer counts, ``asr.py`` rounds
    ``language_probability``/``silence_ratio`` from finite inputs), but the
    store must not rely on that: the input domain is closed here rather than
    at the backend.
    """
    if not isinstance(result, dict):
        raise TypeError("result must be a dict")
    offending = _first_non_finite_path(result)
    if offending is not None:
        raise ResultSerializationError(
            f"non-finite float in result at key {offending}; the backend's JSON "
            f"parser rejects NaN/Infinity, so the result was not stored"
        )
    return json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    ).encode("utf-8")


def _first_non_finite_path(value: Any, path: str = "") -> str | None:
    """Return the dotted/indexed path of the first non-finite float, if any.

    Walks nested dicts and lists so a ``NaN`` buried in ``windows[3].score`` is
    reported by location instead of a generic serialisation failure.
    """
    if isinstance(value, float):
        return None if math.isfinite(value) else (path or "<root>")
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            found = _first_non_finite_path(item, child)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found = _first_non_finite_path(item, f"{path}[{index}]")
            if found is not None:
                return found
        return None
    return None


def save_result(
    job_id: str,
    attempt: int,
    result: dict,
    *,
    model_version: str | None = None,
    model_digest: str | None = None,
) -> ResultRef:
    """Persist ``result`` and return its :class:`ResultRef`.

    Write order: ``result.json`` -> stat/size check -> ``envelope.json``.

    Idempotency: when an envelope already exists for ``(job_id, attempt)``:

    * same ``resultSha256`` and the object is really there -> return the
      existing ref without rewriting anything;
    * different digest -> :class:`ResultConflictError` (nothing is
      overwritten, the existing ref is on the exception);
    * envelope present but object missing/size-mismatched ->
      :class:`ResultStoreError` (inconsistent store, fail loudly).

    Any MinIO outage raises :class:`ResultStoreError`; the caller must not ACK.
    """
    job_id = _validate_job_id(job_id)
    attempt = _validate_attempt(attempt)

    payload = canonical_bytes(result)
    digest = hashlib.sha256(payload).hexdigest()
    size = len(payload)
    bucket = _bucket()
    result_key, envelope_key = _object_keys(job_id, attempt)

    client = _get_client()
    _ensure_bucket(client, bucket)

    existing = _read_envelope(client, bucket, envelope_key)
    if existing is not None:
        _validate_envelope(existing, job_id, attempt)
        existing_ref = ResultRef(
            job_id=job_id,
            attempt=attempt,
            bucket=bucket,
            result_key=result_key,
            envelope_key=envelope_key,
            sha256=str(existing["resultSha256"]),
            bytes=int(existing["resultBytes"]),
        )
        if existing_ref.sha256 != digest:
            raise ResultConflictError(
                f"result conflict for {job_id}/{attempt}: stored "
                f"{existing_ref.sha256}, incoming {digest}; first object kept",
                ref=existing_ref,
            )
        stored_size = _stat_size(client, bucket, result_key)
        if stored_size is None:
            raise ResultStoreError(
                f"envelope exists but {result_key} is missing; refusing to reuse"
            )
        if stored_size != existing_ref.bytes:
            raise ResultStoreError(
                f"{result_key} size {stored_size} != envelope {existing_ref.bytes}"
            )
        logger.info(
            "result already stored for %s/%s (sha256=%s), reusing",
            job_id, attempt, digest,
        )
        return existing_ref

    _put_bytes(client, bucket, result_key, payload)
    stored_size = _stat_size(client, bucket, result_key)
    if stored_size is None:
        raise ResultStoreError(f"{result_key} vanished right after upload")
    if stored_size != size:
        raise ResultStoreError(
            f"{result_key} stored {stored_size} bytes, expected {size}"
        )

    envelope = {
        "schemaVersion": int(settings.result_schema_version),
        "jobId": job_id,
        "attempt": attempt,
        "resultSha256": digest,
        "resultBytes": size,
        "createdAt": _utc_now_iso(),
        "status": result.get("status"),
        "modelVersion": model_version,
        "modelDigest": model_digest,
    }
    _put_bytes(client, bucket, envelope_key, canonical_bytes(envelope))

    logger.info(
        "stored result for %s/%s (%s bytes, sha256=%s)",
        job_id, attempt, size, digest,
    )
    return ResultRef(
        job_id=job_id,
        attempt=attempt,
        bucket=bucket,
        result_key=result_key,
        envelope_key=envelope_key,
        sha256=digest,
        bytes=size,
    )


def mark_pending(ref: ResultRef, **meta) -> None:
    """Record ``ref`` in the Redis pending index (no TTL).

    The hash is keyed per attempt, so clearing an old attempt can never hide a
    newer one. Extra ``meta`` entries (``lastError``, ``deliveredAt``, ...) are
    stringified into the hash. ``firstSeenAt`` is written with HSETNX so
    re-marking keeps the original first-seen timestamp.

    Redis errors propagate unchanged: the caller must not ACK, because the
    message is then the only durable pointer to the stored object.
    """
    if not isinstance(ref, ResultRef):
        raise TypeError("ref must be a ResultRef")
    job_id = _validate_job_id(ref.job_id)
    attempt = _validate_attempt(ref.attempt)

    redis = _get_redis()
    now = _utc_now_iso()
    hash_key = _pending_hash_key(job_id, attempt)

    first_seen = meta.pop("firstSeenAt", None) or now
    fields: dict[str, str] = {
        "bucket": str(ref.bucket),
        "object": str(ref.result_key),
        "sha256": str(ref.sha256),
        "bytes": str(ref.bytes),
        "savedAt": now,
        "lastAttemptAt": now,
    }
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            fields[str(key)] = json.dumps(value, ensure_ascii=False, default=str)
        else:
            fields[str(key)] = str(value)

    pipe = redis.pipeline()
    pipe.sadd(PENDING_SET_KEY, job_id)
    pipe.hset(hash_key, mapping=fields)
    pipe.hsetnx(hash_key, "firstSeenAt", str(first_seen))
    pipe.execute()


def clear_pending(job_id: str, attempt: int) -> None:
    """Drop the pending index entry for exactly one attempt.

    Only when no other attempt hash of the same job remains is the job id
    removed from the pending SET. A key seen by SCAN is treated as live: a
    false "still pending" only causes alert noise, while a false "delivered"
    would hide an undelivered result. Glob metacharacters in ``job_id`` are
    escaped and the keys are prefix-filtered, so another job's attempt can
    never suppress this removal; a second SCAN after ``SREM`` restores the job
    if a new attempt indexed itself in the race window.
    """
    job_id = _validate_job_id(job_id)
    attempt = _validate_attempt(attempt)

    redis = _get_redis()
    hash_key = _pending_hash_key(job_id, attempt)
    pipe = redis.pipeline()
    pipe.delete(hash_key)
    pipe.execute()

    prefix = PENDING_HASH_TEMPLATE.format(job_id=job_id, attempt="")
    pattern = PENDING_HASH_PATTERN.format(job_id=_escape_glob(job_id))
    # SCAN glob specials are escaped above, but a job id containing ":" could
    # still make the pattern match another job's keys; the prefix filter is
    # the authoritative check.
    siblings = [
        key for key in redis.scan_iter(match=pattern, count=100)
        if str(key).startswith(prefix)
    ]
    if not siblings:
        redis.srem(PENDING_SET_KEY, job_id)
        # A newer attempt may have indexed itself between SCAN and SREM. Put
        # the job back rather than hiding an undelivered result; a spurious
        # "still pending" only costs an alert, a lost one loses the pointer.
        late = [
            key for key in redis.scan_iter(match=pattern, count=100)
            if str(key).startswith(prefix)
        ]
        if late:
            redis.sadd(PENDING_SET_KEY, job_id)


def load_result(job_id: str, attempt: int) -> tuple[dict, ResultRef] | None:
    """Read and byte-verify a stored result.

    Order: read envelope -> check ``jobId``/``attempt`` -> read the raw
    ``result.json`` bytes -> compare SHA-256 (and size) -> parse JSON.

    Returns ``None`` only when the envelope is confirmed missing
    (``NoSuchKey``/``NoSuchObject``/``NoSuchBucket``). An orphan ``result.json``
    without an envelope is an unfinished write: ``None`` plus a warning.

    A digest/size mismatch, a malformed envelope or a MinIO outage raises
    :class:`ResultStoreError`. Such a result MUST NOT be treated as
    "not found" and must never trigger re-inference.

    Damage is distinguished from unreachability: a reachable object whose bytes
    contradict its envelope (envelope fields, size, SHA-256) or that is not a
    UTF-8 JSON object raises :class:`ResultCorruptError` (a
    :class:`ResultStoreError` subclass). A store outage stays a plain
    :class:`ResultStoreError`.
    """
    job_id = _validate_job_id(job_id)
    attempt = _validate_attempt(attempt)

    client = _get_client()
    bucket = _bucket()
    result_key, envelope_key = _object_keys(job_id, attempt)

    envelope = _read_envelope(client, bucket, envelope_key)
    if envelope is None:
        if _stat_size(client, bucket, result_key) is not None:
            logger.warning(
                "unfinished result write for %s/%s: result.json exists without "
                "envelope.json; treating as not delivered",
                job_id, attempt,
            )
        return None

    _validate_envelope(envelope, job_id, attempt)
    ref = ResultRef(
        job_id=job_id,
        attempt=attempt,
        bucket=bucket,
        result_key=result_key,
        envelope_key=envelope_key,
        sha256=str(envelope["resultSha256"]),
        bytes=int(envelope["resultBytes"]),
    )

    payload = _read_object_bytes(client, bucket, result_key)
    if payload is None:
        raise ResultStoreError(
            f"envelope exists but {result_key} is missing for {job_id}/{attempt}"
        )
    if len(payload) != ref.bytes:
        raise ResultCorruptError(
            f"{result_key} has {len(payload)} bytes, envelope says {ref.bytes}"
        )
    actual = hashlib.sha256(payload).hexdigest()
    if actual != ref.sha256:
        raise ResultCorruptError(
            f"sha256 mismatch for {job_id}/{attempt}: stored bytes {actual}, "
            f"envelope {ref.sha256}"
        )
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultCorruptError(
            f"result.json for {job_id}/{attempt} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise ResultCorruptError(
            f"result.json for {job_id}/{attempt} is not a JSON object"
        )
    return parsed, ref


# ---------------------------------------------------------------------------
# Async helpers for the stream consumer (the frozen API itself is sync).
# ---------------------------------------------------------------------------

async def save_result_async(
    job_id: str,
    attempt: int,
    result: dict,
    *,
    model_version: str | None = None,
    model_digest: str | None = None,
) -> ResultRef:
    """``asyncio.to_thread`` wrapper around :func:`save_result`."""
    return await asyncio.to_thread(
        save_result,
        job_id,
        attempt,
        result,
        model_version=model_version,
        model_digest=model_digest,
    )


async def mark_pending_async(ref: ResultRef, **meta) -> None:
    """``asyncio.to_thread`` wrapper around :func:`mark_pending`."""
    await asyncio.to_thread(mark_pending, ref, **meta)


async def clear_pending_async(job_id: str, attempt: int) -> None:
    """``asyncio.to_thread`` wrapper around :func:`clear_pending`."""
    await asyncio.to_thread(clear_pending, job_id, attempt)


async def load_result_async(job_id: str, attempt: int) -> tuple[dict, ResultRef] | None:
    """``asyncio.to_thread`` wrapper around :func:`load_result`."""
    return await asyncio.to_thread(load_result, job_id, attempt)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _client_factory():
    """Create a MinIO client (monkeypatchable; no connection at import time).

    Read/connect timeouts are bounded by ``settings.result_read_timeout_seconds``
    so a hung object store surfaces as :class:`ResultStoreError` instead of
    blocking the worker (and the ACK decision) forever.
    """
    from minio import Minio
    from urllib3 import PoolManager, Timeout

    timeout = float(settings.result_read_timeout_seconds)
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        http_client=PoolManager(
            timeout=Timeout(connect=timeout, read=timeout),
            retries=False,
        ),
    )


def _redis_factory():
    """Create a sync Redis client (monkeypatchable; no connection at import)."""
    import redis as _redis

    return _redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5.0,
        socket_timeout=float(settings.result_read_timeout_seconds),
        health_check_interval=30,
    )


def _get_client():
    global _minio_client
    if _minio_client is None:
        _minio_client = _client_factory()
    return _minio_client


def _get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = _redis_factory()
    return _redis_client


def reset_clients() -> None:
    """Drop cached clients (test helper; safe to call at runtime)."""
    global _minio_client, _redis_client
    _minio_client = None
    _redis_client = None
    _ready_buckets.clear()


def _bucket() -> str:
    return (settings.result_bucket or "analysis-artifacts").strip("/")


def _prefix() -> str:
    return (settings.result_prefix or "analysis-results").strip("/")


def _object_keys(job_id: str, attempt: int) -> tuple[str, str]:
    base = f"{_prefix()}/{job_id}/{attempt}"
    return f"{base}/{RESULT_OBJECT_NAME}", f"{base}/{ENVELOPE_OBJECT_NAME}"


def _pending_hash_key(job_id: str, attempt: int) -> str:
    return PENDING_HASH_TEMPLATE.format(job_id=job_id, attempt=attempt)


def _escape_glob(value: str) -> str:
    """Escape Redis SCAN glob metacharacters in a literal key fragment."""
    out = []
    for ch in value:
        if ch in ("*", "?", "[", "]", "\\"):
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id must be a non-empty string")
    if job_id in (".", "..") or any(ch in job_id for ch in ("/", "\\", "\x00")):
        raise ValueError(f"unsafe job_id: {job_id!r}")
    return job_id


def _validate_attempt(attempt: int) -> int:
    # bool is an int subclass; a boolean fencing token is always a bug.
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise ValueError("attempt must be an int")
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    return attempt


def _utc_now_iso() -> str:
    """UTC ISO-8601 with a ``Z`` suffix (parsable by Java ``Instant.parse``)."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _ensure_bucket(client, bucket: str) -> None:
    if bucket in _ready_buckets:
        return
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info("created result bucket %s", bucket)
    except Exception as exc:  # noqa: BLE001 - re-raised with context below
        _raise_store_error(f"ensure bucket {bucket}", exc)
    _ready_buckets.add(bucket)


def _put_bytes(client, bucket: str, key: str, payload: bytes) -> None:
    try:
        client.put_object(
            bucket,
            key,
            io.BytesIO(payload),
            len(payload),
            content_type="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        _raise_store_error(f"put {key}", exc)


def _read_object_bytes(client, bucket: str, key: str) -> bytes | None:
    try:
        response = client.get_object(bucket, key)
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            return None
        _raise_store_error(f"get {key}", exc)
    try:
        return response.read()
    except Exception as exc:  # noqa: BLE001 - body read timeout / reset
        _raise_store_error(f"read {key}", exc)
    finally:
        _close_response(response)


def _close_response(response) -> None:
    for name in ("close", "release_conn"):
        close = getattr(response, name, None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - closing must not mask the result
                logger.debug("failed to close MinIO response", exc_info=True)


def _read_envelope(client, bucket: str, key: str) -> dict | None:
    raw = _read_object_bytes(client, bucket, key)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultCorruptError(f"envelope {key} is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ResultCorruptError(f"envelope {key} is not a JSON object")
    return data


def _stat_size(client, bucket: str, key: str) -> int | None:
    try:
        stat = client.stat_object(bucket, key)
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            return None
        _raise_store_error(f"stat {key}", exc)
    size = getattr(stat, "size", None)
    if size is None:
        return 0
    return int(size)


def _validate_envelope(envelope: dict, job_id: str, attempt: int) -> None:
    """Check the envelope's identity fields; damage raises ResultCorruptError."""
    if str(envelope.get("jobId")) != job_id:
        raise ResultCorruptError(
            f"envelope jobId {envelope.get('jobId')!r} != {job_id!r}"
        )
    raw_attempt = envelope.get("attempt")
    if isinstance(raw_attempt, bool) or not isinstance(raw_attempt, int):
        raise ResultCorruptError(f"envelope attempt {raw_attempt!r} is not an int")
    if raw_attempt != attempt:
        raise ResultCorruptError(
            f"envelope attempt {raw_attempt} != requested {attempt}"
        )
    digest = envelope.get("resultSha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ResultCorruptError("envelope resultSha256 is missing or malformed")
    size = envelope.get("resultBytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ResultCorruptError("envelope resultBytes is missing or malformed")


def _is_missing(exc: BaseException) -> bool:
    """True only for an explicit "this object/bucket does not exist" answer.

    Timeouts, 403/AccessDenied, 5xx and connection resets are *not* missing:
    treating them as missing would re-run inference and can overwrite a good
    result (protocol §6).
    """
    from minio.error import S3Error

    if not isinstance(exc, S3Error):
        return False
    return str(getattr(exc, "code", "") or "") in _MISSING_CODES


def _raise_store_error(action: str, exc: BaseException) -> None:
    detail = f"{type(exc).__name__}: {exc}"
    from minio.error import S3Error

    if isinstance(exc, S3Error):
        code = getattr(exc, "code", None)
        status = getattr(getattr(exc, "response", None), "status", None)
        detail = f"{type(exc).__name__}: code={code} status={status} {exc}"
    raise ResultStoreError(f"MinIO {action} failed: {detail}") from exc
