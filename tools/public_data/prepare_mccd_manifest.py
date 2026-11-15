#!/usr/bin/env python
"""Build an extraction manifest for MCCD clips and assign pseudo-sessions.

MCCD (https://osf.io/fw28j) ships 3,699 five-second clips under three class
folders.  The release has no lesson/session identifier, and the numeric ids
are not chronological, so a naive ``GroupKFold`` by id would leak: clips from
the same recording sit next to each other by scene, not by id.

This script therefore rebuilds coarse pseudo-sessions from image content:

* decode one frame per clip, resize to 9x8 grayscale, compute a 64-bit dHash;
* greedily cluster clips whose hashes are within ``--hamming`` bits.

Clips from the same room/camera land in the same pseudo-session with high
probability, which is enough to make grouped cross-validation defensible.
It is deliberately conservative: the clustering is reported, not hidden, and
the number of clusters is written to the manifest summary so the thesis can
state exactly what was done.

The output CSV matches what ``extract_classroom_features.py`` expects
(``clip_id``, ``lesson_id``, ``window_path``, ``audio_path``, ``start_sec``,
``end_sec``) plus ``label`` and the pseudo-session metadata.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence

MANIFEST_COLUMNS = [
    "clip_id",
    "lesson_id",
    "subject",
    "grade",
    "label",
    "start_sec",
    "end_sec",
    "window_path",
    "audio_path",
    "pseudo_session",
    "frame_hash",
]


def iter_videos(root: Path) -> list[tuple[int, Path]]:
    """Return ``(label, path)`` for every downloaded clip, sorted by label/name."""
    label_dirs = {"negative": 0, "neutral": 1, "positive": 2}
    found: list[tuple[int, Path]] = []
    for folder, label in label_dirs.items():
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.mp4")):
            if path.stat().st_size > 0:
                found.append((label, path))
    return found


def dhash(path: Path, hash_size: int = 8) -> int:
    """64-bit difference hash of the middle frame."""
    import cv2  # imported lazily so tests can run without video support

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise RuntimeError(f"cannot open {path}")
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, total // 2))
        ok, frame = capture.read()
        if not ok:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
        if not ok:
            raise RuntimeError(f"cannot decode a frame from {path}")
    finally:
        capture.release()

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (hash_size + 1, hash_size), interpolation=cv2.INTER_AREA)
    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            bits = (bits << 1) | int(small[row, col] < small[row, col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def cluster_sessions(hashes: Sequence[int], max_distance: int) -> list[int]:
    """Greedy clustering: first clip becomes the representative of a cluster."""
    representatives: list[int] = []
    assignment: list[int] = []
    for value in hashes:
        for index, rep in enumerate(representatives):
            if hamming(value, rep) <= max_distance:
                assignment.append(index)
                break
        else:
            representatives.append(value)
            assignment.append(len(representatives) - 1)
    return assignment


def probe_duration(path: Path) -> float:
    import subprocess

    completed = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {completed.stderr.strip()}")
    return float(completed.stdout.strip())


def build_manifest(
    root: Path,
    *,
    max_distance: int = 10,
    limit: int | None = None,
    require_audio: bool = True,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    videos = iter_videos(root)
    if limit is not None:
        videos = videos[:limit]
    if not videos:
        raise FileNotFoundError(f"no clips found under {root}")

    hashes: list[int] = []
    durations: list[float] = []
    skipped: list[dict[str, str]] = []
    kept: list[tuple[int, Path]] = []
    for label, path in videos:
        try:
            hashes.append(dhash(path))
            durations.append(probe_duration(path))
            kept.append((label, path))
        except Exception as exc:  # noqa: BLE001 - reported, never silently dropped
            skipped.append({"clip": path.name, "reason": f"{type(exc).__name__}: {exc}"})

    sessions = cluster_sessions(hashes, max_distance)
    by_session: dict[int, Counter] = defaultdict(Counter)
    rows: list[dict[str, object]] = []
    for index, (label, path) in enumerate(kept):
        session = sessions[index]
        by_session[session][label] += 1
        rows.append(
            {
                "clip_id": path.stem,
                "lesson_id": f"mccd_s{session:04d}",
                "subject": "",
                "grade": "",
                "label": label,
                "start_sec": "0.000",
                "end_sec": f"{durations[index]:.3f}",
                "window_path": str(path),
                "audio_path": str(path),
                "pseudo_session": session,
                "frame_hash": f"{hashes[index]:016x}",
            }
        )

    label_counts = Counter(int(row["label"]) for row in rows)
    mixed = sum(1 for counts in by_session.values() if len(counts) > 1)
    summary = {
        "clips": len(rows),
        "label_counts": {str(k): v for k, v in sorted(label_counts.items())},
        "pseudo_sessions": len(by_session),
        "sessions_with_multiple_labels": mixed,
        "max_hamming": max_distance,
        "skipped": skipped,
        "clips_per_session": {
            str(k): sum(v.values()) for k, v in sorted(by_session.items())
        },
    }
    return rows, summary


def _write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--videos", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--hamming", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not 0 <= args.hamming <= 64:
        parser.error("--hamming must be within [0, 64]")

    rows, summary = build_manifest(args.videos, max_distance=args.hamming, limit=args.limit)
    _write_csv(args.out, rows)
    summary_path = args.summary or args.out.with_name("manifest_summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "clips_per_session"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
