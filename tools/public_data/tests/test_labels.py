"""Tests for Pri-MCCD climate-label parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

from public_data.labels import (
    LABEL_NAMES,
    LABEL_SCORES,
    label_to_score,
    parse_climate_label,
    parse_clip_name,
)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("neg0001_0.mp4", 0),
        ("neu0002_1.mp4", 1),
        ("pos0003_2.mp4", 2),
        ("neu0023_1.wav", 1),
        ("negative/neg0001_0.mp4", 0),
        (Path("positive") / "pos9999_2.mp4", 2),
        ("NEG0001_0.MP4", 0),  # filenames are matched case-insensitively
    ],
)
def test_parse_climate_label_valid(filename: str | Path, expected: int) -> None:
    assert parse_climate_label(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "neg0001_1.mp4",  # prefix and suffix disagree
        "pos0003_0.mp4",  # prefix and suffix disagree
        "neg0001.mp4",  # suffix missing
        "neg001_0.mp4",  # identifier is not four digits
        "neg00010_0.mp4",  # identifier is not four digits
        "xxx0001_0.mp4",  # unknown prefix
        "neg0001_3.mp4",  # unknown suffix
        "neg0001_0_1.mp4",  # trailing material
        "",
    ],
)
def test_parse_climate_label_invalid(filename: str) -> None:
    with pytest.raises(ValueError):
        parse_climate_label(filename)


def test_parse_clip_name_returns_id_and_label_metadata() -> None:
    clip = parse_clip_name("neu0023_1.wav")
    assert clip.clip_id == "neu0023"
    assert clip.label == 1
    assert clip.label_name == "neutral"
    assert clip.stem == "neu0023_1"


def test_parse_clip_name_rejects_mismatched_prefix_and_suffix() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        parse_clip_name("neu0023_2.mp4")


@pytest.mark.parametrize(
    ("label", "expected"),
    [(0, 0.0), (1, 50.0), (2, 100.0)],
)
def test_label_to_score(label: int, expected: float) -> None:
    assert label_to_score(label) == expected


@pytest.mark.parametrize("label", [-1, 3, 1.5, "1", None])
def test_label_to_score_rejects_unknown_labels(label: object) -> None:
    with pytest.raises(ValueError):
        label_to_score(label)  # type: ignore[arg-type]


def test_label_constants_are_consistent() -> None:
    assert LABEL_NAMES == {0: "negative", 1: "neutral", 2: "positive"}
    assert LABEL_SCORES == {0: 0.0, 1: 50.0, 2: 100.0}
