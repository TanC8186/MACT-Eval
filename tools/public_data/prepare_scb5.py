#!/usr/bin/env python
"""Merge SCB5 student-behaviour subsets into one leakage-free YOLO dataset.

SCB5 is published as separate archives (hand-raising/reading/writing,
discussing, bow-head/turn-head).  The archives overlap: the same classroom
frame appears in more than one archive with different annotations, and the
same image can be in ``train`` in one archive and ``val`` in another.

This script therefore:

* de-duplicates by image content hash (not by filename);
* unions the per-archive annotations for each unique image, remapping class
  ids into one taxonomy;
* assigns the split deterministically from the content hash, so an image can
  never appear in both ``train`` and ``val``;
* writes a YOLO dataset plus ``data.yaml`` and a merge report.

No image is invented or re-labelled beyond the union of the source archives'
own annotations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

#: Merged taxonomy.  Positive / neutral / off-task grouping is recorded in
#: ``CLASS_GROUP`` and is what the rule baseline maps onto.
MERGED_CLASSES: tuple[str, ...] = (
    "hand-raising",
    "reading",
    "writing",
    "discussing",
    "bow-head",
    "turn-head",
)

CLASS_GROUP: dict[str, str] = {
    "hand-raising": "positive",
    "reading": "positive",
    "writing": "positive",
    "discussing": "neutral",
    "bow-head": "off_task",
    "turn-head": "off_task",
}

#: Source-archive class name -> merged class name.
SOURCE_CLASS_NAMES: dict[str, tuple[str, ...]] = {
    "hand": ("hand-raising", "reading", "writing"),
    "disc": ("discussing",),
    "bow": ("bow-head", "turn-head"),
}


@dataclass(frozen=True)
class SourceSubset:
    key: str
    root: Path


def label_path_for(image: Path) -> Path:
    """``.../images/train/x.jpg`` -> ``.../labels/train/x.txt``."""
    parts = list(image.parts)
    if "images" not in parts:
        raise ValueError(f"image path has no images/ segment: {image}")
    parts[parts.index("images")] = "labels"
    return Path(*parts).with_suffix(".txt")


def content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_for_hash(digest: str, val_fraction: float) -> str:
    """Deterministic split from the hash: same image always lands in one split.

    The whole digest is used, not just its prefix: taking a fixed-width prefix
    would collapse every digest that shares that prefix into one bucket.
    """
    bucket = int(digest, 16) % 10_000
    return "val" if bucket < int(val_fraction * 10_000) else "train"


def parse_label_file(path: Path) -> tuple[list[tuple[int, str]], int]:
    """Return (valid rows, number of dropped rows).

    A row is dropped when it is malformed or when its box leaves the image
    (``0 <= x, y, w, h <= 1``).  The source archives contain a few boxes with
    a centre slightly outside the frame; those are reported, never clamped,
    because clamping would invent a position the annotator did not give.
    """
    rows: list[tuple[int, str]] = []
    dropped = 0
    if not path.is_file():
        return rows, dropped
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) != 5:
            if parts:
                dropped += 1
            continue
        try:
            class_id = int(parts[0])
            box = [float(value) for value in parts[1:]]
        except ValueError:
            dropped += 1
            continue
        if class_id < 0 or any(value < 0.0 or value > 1.0 for value in box):
            dropped += 1
            continue
        rows.append((class_id, " ".join(parts[1:])))
    return rows, dropped


def collect(
    subsets: Sequence[SourceSubset],
    val_fraction: float,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Group images by content hash and union their annotations."""
    merged: dict[str, dict[str, object]] = {}
    stats: dict[str, object] = {
        "subsets": {},
        "duplicates": 0,
        "dropped_label_rows": 0,
        "out_of_range_class_rows": 0,
    }

    for subset in subsets:
        names = SOURCE_CLASS_NAMES[subset.key]
        images = sorted(subset.root.rglob("*.jpg"))
        stats["subsets"][subset.key] = {"images": len(images), "root": str(subset.root)}
        for image in images:
            digest = content_hash(image)
            annotations, dropped = parse_label_file(label_path_for(image))
            stats["dropped_label_rows"] += dropped
            entry = merged.get(digest)
            if entry is None:
                entry = {
                    "source_image": image,
                    "annotations": set(),
                    "sources": [],
                }
                merged[digest] = entry
            else:
                stats["duplicates"] += 1
            for class_id, box in annotations:
                if class_id >= len(names):
                    stats["out_of_range_class_rows"] += 1
                    continue
                merged_name = names[class_id]
                entry["annotations"].add(f"{MERGED_CLASSES.index(merged_name)} {box}")
            entry["sources"].append(subset.key)

    for digest, entry in merged.items():
        entry["split"] = split_for_hash(digest, val_fraction)
    return merged, stats


def write_dataset(
    merged: dict[str, dict[str, object]],
    out_dir: Path,
    stats: dict[str, object],
) -> dict[str, object]:
    for split in ("train", "val"):
        (out_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (out_dir / "labels" / split).mkdir(parents=True, exist_ok=True)

    per_split = Counter()
    per_class = Counter()
    overlap_removed = 0
    for index, (digest, entry) in enumerate(sorted(merged.items())):
        split = str(entry["split"])
        source_image = Path(entry["source_image"])
        target_stem = f"{index:06d}_{digest[:8]}"
        target_image = out_dir / "images" / split / f"{target_stem}.jpg"
        target_label = out_dir / "labels" / split / f"{target_stem}.txt"

        shutil.copyfile(source_image, target_image)
        lines = sorted(entry["annotations"])
        target_label.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
        per_split[split] += 1
        for line in lines:
            per_class[MERGED_CLASSES[int(line.split()[0])]] += 1

    data_yaml = out_dir / "data.yaml"
    data_yaml.write_text(
        "path: " + out_dir.resolve().as_posix() + "\n"
        "train: images/train\n"
        "val: images/val\n"
        "names:\n"
        + "".join(f"  {i}: {name}\n" for i, name in enumerate(MERGED_CLASSES)),
        encoding="utf-8",
    )

    report = {
        **stats,
        "unique_images": len(merged),
        "per_split": dict(per_split),
        "per_class_boxes": dict(per_class),
        "class_group": CLASS_GROUP,
        "data_yaml": str(data_yaml),
    }
    (out_dir / "merge_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted", required=True, type=Path,
                        help="Directory containing the extracted SCB5 archives")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--val-fraction", type=float, default=0.15)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not 0 < args.val_fraction < 1:
        parser.error("--val-fraction must be between 0 and 1")

    roots = {
        "hand": args.extracted / "SCB5-Handrise-Read-write-2024-9-17",
        "disc": args.extracted / "SCB5-Discuss-2024-9-17",
        "bow": args.extracted / "SCB_BowTurnHead_20250509" / "SCB5-Turn-Bow-Head-2024-9-17",
    }
    missing = [str(p) for p in roots.values() if not p.is_dir()]
    if missing:
        parser.error("missing extracted subset(s): " + ", ".join(missing))

    subsets = [SourceSubset(key=key, root=root) for key, root in roots.items()]
    merged, stats = collect(subsets, args.val_fraction)
    report = write_dataset(merged, args.out, stats)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
