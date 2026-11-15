"""Feature extraction for the Pri-MCCD public-data experiments.

Implements the observable-feature contract from spec section 5 and
``docs/experiments/rule-baseline-protocol.md``:

* :func:`parse_pose_file` aggregates the official MTCNN per-frame head-pose
  JSON (``aggregated_pose.avg_pitch/avg_yaw/avg_roll``) into the
  ``posture_score`` / ``stability_score`` components;
* :func:`speech_features_from_segments` derives speech coverage, question
  count, speech rate and pause ratio from ASR segments;
* :func:`detection_features` samples a video and aggregates YOLO behaviour
  detections.

Heavy optional dependencies (OpenCV, ultralytics) are imported lazily so this
module stays importable by the unit tests without a GPU or a video stack.
Missing observations are reported as ``None`` / empty fields, never as a
fabricated zero score.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

#: ``posture_score = clip(1 - mean(|pitch|) / 45, 0, 1)`` (protocol section 6).
POSTURE_ANGLE_LIMIT_DEGREES = 45.0

#: ``stability_score = clip(1 - (std_pitch + std_yaw + std_roll) / 90, 0, 1)``.
STABILITY_ANGLE_LIMIT_DEGREES = 90.0

#: Interrogative markers shared with the Java ``SpeechMetricsCalculator`` and
#: the rule-baseline protocol (section 4.1).
QUESTION_MARKERS: tuple[str, ...] = (
    "？",
    "?",
    "吗",
    "呢",
    "吧",
    "谁",
    "什么",
    "怎么",
    "为什么",
    "如何",
    "几",
    "多少",
    "哪",
)

#: Suffixes used by the official Pri-MCCD pose release, e.g.
#: ``neg0001_head_pose_features_mtcnn_aggregated.json``.
POSE_FILE_SUFFIXES: tuple[str, ...] = (
    "_head_pose_features_mtcnn_aggregated",
    "_head_pose_features_mtcnn",
    "_head_pose_features",
    "_head_pose",
    "_pose",
)

#: Normalised YOLO class name -> behaviour feature column.
#:
#: The six classes below are the taxonomy of the merged SCB5 dataset
#: (``prepare_scb5.py``).  A three-class SCB-Dataset3 model simply never emits
#: the extra names, so the same mapping serves both weights.
_BEHAVIOUR_ALIASES: dict[str, str] = {
    "handraising": "hand_raising",
    "handraise": "hand_raising",
    "raisehand": "hand_raising",
    "raisinghand": "hand_raising",
    "handsup": "hand_raising",
    "举手": "hand_raising",
    "reading": "reading",
    "read": "reading",
    "阅读": "reading",
    "writing": "writing",
    "write": "writing",
    "书写": "writing",
    "写字": "writing",
    "discussing": "discussing",
    "discuss": "discussing",
    "discussion": "discussing",
    "讨论": "discussing",
    "bowhead": "bow_head",
    "headdown": "bow_head",
    "低头": "bow_head",
    "turnhead": "turn_head",
    "headturn": "turn_head",
    "转头": "turn_head",
}

#: Behaviours that count towards ``positive_behavior_ratio``.  ``discussing``
#: is deliberately excluded: it is on-task but not a positive-affect signal,
#: and counting it here would recreate the all-ones ratio the three-class model
#: produced.
_POSITIVE_BEHAVIOURS: frozenset[str] = frozenset({"hand_raising", "reading", "writing"})

_PERSON_ALIASES = frozenset(
    {"person", "persons", "people", "head", "heads", "human", "student", "pupil"}
)


def parse_pose_file(path: str | Path) -> dict[str, float | int] | None:
    """Aggregate one Pri-MCCD MTCNN pose JSON file.

    The official file is a JSON list of frame entries::

        [{"frame": 0, "aggregated_pose": {
            "avg_pitch": 3.2, "avg_yaw": -1.0, "avg_roll": 0.4}}, ...]

    Frames without a usable ``aggregated_pose`` (missing entry, missing axis,
    non-numeric or non-finite value) are skipped rather than treated as a
    0-degree pose; a frame where no face was detected must not inflate the
    posture score.  ``frame_count`` counts the frames that were actually used.

    Args:
        path: Pose JSON path.

    Returns:
        A dict with ``posture_score``, ``stability_score``, ``head_motion``,
        per-axis means/stds, ``mean_abs_pitch``, ``frame_count`` and
        ``skipped_frame_count``; or ``None`` when the file is missing, empty or
        contains no usable frame.

    Raises:
        ValueError: if the file is not valid JSON or not a list / object with a
            ``frames`` list.
    """
    pose_path = Path(path)
    if not pose_path.exists():
        return None
    if not pose_path.is_file():
        raise ValueError(f"Pose path is not a file: {pose_path}")

    raw = json.loads(pose_path.read_text(encoding="utf-8-sig"))
    frames = _pose_frames(raw, pose_path)

    pitches: list[float] = []
    yaws: list[float] = []
    rolls: list[float] = []
    skipped = 0
    for entry in frames:
        angles = _pose_angles(entry)
        if angles is None:
            skipped += 1
            continue
        pitch, yaw, roll = angles
        pitches.append(pitch)
        yaws.append(yaw)
        rolls.append(roll)

    if not pitches:
        return None

    mean_pitch = statistics.fmean(pitches)
    mean_yaw = statistics.fmean(yaws)
    mean_roll = statistics.fmean(rolls)
    mean_abs_pitch = statistics.fmean(abs(value) for value in pitches)

    std_pitch = statistics.pstdev(pitches)
    std_yaw = statistics.pstdev(yaws)
    std_roll = statistics.pstdev(rolls)
    head_motion = std_pitch + std_yaw + std_roll

    return {
        "frame_count": len(pitches),
        "skipped_frame_count": skipped,
        "mean_pitch": mean_pitch,
        "mean_yaw": mean_yaw,
        "mean_roll": mean_roll,
        "mean_abs_pitch": mean_abs_pitch,
        "std_pitch": std_pitch,
        "std_yaw": std_yaw,
        "std_roll": std_roll,
        "head_motion": head_motion,
        "posture_score": _clip01(
            1.0 - mean_abs_pitch / POSTURE_ANGLE_LIMIT_DEGREES
        ),
        "stability_score": _clip01(
            1.0 - head_motion / STABILITY_ANGLE_LIMIT_DEGREES
        ),
    }


def speech_features_from_segments(
    segments: Iterable[Mapping[str, Any]] | None,
    duration: float,
    text: str | None = None,
) -> dict[str, float | int]:
    """Derive speech features from faster-whisper segments.

    ``speech_coverage`` is the union of the segment intervals clipped to
    ``[0, duration]`` divided by ``duration`` (overlaps are counted once).
    ``pause_ratio`` is ``1 - speech_coverage``.  ``speech_rate`` is
    ``char_count / (speech_seconds / 60)`` and is ``0`` when no speech was
    detected.

    Question markers follow the protocol: a segment counts as one question if
    it contains at least one marker.  When the segments carry no text, the
    supplied full transcript is split into sentences and the same markers are
    applied.  ``char_count`` excludes whitespace and punctuation/symbols.

    Args:
        segments: Iterable of ``{"start", "end", "text"}`` mappings.  Empty or
            ``None`` is safe.
        duration: Clip/window duration in seconds; must be positive.
        text: Full transcript.  Used for character counting and as the question
            fallback when segment texts are unavailable.

    Returns:
        ``speech_coverage`` (also available under the short alias
        ``coverage``), ``question_count``, ``speech_rate``, ``pause_ratio``,
        ``char_count`` and ``speech_seconds``.

    Raises:
        ValueError: if ``duration`` is not a positive finite number.
    """
    duration_value = _as_float(duration)
    if duration_value is None or duration_value <= 0:
        raise ValueError(f"duration must be a positive number, got {duration!r}")

    intervals: list[tuple[float, float]] = []
    segment_texts: list[str] = []
    for segment in segments or ():
        if not isinstance(segment, Mapping):
            continue
        segment_texts.append(str(segment.get("text") or ""))

        start = _as_float(segment.get("start"))
        end = _as_float(segment.get("end"))
        if start is None or end is None:
            continue
        start = max(0.0, start)
        end = min(duration_value, end)
        if end > start:
            intervals.append((start, end))

    speech_seconds = _union_length(intervals)
    coverage = _clip01(speech_seconds / duration_value)
    pause_ratio = _clip01(1.0 - coverage)

    transcript = str(text) if text is not None and str(text).strip() else ""
    if not transcript:
        transcript = "".join(segment_texts)
    char_count = count_characters(transcript)

    if any(segment_text.strip() for segment_text in segment_texts):
        question_count = sum(
            1
            for segment_text in segment_texts
            if contains_question_marker(segment_text)
        )
    else:
        question_count = _count_question_sentences(transcript)

    speech_rate = (
        char_count / (speech_seconds / 60.0) if speech_seconds > 0 else 0.0
    )

    return {
        "speech_seconds": speech_seconds,
        "speech_coverage": coverage,
        "coverage": coverage,
        "question_count": question_count,
        "speech_rate": speech_rate,
        "pause_ratio": pause_ratio,
        "char_count": char_count,
    }


def contains_question_marker(text: str) -> bool:
    """Return whether ``text`` contains any protocol interrogative marker."""
    return any(marker in text for marker in QUESTION_MARKERS)


def count_characters(text: str) -> int:
    """Count spoken characters, ignoring whitespace and punctuation/symbols."""
    count = 0
    for char in text:
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category.startswith("P") or category.startswith("S"):
            continue
        count += 1
    return count


def detection_features(
    model: Any,
    video_path: str | Path,
    sample_fps: float = 1.0,
    conf: float = 0.15,
    iou_threshold: float = 0.5,
) -> dict[str, float | int | None]:
    """Aggregate YOLO detections over frames sampled from one video.

    Frames are sampled every ``round(video_fps / sample_fps)`` frames (at
    least every frame).  Counts are cumulative over the sampled frames, while
    ``detection_coverage`` and ``mean_confidence`` are per-sample averages.
    A sample with no detections is a valid observation: counts and
    ``positive_behavior_ratio`` are ``0`` and ``mean_confidence`` is ``None``.

    Args:
        model: An ultralytics ``YOLO`` instance or a weights path (loaded
            lazily through ``ultralytics``).  Tests may pass a duck-typed
            object exposing ``predict(frame, conf=..., verbose=False)``.
        video_path: Video file decoded with OpenCV (imported lazily).
        sample_fps: Sampling rate in frames per second; must be positive.
        conf: Minimum detection confidence.

    Returns:
        ``sampled_frames``, ``person_count``, the six per-class counts
        (``hand_raising_count``, ``reading_count``, ``writing_count``,
        ``discussing_count``, ``bow_head_count``, ``turn_head_count``),
        ``total_detections``, ``behaviour_total``, ``detection_coverage``,
        ``mean_confidence`` and ``positive_behavior_ratio``.

    Raises:
        FileNotFoundError: if the video does not exist.
        RuntimeError: if OpenCV / ultralytics is unavailable, the video cannot
            be opened, the FPS is unknown, or no frame could be decoded.
        ValueError: if ``sample_fps`` / ``conf`` is invalid.
    """
    video = Path(video_path)
    if not video.is_file():
        raise FileNotFoundError(f"Video file does not exist: {video}")

    sample_rate = _as_float(sample_fps)
    if sample_rate is None or sample_rate <= 0:
        raise ValueError(f"sample_fps must be positive, got {sample_fps!r}")
    confidence_threshold = _as_float(conf)
    if confidence_threshold is None or not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError(f"conf must be within [0, 1], got {conf!r}")

    model = _ensure_detection_model(model)
    predict = getattr(model, "predict", None)
    if not callable(predict):
        if callable(model):
            predict = model
        else:
            raise TypeError(
                "detection model must expose predict(frame, conf=..., verbose=False)"
            )

    cv2 = _import_cv2()
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"Cannot open video file: {video}")

    sampled_frames = 0
    frames_with_detections = 0
    counts = {
        "hand_raising": 0,
        "reading": 0,
        "writing": 0,
        "discussing": 0,
        "bow_head": 0,
        "turn_head": 0,
    }
    person_count = 0
    total_detections = 0
    confidence_sum = 0.0

    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            raise RuntimeError(
                f"Cannot determine a positive FPS for video: {video}"
            )
        stride = max(1, int(round(fps / sample_rate)))

        frame_index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame_index % stride != 0:
                frame_index += 1
                continue

            sampled_frames += 1
            frame_detections = 0
            behaviour_candidates: list[
                tuple[str, float, tuple[float, float, float, float]]
            ] = []
            for result in _as_results(predict(frame, conf=confidence_threshold, verbose=False)):
                names = getattr(result, "names", None) or getattr(model, "names", None) or {}
                boxes = getattr(result, "boxes", None)
                if boxes is None:
                    continue
                confidences = _as_list(getattr(boxes, "conf", ()))
                class_ids = _as_list(getattr(boxes, "cls", ()))
                xyxy = _as_list(getattr(boxes, "xyxy", ()))
                for index, (raw_class, raw_confidence) in enumerate(
                    zip(class_ids, confidences)
                ):
                    confidence = _as_float(raw_confidence)
                    if confidence is None or confidence < confidence_threshold:
                        continue
                    class_name = _class_name(names, raw_class)
                    behaviour = _behaviour_kind(class_name)
                    if behaviour is not None:
                        behaviour_candidates.append(
                            (behaviour, confidence, _as_box(xyxy, index))
                        )
                    elif _is_person(class_name):
                        person_count += 1
                        total_detections += 1
                        confidence_sum += confidence
                        frame_detections += 1

            for behaviour, confidence, _ in _dedupe_behaviour_boxes(
                behaviour_candidates, iou_threshold
            ):
                counts[behaviour] += 1
                total_detections += 1
                confidence_sum += confidence
                frame_detections += 1

            if frame_detections > 0:
                frames_with_detections += 1
            frame_index += 1
    finally:
        capture.release()

    if sampled_frames == 0:
        raise RuntimeError(f"No frames could be decoded from video: {video}")

    positive_behaviours = sum(counts[name] for name in _POSITIVE_BEHAVIOURS)
    behaviour_total = sum(counts.values())
    return {
        "sampled_frames": sampled_frames,
        "person_count": person_count,
        "hand_raising_count": counts["hand_raising"],
        "reading_count": counts["reading"],
        "writing_count": counts["writing"],
        "discussing_count": counts["discussing"],
        "bow_head_count": counts["bow_head"],
        "turn_head_count": counts["turn_head"],
        "total_detections": total_detections,
        "behaviour_total": behaviour_total,
        "detection_coverage": frames_with_detections / sampled_frames,
        "mean_confidence": (
            confidence_sum / total_detections if total_detections > 0 else None
        ),
        # Denominator stays ``total_detections`` to preserve the existing
        # semantics.  The behaviour pass uses behaviour-only weights, so this
        # equals ``behaviour_total`` in practice; the explicit denominator is
        # reported alongside so the ratio can be re-derived either way.
        "positive_behavior_ratio": (
            positive_behaviours / total_detections if total_detections > 0 else 0.0
        ),
    }


def _intersection_over_union(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """IoU of two ``(x1, y1, x2, y2)`` boxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = inter_w * inter_h
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


def _dedupe_behaviour_boxes(
    detections: list[tuple[str, float, tuple[float, float, float, float]]],
    iou_threshold: float,
) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """Keep the highest-confidence box when two behaviour boxes overlap.

    At a low confidence threshold the detector reports the same student twice
    under slightly different boxes.  Counting both inflates the behaviour
    counts and makes ``positive_behavior_ratio`` depend on the duplicate rate
    rather than on behaviour.  Greedy class-agnostic NMS is enough here because
    one student cannot be raising a hand and reading at the same instant.
    """
    kept: list[tuple[str, float, tuple[float, float, float, float]]] = []
    for candidate in sorted(detections, key=lambda item: item[1], reverse=True):
        _, _, box = candidate
        if any(
            _intersection_over_union(box, other[2]) > iou_threshold
            for other in kept
        ):
            continue
        kept.append(candidate)
    return kept


def _pose_frames(raw: Any, path: Path) -> list[Any]:
    if isinstance(raw, Mapping):
        frames = raw.get("frames")
        if not isinstance(frames, list):
            raise ValueError(
                f"Unsupported pose JSON in {path}: expected a list of frame "
                "entries or an object with a 'frames' list"
            )
        return frames
    if isinstance(raw, list):
        return raw
    raise ValueError(
        f"Unsupported pose JSON in {path}: expected a list of frame entries"
    )


def _pose_angles(entry: Any) -> tuple[float, float, float] | None:
    if not isinstance(entry, Mapping):
        return None
    aggregated = entry.get("aggregated_pose")
    if not isinstance(aggregated, Mapping):
        return None
    angles: list[float] = []
    for key in ("avg_pitch", "avg_yaw", "avg_roll"):
        value = _as_float(aggregated.get(key))
        if value is None:
            return None
        angles.append(value)
    return angles[0], angles[1], angles[2]


def _union_length(intervals: Iterable[tuple[float, float]]) -> float:
    ordered = sorted(intervals)
    if not ordered:
        return 0.0
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    total += current_end - current_start
    return total


def _count_question_sentences(text: str) -> int:
    if not text:
        return 0
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    return sum(
        1 for sentence in sentences if sentence.strip() and contains_question_marker(sentence)
    )


def _ensure_detection_model(model: Any) -> Any:
    if not isinstance(model, (str, Path)):
        return model
    try:
        from ultralytics import YOLO
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "ultralytics is required to load detection weights; "
            "install it with 'pip install ultralytics'"
        ) from exc
    return YOLO(str(model))


def _import_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "OpenCV is required for detection_features; "
            "install it with 'pip install opencv-python'"
        ) from exc
    return cv2


def _as_results(results: Any) -> list[Any]:
    if results is None:
        return []
    if isinstance(results, (list, tuple)):
        return list(results)
    return [results]


def _as_list(values: Any) -> list[Any]:
    if values is None:
        return []
    if hasattr(values, "tolist"):
        values = values.tolist()
    if isinstance(values, (str, bytes)):
        return []
    try:
        return list(values)
    except TypeError:
        return [values]


def _class_name(names: Any, class_id: Any) -> str:
    if isinstance(names, Mapping):
        for key in (class_id, str(class_id)):
            if key in names:
                return str(names[key])
        return str(class_id)
    if isinstance(names, (list, tuple)):
        try:
            index = int(class_id)
        except (TypeError, ValueError):
            return str(class_id)
        if 0 <= index < len(names):
            return str(names[index])
    return str(class_id)


def _normalise_class_name(name: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]", "", name.lower())


def _behaviour_kind(name: str) -> str | None:
    return _BEHAVIOUR_ALIASES.get(_normalise_class_name(name))


def _is_person(name: str) -> bool:
    normalised = _normalise_class_name(name)
    return normalised in _PERSON_ALIASES or "person" in normalised


def _as_box(
    xyxy: list[Any], index: int
) -> tuple[float, float, float, float]:
    """Return box ``index`` as a float tuple, or a zero box when unavailable."""
    if index >= len(xyxy):
        return (0.0, 0.0, 0.0, 0.0)
    raw = _as_list(xyxy[index])
    if len(raw) < 4:
        return (0.0, 0.0, 0.0, 0.0)
    values = [_as_float(value) or 0.0 for value in raw[:4]]
    return (values[0], values[1], values[2], values[3])


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = [
    "POSTURE_ANGLE_LIMIT_DEGREES",
    "POSE_FILE_SUFFIXES",
    "QUESTION_MARKERS",
    "STABILITY_ANGLE_LIMIT_DEGREES",
    "contains_question_marker",
    "count_characters",
    "detection_features",
    "parse_pose_file",
    "speech_features_from_segments",
]
