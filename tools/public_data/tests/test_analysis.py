"""Tests for the post-hoc analysis helpers."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from public_data.analysis import (
    confusion_tables,
    missing_modality_summary,
    paired_tests,
    per_fold_metrics,
)


def _fold_records():
    y = np.array([0, 1, 2, 0, 1, 2])
    records = []
    for fold, (train, test) in enumerate(
        [(np.array([2, 3, 4, 5]), np.array([0, 1])),
         (np.array([0, 1, 4, 5]), np.array([2, 3])),
         (np.array([0, 1, 2, 3]), np.array([4, 5]))]
    ):
        prediction = y[test].copy()
        if fold == 2:
            prediction[0] = 0
        records.append(
            {
                "fold": fold,
                "train_index": train,
                "test_index": test,
                "y_train": y[train],
                "inner_strategy": "GroupKFold",
                "schemes": {
                    "perfect": {
                        "prediction": y[test],
                        "probabilities": np.eye(3)[y[test]],
                        "weights": (0.5, 0.5),
                        "thresholds": (0.5, 1.5),
                    },
                    "worse": {
                        "prediction": prediction,
                        "probabilities": np.eye(3)[prediction],
                        "weights": (0.5, 0.5),
                        "thresholds": (0.5, 1.5),
                    },
                },
            }
        )
    return y, records


def test_per_fold_metrics_has_one_row_per_fold_and_scheme() -> None:
    y, records = _fold_records()
    table = per_fold_metrics(records, y, ["perfect", "worse"])
    assert len(table) == 6
    assert set(table["scheme"]) == {"perfect", "worse"}
    # Each fold contains only two of the three classes; protocol macro-F1 is
    # over all three labels, so a perfectly predicted two-class fold is 2/3.
    assert table.query("scheme == 'perfect'")["macro_f1"].eq(2 / 3).all()


def test_confusion_and_per_class_tables() -> None:
    y = np.array([0, 0, 1, 1, 2, 2])
    predictions = {"perfect": y.copy(), "bad": np.array([0, 1, 1, 1, 2, 2])}
    confusion, per_class = confusion_tables(y, predictions)
    assert len(confusion) == 2 * 3 * 3
    bad = confusion.query("scheme == 'bad' and true_label == 0 and pred_label == 1")
    assert int(bad["count"].iloc[0]) == 1
    assert len(per_class) == 2 * 3
    bad_class_zero = per_class[
        (per_class["scheme"] == "bad") & (per_class["class"] == 0)
    ]
    assert int(bad_class_zero["support"].iloc[0]) == 2


def test_paired_tests_reports_differences_and_holm() -> None:
    rows = []
    for fold in range(10):
        rows.append({"fold": fold, "scheme": "perfect", "macro_f1": 0.9})
        rows.append({"fold": fold, "scheme": "worse", "macro_f1": 0.6})
        rows.append({"fold": fold, "scheme": "close", "macro_f1": 0.85})
    table = per_fold_metrics_from_rows(rows)
    tests = paired_tests(table, "perfect", n_boot=50)
    assert len(tests) == 2
    assert (tests["mean_diff"] < 0).all()
    assert tests["p_holm"].notna().all()
    assert tests["significant_holm"].any()


def per_fold_metrics_from_rows(rows):
    return pd.DataFrame(rows)


def test_missing_modality_summary_applies_h5_rule() -> None:
    metrics = pd.DataFrame(
        [
            {"scheme": "W1_equal", "macro_f1": 0.80, "ordinal_mae": 0.30},
        ]
    )
    stable = pd.DataFrame(
        [
            {"scheme": "W1_equal", "treatment": "neutral_fill", "rate": 0.1,
             "macro_f1": 0.79, "accuracy": 0.8, "ordinal_mae": 0.31},
            {"scheme": "W1_equal", "treatment": "neutral_fill", "rate": 0.3,
             "macro_f1": 0.785, "accuracy": 0.8, "ordinal_mae": 0.32},
            {"scheme": "W1_equal", "treatment": "available_only", "rate": 0.3,
             "macro_f1": 0.70, "accuracy": 0.7, "ordinal_mae": 0.45},
        ]
    )
    summary = missing_modality_summary(stable, metrics)
    neutral = [row for row in summary if row["treatment"] == "neutral_fill"]
    available = [row for row in summary if row["treatment"] == "available_only"]
    assert neutral and neutral[0]["h5_verdict"] == "stable"
    assert available and available[0]["h5_verdict"] == "unstable"


def test_group_level_paired_bootstrap_returns_ci_and_p() -> None:
    from public_data.analysis import paired_bootstrap_ci

    y = np.array([0, 1, 2, 0, 1, 2, 0, 1, 2, 0])
    reference = y.copy()
    scheme = np.array([0, 0, 1, 0, 1, 1, 0, 1, 0, 0])
    groups = np.array([1, 1, 1, 2, 2, 2, 3, 3, 3, 4])
    result = paired_bootstrap_ci(
        y, reference, scheme, groups, metric="macro_f1", n_boot=100
    )
    assert result["point_diff"] <= 0
    assert result["ci_lower"] <= result["ci_upper"]
    assert 0.0 <= result["group_bootstrap_p"] <= 1.0
    assert result["n_groups"] == 4


def test_log_loss_is_reported_when_probabilities_are_present() -> None:
    from public_data.metrics import classification_metrics

    metrics = classification_metrics(
        [0, 1, 2],
        [0, 1, 2],
        y_score=np.eye(3),
    )
    assert math.isclose(metrics["log_loss"], 0.0, abs_tol=1e-9)
