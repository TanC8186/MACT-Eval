#!/usr/bin/env python
"""Extract video + speech features from annotated classroom windows.

Input is ``window_manifest.csv`` produced by ``prepare_classroom_videos.py``
(with ``window_path`` and ``audio_path``).  For each 60 s window:

* YOLOv8 (SCB behavior weights) produces hand-raising / reading / writing
  counts, person count, detection coverage and positive-behavior ratio;
* faster-whisper produces a transcript and segments, from which speech
  coverage, question count, speech rate, pause ratio and ASR confidence are
  derived.

The output is a feature CSV ready for ``learn_fusion_weights.py``.  A failed
window is written to ``<out>_failures.csv`` with the error and blank feature
columns; it is never silently zero-filled.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from public_data import asr, features
else:
    from . import asr, features

BASE_COLUMNS = [
    "clip_id",
    "lesson_id",
    "subject",
    "grade",
    "start_sec",
    "end_sec",
    "label",
    # Emitted so the fusion engine's rule baseline does not have to guess the
    # window length. MCCD clips are 5 s and the public pool is 60 s; defaulting
    # both to 60 s would understate MCCD question density by 12x.
    "window_seconds",
]

VIDEO_COLUMNS = [
    "video_person_count",
    "video_person_detection_coverage",
    "video_person_mean_confidence",
    "video_hand_raising_count",
    "video_reading_count",
    "video_writing_count",
    "video_discussing_count",
    "video_bow_head_count",
    "video_turn_head_count",
    "video_behavior_total_detections",
    "video_behaviour_total",
    "video_behavior_detection_coverage",
    "video_positive_behavior_ratio",
    "video_sampled_frames",
]

SPEECH_COLUMNS = [
    "speech_coverage",
    "speech_question_count",
    "speech_rate",
    "speech_pause_ratio",
    "speech_char_count",
    "speech_asr_confidence",
    "speech_text",
]

OUTPUT_COLUMNS = BASE_COLUMNS + VIDEO_COLUMNS + SPEECH_COLUMNS


def _read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"clip_id", "lesson_id", "window_path", "audio_path", "start_sec", "end_sec"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(
                f"manifest missing columns: {sorted(required - set(reader.fieldnames or []))}"
            )
        return list(reader)


def _existing_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return {(row.get("clip_id") or "") for row in reader}


def _transcript_text(result: dict[str, Any]) -> str:
    text = result.get("text")
    return str(text).strip() if text is not None else ""


def extract_one(
    row: dict[str, str],
    *,
    behavior_detector: Any,
    person_detector: Any,
    whisper_model: Any,
    sample_fps: float,
    conf: float,
    language: str,
    beam_size: int,
) -> dict[str, Any]:
    window_path = Path(row["window_path"])
    audio_path = Path(row["audio_path"])
    duration = float(row["end_sec"]) - float(row["start_sec"])

    behavior = features.detection_features(
        behavior_detector, window_path, sample_fps=sample_fps, conf=conf
    )
    person = features.detection_features(
        person_detector, window_path, sample_fps=sample_fps, conf=conf
    )
    transcript = asr.transcribe_audio(
        audio_path,
        model=whisper_model,
        language=language,
        beam_size=beam_size,
    )
    speech = features.speech_features_from_segments(
        transcript.get("segments"), duration, _transcript_text(transcript)
    )

    output: dict[str, Any] = {
        "clip_id": row["clip_id"],
        "lesson_id": row["lesson_id"],
        "subject": row.get("subject", ""),
        "grade": row.get("grade", ""),
        "start_sec": row.get("start_sec", ""),
        "end_sec": row.get("end_sec", ""),
        "window_seconds": round(duration, 3),
        "label": row.get("label", ""),
        "video_person_count": person.get("person_count"),
        "video_person_detection_coverage": person.get("detection_coverage"),
        "video_person_mean_confidence": person.get("mean_confidence"),
        "video_hand_raising_count": behavior.get("hand_raising_count"),
        "video_reading_count": behavior.get("reading_count"),
        "video_writing_count": behavior.get("writing_count"),
        "video_discussing_count": behavior.get("discussing_count"),
        "video_bow_head_count": behavior.get("bow_head_count"),
        "video_turn_head_count": behavior.get("turn_head_count"),
        "video_behavior_total_detections": behavior.get("total_detections"),
        "video_behaviour_total": behavior.get("behaviour_total"),
        "video_behavior_detection_coverage": behavior.get("detection_coverage"),
        "video_positive_behavior_ratio": behavior.get("positive_behavior_ratio"),
        "video_sampled_frames": behavior.get("sampled_frames"),
        "speech_coverage": speech.get("speech_coverage"),
        "speech_question_count": speech.get("question_count"),
        "speech_rate": speech.get("speech_rate"),
        "speech_pause_ratio": speech.get("pause_ratio"),
        "speech_char_count": speech.get("char_count"),
        "speech_asr_confidence": transcript.get("avg_logprob"),
        "speech_text": _transcript_text(transcript),
    }
    return output


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_mode = "a" if append and path.is_file() else "w"
    write_header = not (append and path.is_file() and path.stat().st_size > 0)
    with path.open(file_mode, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--scb-weights", required=True, type=Path)
    parser.add_argument("--person-weights", type=Path, default=Path("models/yolov8n.pt"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--asr-model", default="small")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--sample-fps", type=float, default=1.0)
    parser.add_argument("--conf", type=float, default=0.15)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    if not args.manifest.is_file():
        parser.error(f"manifest not found: {args.manifest}")
    if not args.scb_weights.is_file():
        parser.error(f"SCB weights not found: {args.scb_weights}")
    if not args.person_weights.is_file():
        parser.error(f"person/COCO weights not found: {args.person_weights}")
    if args.sample_fps <= 0:
        parser.error("--sample-fps must be positive")
    if not 0 <= args.conf <= 1:
        parser.error("--conf must be within [0, 1]")

    rows = _read_manifest(args.manifest)
    if args.limit is not None:
        rows = rows[: args.limit]
    done = _existing_ids(args.out) if args.resume else set()

    try:
        from ultralytics import YOLO
    except ImportError as exc:
        print("error: ultralytics is required for detection features", file=sys.stderr)
        return 2
    detector = YOLO(str(args.scb_weights))
    person_detector = YOLO(str(args.person_weights))
    whisper_model = asr.load_model(
        args.asr_model, device=args.device, compute_type=args.compute_type
    )

    failure_path = args.out.with_name(args.out.stem + "_failures.csv")
    failures: list[dict[str, Any]] = []
    written = 0
    for index, row in enumerate(rows, 1):
        clip_id = row["clip_id"]
        if clip_id in done:
            continue
        try:
            output = extract_one(
                row,
                behavior_detector=detector,
                person_detector=person_detector,
                whisper_model=whisper_model,
                sample_fps=args.sample_fps,
                conf=args.conf,
                language=args.language,
                beam_size=args.beam_size,
            )
            _write_csv(args.out, [output], append=True)
            written += 1
            print(f"[{index}/{len(rows)}] {clip_id} ok", flush=True)
        except Exception as exc:  # noqa: BLE001 - batch job records and continues
            failure = {
                "clip_id": clip_id,
                "window_path": row.get("window_path", ""),
                "audio_path": row.get("audio_path", ""),
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
            }
            failures.append(failure)
            print(f"[{index}/{len(rows)}] {clip_id} FAILED: {exc}", file=sys.stderr, flush=True)

    if failures:
        with failure_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(failures[0].keys()))
            writer.writeheader()
            writer.writerows(failures)
    summary = {
        "requested": len(rows),
        "written": written,
        "failed": len(failures),
        "out": str(args.out),
        "failures": str(failure_path) if failures else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
