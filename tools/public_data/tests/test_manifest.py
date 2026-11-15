"""Tests for Pri-MCCD manifest construction."""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import pandas as pd
import pytest

from public_data.manifest import MANIFEST_COLUMNS, build_manifest


def _touch(path: Path, payload: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


@pytest.fixture
def pri_root(tmp_path: Path) -> Path:
    """Synthetic Pri-MCCD tree: two complete clips, one missing its audio."""
    root = tmp_path / "pri_mccd"
    _touch(root / "negative" / "neg0001_0.mp4")
    _touch(root / "negative" / "neg0001_0.wav")
    _touch(root / "neutral" / "neu0002_1.mp4")
    _touch(root / "neutral" / "neu0002_1.wav")
    _touch(root / "positive" / "pos0003_2.mp4")
    return root


def test_build_manifest_columns_labels_and_counts(pri_root: Path) -> None:
    with pytest.warns(RuntimeWarning, match="missing"):
        df = build_manifest(pri_root)

    assert list(df.columns) == MANIFEST_COLUMNS
    assert len(df) == 3
    assert df["clip_id"].tolist() == ["neg0001", "neu0002", "pos0003"]
    assert df["label"].tolist() == [0, 1, 2]
    assert df["label_name"].tolist() == ["negative", "neutral", "positive"]
    assert df["lesson_id"].tolist() == [None, None, None]
    assert df["split"].tolist() == ["unspecified", "unspecified", "unspecified"]

    assert df.attrs["label_counts"] == {"negative": 1, "neutral": 1, "positive": 1}
    assert df.attrs["missing_audio"] == ["pos0003"]


def test_build_manifest_resolves_video_and_audio_paths(pri_root: Path) -> None:
    with pytest.warns(RuntimeWarning):
        df = build_manifest(pri_root)

    rows = df.set_index("clip_id")
    assert rows.loc["neg0001", "video_path"] == str(
        pri_root / "negative" / "neg0001_0.mp4"
    )
    assert rows.loc["neg0001", "audio_path"] == str(
        pri_root / "negative" / "neg0001_0.wav"
    )
    assert rows.loc["pos0003", "audio_path"] is None


def test_build_manifest_no_warning_when_every_audio_exists(tmp_path: Path) -> None:
    root = tmp_path / "complete"
    for rel in (
        "negative/neg0001_0.mp4",
        "negative/neg0001_0.wav",
        "neutral/neu0002_1.mp4",
        "neutral/neu0002_1.wav",
    ):
        _touch(root / rel)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        df = build_manifest(root)

    assert df.attrs["missing_audio"] == []
    assert not [w for w in recorded if issubclass(w.category, RuntimeWarning)]


def test_build_manifest_reads_metadata_csv_by_clip_id(pri_root: Path) -> None:
    (pri_root / "metadata.csv").write_text(
        "clip_id,lesson_id,split\n"
        "neg0001,lesson01,train\n"
        "neu0002,lesson02,validation\n"
        "pos0003,lesson03,test\n",
        encoding="utf-8",
    )

    with pytest.warns(RuntimeWarning, match="missing"):
        df = build_manifest(pri_root)
    rows = df.set_index("clip_id")
    assert rows.loc["neg0001", "lesson_id"] == "lesson01"
    assert rows.loc["neg0001", "split"] == "train"
    assert rows.loc["neu0002", "lesson_id"] == "lesson02"
    assert rows.loc["neu0002", "split"] == "validation"
    assert rows.loc["pos0003", "lesson_id"] == "lesson03"
    assert rows.loc["pos0003", "split"] == "test"


def test_build_manifest_metadata_matches_file_column_with_extension(
    pri_root: Path,
) -> None:
    metadata = pri_root / "annotations.csv"
    metadata.write_text(
        "file,lesson,fold\n"
        "NEG0001_0.MP4,L1,train\n"
        "neu0002_1.wav,L2,val\n"
        "pos0003_2.mp4,L3,test\n",
        encoding="utf-8",
    )

    with pytest.warns(RuntimeWarning, match="missing"):
        df = build_manifest(pri_root, metadata_path=metadata)
    rows = df.set_index("clip_id")
    assert rows.loc["neg0001", "lesson_id"] == "L1"
    assert rows.loc["neg0001", "split"] == "train"
    assert rows.loc["neu0002", "lesson_id"] == "L2"
    assert rows.loc["neu0002", "split"] == "val"


def test_build_manifest_reads_metadata_json_list(pri_root: Path) -> None:
    (pri_root / "metadata.json").write_text(
        json.dumps(
            [
                {"clip_id": "neg0001", "lesson_id": "L1", "split": "train"},
                {"clip_id": "neu0002", "lesson_id": "L2", "split": "val"},
            ]
        ),
        encoding="utf-8",
    )

    with pytest.warns(RuntimeWarning, match="missing"):
        df = build_manifest(pri_root)
    rows = df.set_index("clip_id")
    assert rows.loc["neu0002", "lesson_id"] == "L2"
    assert rows.loc["neu0002", "split"] == "val"
    # Not present in metadata -> explicit unknown values, never invented.
    assert rows.loc["pos0003", "lesson_id"] is None
    assert rows.loc["pos0003", "split"] == "unspecified"


def test_build_manifest_reads_metadata_json_mapping(tmp_path: Path) -> None:
    root = tmp_path / "mapped"
    _touch(root / "neutral" / "neu0005_1.mp4")
    _touch(root / "neutral" / "neu0005_1.wav")
    metadata = root / "meta.json"
    metadata.write_text(
        json.dumps({"neu0005": {"lesson_id": "L5", "split": "test"}}),
        encoding="utf-8",
    )

    df = build_manifest(root, metadata_path=metadata)
    assert df.loc[0, "lesson_id"] == "L5"
    assert df.loc[0, "split"] == "test"


def test_build_manifest_rejects_directory_label_mismatch(pri_root: Path) -> None:
    _touch(pri_root / "negative" / "pos0004_2.mp4")

    with pytest.raises(ValueError, match="directory"):
        build_manifest(pri_root)


def test_build_manifest_rejects_empty_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No .mp4"):
        build_manifest(tmp_path)


def test_build_manifest_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_manifest(tmp_path / "does-not-exist")


def test_build_manifest_validates_expected_counts(pri_root: Path) -> None:
    with pytest.raises(ValueError, match="count"):
        build_manifest(
            pri_root,
            expected_counts={"negative": 2, "neutral": 1, "positive": 1},
        )

    with pytest.warns(RuntimeWarning):
        df = build_manifest(
            pri_root,
            expected_counts={"negative": 1, "neutral": 1, "positive": 1},
        )
    assert len(df) == 3


def test_build_manifest_is_deterministically_sorted(pri_root: Path) -> None:
    _touch(pri_root / "negative" / "neg0009_0.mp4")
    _touch(pri_root / "negative" / "neg0009_0.wav")

    with pytest.warns(RuntimeWarning):
        df = build_manifest(pri_root)

    assert df["clip_id"].tolist() == ["neg0001", "neg0009", "neu0002", "pos0003"]
    assert isinstance(df, pd.DataFrame)
