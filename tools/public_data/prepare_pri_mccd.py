"""Validate a Pri-MCCD download and write the experiment manifest CSV.

Usage::

    python tools/public_data/prepare_pri_mccd.py \
        --root data/raw/public/pri_mccd \
        --out data/processed/pri_mccd/manifest.csv \
        [--metadata data/raw/public/pri_mccd/metadata.csv]

The command delegates clip discovery and validation to
:func:`public_data.manifest.build_manifest`, then reports per-label counts, the
number of clips without a matching WAV file, and the total audio duration read
from the WAV headers (when readable).  It exits non-zero when the root is
missing, contains no clips, or the manifest cannot be built; it never invents
counts or fills missing audio.
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path
from typing import Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from public_data.labels import LABEL_NAMES
    from public_data.manifest import build_manifest
else:
    from .labels import LABEL_NAMES
    from .manifest import build_manifest


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a Pri-MCCD directory and write a clip manifest CSV "
            "(clip_id, label, label_name, video_path, audio_path, lesson_id, "
            "split)."
        )
    )
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Pri-MCCD root containing negative/, neutral/ and positive/",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output manifest CSV path",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=None,
        help=(
            "Optional CSV/JSON mapping clip ids to lesson_id and/or split; "
            "when omitted, <root>/metadata.csv or metadata.json is used if "
            "present"
        ),
    )
    return parser.parse_args(argv)


def _wav_duration_seconds(path: str | None) -> float | None:
    """Return a WAV file's duration in seconds, or ``None`` if unreadable.

    Only the standard-library ``wave`` reader is used, so unusual codecs or
    placeholder files are reported as unavailable instead of crashing the CLI.
    """
    if not path:
        return None
    try:
        with wave.open(str(path), "rb") as handle:
            frame_rate = handle.getframerate()
            frame_count = handle.getnframes()
    except (OSError, EOFError, ValueError, wave.Error):
        return None
    if frame_rate <= 0:
        return None
    return frame_count / float(frame_rate)


def _total_audio_duration(
    manifest,
) -> tuple[float, int, int] | None:
    """Return ``(seconds, readable_clips, total_clips)`` or ``None``."""
    total_clips = len(manifest)
    seconds = 0.0
    readable = 0
    for audio_path in manifest["audio_path"]:
        duration = _wav_duration_seconds(audio_path)
        if duration is not None:
            seconds += duration
            readable += 1
    if readable == 0:
        return None
    return seconds, readable, total_clips


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        manifest = build_manifest(args.root, metadata_path=args.metadata)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(args.out, index=False, encoding="utf-8")

    counts = manifest.attrs["label_counts"]
    missing_audio = manifest.attrs["missing_audio"]

    print(f"clips: {len(manifest)}")
    for label in sorted(LABEL_NAMES):
        name = LABEL_NAMES[label]
        print(f"{name}: {counts[name]}")
    print(f"missing audio: {len(missing_audio)}")

    duration = _total_audio_duration(manifest)
    if duration is None:
        print("total audio duration: unavailable (no readable .wav files)")
    else:
        seconds, readable, total = duration
        print(
            f"total audio duration: {seconds:.2f} s "
            f"({readable}/{total} clips readable)"
        )

    print(f"manifest: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
