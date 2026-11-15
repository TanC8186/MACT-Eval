"""Tests for the media job dispatch (video/audio object resolution)."""
import pytest

from app.workers import base


@pytest.mark.asyncio
async def test_handle_video_resolves_object_and_passes_params(monkeypatch):
    captured = {}

    def fake_analyze(params):
        captured["params"] = params
        return {"status": "succeeded"}

    monkeypatch.setattr("app.workers.base._download_from_minio", lambda b, o: "/tmp/f1.mp4")
    monkeypatch.setattr("app.workers.base.analyze_video", fake_analyze)

    await base._handle_video({
        "objectName": "f1.mp4",
        "bucket": "classroom-raw-media",
        "lessonSessionId": "lesson-1",
        "totalSeats": "45",
        "sampleInterval": "2",
        "windowDuration": "60",
    })

    assert captured["params"]["video_path"] == "/tmp/f1.mp4"
    assert captured["params"]["lesson_session_id"] == "lesson-1"
    assert captured["params"]["total_seats"] == 45


@pytest.mark.asyncio
async def test_handle_audio_calls_transcribe(monkeypatch):
    captured = {}

    def fake_transcribe(path, model, device, compute):
        captured["path"] = path
        captured["args"] = (model, device, compute)
        return {"status": "succeeded", "text": "你好"}

    monkeypatch.setattr("app.workers.base._download_from_minio", lambda b, o: "/tmp/f1.wav")
    monkeypatch.setattr("app.workers.base.settings.whisper_model_size", "small")
    monkeypatch.setattr("app.workers.base.settings.whisper_device", "cpu")
    monkeypatch.setattr("app.workers.base.settings.whisper_compute_type", "int8")

    import app.workers.asr as asr_mod
    monkeypatch.setattr(asr_mod, "HAS_WHISPER", True)
    monkeypatch.setattr(asr_mod, "transcribe_audio", fake_transcribe)

    result = await base._handle_audio({
        "objectName": "f1.wav",
        "bucket": "classroom-raw-media",
        "lessonSessionId": "lesson-1",
    })

    assert result["status"] == "succeeded"
    assert captured["path"] == "/tmp/f1.wav"
    assert captured["args"] == ("small", "cpu", "int8")


@pytest.mark.asyncio
async def test_dispatch_routes_audio(monkeypatch):
    captured = {}

    async def fake_handle_audio(job):
        captured["job"] = job
        return {"status": "succeeded"}

    monkeypatch.setattr("app.workers.base._handle_audio", fake_handle_audio)
    result = await base.dispatch_job({"type": "AUDIO_ANALYSIS", "objectName": "f1.wav"})
    assert result["status"] == "succeeded"
    assert captured["job"]["objectName"] == "f1.wav"


@pytest.mark.asyncio
async def test_dispatch_routes_video(monkeypatch):
    captured = {}

    async def fake_handle_video(job):
        captured["job"] = job
        return {"status": "succeeded"}

    monkeypatch.setattr("app.workers.base._handle_video", fake_handle_video)
    result = await base.dispatch_job({"type": "VIDEO_ANALYSIS", "objectName": "f1.mp4"})
    assert result["status"] == "succeeded"
    assert captured["job"]["objectName"] == "f1.mp4"
