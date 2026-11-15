"""Extract Pri-MCCD pose, detection and ASR features into one row per clip.

The CLI walks a manifest produced by :mod:`public_data.manifest` and writes
either a CSV or JSONL feature table.  Feature groups are optional and fail
independently:

* pose features need ``--pose-root``;
* detection features need ``--weights`` (skipped with a warning otherwise);
* ASR features need ``--asr-model`` (skipped with a warning otherwise).

A failure in one group is written to ``<out>_failures.csv`` and leaves that
group's columns empty for the clip; failures are never converted into zero
scores.  ``--resume`` skips clips already present in ``--out``.

Examples::

    python tools/public_data/extract_features.py \
        --manifest data/processed/pri_mccd/manifest.csv \
        --pose-root data/raw/public/pri_mccd/pose \
        --weights models/scb_yolov8n.pt \
        --asr-model small \
        --out data/processed/pri_mccd/features.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__:
    from .features import (
        POSE_FILE_SUFFIXES,
        detection_features,
        parse_pose_file,
        speech_features_from_segments,
    )
else:  # pragma: no cover - direct ``python extract_features.py`` execution
    from features import (  # type: ignore[no-redef]
        POSE_FILE_SUFFIXES,
        detection_features,
        parse_pose_file,
        speech_features_from_segments,
    )

BASE_COLUMNS: tuple[str, ...] = (
    "clip_id",
    "label",
    "label_name",
    "split",
    "video_path",
    "audio_path",
    "lesson_id",
    "clip_order",
)

POSE_COLUMNS: tuple[str, ...] = (
    "pose_available",
    "frame_count",
    "skipped_frame_count",
    "posture_score",
    "stability_score",
    "head_motion",
    "mean_pitch",
    "mean_yaw",
    "mean_roll",
    "mean_abs_pitch",
    "std_pitch",
    "std_yaw",
    "std_roll",
)

DETECTION_COLUMNS: tuple[str, ...] = (
    "detection_available",
    "sampled_frames",
    "person_count",
    "hand_raising_count",
    "reading_count",
    "writing_count",
    "total_detections",
    "detection_coverage",
    "mean_confidence",
    "positive_behavior_ratio",
)

ASR_COLUMNS: tuple[str, ...] = (
    "asr_available",
    "asr_model",
    "asr_language",
    "asr_confidence",
    "speech_seconds",
    "speech_coverage",
    "question_count",
    "speech_rate",
    "pause_ratio",
    "char_count",
)

OUTPUT_COLUMNS: tuple[str, ...] = (
    BASE_COLUMNS + POSE_COLUMNS + DETECTION_COLUMNS + ASR_COLUMNS
)

FAILURE_COLUMNS: tuple[str, ...] = (
    "clip_id",
    "stage",
    "error",
    "video_path",
    "audio_path",
)

_JSONL_SUFFIXES = {".jsonl", ".ndjson"}


class _CliError(RuntimeError):
    """Fatal configuration error that should stop the CLI with exit code 2."""


@dataclass
class _Context:
    manifest_dir: Path
    pose_root: Path | None
    pose_index: dict[str, Path]
    detection_model: Any = None
    sample_fps: float = 1.0
    asr_model: Any = None
    asr_model_name: str | None = None
    pose_missing: int = 0
    pose_empty: int = 0
    audio_missing: int = 0
    failures: int = 0


def main(argv: Iterable[str] | None = None) -> int:
    """Run the feature-extraction CLI and return a process exit code."""
    args = _parse_args(argv)
    try:
        return _run(args)
    except _CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    pose_root = Path(args.pose_root) if args.pose_root is not None else None
    out_path = Path(args.out)

    if not manifest_path.is_file():
        raise _CliError(f"manifest does not exist: {manifest_path}")
    if pose_root is not None and not pose_root.is_dir():
        raise _CliError(f"pose root is not a directory: {pose_root}")
    if out_path.suffix.lower() not in {".csv", *_JSONL_SUFFIXES}:
        raise _CliError(
            f"--out must end with .csv, .jsonl or .ndjson, got {out_path.suffix!r}"
        )

    manifest_rows = _read_manifest(manifest_path)
    if pose_root is None:
        print(
            "warning: --pose-root not provided; pose features are skipped",
            file=sys.stderr,
        )
        pose_index: dict[str, Path] = {}
    else:
        pose_index = _build_pose_index(pose_root)

    detection_model = None
    if args.weights is None:
        print(
            "warning: --weights not provided; detection features are skipped",
            file=sys.stderr,
        )
    else:
        weights_path = Path(args.weights)
        if not weights_path.is_file():
            raise _CliError(f"detection weights do not exist: {weights_path}")
        detection_model = _load_detection_model(weights_path)

    asr_model = None
    if args.asr_model is None:
        print(
            "warning: --asr-model not provided; ASR features are skipped",
            file=sys.stderr,
        )
    else:
        asr_model = _load_asr_model(args.asr_model)

    context = _Context(
        manifest_dir=manifest_path.parent,
        pose_root=pose_root,
        pose_index=pose_index,
        detection_model=detection_model,
        sample_fps=args.sample_fps,
        asr_model=asr_model,
        asr_model_name=args.asr_model,
    )

    already_done = _existing_clip_ids(out_path) if args.resume else set()
    failure_path = out_path.with_name(f"{out_path.stem}_failures.csv")

    written = 0
    skipped = 0
    attempted = 0
    with _FeatureWriter(out_path, resume=args.resume) as writer, _FailureWriter(
        failure_path, resume=args.resume
    ) as failure_writer:
        for manifest_row in manifest_rows:
            clip_id = manifest_row["clip_id"]
            if clip_id in already_done:
                skipped += 1
                continue
            if args.limit is not None and attempted >= args.limit:
                break
            attempted += 1

            stage = ["pose"]
            result, clip_failures = _process_row(manifest_row, context, stage)
            writer.write(result)
            written += 1
            for failure in clip_failures:
                failure_writer.write(failure)
            context.failures += len(clip_failures)

    print(f"features written: {written} -> {out_path}")
    print(f"resume skipped: {skipped}")
    print(
        "pose unavailable: "
        f"{context.pose_missing} missing file(s), "
        f"{context.pose_empty} file(s) without usable frames"
    )
    print(f"audio unavailable: {context.audio_missing} clip(s)")
    if context.failures:
        print(
            f"failures: {context.failures} row(s) -> {failure_path}",
            file=sys.stderr,
        )
        return 1
    return 0


def _process_row(
    manifest_row: Mapping[str, Any],
    context: _Context,
    stage: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Extract all available feature groups for one clip.

    Each group is isolated: a failure is recorded and the remaining groups are
    still attempted.  The returned row therefore never hides a failure behind a
    fabricated zero.
    """
    clip_id = manifest_row["clip_id"]
    video_value = manifest_row.get("video_path")
    audio_value = manifest_row.get("audio_path")

    result: dict[str, Any] = {
        column: _csv_text(manifest_row.get(column)) for column in BASE_COLUMNS
    }
    result["clip_id"] = clip_id

    failures: list[dict[str, Any]] = []

    stage[0] = "pose"
    try:
        pose_path = _find_pose_file(
            context.pose_root,
            context.pose_index,
            clip_id,
            _path_stem(video_value),
        )
        if pose_path is None:
            context.pose_missing += 1
        else:
            pose = parse_pose_file(pose_path)
            if pose is None:
                context.pose_empty += 1
            else:
                result["pose_available"] = True
                result.update(pose)
    except Exception as exc:  # noqa: BLE001 - batch job records and continues
        failures.append(_failure(clip_id, stage[0], exc, video_value, audio_value))

    stage[0] = "detection"
    if context.detection_model is not None:
        try:
            video_path = _resolve_manifest_path(video_value, context.manifest_dir)
            if video_path is None:
                raise FileNotFoundError("manifest row has no video_path")
            detection = detection_features(
                context.detection_model,
                video_path,
                sample_fps=context.sample_fps,
            )
            result["detection_available"] = True
            result.update(detection)
        except Exception as exc:  # noqa: BLE001 - batch job records and continues
            failures.append(
                _failure(clip_id, stage[0], exc, video_value, audio_value)
            )

    stage[0] = "asr"
    if context.asr_model is not None:
        try:
            audio_path = _resolve_manifest_path(audio_value, context.manifest_dir)
            if audio_path is None or not audio_path.is_file():
                context.audio_missing += 1
            else:
                speech, language, confidence = _transcribe_audio(
                    context.asr_model, audio_path
                )
                result["asr_available"] = True
                result["asr_model"] = context.asr_model_name
                result["asr_language"] = language or ""
                result["asr_confidence"] = confidence
                result.update(speech)
        except Exception as exc:  # noqa: BLE001 - batch job records and continues
            failures.append(
                _failure(clip_id, stage[0], exc, video_value, audio_value)
            )

    return result, failures


def _failure(
    clip_id: str,
    stage: str,
    exc: BaseException,
    video_value: Any,
    audio_value: Any,
) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "stage": stage,
        "error": f"{type(exc).__name__}: {exc}",
        "video_path": _csv_text(video_value),
        "audio_path": _csv_text(audio_value),
    }


def _read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise _CliError(f"manifest has no header: {path}")
        if "clip_id" not in reader.fieldnames:
            raise _CliError(
                f"manifest must contain a 'clip_id' column; found {reader.fieldnames}"
            )

        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            clip_id = _csv_text(row.get("clip_id"))
            if not clip_id:
                raise _CliError(f"manifest row {line_number} has an empty clip_id")
            normalized = {
                key: _csv_text(value)
                for key, value in row.items()
                if key is not None
            }
            normalized["clip_id"] = clip_id
            rows.append(normalized)

    if not rows:
        raise _CliError(f"manifest has no data rows: {path}")
    return rows


def _build_pose_index(pose_root: Path) -> dict[str, Path]:
    """Index pose files by clip id, tolerating the official long suffix."""
    index: dict[str, Path] = {}
    for path in sorted(pose_root.rglob("*.json"), key=lambda item: str(item).lower()):
        stem = path.stem.lower()
        index.setdefault(stem, path)
        for suffix in POSE_FILE_SUFFIXES:
            if stem.endswith(suffix):
                index.setdefault(stem[: -len(suffix)], path)
                break
    return index


def _find_pose_file(
    pose_root: Path | None,
    pose_index: Mapping[str, Path],
    *keys: str | None,
) -> Path | None:
    if pose_root is None:
        return None
    for key in keys:
        if not key:
            continue
        normalized = key.lower()
        for suffix in ("", *POSE_FILE_SUFFIXES):
            candidate = pose_root / f"{key}{suffix}.json"
            if candidate.is_file():
                return candidate
        indexed = pose_index.get(normalized)
        if indexed is not None:
            return indexed
    return None


def _path_stem(value: Any) -> str | None:
    text = _csv_text(value)
    return Path(text).stem if text else None


def _resolve_manifest_path(value: Any, base_dir: Path) -> Path | None:
    text = _csv_text(value)
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    for candidate in (base_dir / path, Path.cwd() / path):
        if candidate.exists():
            return candidate
    return base_dir / path


def _csv_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def _load_detection_model(weights: Path) -> Any:
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise _CliError(
            "ultralytics is required for --weights; "
            "install it with 'pip install ultralytics'"
        ) from exc
    return YOLO(str(weights))


def _load_asr_model(model_name: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise _CliError(
            "faster-whisper is required for --asr-model; "
            "install it with 'pip install faster-whisper'"
        ) from exc
    return WhisperModel(model_name, device="auto", compute_type="default")


def _transcribe_audio(
    model: Any, audio_path: Path
) -> tuple[dict[str, float | int], str | None, float | None]:
    segment_iter, info = model.transcribe(
        str(audio_path), beam_size=5, vad_filter=False
    )

    segments: list[dict[str, Any]] = []
    logprobs: list[float] = []
    for segment in segment_iter:
        start = _finite_float(getattr(segment, "start", None))
        end = _finite_float(getattr(segment, "end", None))
        if start is None or end is None:
            continue
        segments.append(
            {
                "start": start,
                "end": end,
                "text": str(getattr(segment, "text", "") or ""),
            }
        )
        logprob = _finite_float(getattr(segment, "avg_logprob", None))
        if logprob is not None:
            logprobs.append(logprob)

    text = "".join(segment["text"] for segment in segments).strip()
    duration = _audio_duration(audio_path)
    if duration is None:
        duration = _finite_float(getattr(info, "duration", None))
    if duration is None or duration <= 0:
        raise ValueError(f"Cannot determine a positive duration for {audio_path}")

    features = speech_features_from_segments(segments, duration, text)
    language = getattr(info, "language", None)
    confidence = sum(logprobs) / len(logprobs) if logprobs else None
    return features, language, confidence


def _audio_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            frame_count = handle.getnframes()
            frame_rate = handle.getframerate()
    except (OSError, EOFError, wave.Error):
        return None
    if frame_rate <= 0 or frame_count <= 0:
        return None
    return frame_count / float(frame_rate)


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _existing_clip_ids(path: Path) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()

    if path.suffix.lower() in _JSONL_SUFFIXES:
        clip_ids: set[str] = set()
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSONL in {path} at line {line_number}: {exc}"
                    ) from exc
                clip_id = record.get("clip_id") if isinstance(record, Mapping) else None
                if clip_id is None:
                    raise ValueError(
                        f"JSONL row {line_number} in {path} has no clip_id"
                    )
                clip_ids.add(str(clip_id))
        return clip_ids

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return set()
        if "clip_id" not in reader.fieldnames:
            raise ValueError(
                f"Existing output {path} has no 'clip_id' column; "
                "use a new --out or remove --resume"
            )
        return {
            str(row["clip_id"])
            for row in reader
            if row.get("clip_id")
        }


class _FeatureWriter:
    """Append-safe CSV/JSONL writer for the feature table."""

    def __init__(self, path: Path, *, resume: bool) -> None:
        self.path = path
        self._is_jsonl = path.suffix.lower() in _JSONL_SUFFIXES
        path.parent.mkdir(parents=True, exist_ok=True)

        if self._is_jsonl:
            mode = "a" if resume and path.exists() else "w"
            self._handle = path.open(mode, encoding="utf-8", newline="")
            self._writer: csv.DictWriter | None = None
            return

        if path.exists() and path.stat().st_size > 0 and resume:
            self._validate_header()
            mode = "a"
        else:
            mode = "w"

        self._handle = path.open(mode, encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._handle, fieldnames=OUTPUT_COLUMNS)
        if mode == "w":
            self._writer.writeheader()

    def _validate_header(self) -> None:
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), None)
        if header != list(OUTPUT_COLUMNS):
            raise ValueError(
                f"Existing output {self.path} has different columns; "
                "use a new --out or remove --resume"
            )

    def write(self, row: Mapping[str, Any]) -> None:
        ordered = {column: row.get(column) for column in OUTPUT_COLUMNS}
        if self._is_jsonl:
            self._handle.write(
                json.dumps(ordered, ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            )
        else:
            assert self._writer is not None
            self._writer.writerow(ordered)
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "_FeatureWriter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _FailureWriter:
    """CSV writer for per-stage feature-extraction failures."""

    def __init__(self, path: Path, *, resume: bool) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists() and path.stat().st_size > 0 and resume:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle), None)
            if header != list(FAILURE_COLUMNS):
                raise ValueError(
                    f"Existing failure log {path} has different columns; "
                    "use a new --out or remove --resume"
                )
            mode = "a"
        else:
            mode = "w"

        self._handle = path.open(mode, encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._handle, fieldnames=FAILURE_COLUMNS)
        if mode == "w":
            self._writer.writeheader()

    def write(self, row: Mapping[str, Any]) -> None:
        self._writer.writerow(
            {column: row.get(column) for column in FAILURE_COLUMNS}
        )
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "_FailureWriter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _parse_args(argv: Iterable[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract Pri-MCCD pose, YOLO behaviour and ASR features into one "
            "row per clip. Failed feature groups are recorded in "
            "<out>_failures.csv."
        )
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Pri-MCCD manifest CSV (must contain clip_id)",
    )
    parser.add_argument(
        "--pose-root",
        required=False,
        type=Path,
        default=None,
        help=(
            "Directory containing the official MTCNN pose JSON files; "
            "omit to skip pose features and run ASR/detection only"
        ),
    )
    parser.add_argument(
        "--weights",
        type=Path,
        default=None,
        help="YOLO weights; omit to skip detection features",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output feature table (.csv, .jsonl or .ndjson)",
    )
    parser.add_argument(
        "--sample-fps",
        type=_positive_float,
        default=1.0,
        help="Detection frame sampling rate in frames per second (default: 1.0)",
    )
    parser.add_argument(
        "--asr-model",
        default=None,
        help=(
            "faster-whisper model name or path (e.g. small, large-v3); "
            "omit to skip ASR features"
        ),
    )
    parser.add_argument(
        "--limit",
        type=_non_negative_int,
        default=None,
        help="Process at most N manifest clips (after --resume skips)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip clips already present in --out and append new results",
    )
    return parser.parse_args(argv)


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"must be positive, got {value!r}")
    return number


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value!r}")
    return number


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
