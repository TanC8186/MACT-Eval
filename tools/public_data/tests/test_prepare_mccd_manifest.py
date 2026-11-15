from __future__ import annotations

from pathlib import Path

import pytest

from tools.public_data.prepare_mccd_manifest import (
    MANIFEST_COLUMNS,
    cluster_sessions,
    hamming,
    iter_videos,
)


def test_hamming_counts_differing_bits() -> None:
    assert hamming(0b1010, 0b1010) == 0
    assert hamming(0b1010, 0b1011) == 1
    assert hamming(0b0000, 0b1111) == 4


def test_cluster_sessions_groups_similar_hashes() -> None:
    hashes = [0b0000, 0b0001, 0b1111_0000, 0b1111_0001]
    assignment = cluster_sessions(hashes, max_distance=1)
    assert assignment[0] == assignment[1]
    assert assignment[2] == assignment[3]
    assert assignment[0] != assignment[2]


def test_cluster_sessions_keeps_distant_hashes_separate() -> None:
    assignment = cluster_sessions([0, 0xFF, 0xFFFF], max_distance=2)
    assert len(set(assignment)) == 3


def test_cluster_sessions_single_clip() -> None:
    assert cluster_sessions([12345], max_distance=10) == [0]


def test_iter_videos_maps_folders_to_labels(tmp_path: Path) -> None:
    for folder, label in (("negative", 0), ("neutral", 1), ("positive", 2)):
        directory = tmp_path / folder
        directory.mkdir(parents=True)
        (directory / f"{folder}_0001.mp4").write_bytes(b"x" * 10)
    found = iter_videos(tmp_path)
    assert [label for label, _ in found] == [0, 1, 2]


def test_iter_videos_skips_empty_files(tmp_path: Path) -> None:
    directory = tmp_path / "negative"
    directory.mkdir(parents=True)
    (directory / "empty.mp4").write_bytes(b"")
    (directory / "ok.mp4").write_bytes(b"x" * 10)
    assert [p.name for _, p in iter_videos(tmp_path)] == ["ok.mp4"]


def test_iter_videos_missing_folders_are_ignored(tmp_path: Path) -> None:
    assert iter_videos(tmp_path) == []


def test_manifest_columns_cover_extractor_requirements() -> None:
    required = {"clip_id", "lesson_id", "window_path", "audio_path", "start_sec", "end_sec"}
    assert required.issubset(set(MANIFEST_COLUMNS))
