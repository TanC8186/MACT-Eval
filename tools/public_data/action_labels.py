"""Versioned human annotation schema for frame-local classroom action evidence.

Scope reminder, and it applies to every number derived from these rows:

* an evidence frame is **one sampled instant** of a lesson;
* ``target-N`` is the position of a box inside that single frame — the
  detector sorts boxes by ``(bbox.x, bbox.y)`` and then numbers them 1..N, so
  the index carries no identity across frames and no track id;
* a row here is one judgement about one box in one frame, therefore the row
  count is **not** an action-event count, a student count, or a hand-raise
  count.

The action codes below match the supported detector vocabulary
(``ai-service/app/models/action_evidence.py``, ``ACTION_LABELS``). Actual
capabilities come from the loaded checkpoint's declared classes.

Schema ``action-annotation/1.0`` — CSV header (or a JSON list of the same
objects)::

    lesson_id, window_index, frame_id, timestamp_ms, target_index,
    action_code, present, visibility, annotator, annotated_at, notes

``frame_id`` is the join key back to an evidence frame.  When the annotation
is exported from the ai-service payload, use :func:`evidence_frame_key`; when
it is exported from ``behavior_evidence_frames``, use that table's ``id``.
Both sides of a join must use the same convention.

``target_index`` is ``0`` only for an instance the annotator saw but the model
did **not** box (a missed detection); such a row must say ``present=1`` and
carry a ``notes`` description, and at most one such row may exist per
``(frame_id, action_code)``.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = "action-annotation/1.0"

#: Canonical ``code`` values emitted by the behaviour checkpoint.
ACTION_CODES: tuple[str, ...] = (
    "hand_raising",
    "reading",
    "writing",
    "discussing",
    "bow_head",
    "turn_head",
    "using_phone",
    "leaning_over_table",
    "standing",
    "leave_seat",
)

#: Chinese labels the evidence frames carry (``detection["label"]``).
ACTION_LABELS_ZH: dict[str, str] = {
    "hand_raising": "举手",
    "reading": "阅读",
    "writing": "书写",
    "discussing": "讨论",
    "bow_head": "低头",
    "turn_head": "转头",
    "using_phone": "使用手机",
    "leaning_over_table": "趴桌",
    "standing": "站立",
    "leave_seat": "离座",
}

#: Accepted spellings -> canonical code.  The detector's input aliases use
#: hyphens (``hand-raising``, ``bow-head``); the emitted code uses underscores.
ACTION_ALIASES: dict[str, str] = {
    "hand-raising": "hand_raising",
    "handraising": "hand_raising",
    "raise_hand": "hand_raising",
    "bow-head": "bow_head",
    "bowhead": "bow_head",
    "turn-head": "turn_head",
    "turnhead": "turn_head",
    "using phone": "using_phone",
    "leaning over the table": "leaning_over_table",
    "leave-seat": "leave_seat",
    "leaveseat": "leave_seat",
}

#: Allowed ``visibility`` values, worst case last.
VISIBILITY_VALUES: tuple[str, ...] = ("clear", "partial", "occluded")

VISIBILITY_ALIASES: dict[str, str] = {
    "清晰": "clear",
    "清楚": "clear",
    "部分遮挡": "partial",
    "partially_occluded": "partial",
    "partially-occluded": "partial",
    "遮挡": "occluded",
    "完全遮挡": "occluded",
}

REQUIRED_COLUMNS: tuple[str, ...] = (
    "lesson_id",
    "window_index",
    "frame_id",
    "timestamp_ms",
    "target_index",
    "action_code",
    "present",
    "visibility",
    "annotator",
    "annotated_at",
)
OPTIONAL_COLUMNS: tuple[str, ...] = ("notes",)
ALL_COLUMNS: tuple[str, ...] = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "t", "是"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "f", "否"})


def evidence_frame_key(lesson_id: str, window_index: int, code: str) -> str:
    """Deterministic evidence-frame id for payloads that do not carry a DB id.

    Matches the object-name layout used when frames are uploaded
    (``lessons/<lesson>/jobs/<job>/window-0042-hand_raising.jpg``) but is
    scoped to a lesson rather than a job, because annotation is not per-job.
    """
    return f"{lesson_id}:w{int(window_index):04d}:{code}"


# --------------------------------------------------------------------------
# Loading and normalisation
# --------------------------------------------------------------------------


def load_annotations(path: str | Path) -> list[dict[str, Any]]:
    """Read a CSV or JSON annotation sheet into normalised row dicts.

    Loading is lenient: unparseable numbers, unknown codes and out-of-range
    values are kept as ``None``/verbatim text so that :func:`validate` can
    report them together instead of failing on the first bad row.  A missing
    required column is a structural error and raises ``ValueError``.

    Returns:
        One dict per row with parsed fields plus a ``raw`` sub-dict holding the
        original text (used by :func:`validate` to tell "blank" from "bad").
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"annotation sheet not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        items = _read_json(path)
    elif suffix == ".csv":
        items = _read_csv(path)
    else:
        raise ValueError(f"unsupported annotation file type: {path.suffix!r}")
    return [_normalise_row(item, index) for index, item in enumerate(items)]


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
        if missing:
            raise ValueError(f"{path} missing columns: {missing}")
        return [dict(row) for row in reader]


def _read_json(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        for key in ("annotations", "rows", "records"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            raise ValueError(
                f"{path}: JSON object must contain an 'annotations' list"
            )
    if not isinstance(payload, list):
        raise ValueError(f"{path}: annotation JSON must be a list of objects")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: every annotation must be an object")
        rows.append(item)
    return rows


def _normalise_row(item: dict[str, Any], index: int) -> dict[str, Any]:
    raw = {name: _text(item.get(name)) for name in ALL_COLUMNS}
    return {
        "row_index": index,
        "lesson_id": raw["lesson_id"],
        "window_index": _parse_int(item.get("window_index")),
        "frame_id": raw["frame_id"],
        "timestamp_ms": _parse_int(item.get("timestamp_ms")),
        "target_index": _parse_int(item.get("target_index")),
        "action_code": canonical_action(item.get("action_code")),
        "present": _parse_present(item.get("present")),
        "visibility": canonical_visibility(item.get("visibility")),
        "annotator": raw["annotator"],
        "annotated_at": raw["annotated_at"],
        "notes": raw["notes"],
        "raw": raw,
    }


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _parse_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else None


def _parse_present(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    text = _text(value).lower()
    if text in _TRUE_VALUES:
        return 1
    if text in _FALSE_VALUES:
        return 0
    return None


def canonical_action(value: Any) -> str:
    """Map a checkpoint class spelling to the canonical action code.

    ``hand-raising``/``hand_raising``/``handraising`` all become
    ``hand_raising``; anything not in :data:`ACTION_CODES` is returned
    normalised-but-unknown so :func:`validate` can report it.
    """
    text = _text(value).lower()
    if not text:
        return ""
    text = ACTION_ALIASES.get(text, text)
    return text.replace(" ", "_").replace("-", "_")


def canonical_visibility(value: Any) -> str:
    """Map a visibility spelling (English or Chinese) to the allowed value."""
    text = _text(value).lower()
    if not text:
        return ""
    return VISIBILITY_ALIASES.get(text, text)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return structured errors for an annotation sheet (empty list = clean).

    Each error is ``{"row_index", "field", "code", "message"}``.  Every row is
    checked; validation never stops at the first problem, because a labelling
    session usually has one systematic mistake rather than one bad row.
    """
    errors: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, int, str], int] = {}

    for row in rows:
        index = int(row.get("row_index", 0))
        raw = row.get("raw") or {}

        def add(field: str, code: str, message: str) -> None:
            errors.append(
                {
                    "row_index": index,
                    "field": field,
                    "code": code,
                    "message": message,
                }
            )

        if not row.get("lesson_id"):
            add("lesson_id", "missing_lesson_id", "lesson_id is empty")
        if not row.get("frame_id"):
            add("frame_id", "missing_frame_id", "frame_id is empty")

        for field, lower_bound in (
            ("window_index", 0),
            ("timestamp_ms", 0),
            ("target_index", 0),
        ):
            text = _text(raw.get(field))
            value = row.get(field)
            if not text:
                add(
                    field,
                    "missing_" + field,
                    f"{field} is required but blank",
                )
            elif value is None:
                add(field, "bad_integer", f"{field}={text!r} is not an integer")
            elif value < lower_bound:
                add(
                    field,
                    "negative_" + field,
                    f"{field}={value} must be >= {lower_bound}",
                )

        code = row.get("action_code") or ""
        if not code:
            add("action_code", "missing_action_code", "action_code is empty")
        elif code not in ACTION_CODES:
            add(
                "action_code",
                "unknown_action_code",
                f"{code!r} is not emitted by the behaviour checkpoint "
                f"(allowed: {list(ACTION_CODES)})",
            )

        present_text = _text(raw.get("present"))
        present = row.get("present")
        if not present_text:
            add("present", "missing_present", "present is required but blank")
        elif present is None:
            add(
                "present",
                "invalid_present",
                f"present={present_text!r} must be one of 1/0",
            )

        visibility = row.get("visibility") or ""
        if not visibility:
            add(
                "visibility",
                "missing_visibility",
                "visibility is empty",
            )
        elif visibility not in VISIBILITY_VALUES:
            add(
                "visibility",
                "invalid_visibility",
                f"visibility={visibility!r} not in {list(VISIBILITY_VALUES)}",
            )

        if not row.get("annotator"):
            add("annotator", "missing_annotator", "annotator is empty")
        annotated_at = row.get("annotated_at") or ""
        if not annotated_at:
            add("annotated_at", "missing_annotated_at", "annotated_at is empty")
        elif not _is_iso_timestamp(annotated_at):
            add(
                "annotated_at",
                "bad_annotated_at",
                f"annotated_at={annotated_at!r} is not ISO-8601",
            )

        target_index = row.get("target_index")
        if target_index == 0:
            if present is not None and present != 1:
                add(
                    "target_index",
                    "unboxed_requires_positive",
                    "target_index=0 marks an unboxed instance and must have "
                    "present=1",
                )
            if not row.get("notes"):
                add(
                    "notes",
                    "unboxed_requires_note",
                    "target_index=0 must describe the missed instance in notes",
                )

        key = (
            str(row.get("annotator") or ""),
            str(row.get("frame_id") or ""),
            int(target_index) if isinstance(target_index, int) else -1,
            code,
        )
        if all(key[i] not in ("", -1) for i in range(4)):
            if key in seen:
                add(
                    "target_index",
                    "duplicate_annotation",
                    f"duplicate of row {seen[key]} for "
                    f"(annotator={key[0]}, frame={key[1]}, "
                    f"target={key[2]}, action={key[3]})",
                )
            else:
                seen[key] = index

    errors.sort(key=lambda item: (item["row_index"], item["field"], item["code"]))
    return errors


def _is_iso_timestamp(text: str) -> bool:
    value = text.strip().replace("Z", "+00:00")
    try:
        _dt.datetime.fromisoformat(value)
        return True
    except ValueError:
        pass
    try:
        _dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Inter-annotator agreement
# --------------------------------------------------------------------------


def cohen_kappa(
    a: Iterable[int | None],
    b: Iterable[int | None],
) -> dict[str, Any]:
    """Cohen's kappa for two aligned binary label sequences.

    ``None`` entries are dropped pairwise (an annotator who left a box
    unlabelled has not made a claim about it).

    Returns ``{"n", "kappa", "observed_agreement", "expected_agreement",
    "note"}``.  ``kappa`` is ``None`` — never silently 1.0 — when it is
    mathematically undefined:

    * no double-labelled pairs (``n == 0``);
    * both annotators used one and the same single category, so expected
      agreement is 1 and the denominator is 0 (perfect agreement, zero
      information).
    """
    left = list(a)
    right = list(b)
    if len(left) != len(right):
        raise ValueError(
            f"label sequences must be aligned: {len(left)} != {len(right)}"
        )
    for value in left + right:
        if value is not None and value not in (0, 1):
            raise ValueError(f"labels must be 0/1/None, got {value!r}")

    pairs = [
        (int(x), int(y))
        for x, y in zip(left, right)
        if x is not None and y is not None
    ]
    n = len(pairs)
    if n == 0:
        return {
            "n": 0,
            "kappa": None,
            "observed_agreement": None,
            "expected_agreement": None,
            "note": "no double-labelled pairs",
        }

    observed = sum(1 for x, y in pairs if x == y) / n
    p_left = sum(x for x, _ in pairs) / n
    p_right = sum(y for _, y in pairs) / n
    expected = p_left * p_right + (1 - p_left) * (1 - p_right)
    if expected >= 1.0:
        return {
            "n": n,
            "kappa": None,
            "observed_agreement": observed,
            "expected_agreement": expected,
            "note": "undefined: both annotators used a single identical category",
        }
    return {
        "n": n,
        "kappa": (observed - expected) / (1 - expected),
        "observed_agreement": observed,
        "expected_agreement": expected,
        "note": None,
    }


def annotators(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Sorted annotator names present in a sheet."""
    return sorted({str(row.get("annotator") or "") for row in rows} - {""})


def kappa_per_action(
    rows: Sequence[dict[str, Any]],
    annotator_a: str | None = None,
    annotator_b: str | None = None,
) -> dict[str, Any]:
    """Cohen's kappa per action code over the pairs both annotators labelled.

    Pairs are joined on ``(frame_id, target_index)`` within each action code,
    so an annotator who skipped a box only removes that one pair.
    """
    names = annotators(rows)
    if annotator_a is None or annotator_b is None:
        if len(names) != 2:
            raise ValueError(
                "exactly two annotators are required to compute kappa; "
                f"found {names!r} — pass annotator_a/annotator_b explicitly"
            )
        annotator_a, annotator_b = names
    if annotator_a == annotator_b:
        raise ValueError("annotator_a and annotator_b must differ")

    index: dict[str, dict[tuple[str, int], int]] = {
        annotator_a: {},
        annotator_b: {},
    }
    for row in rows:
        name = row.get("annotator")
        if name not in index:
            continue
        present = row.get("present")
        target = row.get("target_index")
        frame = row.get("frame_id")
        if present is None or target is None or not frame:
            continue
        index[name][(str(frame), int(target), str(row.get("action_code")))] = int(
            present
        )

    per_action: dict[str, dict[str, Any]] = {}
    for code in ACTION_CODES:
        keys = sorted(
            key
            for key in index[annotator_a].keys() & index[annotator_b].keys()
            if key[2] == code
        )
        result = cohen_kappa(
            [index[annotator_a][key] for key in keys],
            [index[annotator_b][key] for key in keys],
        )
        result["label_zh"] = ACTION_LABELS_ZH[code]
        per_action[code] = result

    return {
        "annotator_a": annotator_a,
        "annotator_b": annotator_b,
        "per_action": per_action,
        "n_pairs_total": sum(item["n"] for item in per_action.values()),
    }


__all__ = [
    "ACTION_ALIASES",
    "ACTION_CODES",
    "ACTION_LABELS_ZH",
    "ALL_COLUMNS",
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "SCHEMA_VERSION",
    "VISIBILITY_ALIASES",
    "VISIBILITY_VALUES",
    "annotators",
    "canonical_action",
    "canonical_visibility",
    "cohen_kappa",
    "evidence_frame_key",
    "kappa_per_action",
    "load_annotations",
    "validate",
]
