"""Pri-MCCD climate-label parsing.

Pri-MCCD clip names follow the convention documented with the dataset:

    <prefix><four-digit-id>_<suffix>.<ext>      e.g. neu0023_1.mp4

Both the prefix and the suffix encode the climate label, and they must agree:

    neg -> negative (0)      _0 -> negative (0)
    neu -> neutral  (1)      _1 -> neutral  (1)
    pos -> positive (2)      _2 -> positive (2)

A prefix/suffix disagreement is a data-integrity error, not something to guess
around, so :func:`parse_clip_name` raises ``ValueError`` instead of silently
preferring one encoding.  Labels map to the ordinal scores 0 / 50 / 100 used
throughout the experiment scripts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

LABEL_NAMES: dict[int, str] = {
    0: "negative",
    1: "neutral",
    2: "positive",
}

LABEL_SCORES: dict[int, float] = {
    0: 0.0,
    1: 50.0,
    2: 100.0,
}

_PREFIX_LABELS: dict[str, int] = {"neg": 0, "neu": 1, "pos": 2}
_SUFFIX_LABELS: dict[str, int] = {"0": 0, "1": 1, "2": 2}

#: ``<prefix><four digits>_<label digit>`` — matched against the file stem.
_CLIP_NAME_RE = re.compile(
    r"^(?P<prefix>neg|neu|pos)(?P<identifier>\d{4})_(?P<suffix>[012])$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClimateClipName:
    """Parsed components of a Pri-MCCD clip filename."""

    clip_id: str
    label: int
    label_name: str
    stem: str


def parse_clip_name(filename: str | Path) -> ClimateClipName:
    """Parse a Pri-MCCD filename into clip id, label and label name.

    ``filename`` may be a bare name, a full path, or a ``pathlib.Path``; any
    extension is ignored.  Matching is case-insensitive.

    Raises:
        ValueError: if the name does not follow the Pri-MCCD convention or if
            the prefix and suffix encode different labels.
    """
    stem = Path(filename).stem
    match = _CLIP_NAME_RE.fullmatch(stem)
    if match is None:
        raise ValueError(f"Invalid Pri-MCCD clip name: {filename!r}")

    prefix = match.group("prefix").lower()
    suffix = match.group("suffix")
    prefix_label = _PREFIX_LABELS[prefix]
    suffix_label = _SUFFIX_LABELS[suffix]
    if prefix_label != suffix_label:
        raise ValueError(
            "Pri-MCCD label mismatch in "
            f"{filename!r}: prefix {prefix!r} means "
            f"{LABEL_NAMES[prefix_label]!r} but suffix _{suffix} means "
            f"{LABEL_NAMES[suffix_label]!r}"
        )

    return ClimateClipName(
        clip_id=f"{prefix}{match.group('identifier')}",
        label=prefix_label,
        label_name=LABEL_NAMES[prefix_label],
        stem=stem,
    )


def parse_climate_label(filename: str | Path) -> int:
    """Return the ordinal climate label (0/1/2) encoded in a clip filename."""
    return parse_clip_name(filename).label


def label_name(label: int) -> str:
    """Return the canonical label name for an ordinal label."""
    _validate_label(label)
    return LABEL_NAMES[label]


def label_to_score(label: int) -> float:
    """Map an ordinal climate label to the 0-100 score used by fusion.

    ``0 -> 0`` (negative), ``1 -> 50`` (neutral), ``2 -> 100`` (positive).
    """
    _validate_label(label)
    return LABEL_SCORES[label]


def _validate_label(label: object) -> None:
    if label not in LABEL_NAMES:
        raise ValueError(
            f"Unknown climate label {label!r}; expected one of {sorted(LABEL_NAMES)}"
        )


__all__ = [
    "LABEL_NAMES",
    "LABEL_SCORES",
    "ClimateClipName",
    "label_name",
    "label_to_score",
    "parse_climate_label",
    "parse_clip_name",
]
