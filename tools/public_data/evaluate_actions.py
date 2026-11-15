#!/usr/bin/env python
"""Evaluate frame-local action predictions against human annotation.

This is an **action-level, per-box** evaluation (precision / recall / F1 at a
stated confidence threshold), not a detection-mAP evaluation.  ``E1``
(``docs/experiments/e1-scb-yolo-results.md``) reports mAP for a YOLO
checkpoint on SCB-Dataset3; mAP averages over IoU-matched boxes and says
nothing about whether a *named action* is actually present on a given box.
The two numbers are not comparable and must not be placed in the same table.

Scope, enforced by the loader:

* each row of evidence is **one sampled frame**, not an action event;
* ``target-N`` is the box's position in that frame after sorting by
  ``(bbox.x, bbox.y)`` — the same index in two frames is unrelated;
* there is no track id, so nothing here can support "N students raised hands"
  or "the action lasted T seconds".

Inputs
------
``--annotations``
    A sheet in ``action-annotation/1.0`` format, see
    ``tools/public_data/action_labels.py``.

``--predictions``
    Evidence-frame payload JSON, either the ai-service ``evidence_frames``
    list or ``{"frames": [...]}``.  Each item needs ``window_index``,
    ``code``, ``metadata.detections`` and a lesson id; ``frame_id`` is used
    when present, otherwise it is derived with
    ``action_labels.evidence_frame_key``.  ``metadata.identityScope`` must be
    ``frame_local`` when present.

Labelling convention
--------------------
The annotation defines the label universe.  For an annotator-labelled
``(frame, target, action)`` triple with no annotation row, an **action the
sheet actually uses elsewhere** is treated as absent — once an annotator has
been judging that action on that box, silence about it is a claim that it is
not visible.  An action the sheet never mentions at all was not judged, so a
prediction of it is not scored as a false positive; those are counted in
``n_skipped_action_predictions``.  Rows with ``target_index = 0`` denote
instances the model never boxed: they are recall denominators only.  Model
detections on frames/targets the annotator never touched are excluded and
counted in ``n_unannotated_predictions``; they are never silently treated as
false positives.

Undefined metrics
-----------------
An action with no positive annotation has undefined recall and F1 and the run
exits non-zero.  Precision is never reported as 1.0 from zero positives.

Selection bias
--------------
The ai-service only keeps an evidence frame *because* the behaviour model
fired on it (``detectionSource = "behavior_model"``, one frame per code per
window, max four per window).  Measuring recall on such frames is circular:
a box can only be scored if the model already proposed something there, so
missed detections are invisible and recall comes out too high.  Only precision
is estimable from detector-selected frames.

A recall-valid sample therefore has to be drawn at fixed timestamps regardless
of the model, and marked ``detectionSource = "fixed_timestamp_sample"``.  The
report records the origin of every frame and refuses to claim recall is
meaningful when no independently-sampled frame is present.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from public_data import action_labels
else:
    from . import action_labels

DEFAULT_THRESHOLD = 0.5
DEFAULT_SEED = 20261028
DEFAULT_N_BOOT = 2000
DEFAULT_BINS = 10
#: ``settings.behavior_confidence`` default; predictions below this floor are
#: never produced, so a sweep point below it cannot change recall.
DEFAULT_DETECTOR_FLOOR = 0.4

SWEEP_THRESHOLDS: tuple[float, ...] = tuple(
    round(0.05 * step, 2) for step in range(1, 20)
)

_UNRECORDED = "unrecorded"


# --------------------------------------------------------------------------
# Prediction loading
# --------------------------------------------------------------------------


def load_prediction_frames(path: str | Path) -> list[dict[str, Any]]:
    """Read an evidence-frame payload into normalised frame dicts.

    Each frame keeps its ``detection_source`` because it decides whether recall
    is estimable at all: a ``behavior_model`` frame exists only because the
    model fired on it, so it cannot contain a missed detection.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"prediction payload not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        for key in ("frames", "evidence_frames", "rows"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            raise ValueError(
                f"{path}: JSON object must contain a 'frames' or "
                "'evidence_frames' list"
            )
    if not isinstance(payload, list):
        raise ValueError(f"{path}: prediction payload must be a list")

    frames: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: every frame must be an object")
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        lesson_id = str(
            item.get("lesson_id")
            or item.get("lessonId")
            or item.get("lesson_session_id")
            or metadata.get("lessonId")
            or ""
        ).strip()
        window_index = _as_int(item.get("window_index", item.get("windowIndex")))
        frame_code = str(item.get("code") or "").strip()
        frame_id = str(
            item.get("frame_id") or item.get("frameId") or item.get("id") or ""
        ).strip()
        if not frame_id:
            if not lesson_id or window_index is None or not frame_code:
                raise ValueError(
                    f"{path}: frame without frame_id needs lesson_id, "
                    f"window_index and code; got {item!r}"
                )
            frame_id = action_labels.evidence_frame_key(
                lesson_id, window_index, frame_code
            )

        scope = str(metadata.get("identityScope") or "").strip()
        if scope and scope != "frame_local":
            raise ValueError(
                f"{path}: frame {frame_id} declares identityScope={scope!r}; "
                "only frame_local predictions are interpretable here"
            )

        detections = metadata.get("detections")
        if detections is None:
            detections = item.get("detections")
        if detections is None:
            detections = []
        if not isinstance(detections, list):
            raise ValueError(f"{path}: frame {frame_id} detections must be a list")

        frames.append(
            {
                "frame_id": frame_id,
                "lesson_id": lesson_id,
                "window_index": window_index,
                "timestamp_ms": _as_int(
                    item.get("timestamp_ms", item.get("timestampMs"))
                ),
                "frame_code": frame_code,
                "model_version": metadata.get("modelVersion"),
                "model_digest": metadata.get("modelDigest"),
                "identity_scope": scope or "frame_local",
                "detection_source": (
                    str(metadata.get("detectionSource") or "").strip() or None
                ),
                "detections": detections,
            }
        )
    return frames


def load_predictions(path: str | Path) -> list[dict[str, Any]]:
    """Load evidence-frame payloads into flat prediction records.

    Returns one record per detection:
    ``{"frame_id", "lesson_id", "window_index", "timestamp_ms", "action",
    "target_index", "confidence", "model_version", "model_digest",
    "identity_scope", "frame_code", "detection_source"}``.
    """
    records: list[dict[str, Any]] = []
    for frame in load_prediction_frames(path):
        for detection in frame["detections"]:
            if not isinstance(detection, dict):
                continue
            action = action_labels.canonical_action(detection.get("code"))
            target = _target_index(
                detection.get("subjectId", detection.get("subject_id"))
            )
            confidence = _as_float(detection.get("confidence"))
            if not action or target is None or confidence is None:
                continue
            records.append(
                {
                    "frame_id": frame["frame_id"],
                    "lesson_id": frame["lesson_id"],
                    "window_index": frame["window_index"],
                    "timestamp_ms": frame["timestamp_ms"],
                    "action": action,
                    "target_index": target,
                    "confidence": confidence,
                    "model_version": frame["model_version"],
                    "model_digest": frame["model_digest"],
                    "identity_scope": frame["identity_scope"],
                    "frame_code": frame["frame_code"],
                    "detection_source": frame["detection_source"],
                }
            )
    records.sort(
        key=lambda r: (
            str(r["frame_id"]),
            str(r["action"]),
            int(r["target_index"]),
            -float(r["confidence"]),
        )
    )
    return records


def summarize_frame_sources(
    frames: Sequence[dict[str, Any]], annotated_frames: Iterable[str]
) -> dict[str, Any]:
    """Count annotated frames by how the frame came to exist.

    ``behavior_model`` frames are detector-selected, so recall estimated on
    them is circular.  ``fixed_timestamp_sample`` frames are drawn from the
    video independent of the model and are the only ones where a missed
    detection can be observed.
    """
    annotated = set(str(frame) for frame in annotated_frames)
    counts: dict[str, int] = {}
    n_annotated = 0
    for frame in frames:
        if frame["frame_id"] not in annotated:
            continue
        n_annotated += 1
        key = frame["detection_source"] or _UNRECORDED
        counts[key] = counts.get(key, 0) + 1
    return {
        "by_source": dict(sorted(counts.items())),
        "n_annotated_frames_in_payload": n_annotated,
        "recall_estimable": counts.get("fixed_timestamp_sample", 0) > 0,
    }


def _target_index(value: Any) -> int | None:
    """Parse ``target-3`` / ``3`` into 3."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text.startswith("target-"):
        text = text[len("target-"):]
    try:
        number = int(text)
    except ValueError:
        return None
    return number


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


# --------------------------------------------------------------------------
# Pair construction
# --------------------------------------------------------------------------


def select_annotator(
    rows: Sequence[dict[str, Any]], annotator: str | None
) -> tuple[list[dict[str, Any]], str]:
    """Pick the sheet to evaluate against.

    Two annotators are for kappa, not for averaging; an evaluation must use
    one annotator (usually the adjudicated sheet), so an ambiguous sheet is an
    error rather than a silent merge.
    """
    names = action_labels.annotators(rows)
    if annotator is not None:
        selected = [row for row in rows if row.get("annotator") == annotator]
        if not selected:
            raise ValueError(f"no rows for annotator {annotator!r}")
        return selected, annotator
    if not names:
        raise ValueError("annotation sheet has no annotator column values")
    if len(names) > 1:
        raise ValueError(
            "annotation sheet contains multiple annotators "
            f"{names!r}; pass --annotator to choose one, or evaluate an "
            "adjudicated single-annotator sheet"
        )
    return list(rows), names[0]


def build_pairs(
    annotations: Sequence[dict[str, Any]],
    predictions: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Join annotations and predictions into one labelled pair set.

    Each pair is one ``(frame_id, target_index, action)`` claim with
    ``present`` (annotation) and ``confidence`` (model, ``None`` = no
    detection above the detector floor).
    """
    annotated_targets: set[tuple[str, int]] = set()
    annotation_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    visibility_by_target: dict[tuple[str, int], str] = {}
    for row in annotations:
        frame = str(row.get("frame_id") or "")
        target = row.get("target_index")
        action = str(row.get("action_code") or "")
        present = row.get("present")
        if not frame or target is None or not action or present is None:
            continue
        key = (frame, int(target), action)
        if key in annotation_by_key:
            # validate() reports duplicates; the first row wins here so the
            # join stays deterministic.
            continue
        annotation_by_key[key] = row
        annotated_targets.add((frame, int(target)))
        visibility = str(row.get("visibility") or "")
        if target > 0 and visibility:
            visibility_by_target.setdefault((frame, int(target)), visibility)

    frame_lessons: dict[str, str] = {}
    prediction_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    unannotated = 0
    for record in predictions:
        frame = str(record["frame_id"])
        frame_lessons.setdefault(frame, str(record.get("lesson_id") or ""))
        target = int(record["target_index"])
        action = str(record["action"])
        if (frame, target) not in annotated_targets:
            unannotated += 1
            continue
        key = (frame, target, action)
        previous = prediction_by_key.get(key)
        if previous is None or float(record["confidence"]) > float(
            previous["confidence"]
        ):
            prediction_by_key[key] = record

    pairs: list[dict[str, Any]] = []
    for key in sorted(annotation_by_key, key=lambda k: (k[0], k[2], k[1])):
        frame, target, action = key
        row = annotation_by_key[key]
        detection = prediction_by_key.get(key)
        lesson_id = str(row.get("lesson_id") or "") or frame_lessons.get(frame, "")
        visibility = str(row.get("visibility") or "")
        if target > 0:
            visibility = visibility_by_target.get((frame, target), visibility)
        pairs.append(
            {
                "frame_id": frame,
                "lesson_id": lesson_id,
                "window_index": row.get("window_index"),
                "target_index": target,
                "action": action,
                "present": int(row["present"]),
                "visibility": visibility or _UNRECORDED,
                "confidence": (
                    float(detection["confidence"]) if detection is not None else None
                ),
                "detected": detection is not None,
                "model_version": (
                    detection.get("model_version") if detection is not None else None
                ),
                "model_digest": (
                    detection.get("model_digest") if detection is not None else None
                ),
            }
        )

    # Labelled-but-undetected targets: an action the annotator never used
    # anywhere in the sheet was probably not judged at all, so a prediction of
    # it must not be scored as a false positive.  The silence rule therefore
    # applies only to actions the sheet actually exercises.
    annotated_keys = set(annotation_by_key)
    judged_actions = {key[2] for key in annotated_keys}
    skipped_action_predictions = 0
    for frame, target in sorted(annotated_targets):
        for action in action_labels.ACTION_CODES:
            key = (frame, target, action)
            if key in annotated_keys:
                continue
            if key not in prediction_by_key:
                continue
            if action not in judged_actions:
                skipped_action_predictions += 1
                continue
            detection = prediction_by_key[key]
            lesson_id = frame_lessons.get(frame, "")
            pairs.append(
                {
                    "frame_id": frame,
                    "lesson_id": lesson_id,
                    "window_index": detection.get("window_index"),
                    "target_index": target,
                    "action": action,
                    "present": 0,
                    "visibility": visibility_by_target.get(
                        (frame, target), _UNRECORDED
                    ),
                    "confidence": float(detection["confidence"]),
                    "detected": True,
                    "model_version": detection.get("model_version"),
                    "model_digest": detection.get("model_digest"),
                }
            )

    pairs.sort(
        key=lambda p: (str(p["frame_id"]), str(p["action"]), int(p["target_index"]))
    )
    return {
        "pairs": pairs,
        "n_unannotated_predictions": unannotated,
        "n_skipped_action_predictions": skipped_action_predictions,
        "judged_actions": sorted(judged_actions),
        "n_annotated_targets": len(annotated_targets),
        "n_annotated_frames": len({frame for frame, _ in annotated_targets}),
        "annotated_frame_ids": sorted({frame for frame, _ in annotated_targets}),
    }


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def binary_metrics(
    pairs: Iterable[dict[str, Any]], threshold: float
) -> dict[str, Any]:
    """Precision/recall/F1 for one action at one confidence threshold.

    ``None`` means undefined, never a fabricated 1.0:

    * precision undefined when nothing was predicted;
    * recall undefined when there is no positive annotation;
    * F1 undefined when either component is.
    """
    tp = fp = fn = tn = 0
    for pair in pairs:
        predicted = pair["confidence"] is not None and float(pair["confidence"]) >= threshold
        actual = int(pair["present"]) == 1
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif actual:
            fn += 1
        else:
            tn += 1

    positives = tp + fn
    predicted_count = tp + fp
    precision = tp / predicted_count if predicted_count else None
    recall = tp / positives if positives else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    return {
        "threshold": float(threshold),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "n_positives": positives,
        "n_predicted": predicted_count,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "status": (
            "no_positives"
            if positives == 0
            else ("no_predictions" if predicted_count == 0 else "ok")
        ),
    }


def confidence_sweep(
    pairs: Sequence[dict[str, Any]],
    thresholds: Sequence[float] = SWEEP_THRESHOLDS,
    detector_floor: float | None = DEFAULT_DETECTOR_FLOOR,
) -> list[dict[str, Any]]:
    """Metrics at every candidate threshold, so the operating point is chosen.

    ``reachable`` is ``False`` below the detector's inference floor: no
    prediction can exist under ``behavior_confidence``, so those rows are
    reported for completeness but cannot reflect a deployable setting.
    """
    rows = []
    for threshold in sorted({float(t) for t in thresholds}):
        row = binary_metrics(pairs, threshold)
        row["reachable"] = (
            True if detector_floor is None else threshold >= float(detector_floor)
        )
        rows.append(row)
    return rows


def calibration(
    pairs: Sequence[dict[str, Any]], bins: int = DEFAULT_BINS
) -> dict[str, Any]:
    """Reliability bins and expected calibration error over detected pairs.

    Only pairs the model actually predicted carry a confidence; positives the
    model never proposed are counted separately because they cannot be placed
    in a bin.  Bins are ``[lo, hi)`` with the last bin closed at 1.0.
    """
    if bins <= 0:
        raise ValueError("bins must be positive")
    scored = [pair for pair in pairs if pair["confidence"] is not None]
    edges = [index / bins for index in range(bins + 1)]
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(bins)]
    for pair in scored:
        confidence = min(max(float(pair["confidence"]), 0.0), 1.0)
        index = min(int(confidence * bins), bins - 1)
        buckets[index].append(pair)

    rows = []
    ece = 0.0
    total = len(scored)
    for index, bucket in enumerate(buckets):
        lower, upper = edges[index], edges[index + 1]
        if bucket:
            mean_confidence = sum(float(p["confidence"]) for p in bucket) / len(bucket)
            observed = sum(int(p["present"]) for p in bucket) / len(bucket)
            weight = len(bucket) / total
            ece += weight * abs(observed - mean_confidence)
        else:
            mean_confidence = None
            observed = None
            weight = 0.0
        rows.append(
            {
                "bin_lower": round(lower, 6),
                "bin_upper": round(upper, 6),
                "n": len(bucket),
                "mean_confidence": mean_confidence,
                "observed_frequency": observed,
                "weight": weight,
            }
        )
    return {
        "n_scored": total,
        "n_unscored_positives": sum(
            1 for pair in pairs if pair["confidence"] is None and int(pair["present"]) == 1
        ),
        "n_bins": bins,
        "ece": ece if total else None,
        "bins": rows,
    }


def _stratified(
    pairs: Sequence[dict[str, Any]], key: str, threshold: float
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        label = str(pair.get(key) or _UNRECORDED)
        groups.setdefault(label, []).append(pair)
    return {
        label: binary_metrics(groups[label], threshold)
        for label in sorted(groups)
    }


def bootstrap_f1(
    pairs: Sequence[dict[str, Any]],
    threshold: float,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Percentile bootstrap CI for F1, resampling whole lessons.

    ``metrics.grouped_bootstrap_ci`` resamples groups too, but its metric set
    is fixed to the three-class climate task (accuracy / macro-F1 / ordinal
    MAE / Spearman) and cannot express binary F1 for one action, so the same
    group-resampling convention is implemented here rather than reusing it.

    Lessons, not pairs, are the resampling unit: frames from one lesson share
    a camera, lighting and class, so resampling pairs would understate the
    interval.
    """
    metrics = binary_metrics(pairs, threshold)
    point = metrics["f1"]
    lessons: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        lessons.setdefault(str(pair.get("lesson_id") or _UNRECORDED), []).append(pair)
    names = sorted(lessons)
    if not names or n_boot <= 0:
        return {
            "point": point,
            "mean": None,
            "ci_lower": None,
            "ci_upper": None,
            "n_lessons": len(names),
            "n_boot": n_boot,
            "note": "insufficient input",
        }

    # ``random.Random`` seeded with an int is reproducible across Python
    # releases.  A hand-rolled LCG was rejected here: its low bits cycle with
    # a period of 2, so with two lessons every replicate picked the same pair.
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(n_boot):
        picked: list[dict[str, Any]] = []
        for _ in range(len(names)):
            picked.extend(lessons[names[rng.randrange(len(names))]])
        value = binary_metrics(picked, threshold)["f1"]
        if value is not None:
            samples.append(value)

    if not samples:
        return {
            "point": point,
            "mean": None,
            "ci_lower": None,
            "ci_upper": None,
            "n_lessons": len(names),
            "n_boot": n_boot,
            "note": "every replicate was undefined",
        }
    samples.sort()
    return {
        "point": point,
        "mean": sum(samples) / len(samples),
        "ci_lower": _percentile(samples, 2.5),
        "ci_upper": _percentile(samples, 97.5),
        "n_lessons": len(names),
        "n_boot": n_boot,
        "n_replicates_defined": len(samples),
        "note": None,
    }


def _percentile(sorted_values: Sequence[float], percent: float) -> float:
    if not sorted_values:
        raise ValueError("empty sample")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * (percent / 100.0)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return float(
        sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight
    )


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def evaluate(
    annotations: Sequence[dict[str, Any]],
    predictions: Sequence[dict[str, Any]],
    *,
    frames: Sequence[dict[str, Any]] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    sweep_thresholds: Sequence[float] = SWEEP_THRESHOLDS,
    bins: int = DEFAULT_BINS,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    detector_floor: float | None = DEFAULT_DETECTOR_FLOOR,
    annotator: str | None = None,
) -> dict[str, Any]:
    """Run the full evaluation and return a JSON-serialisable summary.

    ``frames`` is the frame-level payload the predictions came from; when
    given, the report states which frames were detector-selected and whether
    recall is estimable.
    """
    selected, annotator_name = select_annotator(annotations, annotator)
    eligible = [
        row
        for row in selected
        if row.get("present") is not None
        and row.get("target_index") is not None
        and row.get("frame_id")
        and row.get("action_code")
    ]
    if not eligible:
        raise ValueError(
            "no evaluable annotation rows: every row is missing a parseable "
            "present, target_index, frame_id or action_code"
        )
    joined = build_pairs(selected, predictions)
    pairs = joined["pairs"]
    if not pairs:
        raise ValueError("no evaluable pairs: check frame_id join keys")

    per_action: dict[str, Any] = {}
    for action in action_labels.ACTION_CODES:
        action_pairs = [pair for pair in pairs if pair["action"] == action]
        if not action_pairs:
            per_action[action] = {
                "label_zh": action_labels.ACTION_LABELS_ZH[action],
                "status": "no_annotations",
                "metrics": None,
                "sweep": [],
                "calibration": None,
                "by_visibility": {},
                "by_lesson": {},
                "bootstrap_f1": None,
            }
            continue
        metrics = binary_metrics(action_pairs, threshold)
        per_action[action] = {
            "label_zh": action_labels.ACTION_LABELS_ZH[action],
            "status": metrics["status"],
            "metrics": metrics,
            "sweep": confidence_sweep(
                action_pairs, sweep_thresholds, detector_floor=detector_floor
            ),
            "calibration": calibration(action_pairs, bins),
            "by_visibility": _stratified(action_pairs, "visibility", threshold),
            "by_lesson": _stratified(action_pairs, "lesson_id", threshold),
            "bootstrap_f1": bootstrap_f1(action_pairs, threshold, n_boot, seed),
        }

    total_positives = sum(int(pair["present"]) for pair in pairs)
    total_predicted = sum(1 for pair in pairs if pair["confidence"] is not None)
    model_versions = sorted(
        {str(p.get("model_version")) for p in pairs if p.get("model_version")}
    )
    model_digests = sorted(
        {str(p.get("model_digest")) for p in pairs if p.get("model_digest")}
    )
    sources = (
        summarize_frame_sources(frames, joined["annotated_frame_ids"])
        if frames is not None
        else None
    )

    return {
        "schema": "action-eval/1.0",
        "annotation_schema": action_labels.SCHEMA_VERSION,
        "scope": {
            "unit": "frame_local_box",
            "identity": "target-N is a per-frame detection index, not a student id",
            "not_an_event_count": True,
            "not_a_detection_map": (
                "action-level precision/recall/F1, not IoU-mAP; not comparable "
                "to docs/experiments/e1-scb-yolo-results.md"
            ),
        },
        "annotator": annotator_name,
        "threshold": float(threshold),
        "detector_floor": detector_floor,
        "seed": seed,
        "n_boot": n_boot,
        "n_pairs": len(pairs),
        "n_positive_annotations": total_positives,
        "n_predicted": total_predicted,
        "n_unannotated_predictions": joined["n_unannotated_predictions"],
        "n_skipped_action_predictions": joined["n_skipped_action_predictions"],
        "judged_actions": joined["judged_actions"],
        "n_annotated_frames": joined["n_annotated_frames"],
        "n_annotated_targets": joined["n_annotated_targets"],
        "frame_sources": sources,
        "recall_estimable": None if sources is None else sources["recall_estimable"],
        "model_versions": model_versions,
        "model_digests": model_digests,
        "per_action": per_action,
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _number(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    return f"{float(value):.{digits}f}"


def _format_sources(sources: dict[str, Any] | None) -> str:
    if not sources:
        return "n/a (frame payload not supplied)"
    if not sources["by_source"]:
        return "none"
    return ", ".join(
        f"{name}={count}" for name, count in sources["by_source"].items()
    )


def _recall_note(estimable: Any) -> str:
    if estimable is None:
        return "n/a (frame payload not supplied)"
    if not estimable:
        return (
            "**no** — every annotated frame was detector-selected "
            "(`behavior_model`), so missed detections cannot appear and recall "
            "is not estimable from this sample; only precision is"
        )
    return "yes — the sample includes frames drawn independently of the model"


def render_markdown(summary: dict[str, Any], *, title: str | None = None) -> str:
    """Human-readable report; every undefined value prints ``n/a``."""
    lines: list[str] = []
    if title:
        lines += [f"# {title}", ""]
    lines += [
        "## Scope",
        "",
        f"- Unit of evaluation: {summary['scope']['unit']}",
        f"- Identity: {summary['scope']['identity']}",
        "- Action-event counts are **not** derivable from these rows.",
        f"- {summary['scope']['not_a_detection_map']}",
        "",
        "## Run",
        "",
        "| item | value |",
        "|---|---|",
        f"| annotator | {summary['annotator']} |",
        f"| confidence threshold | {_number(summary['threshold'], 2)} |",
        f"| detector floor | {_number(summary['detector_floor'], 2)} |",
        f"| seed | {summary['seed']} |",
        f"| bootstrap replicates | {summary['n_boot']} |",
        f"| evaluable pairs | {summary['n_pairs']} |",
        f"| positive annotations | {summary['n_positive_annotations']} |",
        f"| model detections on annotated boxes | {summary['n_predicted']} |",
        f"| unannotated predictions (excluded) | {summary['n_unannotated_predictions']} |",
        f"| predictions of never-judged actions (excluded) | {summary['n_skipped_action_predictions']} |",
        f"| actions the sheet exercises | {', '.join(summary['judged_actions']) or 'none'} |",
        f"| annotated frames | {summary['n_annotated_frames']} |",
        f"| annotated boxes | {summary['n_annotated_targets']} |",
        f"| model version | {', '.join(summary['model_versions']) or 'n/a'} |",
        f"| model digest | {', '.join(summary['model_digests']) or 'n/a'} |",
        f"| annotated frames by origin | "
        f"{_format_sources(summary.get('frame_sources'))} |",
        f"| recall estimable | {_recall_note(summary.get('recall_estimable'))} |",
        "",
        "## Per action",
        "",
        "| action | status | TP | FP | FN | TN | precision | recall | F1 | F1 95% CI | ECE |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for action, block in summary["per_action"].items():
        metrics = block.get("metrics")
        if not metrics:
            lines.append(
                f"| {action} ({block['label_zh']}) | {block['status']} | "
                "n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |"
            )
            continue
        ci = block.get("bootstrap_f1") or {}
        interval = (
            f"[{_number(ci.get('ci_lower'))}, {_number(ci.get('ci_upper'))}]"
            if ci.get("ci_lower") is not None
            else "n/a"
        )
        lines.append(
            "| {name} ({zh}) | {status} | {tp} | {fp} | {fn} | {tn} | "
            "{p} | {r} | {f1} | {ci} | {ece} |".format(
                name=action,
                zh=block["label_zh"],
                status=metrics["status"],
                tp=metrics["tp"],
                fp=metrics["fp"],
                fn=metrics["fn"],
                tn=metrics["tn"],
                p=_number(metrics["precision"]),
                r=_number(metrics["recall"]),
                f1=_number(metrics["f1"]),
                ci=interval,
                ece=_number(
                    (block.get("calibration") or {}).get("ece"), 3
                ),
            )
        )

    lines += [
        "",
        "ECE = expected calibration error over detected pairs; "
        "positives the model never proposed carry no confidence and are "
        "outside every bin.",
        "",
        "## Confidence sweep",
        "",
    ]
    for action, block in summary["per_action"].items():
        sweep = block.get("sweep") or []
        if not sweep:
            continue
        lines += [
            f"### {action} ({block['label_zh']})",
            "",
            "| threshold | reachable | TP | FP | FN | precision | recall | F1 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in sweep:
            lines.append(
                "| {t} | {reach} | {tp} | {fp} | {fn} | {p} | {r} | {f1} |".format(
                    t=_number(row["threshold"], 2),
                    reach="yes" if row["reachable"] else "no (below detector floor)",
                    tp=row["tp"],
                    fp=row["fp"],
                    fn=row["fn"],
                    p=_number(row["precision"]),
                    r=_number(row["recall"]),
                    f1=_number(row["f1"]),
                )
            )
        lines.append("")

    lines += ["## Calibration", ""]
    for action, block in summary["per_action"].items():
        cal = block.get("calibration")
        if not cal:
            continue
        lines += [
            f"### {action} ({block['label_zh']})",
            "",
            f"- scored detections: {cal['n_scored']}",
            f"- positives without any detection: {cal['n_unscored_positives']}",
            f"- ECE: {_number(cal['ece'], 4)}",
            "",
            "| bin | n | mean confidence | observed frequency |",
            "|---|---|---|---|",
        ]
        for bucket in cal["bins"]:
            lines.append(
                "| [{lo}, {hi}) | {n} | {conf} | {obs} |".format(
                    lo=_number(bucket["bin_lower"], 2),
                    hi=_number(bucket["bin_upper"], 2),
                    n=bucket["n"],
                    conf=_number(bucket["mean_confidence"], 4),
                    obs=_number(bucket["observed_frequency"], 4),
                )
            )
        lines.append("")

    lines += ["## By visibility", ""]
    for action, block in summary["per_action"].items():
        strata = block.get("by_visibility") or {}
        if not strata:
            continue
        lines += [
            f"### {action} ({block['label_zh']})",
            "",
            "| visibility | TP | FP | FN | precision | recall | F1 |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, metrics in strata.items():
            lines.append(
                "| {v} | {tp} | {fp} | {fn} | {p} | {r} | {f1} |".format(
                    v=name,
                    tp=metrics["tp"],
                    fp=metrics["fp"],
                    fn=metrics["fn"],
                    p=_number(metrics["precision"]),
                    r=_number(metrics["recall"]),
                    f1=_number(metrics["f1"]),
                )
            )
        lines.append("")

    lines += ["## By lesson", ""]
    for action, block in summary["per_action"].items():
        strata = block.get("by_lesson") or {}
        if not strata:
            continue
        lines += [
            f"### {action} ({block['label_zh']})",
            "",
            "| lesson | TP | FP | FN | precision | recall | F1 |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, metrics in strata.items():
            lines.append(
                "| {v} | {tp} | {fp} | {fn} | {p} | {r} | {f1} |".format(
                    v=name,
                    tp=metrics["tp"],
                    fp=metrics["fp"],
                    fn=metrics["fn"],
                    p=_number(metrics["precision"]),
                    r=_number(metrics["recall"]),
                    f1=_number(metrics["f1"]),
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--annotations", required=True, type=Path,
                        help="action-annotation/1.0 CSV or JSON sheet")
    parser.add_argument("--predictions", required=True, type=Path,
                        help="evidence-frame payload JSON with detections")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="write action-eval.json and action-eval.md here")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--bins", type=int, default=DEFAULT_BINS)
    parser.add_argument("--n-boot", type=int, default=DEFAULT_N_BOOT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--annotator", default=None,
                        help="evaluate this annotator's rows only")
    parser.add_argument("--detector-floor", type=float, default=DEFAULT_DETECTOR_FLOOR,
                        help="inference confidence floor; sweep rows below it "
                             "are unreachable (use -1 to disable)")
    parser.add_argument("--title", default=None,
                        help="heading for the markdown report")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be within [0, 1]")
    if args.bins <= 0:
        parser.error("--bins must be positive")
    if args.n_boot <= 0:
        parser.error("--n-boot must be positive")
    detector_floor = args.detector_floor if args.detector_floor >= 0 else None

    try:
        annotations = action_labels.load_annotations(args.annotations)
    except (OSError, ValueError) as exc:
        print(f"error: could not read annotations: {exc}", file=sys.stderr)
        return 2
    errors = action_labels.validate(annotations)
    if errors:
        print(
            f"error: {len(errors)} annotation validation error(s); "
            "nothing was evaluated",
            file=sys.stderr,
        )
        for error in errors[:20]:
            print(
                f"  row {error['row_index']} [{error['field']}] "
                f"{error['code']}: {error['message']}",
                file=sys.stderr,
            )
        if len(errors) > 20:
            print(f"  ... {len(errors) - 20} more", file=sys.stderr)
        return 2
    if not annotations:
        print("error: annotation sheet is empty", file=sys.stderr)
        return 2

    try:
        frames = load_prediction_frames(args.predictions)
        predictions = load_predictions(args.predictions)
    except (OSError, ValueError) as exc:
        print(f"error: could not read predictions: {exc}", file=sys.stderr)
        return 2

    try:
        summary = evaluate(
            annotations,
            predictions,
            frames=frames,
            threshold=args.threshold,
            bins=args.bins,
            n_boot=args.n_boot,
            seed=args.seed,
            detector_floor=detector_floor,
            annotator=args.annotator,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    markdown = render_markdown(summary, title=args.title)
    if args.out_dir is not None:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "action-eval.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.out_dir / "action-eval.md").write_text(markdown, encoding="utf-8")
    print(markdown, end="")

    if summary["n_positive_annotations"] == 0:
        print(
            "error: the sheet contains zero positive annotations, so recall "
            "and F1 are undefined for every action. Precision is NOT 1.0; it "
            "is undefined. Nothing was validated.",
            file=sys.stderr,
        )
        return 3

    undefined = sorted(
        action
        for action, block in summary["per_action"].items()
        if block["metrics"] and block["metrics"]["status"] == "no_positives"
    )
    if undefined:
        print(
            "warning: no positive annotation for "
            f"{undefined}; their recall/F1 are undefined, not 1.0",
            file=sys.stderr,
        )
    if summary["recall_estimable"] is False:
        print(
            "warning: every annotated frame came from the detector's own "
            "evidence selection, so recall is not estimable — annotate a "
            "fixed-timestamp sample (detectionSource=fixed_timestamp_sample) "
            "before quoting recall or F1",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
