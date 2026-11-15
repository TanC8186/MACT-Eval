from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tools.public_data.prepare_scb5 import (
    CLASS_GROUP,
    MERGED_CLASSES,
    SourceSubset,
    collect,
    content_hash,
    label_path_for,
    parse_label_file,
    split_for_hash,
    write_dataset,
)


def _write_pair(root: Path, split: str, name: str, rows: list[str], payload: bytes) -> Path:
    image = root / "images" / split / f"{name}.jpg"
    label = root / "labels" / split / f"{name}.txt"
    image.parent.mkdir(parents=True, exist_ok=True)
    label.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(payload)
    label.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return image


def _subset(
    tmp_path: Path, key: str, name: str, files: dict[str, tuple[list[str], bytes]]
) -> SourceSubset:
    root = tmp_path / name
    for rel, (rows, payload) in files.items():
        split, stem = rel.split("/")
        _write_pair(root, split, stem, rows, payload)
    return SourceSubset(key=key, root=root)


def test_label_path_swaps_images_segment(tmp_path: Path) -> None:
    image = tmp_path / "ds" / "images" / "train" / "a.jpg"
    assert label_path_for(image) == tmp_path / "ds" / "labels" / "train" / "a.txt"


def test_label_path_requires_images_segment(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        label_path_for(tmp_path / "a.jpg")


def test_parse_label_file_skips_malformed_lines(tmp_path: Path) -> None:
    label = tmp_path / "a.txt"
    label.write_text(
        "0 0.1 0.2 0.3 0.4\n"
        "not-a-class 0.1 0.2 0.3 0.4\n"
        "1 0.1 0.2\n"
        "\n"
        "2 0.5 0.6 0.7 0.8\n",
        encoding="utf-8",
    )
    rows, dropped = parse_label_file(label)
    assert rows == [(0, "0.1 0.2 0.3 0.4"), (2, "0.5 0.6 0.7 0.8")]
    assert dropped == 2


def test_parse_label_file_missing_returns_empty(tmp_path: Path) -> None:
    assert parse_label_file(tmp_path / "missing.txt") == ([], 0)


def test_parse_label_file_drops_out_of_range_boxes(tmp_path: Path) -> None:
    label = tmp_path / "b.txt"
    label.write_text(
        "0 0.5 0.5 0.2 0.2\n"
        "1 1.007 0.24 0.12 0.14\n"
        "2 0.5 0.5 0.2 1.4\n",
        encoding="utf-8",
    )
    rows, dropped = parse_label_file(label)
    assert rows == [(0, "0.5 0.5 0.2 0.2")]
    assert dropped == 2


def test_split_for_hash_is_deterministic_and_bounded() -> None:
    digest = "a" * 64
    assert split_for_hash(digest, 0.15) in {"train", "val"}
    assert split_for_hash(digest, 0.15) == split_for_hash(digest, 0.15)


def test_split_for_hash_assigns_both_splits_across_hashes() -> None:
    # Sequential integers all share a bucket; use real digests.
    digests = [hashlib.sha256(f"frame-{i}".encode()).hexdigest() for i in range(200)]
    splits = {split_for_hash(d, 0.3) for d in digests}
    assert splits == {"train", "val"}


def test_split_for_hash_respects_approximate_fraction() -> None:
    digests = [hashlib.sha256(f"frame-{i}".encode()).hexdigest() for i in range(2000)]
    val = sum(1 for d in digests if split_for_hash(d, 0.15) == "val")
    assert 0.10 <= val / len(digests) <= 0.20


def test_identical_content_is_merged_to_one_image(tmp_path: Path) -> None:
    payload = b"identical-frame-bytes"
    hand = _subset(tmp_path, "hand", "hand", {
        "train/0001": (["0 0.1 0.2 0.3 0.4"], payload),
    })
    bow = _subset(tmp_path, "bow", "bow", {
        "val/0001": (["1 0.5 0.6 0.1 0.2"], payload),
    })

    merged, stats = collect([hand, bow], 0.0)

    assert len(merged) == 1
    entry = next(iter(merged.values()))
    assert stats["duplicates"] == 1
    # hand class 0 -> merged 0 (hand-raising); bow class 1 -> merged 5 (turn-head)
    assert entry["annotations"] == {"0 0.1 0.2 0.3 0.4", "5 0.5 0.6 0.1 0.2"}


def test_split_comes_from_hash_never_from_source_split(tmp_path: Path) -> None:
    payload = b"same-bytes"
    hand = _subset(tmp_path, "hand", "hand", {"train/x": (["0 0.1 0.2 0.3 0.4"], payload)})
    bow = _subset(tmp_path, "bow", "bow", {"val/x": (["1 0.1 0.2 0.3 0.4"], payload)})

    merged, _ = collect([hand, bow], 0.15)

    splits = {entry["split"] for entry in merged.values()}
    assert len(splits) == 1, "one content hash must map to exactly one split"


def test_collect_ignores_out_of_range_class_ids(tmp_path: Path) -> None:
    hand = _subset(tmp_path, "hand", "hand", {
        "train/a": (["9 0.1 0.2 0.3 0.4", "0 0.1 0.2 0.3 0.4"], b"payload-a"),
    })

    merged, stats = collect([hand], 0.0)

    entry = next(iter(merged.values()))
    assert entry["annotations"] == {"0 0.1 0.2 0.3 0.4"}
    assert stats["out_of_range_class_rows"] == 1


def test_write_dataset_produces_yaml_and_report(tmp_path: Path) -> None:
    payload = b"frame"
    hand = _subset(tmp_path, "hand", "hand", {"train/a": (["0 0.1 0.2 0.3 0.4"], payload)})
    merged, stats = collect([hand], 0.0)
    out = tmp_path / "out"

    report = write_dataset(merged, out, stats)

    assert report["unique_images"] == 1
    assert report["per_split"]["train"] == 1
    assert report["per_class_boxes"] == {"hand-raising": 1}
    assert (out / "data.yaml").is_file()
    assert (out / "merge_report.json").is_file()
    yaml_text = (out / "data.yaml").read_text(encoding="utf-8")
    for name in MERGED_CLASSES:
        assert name in yaml_text
    assert list((out / "images" / "train").glob("*.jpg"))
    assert list((out / "labels" / "train").glob("*.txt"))


def test_merged_taxonomy_covers_rule_groups() -> None:
    assert set(CLASS_GROUP) == set(MERGED_CLASSES)
    assert CLASS_GROUP["hand-raising"] == "positive"
    assert CLASS_GROUP["bow-head"] == "off_task"
    assert CLASS_GROUP["discussing"] == "neutral"


def test_content_hash_is_stable(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"abc")
    b.write_bytes(b"abc")
    assert content_hash(a) == content_hash(b)
    b.write_bytes(b"abd")
    assert content_hash(a) != content_hash(b)
