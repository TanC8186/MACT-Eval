"""
ASR worker: audio transcription using faster-whisper.

Loads the model lazily so the service can boot even when the model
weights are not yet downloaded. Defaults to the "small" model with int8
quantization, which runs acceptably on CPU for classroom audio.

This module NEVER fabricates transcript text: if faster-whisper is not
installed, callers receive {"status": "not_implemented"}.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Windows DLL-load-order workaround. ctranslate2 bundles its own cuDNN loader
# (ctranslate2/cudnn64_9.dll). When that loader is the first one in the process
# and a YOLO inference later selects CUDA, loading cuDNN fails with
#     "Could not load symbol cudnnGetLibConfig. Error code 127"
# and the process is terminated by the loader -- no Python exception, so no
# try/except and no asyncio done-callback can see it. Importing torch first
# makes cudnn64_9.dll resolve to torch/lib/cudnn64_9.dll, which exports the
# symbol ctranslate2's copy expects.
if sys.platform == "win32":
    try:
        import torch  # noqa: F401  (imported for DLL load order only)
    except Exception:  # pragma: no cover - torch is optional for ASR
        logger.debug("torch preload for ctranslate2 DLL order failed", exc_info=True)

try:
    from faster_whisper import WhisperModel
    HAS_WHISPER = True
except ImportError:  # pragma: no cover - depends on environment
    WhisperModel = None  # type: ignore[assignment]
    HAS_WHISPER = False

_model: Any = None
_model_key: tuple[str, str, str] | None = None
_model_lock = threading.Lock()


def get_model(model_name: str, device: str, compute_type: str) -> Any:
    """Return a lazily-loaded, cached WhisperModel instance.

    Cache key includes device/compute_type so a config change reloads correctly.
    A lock prevents concurrent double-loading of large weights.
    """
    global _model, _model_key
    key = (model_name, device, compute_type)
    if _model is not None and _model_key == key:
        return _model
    with _model_lock:
        if _model is None or _model_key != key:
            _model = WhisperModel(model_name, device=device, compute_type=compute_type)
            _model_key = key
    return _model


def transcribe_audio(
    audio_path: str,
    model_name: str = "small",
    device: str = "cpu",
    compute_type: str = "int8",
) -> dict:
    """
    Transcribe an audio file and return structured segments.

    Expected response shape (shared with the Java AiServiceClient):

        {
            "status": "succeeded" | "failed" | "not_implemented",
            "text": str,
            "language": str,
            "segments": [{"start": float, "end": float, "text": str}, ...],
        }

    Returns {"status": "not_implemented"} when faster-whisper is
    unavailable. Never fabricates transcript text.
    """
    if not audio_path or not os.path.isfile(audio_path):
        return {
            "status": "failed",
            "error_code": "AUDIO_NOT_FOUND",
            "error": "audio_path does not exist",
        }
    if not HAS_WHISPER:
        return {"status": "not_implemented", "error": "faster-whisper is not installed"}

    try:
        model = get_model(model_name, device, compute_type)
        segments, info = model.transcribe(audio_path, vad_filter=True)

        text_parts: list[str] = []
        segment_list: list[dict] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if text:
                text_parts.append(text)
            segment_list.append({
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": text,
            })

        duration_ms = int(round(float(getattr(info, "duration", 0.0) or 0.0) * 1000))
        speech_seconds = sum(max(0.0, float(s["end"]) - float(s["start"])) for s in segment_list)
        silence_ratio = (
            1.0 - min(1.0, speech_seconds / (duration_ms / 1000.0))
            if duration_ms > 0 else None
        )
        return {
            "status": "succeeded",
            "text": " ".join(text_parts),
            "language": info.language,
            "language_probability": round(float(info.language_probability), 4),
            "segments": segment_list,
            "model_version": f"faster-whisper-{model_name}",
            # Full media duration is required to distinguish trailing silence
            # from audio that simply was not provided.
            "media_duration_ms": duration_ms or None,
            "coverage_start_ms": 0 if duration_ms else None,
            "coverage_end_ms": duration_ms or None,
            "silence_ratio": round(silence_ratio, 4) if silence_ratio is not None else None,
            "config": {
                "model": model_name,
                "device": device,
                "compute_type": compute_type,
                "vad_filter": True,
            },
        }
    except Exception as e:  # pragma: no cover - depends on model/audio state
        logger.error("ASR transcription failed: %s", e, exc_info=True)
        return {"status": "failed", "error": str(e)}
