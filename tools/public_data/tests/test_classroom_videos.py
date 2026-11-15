"""Tests for public-classroom windowing and annotation merging."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from public_data import merge_annotations, prepare_classroom_videos


def _write_sheet(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["clip_id", "lesson_id", "label"],
        )
        writer.writeheader()
        writer.writerows(rows)


def test_merge_annotations_keeps_agreements_and_flags_disagreements(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_sheet(
        a,
        [
            {"clip_id": "c1", "lesson_id": "L1", "label": "0"},
            {"clip_id": "c2", "lesson_id": "L1", "label": "1"},
            {"clip_id": "c3", "lesson_id": "L2", "label": "2"},
        ],
    )
    _write_sheet(
        b,
        [
            {"clip_id": "c1", "lesson_id": "L1", "label": "0"},
            {"clip_id": "c2", "lesson_id": "L1", "label": "2"},
            {"clip_id": "c3", "lesson_id": "L2", "label": "2"},
        ],
    )
    summary = merge_annotations.merge(a, b, tmp_path / "out")
    assert summary["n_clips"] == 3
    assert summary["n_agreed"] == 2
    assert summary["n_unresolved"] == 1
    assert summary["cohens_kappa"] is not None
    merged = list(csv.DictReader((tmp_path / "out" / "merged_labels.csv").open(encoding="utf-8-sig")))
    assert [row["label"] for row in merged] == ["0", "", "2"]
    adjudication = list(
        csv.DictReader((tmp_path / "out" / "adjudication_needed.csv").open(encoding="utf-8-sig"))
    )
    assert [row["clip_id"] for row in adjudication] == ["c2"]


def test_merge_annotations_uses_adjudicated_label(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    adj = tmp_path / "adj.csv"
    _write_sheet(a, [{"clip_id": "c1", "lesson_id": "L1", "label": "0"}])
    _write_sheet(b, [{"clip_id": "c1", "lesson_id": "L1", "label": "2"}])
    _write_sheet(adj, [{"clip_id": "c1", "lesson_id": "L1", "label": "1"}])
    summary = merge_annotations.merge(a, b, tmp_path / "out", adj)
    assert summary["n_unresolved"] == 0
    assert summary["n_adjudicated"] == 1
    merged = list(csv.DictReader((tmp_path / "out" / "merged_labels.csv").open(encoding="utf-8-sig")))
    assert merged[0]["label"] == "1"


def test_merge_annotations_rejects_invalid_label(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_sheet(a, [{"clip_id": "c1", "lesson_id": "L1", "label": "9"}])
    _write_sheet(b, [{"clip_id": "c1", "lesson_id": "L1", "label": "0"}])
    with pytest.raises(ValueError):
        merge_annotations.merge(a, b, tmp_path / "out")


def test_annotation_html_contains_window_sources(tmp_path: Path) -> None:
    window = prepare_classroom_videos.Window(
        clip_id="v_w000",
        lesson_id="v",
        video_id="v",
        subject="语文",
        grade="小学",
        teacher="T",
        source_page="https://example.org",
        license_note="test",
        start_sec=0.0,
        end_sec=60.0,
        duration_sec=60.0,
        window_path=tmp_path / "v" / "v_w000.mp4",
        audio_path=tmp_path / "v" / "v_w000.wav",
    )
    html = prepare_classroom_videos._html([window], "A")
    assert '"dir": "v"' in html
    assert '"src": "v_w000.mp4"' in html
    assert "mact_classroom_annotation_A" in html
    assert "导出 CSV" in html
