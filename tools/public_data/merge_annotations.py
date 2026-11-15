#!/usr/bin/env python
"""Merge two independent classroom-climate annotation sheets.

The annotators fill ``annotation_A.csv`` and ``annotation_B.csv`` produced by
``prepare_classroom_videos.py``.  This script:

* validates that both sheets cover the same clip ids;
* computes Cohen's kappa on the clips both annotators labelled;
* keeps the agreed label; writes disagreements to
  ``adjudication_needed.csv`` for a third reviewer;
* if ``--adjudicated`` is supplied later, uses its ``label`` column to resolve
  disagreements and writes ``merged_labels.csv``.

Labels are 0 = Negative, 1 = Neutral, 2 = Positive; blank means unlabelled.
No label is invented for a missing or disputed clip.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Sequence

from sklearn.metrics import cohen_kappa_score

LABELS = {"0", "1", "2"}


def _read(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"clip_id", "lesson_id", "label"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path} missing columns: {sorted(required - set(reader.fieldnames or []))}")
        rows: dict[str, dict[str, str]] = {}
        for row in reader:
            clip_id = (row.get("clip_id") or "").strip()
            if not clip_id:
                raise ValueError(f"{path} contains a blank clip_id")
            label = (row.get("label") or "").strip()
            if label and label not in LABELS:
                raise ValueError(f"{path}: clip {clip_id} has invalid label {label!r}")
            rows[clip_id] = {**row, "clip_id": clip_id, "label": label}
    return rows


def merge(
    a_path: Path,
    b_path: Path,
    out_dir: Path,
    adjudicated_path: Path | None = None,
) -> dict[str, Any]:
    a = _read(a_path)
    b = _read(b_path)
    missing_a = sorted(set(b) - set(a))
    missing_b = sorted(set(a) - set(b))
    if missing_a or missing_b:
        raise ValueError(
            f"annotation sheets do not cover the same clips: "
            f"missing in A={missing_a[:5]}, missing in B={missing_b[:5]}"
        )

    adjudicated: dict[str, str] = {}
    if adjudicated_path is not None:
        for clip_id, row in _read(adjudicated_path).items():
            label = (row.get("label") or "").strip()
            if label:
                adjudicated[clip_id] = label

    merged_rows: list[dict[str, str]] = []
    adjudication_rows: list[dict[str, str]] = []
    y_a: list[str] = []
    y_b: list[str] = []
    for clip_id in sorted(a):
        row_a = a[clip_id]
        row_b = b[clip_id]
        label_a = row_a["label"]
        label_b = row_b["label"]
        if label_a and label_b:
            y_a.append(label_a)
            y_b.append(label_b)
        if label_a and label_b and label_a == label_b:
            label = label_a
            source = "agreement"
        elif clip_id in adjudicated:
            label = adjudicated[clip_id]
            source = "adjudicated"
        else:
            label = ""
            source = "unresolved"
            adjudication_rows.append(
                {
                    "clip_id": clip_id,
                    "lesson_id": row_a.get("lesson_id", ""),
                    "subject": row_a.get("subject", ""),
                    "grade": row_a.get("grade", ""),
                    "start_sec": row_a.get("start_sec", ""),
                    "end_sec": row_a.get("end_sec", ""),
                    "window_path": row_a.get("window_path", ""),
                    "label_a": label_a,
                    "label_b": label_b,
                    "label": "",
                    "notes": "",
                }
            )
        merged_rows.append(
            {
                "clip_id": clip_id,
                "lesson_id": row_a.get("lesson_id", ""),
                "subject": row_a.get("subject", ""),
                "grade": row_a.get("grade", ""),
                "start_sec": row_a.get("start_sec", ""),
                "end_sec": row_a.get("end_sec", ""),
                "window_path": row_a.get("window_path", ""),
                "label_a": label_a,
                "label_b": label_b,
                "label": label,
                "source": source,
            }
        )

    kappa = float(cohen_kappa_score(y_a, y_b)) if y_a and len(set(y_a + y_b)) > 1 else None
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "merged_labels.csv", merged_rows)
    _write_csv(out_dir / "adjudication_needed.csv", adjudication_rows)
    summary = {
        "n_clips": len(merged_rows),
        "n_double_labelled": len(y_a),
        "n_agreed": sum(1 for row in merged_rows if row["source"] == "agreement"),
        "n_adjudicated": sum(1 for row in merged_rows if row["source"] == "adjudicated"),
        "n_unresolved": len(adjudication_rows),
        "cohens_kappa": kappa,
        "label_counts": {
            str(label): sum(1 for row in merged_rows if row["label"] == str(label))
            for label in (0, 1, 2)
        },
    }
    (out_dir / "agreement_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md = [
        "# Annotation agreement",
        "",
        f"- Clips: {summary['n_clips']}",
        f"- Double-labelled: {summary['n_double_labelled']}",
        f"- Agreed: {summary['n_agreed']}",
        f"- Adjudicated: {summary['n_adjudicated']}",
        f"- Unresolved: {summary['n_unresolved']}",
        f"- Cohen's kappa: {summary['cohens_kappa']}",
        f"- Label counts: {summary['label_counts']}",
        "",
    ]
    (out_dir / "agreement_summary.md").write_text("\n".join(md), encoding="utf-8")
    return summary


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if rows:
        fieldnames = list(rows[0].keys())
    else:
        fieldnames = ["clip_id", "lesson_id", "subject", "grade", "start_sec", "end_sec", "window_path", "label_a", "label_b", "label", "source"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", required=True, type=Path)
    parser.add_argument("--b", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--adjudicated", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = merge(args.a, args.b, args.out_dir, args.adjudicated)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
