"""Tests for the durable result store (protocol §2/§3).

All MinIO/Redis access is faked: these tests must run without a live object
store and must not depend on network availability.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
from types import SimpleNamespace

import pytest
from minio.error import S3Error

from app.core.config import settings
from app.workers import result_store
from app.workers.result_store import (
    PENDING_SET_KEY,
    ResultConflictError,
    ResultRef,
    ResultStoreError,
    canonical_bytes,
    clear_pending,
    load_result,
    mark_pending,
    save_result,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _s3_error(code: str, status: int = 400) -> S3Error:
    return S3Error(
        code, f"message-{code}", "resource", "req-id", "host-id",
        SimpleNamespace(status=status), "analysis-artifacts", "key",
    )


class _ObjectStat:
    def __init__(self, size: int) -> None:
        self.size = size


class _ObjectResponse:
    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)
        self.closed = False

    def read(self) -> bytes:
        return self._buffer.read()

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        pass


class FakeMinio:
    """Minimal S3 facade: bucket_exists/make_bucket/put/stat/get."""

    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[tuple[str, str]] = []
        self.failures: dict[str, Exception] = {}

    def _fail_if_configured(self, op: str) -> None:
        if op in self.failures:
            raise self.failures[op]

    def bucket_exists(self, bucket: str) -> bool:
        self._fail_if_configured("bucket_exists")
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self._fail_if_configured("make_bucket")
        self.buckets.add(bucket)

    def put_object(self, bucket, key, data, length, content_type=None):
        self._fail_if_configured("put_object")
        payload = data.read()
        assert len(payload) == length
        self.objects[(bucket, key)] = payload
        self.put_calls.append((bucket, key))

    def stat_object(self, bucket, key):
        self._fail_if_configured("stat_object")
        payload = self.objects.get((bucket, key))
        if payload is None:
            raise _s3_error("NoSuchKey", 404)
        return _ObjectStat(len(payload))

    def get_object(self, bucket, key):
        self._fail_if_configured("get_object")
        payload = self.objects.get((bucket, key))
        if payload is None:
            raise _s3_error("NoSuchKey", 404)
        return _ObjectResponse(payload)


class _FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple, dict]] = []

    def sadd(self, key, member):
        self._ops.append(("sadd", (key, member), {}))
        return self

    def hset(self, key, mapping=None, **kwargs):
        self._ops.append(("hset", (key,), {"mapping": mapping, **kwargs}))
        return self

    def hsetnx(self, key, field, value):
        self._ops.append(("hsetnx", (key, field, value), {}))
        return self

    def delete(self, key):
        self._ops.append(("delete", (key,), {}))
        return self

    def execute(self):
        self._redis.execute_count += 1
        if self._redis.fail_on_execute is not None:
            raise self._redis.fail_on_execute
        applied = []
        for name, args, kwargs in self._ops:
            applied.append(getattr(self._redis, name)(*args, **kwargs))
        return applied


class FakeRedis:
    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.fail_on_execute: Exception | None = None
        self.fail_on_scan: Exception | None = None
        self.execute_count = 0

    # pipeline-backed commands
    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)
        return 1

    def hset(self, key, mapping=None, **kwargs):
        fields = dict(mapping or {})
        fields.update(kwargs)
        self.hashes.setdefault(key, {}).update(fields)
        return len(fields)

    def hsetnx(self, key, field, value):
        target = self.hashes.setdefault(key, {})
        if field in target:
            return 0
        target[field] = value
        return 1

    def delete(self, key):
        existed = key in self.hashes
        self.hashes.pop(key, None)
        return 1 if existed else 0

    def srem(self, key, member):
        members = self.sets.get(key)
        if not members or member not in members:
            return 0
        members.discard(member)
        return 1

    def pipeline(self):
        return _FakePipeline(self)

    def scan_iter(self, match=None, count=None):
        if self.fail_on_scan is not None:
            raise self.fail_on_scan
        import fnmatch

        pattern = (match or "*").replace("\\", "")
        for key in list(self.hashes):
            if fnmatch.fnmatchcase(key, pattern):
                yield key


@pytest.fixture
def store(monkeypatch):
    fake_minio = FakeMinio()
    fake_redis = FakeRedis()
    monkeypatch.setattr(result_store, "_minio_client", fake_minio)
    monkeypatch.setattr(result_store, "_redis_client", fake_redis)
    monkeypatch.setattr(result_store, "_ready_buckets", set())
    yield SimpleNamespace(minio=fake_minio, redis=fake_redis)
    result_store.reset_clients()


def _result(**overrides):
    result = {
        "status": "SUCCEEDED",
        "attempt": 2,
        "summary": "课堂氛围良好",
        "windows": [{"index": 0, "score": 88.5}],
    }
    result.update(overrides)
    return result


def _result_key(job_id="job-1", attempt=2):
    return f"{settings.result_prefix}/{job_id}/{attempt}/result.json"


def _envelope_key(job_id="job-1", attempt=2):
    return f"{settings.result_prefix}/{job_id}/{attempt}/envelope.json"


# ---------------------------------------------------------------------------
# canonical bytes
# ---------------------------------------------------------------------------

def test_canonical_bytes_is_stable_and_key_order_independent():
    first = canonical_bytes({"b": 1, "a": "课堂"})
    second = canonical_bytes({"a": "课堂", "b": 1})

    assert first == second
    assert first == '{"a":"课堂","b":1}'.encode("utf-8")

    class _Opaque:
        def __str__(self) -> str:
            return "opaque"

    # Non-JSON values fall back to str() instead of failing the store write.
    assert canonical_bytes({"x": _Opaque()}) == b'{"x":"opaque"}'


# ---------------------------------------------------------------------------
# canonical bytes: non-finite floats (F9)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_bytes_rejects_non_finite_floats(value):
    with pytest.raises(result_store.ResultSerializationError) as excinfo:
        canonical_bytes({"confidence": value})

    message = str(excinfo.value)
    assert "non-finite float in result at key confidence" in message
    # A ResultStoreError subclass: callers that only catch the base class still
    # treat it as "store state unknown" and never ACK.
    assert isinstance(excinfo.value, ResultStoreError)


def test_canonical_bytes_reports_the_nested_path_of_a_non_finite_float():
    payload = {"windows": [{"score": 88.5}, {"score": float("nan")}]}

    with pytest.raises(result_store.ResultSerializationError) as excinfo:
        canonical_bytes(payload)

    assert "windows[1].score" in str(excinfo.value)


def test_canonical_bytes_rejects_non_finite_values_inside_lists():
    with pytest.raises(result_store.ResultSerializationError, match=r"probs\[1\]"):
        canonical_bytes({"probs": [0.5, float("inf")]})


def test_canonical_bytes_still_accepts_finite_floats():
    payload = {"score": 0.0, "ratio": -1.5, "big": 1e308, "tiny": 5e-324}

    assert json.loads(canonical_bytes(payload)) == payload


def test_save_result_with_a_non_finite_float_writes_nothing(store):
    with pytest.raises(result_store.ResultSerializationError):
        save_result("job-1", 2, _result(confidence=float("nan")))

    assert store.minio.objects == {}, "no object may be written for a bad payload"
    assert store.minio.put_calls == []


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------

def test_save_then_load_roundtrip(store):
    result = _result()

    ref = save_result("job-1", 2, result, model_version="yolov8n", model_digest="abc123")

    assert isinstance(ref, ResultRef)
    assert ref.job_id == "job-1"
    assert ref.attempt == 2
    assert ref.bucket == settings.result_bucket
    assert ref.result_key == _result_key()
    assert ref.envelope_key == _envelope_key()
    expected = canonical_bytes(result)
    assert ref.bytes == len(expected)
    assert ref.sha256 == hashlib.sha256(expected).hexdigest()

    # The stored bytes are exactly the canonical bytes (no re-serialisation).
    assert store.minio.objects[(settings.result_bucket, _result_key())] == expected

    loaded, loaded_ref = load_result("job-1", 2)
    assert loaded == result
    assert loaded_ref == ref

    envelope = json.loads(
        store.minio.objects[(settings.result_bucket, _envelope_key())].decode("utf-8")
    )
    assert envelope["schemaVersion"] == settings.result_schema_version
    assert envelope["jobId"] == "job-1"
    assert envelope["attempt"] == 2
    assert envelope["resultSha256"] == ref.sha256
    assert envelope["resultBytes"] == ref.bytes
    assert envelope["status"] == "SUCCEEDED"
    assert envelope["modelVersion"] == "yolov8n"
    assert envelope["modelDigest"] == "abc123"
    assert envelope["createdAt"].endswith("Z")


def test_result_object_is_written_before_envelope(store):
    save_result("job-1", 2, _result())

    # put order proves the envelope is the commit marker.
    assert [key for _, key in store.minio.put_calls] == [
        _result_key(), _envelope_key(),
    ]


def test_chinese_content_roundtrip(store):
    result = {"status": "SUCCEEDED", "note": "教师提问 12 次，学生应答「很好」"}

    save_result("job-1", 1, result)
    loaded, _ = load_result("job-1", 1)

    assert loaded["note"] == "教师提问 12 次，学生应答「很好」"


# ---------------------------------------------------------------------------
# idempotency / conflict
# ---------------------------------------------------------------------------

def test_same_digest_is_idempotent_and_does_not_rewrite(store):
    first = save_result("job-1", 2, _result())
    puts_after_first = list(store.minio.put_calls)

    # Same logical payload with a different dict insertion order: canonical
    # bytes are identical, so the stored object must be reused, not rewritten.
    reordered = {
        "windows": [{"index": 0, "score": 88.5}],
        "summary": "课堂氛围良好",
        "attempt": 2,
        "status": "SUCCEEDED",
    }
    second = save_result("job-1", 2, reordered)

    assert second == first
    assert store.minio.put_calls == puts_after_first


def test_conflict_raises_and_keeps_first_object(store):
    first = save_result("job-1", 2, _result())
    stored_before = store.minio.objects[(settings.result_bucket, _result_key())]

    with pytest.raises(ResultConflictError) as excinfo:
        save_result("job-1", 2, _result(status="FAILED"))

    assert excinfo.value.ref == first
    assert store.minio.objects[(settings.result_bucket, _result_key())] == stored_before
    envelope = json.loads(
        store.minio.objects[(settings.result_bucket, _envelope_key())].decode("utf-8")
    )
    assert envelope["resultSha256"] == first.sha256


# ---------------------------------------------------------------------------
# load: missing / tampered / orphan
# ---------------------------------------------------------------------------

def test_missing_envelope_returns_none(store):
    save_result("job-1", 2, _result())
    del store.minio.objects[(settings.result_bucket, _envelope_key())]

    assert load_result("job-1", 2) is None


def test_orphan_result_is_not_delivered_and_logs_warning(store, caplog):
    save_result("job-1", 2, _result())
    del store.minio.objects[(settings.result_bucket, _envelope_key())]

    with caplog.at_level(logging.WARNING, logger=result_store.__name__):
        assert load_result("job-1", 2) is None

    assert any("without" in record.getMessage() for record in caplog.records)


def test_absent_objects_return_none(store):
    assert load_result("job-unknown", 1) is None
    # A confirmed NoSuchBucket also means "not there".
    store.minio.buckets.clear()
    store.minio.failures["get_object"] = _s3_error("NoSuchBucket", 404)
    assert load_result("job-unknown", 1) is None


def test_tampered_result_bytes_raise(store):
    save_result("job-1", 2, _result())
    key = (settings.result_bucket, _result_key())
    store.minio.objects[key] = store.minio.objects[key].replace(b"88.5", b"99.5")

    with pytest.raises(ResultStoreError, match="sha256 mismatch"):
        load_result("job-1", 2)


# ---------------------------------------------------------------------------
# load: corrupt vs unreachable (F13)
# ---------------------------------------------------------------------------

def test_tampered_result_raises_the_corrupt_subclass(store):
    """Damage is classified separately so the worker can keep the index."""
    save_result("job-1", 2, _result())
    key = (settings.result_bucket, _result_key())
    store.minio.objects[key] = b'{"tampered":true}'

    with pytest.raises(result_store.ResultCorruptError):
        load_result("job-1", 2)


def test_malformed_envelope_json_is_corrupt(store):
    save_result("job-1", 2, _result())
    store.minio.objects[(settings.result_bucket, _envelope_key())] = b"{not json"

    with pytest.raises(result_store.ResultCorruptError):
        load_result("job-1", 2)


def test_envelope_identity_mismatch_is_corrupt(store):
    save_result("job-1", 2, _result())
    key = (settings.result_bucket, _envelope_key())
    envelope = json.loads(store.minio.objects[key].decode("utf-8"))
    envelope["jobId"] = "job-other"
    store.minio.objects[key] = canonical_bytes(envelope)

    with pytest.raises(result_store.ResultCorruptError):
        load_result("job-1", 2)


def test_non_utf8_result_payload_is_corrupt(store):
    save_result("job-1", 2, _result())
    key = (settings.result_bucket, _result_key())
    payload = b"\xff\xfe\x00garbage"
    store.minio.objects[key] = payload
    # Keep the envelope consistent so the failure is the JSON decode, not the size.
    envelope_key = (settings.result_bucket, _envelope_key())
    envelope = json.loads(store.minio.objects[envelope_key].decode("utf-8"))
    envelope["resultBytes"] = len(payload)
    envelope["resultSha256"] = hashlib.sha256(payload).hexdigest()
    store.minio.objects[envelope_key] = canonical_bytes(envelope)

    with pytest.raises(result_store.ResultCorruptError, match="not valid UTF-8 JSON"):
        load_result("job-1", 2)


def test_store_outage_is_not_classified_as_corrupt(store):
    """An unreachable store stays a plain ResultStoreError (retry-able)."""
    save_result("job-1", 2, _result())
    store.minio.failures["get_object"] = ConnectionResetError("connection reset")

    with pytest.raises(ResultStoreError) as excinfo:
        load_result("job-1", 2)

    assert not isinstance(excinfo.value, result_store.ResultCorruptError)


def test_result_corrupt_error_is_a_result_store_error():
    assert issubclass(result_store.ResultCorruptError, ResultStoreError)
    assert issubclass(result_store.ResultSerializationError, ResultStoreError)


def test_envelope_without_result_object_raises(store):
    save_result("job-1", 2, _result())
    del store.minio.objects[(settings.result_bucket, _result_key())]

    with pytest.raises(ResultStoreError, match="missing"):
        load_result("job-1", 2)


def test_envelope_job_or_attempt_mismatch_raises(store):
    save_result("job-1", 2, _result())
    key = (settings.result_bucket, _envelope_key())
    envelope = json.loads(store.minio.objects[key].decode("utf-8"))
    envelope["attempt"] = 3
    store.minio.objects[key] = canonical_bytes(envelope)

    with pytest.raises(ResultStoreError, match="attempt"):
        load_result("job-1", 2)


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------

def test_forbidden_is_an_error_not_absent(store):
    save_result("job-1", 2, _result())
    store.minio.failures["get_object"] = _s3_error("AccessDenied", 403)

    with pytest.raises(ResultStoreError) as excinfo:
        load_result("job-1", 2)
    assert "AccessDenied" in str(excinfo.value)


def test_connection_error_is_an_error_not_absent(store):
    store.minio.failures["get_object"] = ConnectionResetError("connection reset")

    with pytest.raises(ResultStoreError):
        load_result("job-1", 2)


def test_put_failure_raises_and_writes_no_envelope(store):
    store.minio.failures["put_object"] = _s3_error("InternalError", 500)

    with pytest.raises(ResultStoreError):
        save_result("job-1", 2, _result())

    assert store.minio.objects == {}


def test_no_such_key_on_stat_means_absent(store):
    store.minio.failures["stat_object"] = _s3_error("NoSuchKey", 404)

    assert load_result("job-1", 2) is None


# ---------------------------------------------------------------------------
# Redis pending index
# ---------------------------------------------------------------------------

def test_mark_pending_records_hash_and_set(store):
    ref = save_result("job-1", 2, _result())

    mark_pending(ref, lastError=None)

    hash_key = "analysis:result:job-1:2"
    assert "job-1" in store.redis.sets[PENDING_SET_KEY]
    fields = store.redis.hashes[hash_key]
    assert fields["bucket"] == settings.result_bucket
    assert fields["object"] == ref.result_key
    assert fields["sha256"] == ref.sha256
    assert fields["bytes"] == str(ref.bytes)
    assert fields["firstSeenAt"] == fields["savedAt"]
    assert "lastAttemptAt" in fields
    assert "lastError" not in fields  # None values are skipped


def test_mark_pending_keeps_first_seen_at_on_remark(store, monkeypatch):
    ref = save_result("job-1", 2, _result())

    times = iter(["2026-10-26T00:00:00.000000Z", "2026-10-26T00:05:00.000000Z"])
    monkeypatch.setattr(result_store, "_utc_now_iso", lambda: next(times))

    mark_pending(ref)
    mark_pending(ref)

    fields = store.redis.hashes["analysis:result:job-1:2"]
    assert fields["firstSeenAt"] == "2026-10-26T00:00:00.000000Z"
    assert fields["lastAttemptAt"] == "2026-10-26T00:05:00.000000Z"


def test_mark_pending_propagates_redis_failure(store):
    ref = save_result("job-1", 2, _result())
    store.redis.fail_on_execute = ConnectionError("redis down")

    with pytest.raises(ConnectionError):
        mark_pending(ref)


def test_clear_pending_keeps_sibling_attempt(store):
    ref_one = save_result("job-1", 1, _result(attempt=1))
    ref_two = save_result("job-1", 2, _result(attempt=2))
    mark_pending(ref_one)
    mark_pending(ref_two)

    clear_pending("job-1", 1)

    assert "analysis:result:job-1:1" not in store.redis.hashes
    assert "analysis:result:job-1:2" in store.redis.hashes
    assert "job-1" in store.redis.sets[PENDING_SET_KEY]

    clear_pending("job-1", 2)

    assert "analysis:result:job-1:2" not in store.redis.hashes
    assert "job-1" not in store.redis.sets[PENDING_SET_KEY]


def test_clear_pending_does_not_touch_other_jobs(store):
    ref_a = save_result("job-a", 1, _result(attempt=1))
    ref_b = save_result("job-b", 1, _result(attempt=1))
    mark_pending(ref_a)
    mark_pending(ref_b)

    clear_pending("job-a", 1)

    assert "analysis:result:job-b:1" in store.redis.hashes
    assert "job-b" in store.redis.sets[PENDING_SET_KEY]


def test_clear_pending_propagates_redis_failure(store):
    store.redis.fail_on_execute = ConnectionError("redis down")

    with pytest.raises(ConnectionError):
        clear_pending("job-1", 2)


def test_clear_pending_restores_job_when_new_attempt_races_in(store):
    ref = save_result("job-1", 1, _result(attempt=1))
    mark_pending(ref)

    original_scan = store.redis.scan_iter
    calls = {"count": 0}

    def racing_scan(match=None, count=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return iter(())
        # A new attempt indexed itself right after the first SCAN.
        return iter(["analysis:result:job-1:2"])

    store.redis.scan_iter = racing_scan
    clear_pending("job-1", 1)
    store.redis.scan_iter = original_scan

    assert calls["count"] == 2
    assert "job-1" in store.redis.sets[PENDING_SET_KEY]


def test_clear_pending_escapes_glob_characters_in_job_id(store):
    ref_plain = save_result("jobA1", 1, _result(attempt=1))
    ref_glob = save_result("job*", 1, _result(attempt=1))
    mark_pending(ref_plain)
    mark_pending(ref_glob)

    clear_pending("job*", 1)

    assert "analysis:result:jobA1:1" in store.redis.hashes
    assert "jobA1" in store.redis.sets[PENDING_SET_KEY]


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attempt", [True, False, "2", 2.0, -1, None])
def test_invalid_attempt_rejected(store, attempt):
    with pytest.raises(ValueError):
        save_result("job-1", attempt, _result())


@pytest.mark.parametrize("job_id", ["", "  ", "../escape", "a/b", "a\\b", None])
def test_unsafe_job_id_rejected(store, job_id):
    with pytest.raises(ValueError):
        save_result(job_id, 1, _result())


def test_result_must_be_dict(store):
    with pytest.raises(TypeError):
        save_result("job-1", 1, ["not", "a", "dict"])


# ---------------------------------------------------------------------------
# lazy clients
# ---------------------------------------------------------------------------

def test_clients_are_created_lazily_through_factories(monkeypatch):
    created = SimpleNamespace(minio=[], redis=[])

    def factory():
        created.minio.append(1)
        return FakeMinio()

    def redis_factory():
        created.redis.append(1)
        return FakeRedis()

    monkeypatch.setattr(result_store, "_minio_client", None)
    monkeypatch.setattr(result_store, "_redis_client", None)
    monkeypatch.setattr(result_store, "_ready_buckets", set())
    monkeypatch.setattr(result_store, "_client_factory", factory)
    monkeypatch.setattr(result_store, "_redis_factory", redis_factory)

    # Importing/first reference performs no I/O; the factory runs on first use.
    assert result_store._minio_client is None
    assert result_store._redis_client is None

    ref = save_result("job-1", 1, _result(attempt=1))
    mark_pending(ref)

    assert len(created.minio) == 1
    assert len(created.redis) == 1
    # Second call reuses the cached client.
    save_result("job-2", 1, _result(attempt=1))
    assert len(created.minio) == 1

    result_store.reset_clients()
