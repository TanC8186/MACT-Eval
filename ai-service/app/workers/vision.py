"""
Vision worker: classroom video/image analysis using OpenCV.
Reads job from Redis Stream, processes video frames, publishes results.
"""
from __future__ import annotations

import hashlib
import io
import logging
import json
import math
import os
from pathlib import Path
import cv2

from app.core.config import settings
from app.models.scene_classifier import classify_scene
from app.models.action_evidence import get_action_detector

logger = logging.getLogger(__name__)

# Lazy import to avoid issues if not installed
try:
    from app.models.detector import ClassroomDetector, YoloDetector
    HAS_DETECTOR = True
except ImportError:
    HAS_DETECTOR = False

# 权重解析基准目录（与 scripts/evaluate_face.py 的 ROOT/REPO_ROOT 路径解析保持一致）
_REPO_ROOT = Path(__file__).resolve().parents[3]      # 仓库根（Task 1 实测下载到仓库根 models/yolov8n.pt）

# Process-level YOLO cache: loading weights is expensive; concurrent video jobs
# must not each spawn a full model instance (GPU/CPU memory blow-up).
_yolo_cache: dict[tuple[str, int, int, str | None], "YoloDetector"] = {}
_yolo_lock = None
_weights_digest_cache: dict[tuple[str, int, int], str] = {}


def _file_sha256(path: str) -> str | None:
    """SHA-256 of the actual weight file.

    Replacing best.pt must change the reported model digest even when the file
    name stays the same.
    """
    if not path:
        return None
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = (path, stat.st_size, stat.st_mtime_ns)
    cached = _weights_digest_cache.get(key)
    if cached:
        return cached
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        _weights_digest_cache.clear()
        _weights_digest_cache[key] = value
        return value
    except OSError:
        return None


def _get_yolo_lock():
    global _yolo_lock
    if _yolo_lock is None:
        import threading
        _yolo_lock = threading.Lock()
    return _yolo_lock


def _resolve_yolo_device() -> str | None:
    """Map MACT_AI_DEVICE onto the device argument ultralytics expects.

    "auto"/"" keeps ultralytics' own selection. An explicit "cuda" that the
    machine cannot honour degrades to "cpu" instead of failing every frame
    (the detector swallows inference errors, so a silent fallback would
    just produce empty observations).
    """
    configured = (getattr(settings, "device", "") or "").strip().lower()
    if configured in ("", "auto"):
        return None
    if configured.startswith("cuda"):
        try:
            import torch
            if not torch.cuda.is_available():
                logger.warning("MACT_AI_DEVICE=%s but CUDA is unavailable; using CPU", configured)
                return "cpu"
        except Exception:
            logger.warning("CUDA availability check failed; using CPU", exc_info=True)
            return "cpu"
    return configured


def _get_yolo_detector(weights: str):
    """Return a shared YoloDetector for the given weights path and device."""
    if not weights:
        return None
    device = _resolve_yolo_device()
    try:
        stat = Path(weights).stat()
    except OSError:
        return None
    key = (weights, stat.st_size, stat.st_mtime_ns, device)
    with _get_yolo_lock():
        cached = _yolo_cache.get(key)
        if cached is not None and cached.is_available():
            return cached
        detector = YoloDetector(weights_path=weights, device=device)
        if detector.is_available():
            _yolo_cache.clear()
            _yolo_cache[key] = detector
            return detector
        return None


def _encode_evidence_jpeg(frame) -> bytes | None:
    """Compress a sampled frame for human verification; bounded by width/quality."""
    if frame is None:
        return None
    max_width = max(320, int(getattr(settings, "evidence_frame_width", 960) or 960))
    quality = max(30, min(90, int(getattr(settings, "evidence_frame_quality", 70) or 70)))
    height, width = frame.shape[:2]
    if width > max_width:
        scale = max_width / float(width)
        frame = cv2.resize(frame, (max_width, max(1, int(height * scale))))
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buffer.tobytes() if ok else None


def _collect_evidence_frame(candidates: dict, window_index: int, frame, observation,
                            action_detections: list[dict] | None = None,
                            model_context: dict | None = None) -> None:
    if not getattr(settings, "evidence_frames_enabled", True):
        return
    max_per_window = max(1, min(6, int(getattr(settings, "evidence_frame_max_per_window", 4) or 4)))
    bucket = candidates.setdefault(window_index, [])
    encoded = None
    priority = {"hand_raising": 7, "standing": 6, "leave_seat": 6,
                "using_phone": 6, "leaning_over_table": 6,
                "turn_head": 5, "bow_head": 5, "discussing": 4,
                "writing": 2, "reading": 1}

    def add(code: str, label: str, detections: list[dict], source: str) -> None:
        nonlocal encoded
        matching = [d for d in detections if d["code"] == code]
        rank = (priority.get(code, 0), max((d.get("confidence", 0) for d in matching), default=0))
        existing = next((item for item in bucket if item["code"] == code), None)
        if existing and (code == "overview" or rank <= existing.get("rank", (0, 0))):
            return
        replace = existing
        if not replace and len(bucket) >= max_per_window:
            actions = [item for item in bucket if item["code"] != "overview"]
            replace = min(actions, key=lambda item: item.get("rank", (0, 0))) if actions else None
            if replace is None or rank <= replace.get("rank", (0, 0)):
                return
        if encoded is None:
            encoded = _encode_evidence_jpeg(frame)
        if encoded:
            if replace:
                bucket.remove(replace)
            metadata = dict(model_context or {})
            metadata.update({"detectionSource": source,
                             "imageWidth": int(frame.shape[1]), "imageHeight": int(frame.shape[0]),
                             "detections": detections,
                             "primarySubjectId": max(matching, key=lambda d: d.get("confidence", 0))["subjectId"] if matching else None,
                             "identityScope": "frame_local"})
            bucket.append({
                "code": code,
                "label": label,
                "jpeg": encoded,
                "timestamp_ms": int(getattr(observation, "timestamp_ms", 0) or 0),
                "metadata": metadata, "rank": rank,
            })

    if not bucket:
        add("overview", "课堂全景", action_detections or [], "overview")
    if action_detections is not None:
        for detection in action_detections:
            add(detection["code"], detection["label"], action_detections, "behavior_model")



def _upload_evidence_frames(candidates: dict, lesson_id: str, job_id: str) -> list[dict]:
    """Upload evidence thumbnails to MinIO; failure never fails the analysis."""
    if not job_id or not candidates:
        return []
    try:
        from minio import Minio

        client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        bucket = getattr(settings, "evidence_frames_bucket", "analysis-artifacts")
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        lesson_key = str(lesson_id).replace("/", "_").replace("\\", "_")
        job_key = str(job_id).replace("/", "_").replace("\\", "_")
        uploaded = []
        for window_index in sorted(candidates):
            for item in candidates[window_index]:
                object_name = (
                    f"lessons/{lesson_key}/jobs/{job_key}/"
                    f"window-{window_index:04d}-{item['code']}.jpg"
                )
                payload = item["jpeg"]
                client.put_object(
                    bucket, object_name, io.BytesIO(payload), len(payload),
                    content_type="image/jpeg",
                )
                uploaded.append({
                    "window_index": window_index,
                    "timestamp_ms": item["timestamp_ms"],
                    "bucket": bucket,
                    "object_name": object_name,
                    "code": item["code"],
                    "label": item["label"],
                    "content_type": "image/jpeg",
                    "metadata": item.get("metadata", {}),
                })
        logger.info("Uploaded %s evidence frames for job %s", len(uploaded), job_id)
        return uploaded
    except Exception as e:  # noqa: BLE001 - evidence frames are optional
        logger.warning("Evidence frame upload skipped for job %s: %s", job_id, e)
        return []


def _build_scene_window_metadata(scene_frames: list[dict]) -> dict:
    """Summarize scene classifications and gate unreliable windows."""
    if not scene_frames:
        return {
            "dominant_scene": "unknown",
            "usable_frame_ratio": 0.0,
            "scene_distribution": {},
            "view_switch_count": 0,
            "data_quality": "unavailable",
        }

    counts: dict[str, int] = {}
    usable_count = 0
    for item in scene_frames:
        scene = str(item.get("scene_type", "unknown"))
        counts[scene] = counts.get(scene, 0) + 1
        if item.get("usable") is True:
            usable_count += 1

    total = len(scene_frames)
    dominant_scene = max(counts, key=counts.get)
    usable_ratio = usable_count / total
    switch_count = sum(
        1 for previous, current in zip(scene_frames, scene_frames[1:])
        if previous.get("scene_type", "unknown") != current.get("scene_type", "unknown")
    )
    quality = "usable" if usable_ratio >= 0.7 else "limited" if usable_ratio >= 0.4 else "unavailable"
    return {
        "dominant_scene": dominant_scene,
        "usable_frame_ratio": round(usable_ratio, 2),
        "scene_distribution": {key: round(value / total, 2) for key, value in counts.items()},
        "view_switch_count": switch_count,
        "data_quality": quality,
    }


def _should_reset_tracking(current_scene: str | None, previous_scene: str | None) -> bool:
    """Reset temporal detector state whenever the classified scene changes."""
    return previous_scene is not None and current_scene is not None and current_scene != previous_scene



def _build_window_scene_metadata(scene_frames: list[dict], window_duration: int = 60_000) -> list[dict]:
    """Group sampled scene metadata into fixed-duration windows."""
    if not scene_frames:
        return []
    groups: dict[int, list[dict]] = {}
    for item in scene_frames:
        index = max(0, int(item.get("timestamp_ms", 0)) // window_duration)
        groups.setdefault(index, []).append(item)
    result = []
    for index in sorted(groups):
        metadata = _build_scene_window_metadata(groups[index])
        result.append({"window_index": index, **metadata})
    return result


def _group_observations_by_window(observations: list, window_duration: int = 60) -> dict[int, list]:
    """Group observations by their original media timestamp, not list position."""
    groups: dict[int, list] = {}
    for observation in observations:
        timestamp_ms = int(getattr(observation, "timestamp_ms", 0))
        index = max(0, timestamp_ms // (window_duration * 1000))
        groups.setdefault(index, []).append(observation)
    return groups


def _resolve_face_weights(settings) -> str:
    """解析 YOLO 人脸权重路径：显式配置优先，其次自动探测（cwd 相对 + 仓库根绝对）。

    候选顺序: runs/detect/face/weights/best.pt -> models/yolov8n-face.pt -> models/yolov8n.pt
    返回空串表示无可用权重（调用方回退 Haar）。
    """
    explicit = getattr(settings, "face_weights", "") or ""
    if explicit:
        return explicit
    for candidate in (
        "runs/detect/face/weights/best.pt",
        "models/yolov8n-face.pt",
        "models/yolov8n.pt",
    ):
        for base in (Path("."), _REPO_ROOT):
            path = base / candidate
            if path.exists():
                return str(path.resolve())
    return ""


def _sampling_policy(total_frames: int, fps: float, sample_interval: int,
                     window_duration: int, min_observations: int,
                     base_max_samples: int = 300,
                     hard_max_samples: int = 2400) -> dict:
    """Choose a stride that is compatible with ``min_observations``.

    A cap chosen only from duration can still produce zero windows: e.g. a
    40-minute video with ``min_observations=12`` needs at most a 2x stride
    (4 s spacing), not the 3x stride implied by a 560-sample cap.  This policy
    first derives the maximum legal stride from the requested window size and
    then raises the sample cap far enough to honour it.  If the legal stride
    would exceed the hard cap (or the request is physically impossible), the
    caller receives ``min_observations_unreachable=True`` and must fail the job
    explicitly rather than returning an empty SUCCEEDED result.
    """
    fps = float(fps or 0) or 25.0
    sample_interval = max(1, int(sample_interval or 1))
    window_duration = max(1, int(window_duration or 60))
    min_observations = max(1, int(min_observations or 1))
    frame_count_unknown = not total_frames or int(total_frames) <= 1
    duration_s = (float(total_frames) / fps) if not frame_count_unknown else 0.0
    estimated_windows = max(1, int(math.ceil(duration_s / window_duration))) if duration_s else 0
    frame_skip = max(1, int(fps * sample_interval))
    expected_samples = (max(1, int(total_frames) // frame_skip)
                        if not frame_count_unknown else 0)

    feasible = sample_interval * min_observations <= window_duration
    max_stride_for_min = (max(1, window_duration // (sample_interval * min_observations))
                          if feasible else 0)
    # Two spare samples per window absorb rounding and boundary effects.
    target_samples = estimated_windows * (min_observations + 2)
    max_samples = min(hard_max_samples, max(base_max_samples, target_samples))

    stride_by_cap = (max(1, int(math.ceil(expected_samples / max_samples)))
                     if expected_samples > max_samples else 1)
    stride_mult = min(stride_by_cap, max_stride_for_min) if feasible else 1
    required_for_stride = (int(math.ceil(expected_samples / stride_mult))
                           if expected_samples and stride_mult else 0)
    max_samples = min(hard_max_samples, max(max_samples, required_for_stride))

    effective_interval = sample_interval * stride_mult
    estimated_per_window = (round(window_duration / effective_interval, 2)
                            if not frame_count_unknown and effective_interval else None)
    unreachable_by_config = not feasible
    unreachable_by_cap = (feasible and expected_samples > 0 and max_stride_for_min > 0
                          and int(math.ceil(expected_samples / max_stride_for_min))
                          > hard_max_samples)
    unreachable = (unreachable_by_config or unreachable_by_cap
                   or (estimated_per_window is not None
                       and estimated_per_window < min_observations))
    return {
        "base_max_samples": base_max_samples,
        "hard_max_samples": hard_max_samples,
        "max_samples": max_samples,
        "estimated_windows": estimated_windows,
        "target_samples_per_window": min_observations + 2,
        "stride_mult": stride_mult,
        "max_stride_for_min_observations": max_stride_for_min,
        "effective_sample_interval_sec": effective_interval,
        "estimated_samples_per_window": estimated_per_window,
        "sampling_limit_applied": (expected_samples > hard_max_samples),
        "frame_count_unknown": frame_count_unknown,
        "duration_seconds": round(duration_s, 3) if duration_s else None,
        "min_observations_unreachable": unreachable,
        "unreachable_reason": (
            "min_observations_requires_more_than_hard_sample_cap" if unreachable_by_cap
            else "min_observations_exceeds_window_capacity" if unreachable_by_config
            else "stride_cannot_meet_min_observations" if unreachable else None),
    }


def analyze_video(job_params: dict) -> dict:
    """
    Process a classroom video and return behavior observations.

    Synchronous CPU/IO-bound worker. Call via run_in_threadpool (REST)
    or asyncio.to_thread (Redis queue) — never call it on the event loop.

    Job params expected:
    {
        "video_path": str,        # Path to video file
        "lesson_session_id": str, # Lesson session ID
        "total_seats": int,       # Configured seat count
        "sample_interval": int,   # Frame sampling interval in seconds (default: 2)
        "window_duration": int,   # Aggregation window in seconds (default: 60)
        "min_observations": int,  # Minimum usable observations per window (default: 5)
    }
    """
    video_path = job_params.get("video_path")
    lesson_id = job_params.get("lesson_session_id", "unknown")
    total_seats = int(job_params.get("total_seats", 45))
    sample_interval = int(job_params.get("sample_interval", 2))
    window_duration = int(job_params.get("window_duration", 60))
    min_observations = max(1, int(job_params.get("min_observations", 5)))
    allow_demo = bool(job_params.get("allow_demo", False))
    scene_gate = bool(job_params.get("scene_gate", True))

    logger.info(f"Starting video analysis: lesson={lesson_id}, path={video_path}")

    if not video_path or not os.path.exists(video_path):
        if settings.demo_enabled and allow_demo:
            logger.warning(f"Video not found, using explicit demo mode for lesson {lesson_id}")
            return _generate_demo_observations(lesson_id, total_seats, window_duration)
        return {
            "status": "failed",
            "error_code": "VIDEO_NOT_FOUND",
            "error": "video_path does not exist",
            "lesson_session_id": lesson_id,
        }

    # Backend selection: YOLO preferred (configurable), automatic Haar fallback
    backend = getattr(settings, "vision_backend", "yolo")
    weights = _resolve_face_weights(settings)
    detector = None
    shared_yolo = False
    if backend == "yolo" and weights:
        detector = _get_yolo_detector(weights)
        shared_yolo = detector is not None
        if detector is None:
            logger.warning("YOLO backend unavailable, falling back to Haar")
    if detector is None:
        detector = ClassroomDetector()
    model_version = Path(weights).stem if shared_yolo else "haar"
    model_digest = _file_sha256(weights) if shared_yolo else "opencv-haar-builtin"
    # Explicit mode: a face-locator weight file is NOT the offline behaviour
    # detector used in E1. Record which proxy produced the observations.
    detection_mode = ("face_locator_proxy" if shared_yolo and detector.mode == "face"
                      else "person_locator_proxy" if shared_yolo else "haar_face_proxy")
    action_detector = (get_action_detector(settings, _REPO_ROOT, _resolve_yolo_device())
                       if getattr(settings, "evidence_frames_enabled", True) else None)
    action_context = action_detector.context if action_detector else None
    observations = []
    evidence_candidates: dict[int, list[dict]] = {}
    sampled_by_window: dict[int, int] = {}
    usable_by_window: dict[int, int] = {}
    last_timestamp_ms = 0
    all_frames = []
    all_scene_frames = []
    scene_by_frame: list[dict] = []
    previous_frame = None
    previous_scene_type = None

    cap = None
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return {
                "status": "failed",
                "error_code": "VIDEO_OPEN_FAILED",
                "error": "unable to open video file",
                "lesson_session_id": lesson_id,
            }
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            fps = 25  # Assume 25fps as fallback

        frame_skip = max(1, int(fps * sample_interval))  # Frames to skip between samples
        frame_idx = 0
        sampled_count = 0

        # Spread samples across the whole video. The cap scales with duration
        # so a long lesson cannot silently lose every window to min_observations.
        total_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        sampling = _sampling_policy(
            total_video_frames, fps, sample_interval, window_duration,
            min_observations)
        if sampling["min_observations_unreachable"]:
            return {
                "status": "failed",
                "error_code": "MIN_OBSERVATIONS_UNREACHABLE",
                "error": (
                    f"cannot collect {min_observations} samples per "
                    f"{window_duration}s window with sample_interval="
                    f"{sample_interval}s under the {sampling['hard_max_samples']} sample cap"),
                "lesson_session_id": lesson_id,
                "sampling": sampling,
            }
        max_samples = sampling["max_samples"]
        stride_mult = sampling["stride_mult"]
        effective_skip = max(1, frame_skip * stride_mult)
        expected_samples = (
            max(1, total_video_frames // frame_skip) if total_video_frames > 0 else 0
        )
        truncated = expected_samples > max_samples

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Sample at configured interval (with stride so long videos cover full duration)
            if frame_idx % effective_skip == 0 and sampled_count < max_samples:
                timestamp_ms = int((frame_idx / fps) * 1000)
                last_timestamp_ms = timestamp_ms
                window_index = max(0, timestamp_ms // (window_duration * 1000))
                sampled_by_window[window_index] = sampled_by_window.get(window_index, 0) + 1
                obs = detector.process_frame(frame, sampled_count, timestamp_ms)
                scene = classify_scene(
                    frame,
                    previous_frame=previous_frame,
                    face_count=obs.person_count,
                )
                if not scene_gate:
                    scene = type(scene)(scene_type=scene.scene_type, usable=True, confidence=scene.confidence, reason="scene gate disabled")
                scene_meta = {"timestamp_ms": timestamp_ms, "scene_type": scene.scene_type.value, "usable": scene.usable, "confidence": scene.confidence}
                if _should_reset_tracking(scene.scene_type.value, previous_scene_type):
                    detector.reset_tracking()
                previous_scene_type = scene.scene_type.value
                all_scene_frames.append(scene_meta)
                scene_by_frame.append(scene_meta)
                # 仅将已确认的学生视角帧送入行为窗口，避免讲台/课件污染评分。
                if scene.usable:
                    usable_by_window[window_index] = usable_by_window.get(window_index, 0) + 1
                    all_frames.append(obs)
                    actions = None
                    if action_detector is not None:
                        try:
                            actions = action_detector.detect(frame)
                        except Exception as exc:
                            logger.warning("Action evidence inference failed: %s", exc)
                            # Never replace failed action predictions with heuristic labels.
                            actions = []
                    _collect_evidence_frame(
                        evidence_candidates,
                        max(0, timestamp_ms // (window_duration * 1000)),
                        frame,
                        obs,
                        actions,
                        action_context,
                    )
                previous_frame = frame
                sampled_count += 1

            frame_idx += 1

            if sampled_count >= max_samples:
                break

        max_samples_hit = sampled_count >= max_samples
        if sampling["frame_count_unknown"] and max_samples_hit:
            # The cap is the only coverage bound we can prove when the
            # container does not expose its frame count.
            truncated = True
            sampling["sampling_limit_applied"] = True
        insufficient_windows = sorted(
            index for index in sampled_by_window
            if usable_by_window.get(index, 0) < min_observations)

        # Aggregate by each observation's original timestamp so filtered frames
        # cannot shift later windows into the wrong scene bucket.
        grouped_frames = _group_observations_by_window(all_frames, window_duration)
        for window_index in sorted(grouped_frames):
            window_obs = grouped_frames[window_index]
            if len(window_obs) >= min_observations:
                window = detector.aggregate_window(window_obs, total_seats)
                if window:
                    observations.append({
                        "window_index": window_index,
                        "window_start_ms": window.window_start_ms,
                        "window_end_ms": window.window_end_ms,
                        "visible_seat_count": window.visible_seat_count,
                        "total_configured_seats": window.total_configured_seats,
                        "coverage": window.coverage,
                        "data_quality": window.data_quality,
                        "in_seat_count": window.in_seat_count,
                        "leave_seat_count": window.leave_seat_count,
                        "hand_raised_count": window.hand_raised_count,
                        "standing_count": window.standing_count,
                        "upright_posture_count": window.upright_posture_count,
                        "upright_proportion": window.upright_proportion,
                    })

        status = "succeeded"
        error_code = None
        error = None
        duration_s = sampling.get("duration_seconds") or 0
        if not observations and sampled_count > 0 and (
                truncated or duration_s >= window_duration):
            status = "partial"
            error_code = "NO_WINDOWS"
            error = ("sampled frames produced no window with at least "
                     f"{min_observations} usable observations")
        logger.info(
            "Video analysis complete: %s windows, %s frames, status=%s",
            len(observations), len(all_frames), status)

    except Exception as e:
        logger.error(f"Video analysis failed: {e}", exc_info=True)
        return {"status": "failed", "error": str(e)}
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        # 共享 YOLO 实例保留；Haar 本地实例可释放
        if detector is not None and not shared_yolo:
            detector.close()

    job_id = str(job_params.get("job_id") or job_params.get("jobId") or "")
    # Stamp the effective (post-stride) sampling interval onto every evidence
    # frame: the gallery must not claim a fixed 10s cadence for a 20-minute
    # clip (4s effective) or a 90s one. Only known after _sampling_policy.
    effective_interval = round(sample_interval * stride_mult, 2)
    for window_items in evidence_candidates.values():
        for item in window_items:
            meta = item.get("metadata")
            if isinstance(meta, dict):
                meta["requestedSampleIntervalSec"] = sample_interval
                meta["effectiveSampleIntervalSec"] = effective_interval
    evidence_frames = _upload_evidence_frames(evidence_candidates, lesson_id, job_id)

    return {
        "status": status,
        "mode": "real",
        "error_code": error_code,
        "error": error,
        "lesson_session_id": lesson_id,
        "evidence_frames": evidence_frames,
        "frame_count": len(all_frames),
        "sampled_frame_count": sampled_count,
        "video_frame_count": total_video_frames,
        "truncated": truncated,
        "effective_sample_interval_sec": round(sample_interval * stride_mult, 2),
        "sampling": sampling,
        "window_sampling": {
            "sampled_by_window": {str(k): v for k, v in sorted(sampled_by_window.items())},
            "usable_by_window": {str(k): v for k, v in sorted(usable_by_window.items())},
            "insufficient_window_count": len(insufficient_windows),
            "insufficient_windows": insufficient_windows[:50],
            "min_observations": min_observations,
        },
        "window_duration_seconds": window_duration,
        "media_duration_ms": int(round((total_video_frames / fps) * 1000)) if total_video_frames else None,
        "coverage_start_ms": 0,
        "coverage_end_ms": last_timestamp_ms,
        "last_sample_timestamp_ms": last_timestamp_ms,
        "window_count": len(observations),
        "observations": observations,
        "scene_metadata": _build_scene_window_metadata(all_scene_frames),
        "window_scene_metadata": _build_window_scene_metadata(all_scene_frames, window_duration * 1000),
        "model_version": model_version,
        "model_digest": model_digest,
        "detector_backend": "yolo" if shared_yolo else "haar",
        "detection_mode": detection_mode,
        "action_evidence_model": action_context,
        "behavior_measurements_available": False,
        "unavailable_metrics": ["in_seat_count", "leave_seat_count", "standing_count",
                                "upright_posture_count", "upright_proportion", "hand_raised_count"],
        "measurement_note": "Person/face localization does not measure posture or seat occupancy; classroom action predictions are provided as reviewable evidence only.",
        "config": {
            "vision_backend": backend,
            "weights_path": weights,
            "sample_interval": sample_interval,
            "window_duration": window_duration,
            "min_observations": min_observations,
            "scene_gate": scene_gate,
            "device": getattr(settings, "device", "auto"),
            "action_evidence_model": action_context,
        },
    }


def _generate_demo_observations(lesson_id: str, total_seats: int, window_duration: int) -> dict:
    """
    Generate realistic demo observations when no video file is available.
    Simulates a typical 40-minute classroom session with 2-minute windows.
    """
    import random
    import math

    random.seed(hash(lesson_id) % (2**31))
    total_windows = 20  # 40 minutes / 2-minute windows
    observations = []

    base_visible = int(total_seats * 0.82)  # ~82% seats visible typically

    for w in range(total_windows):
        # Simulate natural variation: students get tired, then perk up
        time_factor = math.sin(w / total_windows * math.pi) * 0.1  # U-shaped curve

        visible = base_visible + random.randint(-3, 3)
        visible = max(5, min(total_seats, visible))

        upright_pct = 0.72 + time_factor + random.uniform(-0.08, 0.08)
        upright_pct = max(0.3, min(0.95, upright_pct))

        upright_count = int(visible * upright_pct)
        hand_raised = 0  # 与真实后端口径一致：当前未实现举手检测
        standing = random.randint(0, max(1, visible // 15))

        coverage = visible / total_seats
        quality = "usable" if coverage > 0.6 else "limited" if coverage > 0.4 else "unavailable"

        observations.append({
            "window_start_ms": w * window_duration * 1000,
            "window_end_ms": (w + 1) * window_duration * 1000,
            "visible_seat_count": visible,
            "total_configured_seats": total_seats,
            "coverage": round(coverage, 2),
            "data_quality": quality,
            "in_seat_count": visible - standing,
            "leave_seat_count": random.randint(0, 1),
            "hand_raised_count": hand_raised,
            "standing_count": standing,
            "upright_posture_count": upright_count,
            "upright_proportion": round(upright_pct, 2),
        })

    return {
        "status": "succeeded",
        "lesson_session_id": lesson_id,
        "mode": "demo",
        "frame_count": 600,
        "media_duration_ms": total_windows * window_duration * 1000,
        "coverage_start_ms": 0,
        "coverage_end_ms": total_windows * window_duration * 1000,
        "window_count": len(observations),
        "observations": observations,
        "evidence_frames": [],
        "model_version": "mact-eval-vision-demo-0.1.0",
        "model_digest": "demo-no-weights",
        "detector_backend": "demo",
        "detection_mode": "demo",
        "config": {"window_duration": window_duration},
    }
