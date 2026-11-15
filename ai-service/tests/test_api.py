from pathlib import Path
import shutil

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.workers.asr import HAS_WHISPER

UPLOAD_ROOT = Path("data/uploads").resolve()


def _under_upload_root(name: str) -> str:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    return str(UPLOAD_ROOT / name)


def test_health_reports_capability_boundaries(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    status = client.get("/status").json()
    assert status["capabilities"]["courseware"]["status"] == "available"
    assert status["capabilities"]["speaker_diarization"]["status"] == "not_implemented"

    expected_audio = "available" if HAS_WHISPER else "not_implemented"
    assert status["capabilities"]["audio_transcription"]["status"] == expected_audio


def test_protected_endpoints_reject_missing_token(bare_client: TestClient):
    response = bare_client.post("/analyze/video", json={
        "video_path": _under_upload_root("missing.mp4"),
        "lesson_session_id": "lesson-test",
    })
    assert response.status_code == 401


def test_protected_endpoints_reject_wrong_token(bare_client: TestClient):
    response = bare_client.post(
        "/analyze/video",
        json={"video_path": _under_upload_root("missing.mp4"), "lesson_session_id": "lesson-test"},
        headers={"X-Service-Token": "not-the-right-token"},
    )
    assert response.status_code == 401


def test_missing_video_fails_without_explicit_demo(client: TestClient):
    response = client.post("/analyze/video", json={
        "video_path": _under_upload_root("definitely-missing.mp4"),
        "lesson_session_id": "lesson-test",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "VIDEO_NOT_FOUND"


def test_demo_video_requires_service_and_request_opt_in(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "demo_enabled", True)

    response = client.post("/analyze/video", json={
        "video_path": _under_upload_root("definitely-missing-demo.mp4"),
        "lesson_session_id": "lesson-demo",
        "allow_demo": True,
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["mode"] == "demo"
    assert payload["window_count"] > 0


def test_courseware_text_parsing_uses_real_file(client: TestClient, tmp_path: Path):
    source = tmp_path / "lesson.txt"
    source.write_text("教学目标\n理解形成性评价\n教学重点\n课堂反馈", encoding="utf-8")
    target = UPLOAD_ROOT / "lesson-text-api.txt"
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, target)

    response = client.post("/analyze/courseware", json={
        "file_path": str(target),
        "original_name": target.name,
        "lesson_session_id": "lesson-text",
    })

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["mode"] == "real"
    assert payload["objectives"]
    assert payload["key_points"]


def test_video_request_validates_seat_count(client: TestClient):
    response = client.post("/analyze/video", json={"total_seats": 0})

    assert response.status_code == 422


def test_asr_returns_501_when_whisper_unavailable(client: TestClient):
    if HAS_WHISPER:
        pytest.skip("faster-whisper installed; skipping 501 check")

    response = client.post(
        "/asr",
        files={"file": ("test.wav", b"fake-audio-bytes", "audio/wav")},
    )

    assert response.status_code == 501
    payload = response.json()
    assert payload["status"] == "not_implemented"
    assert payload["route"] == "/asr"


def test_unimplemented_routes_return_501_not_404(client: TestClient):
    """Routes that Java AiServiceClient still calls must answer explicitly."""
    face = client.post(
        "/analyze_face",
        files={"file": ("face.jpg", b"fake-image-bytes", "image/jpeg")},
    )
    assert face.status_code == 501
    assert face.json()["status"] == "not_implemented"

    diarize = client.post(
        "/diarize",
        files={"file": ("audio.wav", b"fake-audio-bytes", "audio/wav")},
    )
    assert diarize.status_code == 501
    assert diarize.json()["status"] == "not_implemented"

    encode = client.post("/encode_text", json={"text": "你好"})
    assert encode.status_code == 501
    assert encode.json()["status"] == "not_implemented"
