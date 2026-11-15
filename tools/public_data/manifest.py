"""Build the Pri-MCCD manifest used by every public-data experiment.

Expected layout (Pri-MCCD release)::

    <root>/
        negative/*.mp4 + *.wav
        neutral/*.mp4  + *.wav
        positive/*.mp4 + *.wav
        metadata.csv | metadata.json     (optional)

Each manifest row joins one video clip with its same-stem WAV file in the same
class directory.  Missing audio is preserved as ``None`` and reported through a
``RuntimeWarning`` plus ``df.attrs["missing_audio"]``; it is never replaced by
a placeholder file or a fabricated score.

Optional metadata supplies lesson ids and/or splits.  CSV columns are matched
case-insensitively; the identifier may be called ``clip_id``, ``clip``,
``file``, ``filename``, ``name`` or ``video`` (with or without extension), the
lesson column ``lesson_id``, ``lesson``, ``class_id`` or ``class``, the
split column ``split``, ``fold`` or ``partition``, and the temporal order
column ``clip_order``, ``clip_index``, ``window_index``, ``order`` or
``sequence``.  JSON accepts either a list
of such records, ``{"clips": [...]}``, or a mapping ``{clip_id: lesson_id}`` /
``{clip_id: {"lesson_id": ..., "split": ...}}``.

Rows without metadata keep ``lesson_id = None`` and ``split = "unspecified"``
so downstream code can tell "unknown" from an invented value.
"""
from __future__ import annotations

import csv
import json
import warnings
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from .labels import LABEL_NAMES, ClimateClipName, parse_clip_name

MANIFEST_COLUMNS = [
    "clip_id",
    "label",
    "label_name",
    "video_path",
    "audio_path",
    "lesson_id",
    "split",
    "clip_order",
]

#: Value used when no split metadata is available.
DEFAULT_SPLIT = "unspecified"

_CLASS_DIRECTORIES: dict[str, int] = {
    "negative": 0,
    "neutral": 1,
    "positive": 2,
    "neg": 0,
    "neu": 1,
    "pos": 2,
}

_METADATA_CANDIDATES = ("metadata.csv", "metadata.json")

_IDENTIFIER_KEYS = ("clip_id", "clip", "file", "filename", "name", "video")
_LESSON_KEYS = ("lesson_id", "lesson", "class_id", "class")
_SPLIT_KEYS = ("split", "fold", "partition")
_ORDER_KEYS = ("clip_order", "clip_index", "window_index", "order", "sequence")


def build_manifest(
    root: str | Path,
    *,
    metadata_path: str | Path | None = None,
    expected_counts: Mapping[int | str, int] | None = None,
) -> pd.DataFrame:
    """Walk ``root`` and return a validated clip manifest.

    Args:
        root: Dataset root containing ``negative/``, ``neutral/`` and
            ``positive/`` subdirectories.
        metadata_path: Optional CSV/JSON file with lesson ids and splits.  When
            omitted, ``<root>/metadata.csv`` or ``<root>/metadata.json`` is
            used automatically if present.
        expected_counts: Optional per-label counts (keys may be 0/1/2 or
            ``negative``/``neutral``/``positive``).  A mismatch raises
            ``ValueError``.

    Returns:
        A DataFrame with :data:`MANIFEST_COLUMNS`, deterministically sorted.
        ``df.attrs["label_counts"]`` and ``df.attrs["missing_audio"]`` record
        the validation summary.

    Raises:
        FileNotFoundError: if ``root`` or an explicit metadata file is missing.
        ValueError: if no clips are found, a filename is invalid, a clip sits
            in the wrong class directory, metadata is malformed, or
            ``expected_counts`` does not match.
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"Pri-MCCD root does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"Pri-MCCD root is not a directory: {root}")

    metadata = _load_metadata(_resolve_metadata_path(root, metadata_path))

    records: list[dict[str, Any]] = []
    missing_audio: list[str] = []

    for directory in sorted(
        (p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower()
    ):
        directory_label = _CLASS_DIRECTORIES.get(directory.name.lower())
        if directory_label is None:
            continue

        audio_index = _index_audio(directory)
        videos = sorted(
            (
                p
                for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() == ".mp4"
            ),
            key=lambda p: p.name.lower(),
        )

        for video in videos:
            clip = parse_clip_name(video)
            if clip.label != directory_label:
                raise ValueError(
                    f"Directory/label mismatch for {video}: directory "
                    f"{directory.name!r} implies {LABEL_NAMES[directory_label]!r} "
                    f"but the filename implies {clip.label_name!r}"
                )

            audio = audio_index.get(clip.stem.lower())
            if audio is None:
                missing_audio.append(clip.clip_id)

            record: dict[str, Any] = {
                "clip_id": clip.clip_id,
                "label": clip.label,
                "label_name": clip.label_name,
                "video_path": str(video),
                "audio_path": str(audio) if audio is not None else None,
                "lesson_id": None,
                "split": DEFAULT_SPLIT,
                "clip_order": None,
            }
            meta = _metadata_for(metadata, clip)
            if meta is not None:
                record["lesson_id"] = meta.get("lesson_id")
                record["split"] = meta.get("split") or DEFAULT_SPLIT
                record["clip_order"] = meta.get("clip_order")
            records.append(record)

    if not records:
        raise ValueError(
            f"No .mp4 clips found under {root}; expected negative/, "
            "neutral/ and positive/ subdirectories"
        )

    records.sort(key=lambda row: (row["label"], row["clip_id"]))
    manifest = pd.DataFrame.from_records(records, columns=MANIFEST_COLUMNS)

    label_counts = {
        LABEL_NAMES[label]: int((manifest["label"] == label).sum())
        for label in LABEL_NAMES
    }
    manifest.attrs["label_counts"] = label_counts
    manifest.attrs["missing_audio"] = missing_audio
    manifest.attrs["root"] = str(root)

    if expected_counts is not None:
        _validate_expected_counts(expected_counts, label_counts)

    if missing_audio:
        preview = ", ".join(missing_audio[:5])
        if len(missing_audio) > 5:
            preview += ", ..."
        warnings.warn(
            f"{len(missing_audio)} Pri-MCCD clip(s) missing a matching .wav "
            f"file: {preview}",
            RuntimeWarning,
            stacklevel=2,
        )

    return manifest


def _resolve_metadata_path(root: Path, explicit: str | Path | None) -> Path | None:
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise FileNotFoundError(f"Metadata file does not exist: {path}")
        return path
    for name in _METADATA_CANDIDATES:
        candidate = root / name
        if candidate.exists():
            return candidate
    return None


def _index_audio(directory: Path) -> dict[str, Path]:
    """Map lowercase WAV stems to paths within one class directory."""
    index: dict[str, Path] = {}
    for path in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and path.suffix.lower() == ".wav":
            index.setdefault(path.stem.lower(), path)
    return index


def _metadata_for(
    metadata: Mapping[str, Mapping[str, str | None]], clip: ClimateClipName
) -> Mapping[str, str | None] | None:
    for key in (clip.stem.lower(), clip.clip_id.lower()):
        if key in metadata:
            return metadata[key]
    return None


def _load_metadata(
    path: Path | None,
) -> dict[str, dict[str, str | None]]:
    if path is None:
        return {}

    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = _json_rows(raw, path)
    else:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"Metadata CSV has no header: {path}")
            rows = list(reader)

    return _index_metadata_rows(rows, path)


def _json_rows(raw: Any, path: Path) -> list[Mapping[str, Any]]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("clips"), list):
            return raw["clips"]
        rows: list[Mapping[str, Any]] = []
        for identifier, value in raw.items():
            if isinstance(value, dict):
                rows.append({"clip_id": identifier, **value})
            else:
                rows.append({"clip_id": identifier, "lesson_id": value})
        return rows
    raise ValueError(
        f"Unsupported metadata JSON in {path}: expected a list or an object"
    )


def _index_metadata_rows(
    rows: Iterable[Mapping[str, Any]], path: Path
) -> dict[str, dict[str, str | None]]:
    index: dict[str, dict[str, str | None]] = {}
    for row in rows:
        normalized = {
            str(key).strip().lower(): value
            for key, value in row.items()
            if key is not None
        }
        identifier = _first_value(normalized, _IDENTIFIER_KEYS)
        if identifier is None:
            raise ValueError(
                f"Metadata row in {path} has no clip identifier: {dict(row)!r}"
            )

        fields: dict[str, str | None] = {}
        lesson_id = _first_value(normalized, _LESSON_KEYS)
        if lesson_id is not None:
            fields["lesson_id"] = lesson_id
        split = _first_value(normalized, _SPLIT_KEYS)
        if split is not None:
            fields["split"] = split
        order = _first_value(normalized, _ORDER_KEYS)
        if order is not None:
            fields["clip_order"] = order

        for alias in _identifier_aliases(identifier):
            index.setdefault(alias, fields)
    return index


def _first_value(
    row: Mapping[str, Any], keys: Iterable[str]
) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _identifier_aliases(identifier: str) -> set[str]:
    raw = identifier.strip().lower()
    stem = Path(raw).stem
    aliases = {raw, stem}
    try:
        aliases.add(parse_clip_name(stem).clip_id.lower())
    except ValueError:
        pass
    return aliases


def _validate_expected_counts(
    expected: Mapping[int | str, int], actual: Mapping[str, int]
) -> None:
    normalized: dict[str, int] = {}
    for key, value in expected.items():
        if isinstance(key, str):
            name = key.strip().lower()
            if name in LABEL_NAMES.values():
                normalized[name] = int(value)
            elif name.isdigit() and int(name) in LABEL_NAMES:
                normalized[LABEL_NAMES[int(name)]] = int(value)
            else:
                raise ValueError(f"Unknown label in expected_counts: {key!r}")
        elif key in LABEL_NAMES:
            normalized[LABEL_NAMES[key]] = int(value)
        else:
            raise ValueError(f"Unknown label in expected_counts: {key!r}")

    mismatches = [
        f"{name}: expected {expected_count}, found {actual.get(name, 0)}"
        for name, expected_count in normalized.items()
        if actual.get(name, 0) != expected_count
    ]
    if mismatches:
        raise ValueError("Label count mismatch: " + "; ".join(mismatches))


__all__ = ["DEFAULT_SPLIT", "MANIFEST_COLUMNS", "build_manifest"]
