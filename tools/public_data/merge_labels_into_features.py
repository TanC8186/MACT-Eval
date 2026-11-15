#!/usr/bin/env python
"""Join adjudicated classroom-climate labels onto the feature table.

``merge_annotations.py`` writes ``merged_labels.csv`` with a ``label`` column
(0/1/2) and blank rows for unresolved clips.  This script left-joins those
labels onto the feature CSV keyed by ``clip_id`` and writes only labelled
rows, so the fusion experiment never silently trains on unlabelled data.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

import pandas as pd


def merge(features_path: Path, labels_path: Path, out_path: Path) -> dict[str, object]:
    features = pd.read_csv(features_path)
    labels = pd.read_csv(labels_path)
    if "clip_id" not in features.columns or "clip_id" not in labels.columns:
        raise ValueError("both files must contain a clip_id column")
    if "label" not in labels.columns:
        raise ValueError("labels file must contain a label column")
    labels = labels[["clip_id", "label"]].drop_duplicates("clip_id")
    merged = features.drop(columns=["label"], errors="ignore").merge(
        labels, on="clip_id", how="left"
    )
    merged["label"] = pd.to_numeric(merged["label"], errors="coerce")
    before = len(merged)
    merged = merged[merged["label"].isin([0, 1, 2])].copy()
    merged["label"] = merged["label"].astype(int)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out_path, index=False, encoding="utf-8")
    return {
        "features_rows": int(before),
        "labelled_rows": int(len(merged)),
        "unlabelled_rows": int(before - len(merged)),
        "label_counts": {
            str(int(k)): int(v) for k, v in merged["label"].value_counts().sort_index().items()
        },
        "out": str(out_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(merge(args.features, args.labels, args.out), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
