"""Regression tests for real audio/courseware input handling."""
from pathlib import Path

import pytest

from app.workers.asr import transcribe_audio
from app.workers.courseware import parse_courseware


@pytest.mark.asyncio
async def test_audio_missing_file_returns_explicit_failure():
    result = transcribe_audio("missing-audio.wav")
    assert result["status"] in {"failed", "not_implemented"}
    if result["status"] == "failed":
        assert result.get("error_code") == "AUDIO_NOT_FOUND"


@pytest.mark.asyncio
async def test_empty_courseware_is_not_reported_as_success(tmp_path: Path):
    upload_root = Path("data/uploads").resolve()
    upload_root.mkdir(parents=True, exist_ok=True)
    source = upload_root / "empty-courseware-test.txt"
    source.write_text("   \n", encoding="utf-8")
    result = parse_courseware({"file_path": str(source), "original_name": source.name})
    assert result["status"] == "failed"
    assert result["error_code"] == "EMPTY_CONTENT"
