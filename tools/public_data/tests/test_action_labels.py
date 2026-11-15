"""Tests for the frame-local action annotation schema and the evaluation CLI.

The fixture built here is **SYNTHETIC — 不是真实课堂标注**.  It exists only to
prove that loading, joining, metrics, calibration, bootstrap and the CLI run
end to end.  Every number it produces is fabricated and must never be quoted
as a property of the deployed behaviour model.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from public_data import action_labels
from public_data.action_labels import (
    ACTION_CODES,
    ALL_COLUMNS,
    cohen_kappa,
    evidence_frame_key,
    kappa_per_action,
    load_annotations,
    validate,
)
from public_data.evaluate_actions import (



    _percentile,
    binary_metrics,
    bootstrap_f1,
    build_pairs,
    calibration,
    confidence_sweep,
    evaluate,
    load_predictions,
    main,
    render_markdown,
    select_annotator,
)

#: 测试临时目录必须落在项目内（系统 TEMP 在 C 盘，本项目禁止写那里）。
#: 这些夹具只写几 KB 的 CSV/JSON，但"小"不是理由——约束是位置不是大小。
_TMP_ROOT = Path(__file__).resolve().parents[3] / ".cache" / "tmp" / "public-data-tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

LESSONS = ("jingyesi", "daqi_yuqiang", "qiutian_de_yu", "zhiliang_midu")
ANNOTATED_AT = "2026-10-28T10:00:00"
#: Detector confidences cycled across synthetic detections; chosen to spread
#: across calibration bins rather than to describe any real model.
CONFIDENCE_CYCLE = (0.92, 0.61, 0.35, 0.48, 0.77, 0.88, 0.55, 0.42, 0.96, 0.31, 0.67, 0.83)
VISIBILITY_CYCLE = ("clear", "partial", "occluded")


def synthetic_fixture() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return ``(annotation_rows, prediction_frames)`` for 12 synthetic frames.

    Layout: 4 lessons x 3 windows.  Every frame annotates ``hand_raising`` on
    three boxes; even frames add ``reading`` on box 1; frames 2 and 7 add
    ``writing`` on box 2; box 0 of lesson 4 records an instance the model
    never boxed.  Present/absent and detected/undetected are decided by index
    arithmetic so the fixture is byte-identical on every run.
    """
    rows: list[dict[str, object]] = []
    frames: list[dict[str, object]] = []

    def frame_id(lesson: str, window: int, code: str) -> str:
        return evidence_frame_key(lesson, window, code)

    def add_row(**values: object) -> None:
        row = {column: "" for column in ALL_COLUMNS}
        row.update(
            annotator="synth-A",
            annotated_at=ANNOTATED_AT,
            notes="SYNTHETIC — 不是真实课堂标注",
        )
        row.update(values)
        rows.append(row)

    for index in range(12):
        lesson = LESSONS[index // 3]
        window = index % 3
        trigger = "hand_raising" if index % 2 == 0 else "reading"
        fid = frame_id(lesson, window, trigger)
        detections = []

        for target in (1, 2, 3):
            seed = index * 3 + target
            present = 1 if seed % 3 != 0 else 0
            add_row(
                lesson_id=lesson,
                window_index=window,
                frame_id=fid,
                timestamp_ms=window * 60_000 + target * 1_000,
                target_index=target,
                action_code="hand_raising",
                present=present,
                visibility=VISIBILITY_CYCLE[(index + target) % 3],
            )
            # A present action is usually but not always detected; an absent
            # one is occasionally a false positive.
            if present and seed % 7 != 0:
                confidence = CONFIDENCE_CYCLE[seed % len(CONFIDENCE_CYCLE)]
                detections.append(
                    {
                        "subjectId": f"target-{target}",
                        "code": "hand_raising",
                        "label": "举手",
                        "confidence": confidence,
                        "bbox": [0.1 * target, 0.2, 0.08, 0.3],
                    }
                )
            elif not present and seed % 5 == 0:
                confidence = CONFIDENCE_CYCLE[seed % len(CONFIDENCE_CYCLE)]
                detections.append(
                    {
                        "subjectId": f"target-{target}",
                        "code": "hand_raising",
                        "label": "举手",
                        "confidence": confidence,
                        "bbox": [0.1 * target, 0.2, 0.08, 0.3],
                    }
                )

        if index % 2 == 0:
            add_row(
                lesson_id=lesson,
                window_index=window,
                frame_id=fid,
                timestamp_ms=window * 60_000 + 500,
                target_index=1,
                action_code="reading",
                present=1 if index % 4 == 0 else 0,
                visibility="clear",
            )
            if index % 4 == 0:
                detections.append(
                    {
                        "subjectId": "target-1",
                        "code": "reading",
                        "label": "阅读",
                        "confidence": 0.72,
                        "bbox": [0.1, 0.2, 0.08, 0.3],
                    }
                )

        if index in (2, 7):
            add_row(
                lesson_id=lesson,
                window_index=window,
                frame_id=fid,
                timestamp_ms=window * 60_000 + 700,
                target_index=2,
                action_code="writing",
                present=1,
                visibility="partial",
            )
            detections.append(
                {
                    "subjectId": "target-2",
                    "code": "writing",
                    "label": "书写",
                    "confidence": 0.44,
                    "bbox": [0.2, 0.2, 0.08, 0.3],
                }
            )

        if index == 9:
            # An instance the model never boxed: recall denominator only.
            add_row(
                lesson_id=lesson,
                window_index=window,
                frame_id=fid,
                timestamp_ms=window * 60_000 + 900,
                target_index=0,
                action_code="hand_raising",
                present=1,
                visibility="occluded",
                notes="SYNTHETIC — 未框出的举手实例（不是真实标注）",
            )

        frames.append(
            {
                "lesson_id": lesson,
                "window_index": window,
                "timestamp_ms": window * 60_000,
                "code": trigger,
                "label": "举手" if trigger == "hand_raising" else "阅读",
                "metadata": {
                    "detectionSource": "fixed_timestamp_sample",
                    "modelVersion": "synthetic/best",
                    "modelDigest": "0" * 64,
                    "identityScope": "frame_local",
                    "detections": detections,
                },
            }
        )

    rows.sort(
        key=lambda row: (
            str(row["frame_id"]),
            str(row["action_code"]),
            int(row["target_index"]),  # type: ignore[arg-type]
        )
    )
    return rows, frames


def write_fixture(
    directory: Path,
) -> tuple[Path, Path]:
    """Write the synthetic sheet and prediction payload; return both paths."""
    rows, frames = synthetic_fixture()
    directory.mkdir(parents=True, exist_ok=True)
    annotations = directory / "annotations.csv"
    with annotations.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ALL_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    predictions = directory / "predictions.json"
    predictions.write_text(
        json.dumps(
            {
                "SAMPLE_KIND": "SYNTHETIC — 不是真实课堂标注",
                "frames": frames,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return annotations, predictions


def _row(**overrides: object) -> dict[str, object]:
    base = {
        "lesson_id": "L1",
        "window_index": 0,
        "frame_id": "L1:w0000:hand_raising",
        "timestamp_ms": 1000,
        "target_index": 1,
        "action_code": "hand_raising",
        "present": 1,
        "visibility": "clear",
        "annotator": "A",
        "annotated_at": ANNOTATED_AT,
        "notes": "",
    }
    base.update(overrides)
    return base


def _write_csv(path: Path, rows: list[dict[str, object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ALL_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------
# Schema constants
# --------------------------------------------------------------------------


def test_action_codes_match_the_supported_detector_vocabulary() -> None:
    assert ACTION_CODES == (
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
    assert set(action_labels.ACTION_LABELS_ZH) == set(ACTION_CODES)


@pytest.mark.parametrize("source,expected", [("using phone", "using_phone"),
                                             ("leaning over the table", "leaning_over_table")])
def test_case_checkpoint_class_names_are_accepted_by_annotation_schema(source, expected):
    assert action_labels.canonical_action(source) == expected


def test_evidence_frame_key_is_stable_and_lesson_scoped() -> None:
    assert evidence_frame_key("jingyesi", 3, "hand_raising") == (
        "jingyesi:w0003:hand_raising"
    )


# --------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------


def test_load_annotations_normalises_aliases(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        [
            _row(action_code="hand-raising", visibility="部分遮挡"),
            _row(action_code="bow-head", visibility="部分遮挡", target_index=2),
        ],
    )
    rows = load_annotations(path)

    assert [row["action_code"] for row in rows] == ["hand_raising", "bow_head"]
    assert all(row["visibility"] == "partial" for row in rows)
    assert rows[0]["present"] == 1
    assert validate(rows) == []


def test_load_annotations_reads_json_object(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text(
        json.dumps({"annotations": [_row(present=False)]}), encoding="utf-8"
    )
    rows = load_annotations(path)

    assert len(rows) == 1
    assert rows[0]["present"] == 0


def test_load_annotations_requires_schema_columns(tmp_path: Path) -> None:
    path = tmp_path / "a.csv"
    path.write_text("lesson_id,frame_id\nL1,f\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing columns"):
        load_annotations(path)


def test_load_annotations_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_annotations(tmp_path / "nope.csv")


def test_validate_reports_every_problem_in_one_pass() -> None:
    errors = _validate_from(
        [
            _row(action_code="hand_waving"),          # row 0: unknown code
            _row(action_code="hand_waving"),           # row 1: duplicate of row 0
            _row(present=7, target_index=2),           # row 2: bad present
            _row(timestamp_ms="", target_index=3),     # row 3: missing timestamp
            _row(visibility="hazy", target_index=4),   # row 4: bad visibility
            _row(window_index=-1, target_index=5),     # row 5: negative window
        ]
    )

    codes = {error["code"] for error in errors}
    assert "unknown_action_code" in codes
    assert "duplicate_annotation" in codes
    assert "invalid_present" in codes
    assert "missing_timestamp_ms" in codes
    assert "invalid_visibility" in codes
    assert "negative_window_index" in codes
    # One pass: every row was inspected, not just the first failure.
    assert {error["row_index"] for error in errors} == {0, 1, 2, 3, 4, 5}


def _validate_from(raw_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Validate in-memory rows by round-tripping them through the loader."""
    import tempfile

    with tempfile.TemporaryDirectory(dir=str(_TMP_ROOT)) as directory:
        path = _write_csv(Path(directory) / "a.csv", raw_rows)
        return validate(load_annotations(path))


def test_validate_accepts_a_clean_sheet(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        [_row(), _row(target_index=2, action_code="reading", visibility="occluded")],
    )
    assert validate(load_annotations(path)) == []


def test_unboxed_instance_requires_positive_label_and_note() -> None:
    rows = _validate_from(
        [
            _row(target_index=0, present=0, notes=""),
            _row(target_index=0, present=1, notes="", action_code="reading"),
        ]
    )
    codes = [error["code"] for error in rows]
    assert "unboxed_requires_positive" in codes
    assert "unboxed_requires_note" in codes


def test_missing_annotator_and_timestamp_are_reported() -> None:
    errors = _validate_from([_row(annotator="", annotated_at="not-a-date")])
    fields = {(error["field"], error["code"]) for error in errors}
    assert ("annotator", "missing_annotator") in fields
    assert ("annotated_at", "bad_annotated_at") in fields


# --------------------------------------------------------------------------
# Cohen's kappa
# --------------------------------------------------------------------------


def test_kappa_perfect_agreement_is_one() -> None:
    result = cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0])
    assert result["kappa"] == pytest.approx(1.0)
    assert result["n"] == 4


def test_kappa_single_class_agreement_is_undefined_not_one() -> None:
    result = cohen_kappa([1, 1, 1], [1, 1, 1])
    assert result["kappa"] is None
    assert result["observed_agreement"] == pytest.approx(1.0)
    assert "undefined" in (result["note"] or "")


def test_kappa_without_overlap_is_undefined() -> None:
    result = cohen_kappa([None, None], [1, 0])
    assert result["n"] == 0
    assert result["kappa"] is None


def test_kappa_known_value() -> None:
    # 8 pairs, 6 agree; p_pos_a = 4/8, p_pos_b = 4/8 -> pe = 0.5
    a = [1, 1, 1, 1, 0, 0, 0, 0]
    b = [1, 1, 1, 0, 1, 0, 0, 0]
    result = cohen_kappa(a, b)
    assert result["observed_agreement"] == pytest.approx(6 / 8)
    assert result["expected_agreement"] == pytest.approx(0.5)
    assert result["kappa"] == pytest.approx((0.75 - 0.5) / 0.5)


def test_kappa_pairs_drop_none_independently() -> None:
    result = cohen_kappa([1, None, 0, 1], [1, 1, 0, 0])
    assert result["n"] == 3


def test_kappa_rejects_misaligned_or_invalid_input() -> None:
    with pytest.raises(ValueError):
        cohen_kappa([1, 0], [1])
    with pytest.raises(ValueError):
        cohen_kappa([2], [1])


def test_kappa_per_action_joins_on_frame_and_target(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv",
        [
            _row(annotator="A", target_index=1, present=1),
            _row(annotator="B", target_index=1, present=1),
            _row(annotator="A", target_index=2, present=1),
            _row(annotator="B", target_index=2, present=0),
            _row(annotator="A", action_code="reading", present=1),
            _row(annotator="B", action_code="reading", present=1),
        ],
    )
    summary = kappa_per_action(load_annotations(path))

    assert summary["annotator_a"] == "A"
    assert summary["annotator_b"] == "B"
    assert summary["per_action"]["hand_raising"]["n"] == 2
    assert summary["per_action"]["hand_raising"]["kappa"] == pytest.approx(0.0)
    assert summary["per_action"]["reading"]["kappa"] is None
    assert summary["per_action"]["writing"]["n"] == 0


def test_kappa_per_action_requires_two_named_annotators(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "a.csv", [_row(annotator="A")])
    with pytest.raises(ValueError, match="exactly two annotators"):
        kappa_per_action(load_annotations(path))


# --------------------------------------------------------------------------
# Binary metrics
# --------------------------------------------------------------------------


def _pair(present: int, confidence: float | None, **overrides: object) -> dict[str, object]:
    pair = {
        "frame_id": "f",
        "lesson_id": "L1",
        "window_index": 0,
        "target_index": 1,
        "action": "hand_raising",
        "present": present,
        "visibility": "clear",
        "confidence": confidence,
        "detected": confidence is not None,
        "model_version": "m",
        "model_digest": "d",
    }
    pair.update(overrides)
    return pair


def test_binary_metrics_hand_computed() -> None:
    pairs = [
        _pair(1, 0.9),   # TP
        _pair(1, 0.6),   # TP
        _pair(1, 0.3),   # FN at 0.5 (below threshold)
        _pair(1, None),  # FN (never detected)
        _pair(0, 0.8),   # FP
        _pair(0, 0.2),   # TN
    ]
    metrics = binary_metrics(pairs, 0.5)

    assert (metrics["tp"], metrics["fp"], metrics["fn"], metrics["tn"]) == (2, 1, 2, 1)
    assert metrics["precision"] == pytest.approx(2 / 3)
    assert metrics["recall"] == pytest.approx(2 / 4)
    assert metrics["f1"] == pytest.approx(2 * (2 / 3) * 0.5 / ((2 / 3) + 0.5))
    assert metrics["status"] == "ok"


def test_binary_metrics_zero_positives_is_undefined_not_one() -> None:
    metrics = binary_metrics([_pair(0, 0.1), _pair(0, 0.2)], 0.5)

    assert metrics["n_positives"] == 0
    assert metrics["recall"] is None
    assert metrics["f1"] is None
    assert metrics["precision"] is None  # nothing predicted either
    assert metrics["status"] == "no_positives"


def test_binary_metrics_no_predictions_has_undefined_precision() -> None:
    metrics = binary_metrics([_pair(1, None), _pair(1, 0.1)], 0.5)

    assert metrics["recall"] == pytest.approx(0.0)
    assert metrics["precision"] is None
    assert metrics["f1"] is None
    assert metrics["status"] == "no_predictions"


def test_binary_metrics_zero_division_guard_is_zero_not_nan() -> None:
    # A prediction that is wrong and no positives: precision 0, recall undefined.
    metrics = binary_metrics([_pair(0, 0.9)], 0.5)
    assert metrics["precision"] == pytest.approx(0.0)
    assert metrics["recall"] is None
    assert metrics["f1"] is None


def test_predictions_below_detector_floor_are_marked_unreachable() -> None:
    pairs = [_pair(1, 0.9)]
    sweep = confidence_sweep(pairs, thresholds=(0.2, 0.5), detector_floor=0.4)
    reachable = {row["threshold"]: row["reachable"] for row in sweep}

    assert reachable == {0.2: False, 0.5: True}


def test_sweep_threshold_changes_operating_point() -> None:
    pairs = [_pair(1, 0.9), _pair(1, 0.55), _pair(0, 0.6)]
    sweep = {
        row["threshold"]: row
        for row in confidence_sweep(pairs, thresholds=(0.5, 0.7), detector_floor=None)
    }

    assert sweep[0.5]["tp"] == 2 and sweep[0.5]["fp"] == 1
    assert sweep[0.7]["tp"] == 1 and sweep[0.7]["fp"] == 0
    assert sweep[0.7]["precision"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


def test_calibration_bins_hand_computed() -> None:
    pairs = [
        _pair(1, 0.95),
        _pair(0, 0.95),
        _pair(0, 0.15),
        _pair(0, 0.15),
    ]
    result = calibration(pairs, bins=10)

    filled = [bucket for bucket in result["bins"] if bucket["n"]]
    assert filled[0]["mean_confidence"] == pytest.approx(0.15)
    assert filled[0]["observed_frequency"] == pytest.approx(0.0)
    assert filled[-1]["mean_confidence"] == pytest.approx(0.95)
    assert filled[-1]["observed_frequency"] == pytest.approx(0.5)
    # ECE = 0.5*|0 - 0.15| + 0.5*|0.5 - 0.95| = 0.075 + 0.225
    assert result["ece"] == pytest.approx(0.3)


def test_calibration_counts_undetected_positives_separately() -> None:
    result = calibration([_pair(1, None), _pair(1, 0.6)], bins=5)

    assert result["n_scored"] == 1
    assert result["n_unscored_positives"] == 1


def test_calibration_empty_and_bad_bins() -> None:
    result = calibration([_pair(0, None)], bins=4)
    assert result["ece"] is None
    assert result["n_scored"] == 0

    with pytest.raises(ValueError):
        calibration([], bins=0)


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------


def test_percentile_interpolates() -> None:
    assert _percentile([1.0, 2.0, 3.0], 50) == pytest.approx(2.0)
    assert _percentile([1.0], 97.5) == pytest.approx(1.0)
    assert _percentile([0.0, 10.0], 0) == pytest.approx(0.0)
    assert _percentile([0.0, 10.0], 100) == pytest.approx(10.0)


def test_bootstrap_f1_is_deterministic() -> None:
    pairs = [
        _pair(1, 0.9, lesson_id="L1"),
        _pair(1, 0.2, lesson_id="L1"),
        _pair(0, 0.8, lesson_id="L2"),
        _pair(1, 0.7, lesson_id="L2"),
        _pair(1, 0.6, lesson_id="L3"),
        _pair(0, 0.9, lesson_id="L3"),
    ]
    first = bootstrap_f1(pairs, 0.5, n_boot=200, seed=7)
    second = bootstrap_f1(pairs, 0.5, n_boot=200, seed=7)

    assert first == second
    assert first["n_lessons"] == 3
    assert first["ci_lower"] <= first["point"] <= first["ci_upper"]


def test_bootstrap_f1_widens_when_lessons_disagree() -> None:
    # L_good is perfect, L_bad is all wrong: resampling lessons must move F1.
    pairs = [_pair(1, 0.9, lesson_id="L_good"), _pair(0, 0.05, lesson_id="L_good")]
    pairs += [_pair(1, 0.1, lesson_id="L_bad"), _pair(0, 0.9, lesson_id="L_bad")]
    result = bootstrap_f1(pairs, 0.5, n_boot=300, seed=1)

    assert result["ci_lower"] < result["ci_upper"]


def test_bootstrap_f1_undefined_without_positives() -> None:
    result = bootstrap_f1([_pair(0, 0.9, lesson_id="L1")], 0.5, n_boot=10)
    assert result["point"] is None
    assert result["ci_lower"] is None


# --------------------------------------------------------------------------
# Join
# --------------------------------------------------------------------------


def test_build_pairs_excludes_predictions_on_unannotated_boxes(tmp_path: Path) -> None:
    annotations = load_annotations(
        _write_csv(tmp_path / "a.csv", [_row(target_index=1)])
    )
    frames = [
        {
            "lesson_id": "L1",
            "window_index": 0,
            "code": "hand_raising",
            "metadata": {
                "identityScope": "frame_local",
                "detections": [
                    {"subjectId": "target-1", "code": "hand_raising", "confidence": 0.9},
                    {"subjectId": "target-9", "code": "hand_raising", "confidence": 0.8},
                ],
            },
        }
    ]
    joined = build_pairs(annotations, load_predictions(_payload(tmp_path, frames)))

    keys = {(pair["target_index"], pair["action"]) for pair in joined["pairs"]}
    assert (1, "hand_raising") in keys
    assert (9, "hand_raising") not in keys
    assert joined["n_unannotated_predictions"] == 1


def test_predictions_of_never_judged_actions_are_excluded(tmp_path: Path) -> None:
    # The sheet only ever judges hand_raising, so a turn_head prediction on the
    # same box is not evidence of a false positive.
    annotations = load_annotations(
        _write_csv(tmp_path / "a.csv", [_row(target_index=1, present=0)])
    )
    frames = [
        {
            "lesson_id": "L1",
            "window_index": 0,
            "code": "hand_raising",
            "metadata": {
                "identityScope": "frame_local",
                "detections": [
                    {"subjectId": "target-1", "code": "turn_head", "confidence": 0.9}
                ],
            },
        }
    ]
    joined = build_pairs(annotations, load_predictions(_payload(tmp_path, frames)))

    assert joined["judged_actions"] == ["hand_raising"]
    assert joined["n_skipped_action_predictions"] == 1
    assert [pair["action"] for pair in joined["pairs"]] == ["hand_raising"]
    assert joined["pairs"][0]["confidence"] is None


def test_predictions_reject_non_frame_local_scope(tmp_path: Path) -> None:
    frames = [
        {
            "lesson_id": "L1",
            "window_index": 0,
            "code": "hand_raising",
            "metadata": {"identityScope": "track_id", "detections": []},
        }
    ]
    with pytest.raises(ValueError, match="identityScope"):
        load_predictions(_payload(tmp_path, frames))


def test_predictions_parse_target_subject_ids(tmp_path: Path) -> None:
    frames = [
        {
            "frame_id": "L1:w0000:hand_raising",
            "code": "hand_raising",
            "metadata": {
                "detections": [
                    {"subjectId": "target-3", "code": "hand-raising", "confidence": 0.5}
                ]
            },
        }
    ]
    records = load_predictions(_payload(tmp_path, frames))

    assert records[0]["target_index"] == 3
    assert records[0]["action"] == "hand_raising"


def test_select_annotator_refuses_to_average_two_annotators(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path / "a.csv", [_row(annotator="A"), _row(annotator="B")]
    )
    with pytest.raises(ValueError, match="multiple annotators"):
        select_annotator(load_annotations(path), None)
    chosen, name = select_annotator(load_annotations(path), "B")
    assert name == "B"
    assert len(chosen) == 1


def _payload(directory: Path, frames: list[dict[str, object]]) -> Path:
    path = directory / "pred.json"
    path.write_text(json.dumps({"frames": frames}), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# End-to-end CLI
# --------------------------------------------------------------------------


def test_cli_runs_on_synthetic_fixture(tmp_path: Path) -> None:
    annotations, predictions = write_fixture(tmp_path / "fixture")
    out_dir = tmp_path / "out"

    code = main(
        [
            "--annotations", str(annotations),
            "--predictions", str(predictions),
            "--out-dir", str(out_dir),
            "--n-boot", "100",
        ]
    )

    assert code == 0
    summary = json.loads((out_dir / "action-eval.json").read_text(encoding="utf-8"))
    markdown = (out_dir / "action-eval.md").read_text(encoding="utf-8")

    assert summary["scope"]["unit"] == "frame_local_box"
    assert summary["n_pairs"] > 30
    assert summary["n_positive_annotations"] > 0
    hand = summary["per_action"]["hand_raising"]
    assert hand["metrics"]["status"] == "ok"
    assert hand["metrics"]["n_positives"] > 0
    assert len(hand["sweep"]) > 5
    assert hand["calibration"]["ece"] is not None
    assert set(hand["by_visibility"]) <= {"clear", "partial", "occluded"}
    assert "not a student id" in markdown
    assert "n/a" in markdown


def test_cli_output_is_deterministic(tmp_path: Path) -> None:
    annotations, predictions = write_fixture(tmp_path / "fixture")
    outputs = []
    for name in ("one", "two"):
        out_dir = tmp_path / name
        assert main(
            [
                "--annotations", str(annotations),
                "--predictions", str(predictions),
                "--out-dir", str(out_dir),
                "--n-boot", "50",
                "--seed", "11",
            ]
        ) == 0
        outputs.append((out_dir / "action-eval.json").read_text(encoding="utf-8"))

    assert outputs[0] == outputs[1]


def test_cli_flags_detector_selected_sample_as_non_estimable(tmp_path: Path) -> None:
    # Frames that exist because the model fired cannot contain a miss, so the
    # run must say recall is not estimable rather than print a recall number.
    annotations = _write_csv(
        tmp_path / "a.csv",
        [_row(target_index=1, present=1), _row(target_index=2, present=0)],
    )
    predictions = _payload(
        tmp_path,
        [
            {
                "lesson_id": "L1",
                "window_index": 0,
                "code": "hand_raising",
                "metadata": {
                    "detectionSource": "behavior_model",
                    "identityScope": "frame_local",
                    "detections": [
                        {
                            "subjectId": "target-1",
                            "code": "hand_raising",
                            "confidence": 0.9,
                        }
                    ],
                },
            }
        ],
    )
    out_dir = tmp_path / "out"

    code = main(
        [
            "--annotations", str(annotations),
            "--predictions", str(predictions),
            "--out-dir", str(out_dir),
            "--n-boot", "10",
        ]
    )

    assert code == 0
    summary = json.loads((out_dir / "action-eval.json").read_text(encoding="utf-8"))
    assert summary["recall_estimable"] is False
    assert summary["frame_sources"]["by_source"] == {"behavior_model": 1}
    assert "recall is not estimable" in (out_dir / "action-eval.md").read_text(
        encoding="utf-8"
    )


def test_fixed_timestamp_frames_make_recall_estimable(tmp_path: Path) -> None:
    annotations = _write_csv(tmp_path / "a.csv", [_row(target_index=1, present=1)])
    predictions = _payload(
        tmp_path,
        [
            {
                "lesson_id": "L1",
                "window_index": 0,
                "code": "hand_raising",
                "metadata": {
                    "detectionSource": "fixed_timestamp_sample",
                    "identityScope": "frame_local",
                    "detections": [],
                },
            }
        ],
    )
    out_dir = tmp_path / "out"
    assert main(
        [
            "--annotations", str(annotations),
            "--predictions", str(predictions),
            "--out-dir", str(out_dir),
            "--n-boot", "10",
        ]
    ) == 0
    summary = json.loads((out_dir / "action-eval.json").read_text(encoding="utf-8"))

    assert summary["recall_estimable"] is True
    assert summary["per_action"]["hand_raising"]["metrics"]["recall"] == pytest.approx(0.0)


def test_cli_zero_positive_sheet_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    annotations = _write_csv(
        tmp_path / "zero.csv",
        [_row(present=0, target_index=1), _row(present=0, target_index=2)],
    )
    predictions = _payload(
        tmp_path,
        [
            {
                "lesson_id": "L1",
                "window_index": 0,
                "code": "hand_raising",
                "metadata": {
                    "identityScope": "frame_local",
                    "detections": [
                        {
                            "subjectId": "target-1",
                            "code": "hand_raising",
                            "confidence": 0.8,
                        }
                    ],
                },
            }
        ],
    )

    code = main(
        [
            "--annotations", str(annotations),
            "--predictions", str(predictions),
            "--n-boot", "10",
        ]
    )
    captured = capsys.readouterr()

    assert code != 0
    assert "zero positive annotations" in captured.err
    assert "NOT 1.0" in captured.err
    # The table must not claim a precision of 1.0000 for a sheet with no
    # positive evidence.
    assert "1.0000" not in captured.out


def test_cli_rejects_invalid_annotations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    annotations = _write_csv(tmp_path / "bad.csv", [_row(action_code="hand_waving")])
    predictions = _payload(tmp_path, [])
    code = main(["--annotations", str(annotations), "--predictions", str(predictions)])

    assert code == 2
    assert "unknown_action_code" in capsys.readouterr().err


def test_cli_rejects_missing_files(tmp_path: Path) -> None:
    code = main(
        [
            "--annotations", str(tmp_path / "nope.csv"),
            "--predictions", str(tmp_path / "nope.json"),
        ]
    )
    assert code == 2


def test_evaluate_rejects_empty_join(tmp_path: Path) -> None:
    # Rows whose ``present`` cannot be parsed contribute no pair, so the run
    # must fail instead of reporting 0/0 as if it were a result.
    path = _write_csv(tmp_path / "a.csv", [_row(present="maybe")])
    annotations = load_annotations(path)

    with pytest.raises(ValueError, match="no evaluable"):
        evaluate(annotations, [])


def test_empty_predictions_report_zero_recall_not_a_crash(tmp_path: Path) -> None:
    annotations = load_annotations(
        _write_csv(tmp_path / "a.csv", [_row(target_index=1), _row(target_index=2, present=0)])
    )
    summary = evaluate(annotations, [], n_boot=10)
    hand = summary["per_action"]["hand_raising"]

    assert hand["metrics"]["recall"] == pytest.approx(0.0)
    assert hand["metrics"]["precision"] is None
    assert summary["n_predicted"] == 0


def test_render_markdown_states_scope() -> None:
    annotations, frames = synthetic_fixture()
    summary = evaluate(
        action_labels.load_annotations(_write_rows(annotations)),
        load_predictions(_payload_path(frames)),
        n_boot=20,
    )
    text = render_markdown(summary, title="fixture")
    assert text.startswith("# fixture")
    assert "frame_local_box" in text
    assert "expected calibration error" in text


def _write_rows(rows: list[dict[str, object]]) -> Path:
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="action-rows-"))
    return _write_csv(directory / "a.csv", rows)


def _payload_path(frames: list[dict[str, object]]) -> Path:
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="action-frames-"))
    return _payload(directory, frames)
