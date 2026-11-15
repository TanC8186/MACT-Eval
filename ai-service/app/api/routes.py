"""
AI Service API routes - vision, audio, grading endpoints.

Real capabilities:
- POST /analyze/video   -> OpenCV classroom video analysis
- POST /analyze/courseware -> courseware text parsing
- POST /asr             -> faster-whisper audio transcription

Explicitly NOT implemented (return HTTP 501, never fake data):
- POST /analyze_face    (MediaPipe face landmarks)
- POST /diarize         (pyannote speaker diarization)
- POST /encode_text     (BERT text encoding)
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core.auth import require_service_token
from app.core.config import settings
from app.core.paths import media_work_dir, resolve_under_roots, safe_basename
from app.workers.vision import analyze_video, _resolve_face_weights
from app.workers.courseware import parse_courseware
from app.workers.prep import analyze_prep
from app.workers.asr import HAS_WHISPER, transcribe_audio

logger = logging.getLogger(__name__)
router = APIRouter()

# Protect analysis endpoints from anonymous LAN access / LLM abuse.
PROTECTED = [Depends(require_service_token)]
MAX_ASR_BYTES = 200 * 1024 * 1024  # 200MB


class VideoAnalysisRequest(BaseModel):
    video_path: Optional[str] = None
    lesson_session_id: str = "unknown"
    total_seats: int = Field(default=45, ge=1, le=500)
    sample_interval: int = Field(default=2, ge=1, le=60)
    window_duration: int = Field(default=60, ge=10, le=600)
    allow_demo: bool = False


class CoursewareAnalysisRequest(BaseModel):
    file_path: Optional[str] = None
    original_name: str = "unknown"
    lesson_session_id: str = "unknown"
    allow_demo: bool = False


class PrepAnalysisRequest(BaseModel):
    full_text: str = Field(default="", max_length=50_000)
    course_name: str = Field(default="", alias="courseName")
    course_description: str = Field(default="", alias="courseDescription")
    department: str = ""

    model_config = {"populate_by_name": True}


class TextEncodeRequest(BaseModel):
    text: str = ""


@router.post("/analyze/video", dependencies=PROTECTED)
async def analyze_video_endpoint(request: VideoAnalysisRequest):
    """Process a classroom video and return behavior observations."""
    if request.video_path:
        safe = resolve_under_roots(request.video_path)
        if safe is None:
            raise HTTPException(status_code=400, detail="video_path is outside allowed roots")
        payload = request.model_dump()
        payload["video_path"] = str(safe)
    else:
        payload = request.model_dump()
    # Heavy OpenCV/YOLO work must not block the event loop.
    result = await run_in_threadpool(analyze_video, payload)
    return result


@router.post("/analyze/courseware", dependencies=PROTECTED)
async def analyze_courseware_endpoint(request: CoursewareAnalysisRequest):
    """Parse courseware file and extract text content."""
    if request.file_path:
        safe = resolve_under_roots(request.file_path)
        if safe is None:
            raise HTTPException(status_code=400, detail="file_path is outside allowed roots")
        payload = request.model_dump()
        payload["file_path"] = str(safe)
    else:
        payload = request.model_dump()
    result = await run_in_threadpool(parse_courseware, payload)
    return result


@router.post("/analyze/prep", dependencies=PROTECTED)
async def analyze_prep_endpoint(request: PrepAnalysisRequest):
    """DeepSeek courseware prep analysis (manual testing / direct call)."""
    result = await analyze_prep(request.model_dump(by_alias=True))
    return result


@router.post("/asr", dependencies=PROTECTED)
async def asr_endpoint(response: Response, file: UploadFile = File(...)):
    """
    Transcribe an uploaded audio file using faster-whisper.

    Returns HTTP 501 with {"status": "not_implemented"} when
    faster-whisper is not installed. Never silently returns fake text.
    """
    if not HAS_WHISPER:
        response.status_code = 501
        return {
            "status": "not_implemented",
            "route": "/asr",
            "message": "faster-whisper is not installed",
        }

    raw_name = file.filename or "audio.wav"
    safe_name = safe_basename(raw_name) or "audio.wav"
    suffix = os.path.splitext(safe_name)[1].lower() or ".wav"
    allowed_ext = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm"}
    if suffix not in allowed_ext:
        raise HTTPException(status_code=400, detail=f"Unsupported audio extension: {suffix}")

    tmp_path = None
    try:
        # Stream to disk with a hard size cap; never hold the whole file in RAM.
        size = 0
        # dir= keeps ASR uploads on the configured work dir instead of the OS
        # temp dir (system drive); a 2 GB audio upload must not land on C:.
        work_dir = media_work_dir()
        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix,
                                        dir=str(work_dir)) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ASR_BYTES:
                    raise HTTPException(status_code=413, detail="Audio file exceeds 200MB limit")
                tmp.write(chunk)

        result = await run_in_threadpool(
            transcribe_audio,
            tmp_path,
            settings.whisper_model_size,
            settings.whisper_device,
            settings.whisper_compute_type,
        )
        return result
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.warning("Failed to clean up temp audio file: %s", tmp_path)


# ---------------------------------------------------------------------------
# Explicitly NOT implemented routes (HTTP 501).
# These keep the Java AiServiceClient contract explicit: calling one of these
# returns a clear "not_implemented" error instead of a silent 404.
# ---------------------------------------------------------------------------


@router.post("/analyze_face", status_code=501)
async def analyze_face_endpoint(file: UploadFile = File(...)):
    """Face landmark analysis is NOT implemented on this service."""
    return {
        "status": "not_implemented",
        "route": "/analyze_face",
        "message": "MediaPipe face analysis is not implemented",
    }


@router.post("/diarize", status_code=501)
async def diarize_endpoint(file: UploadFile = File(...)):
    """Speaker diarization is NOT implemented on this service."""
    return {
        "status": "not_implemented",
        "route": "/diarize",
        "message": "pyannote speaker diarization is not implemented",
    }


@router.post("/encode_text", status_code=501)
async def encode_text_endpoint(request: TextEncodeRequest):
    """BERT text encoding is NOT implemented on this service."""
    return {
        "status": "not_implemented",
        "route": "/encode_text",
        "message": "BERT text encoding is not implemented",
    }


@router.get("/status")
async def service_status():
    """Check AI service health and loaded models."""
    return {
        "status": "ok",
        "service": settings.service_name,
        "demo_enabled": settings.demo_enabled,
        "capabilities": {
            "vision": {
                "status": "available" if _resolve_face_weights(settings) else "prototype",
                "engine": "YOLOv8 + OpenCV fallback" if _resolve_face_weights(settings) else "OpenCV Haar cascades",
            },
            "courseware": {"status": "available", "engine": "python-pptx + python-docx + PyPDF2"},
            "audio_transcription": {
                "status": "available" if HAS_WHISPER else "not_implemented",
                "engine": "faster-whisper" if HAS_WHISPER else "not_implemented",
            },
            "speaker_diarization": {"status": "not_implemented", "engine": "pyannote"},
            "face_landmarks": {"status": "not_implemented", "engine": "mediapipe"},
            "text_encoding": {"status": "not_implemented", "engine": "bert"},
            "grading": {"status": "not_implemented"},
        },
    }
