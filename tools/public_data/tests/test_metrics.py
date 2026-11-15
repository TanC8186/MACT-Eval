"""Tests for classification/ordinal metrics and grouped bootstrap CIs."""
from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.stats import spearmanr

from public_data.metrics import classification_metrics, grouped_bootstrap_ci

Y_TRUE = [0, 0, 1, 1, 2, 2]
Y_PRED = [0, 1, 1, 2, 2, 2]
Y_SCORE = np.array(
    [
        [0.8, 0.1, 0.1],
        [0.5, 0.4, 0.1],
        [0.1, 0.7, 0.2],
        [0.1, 0.3, 0.6],
        [0.1, 0.2, 0.7],
        [0.0, 0.2, 0.8],
    ]
)


def test_classification_metrics_known_values() -> None:
    metrics = classification_metrics(Y_TRUE, Y_PRED)

    assert metrics["accuracy"] == pytest.approx(4 / 6)
    assert metrics["macro_f1"] == pytest.approx(0.6555555555555556)
    assert metrics["ordinal_mae"] == pytest.approx(1 / 3)
    # Spearman is score-vs-label, not prediction-vs-label; without a score
    # the protocol metric is undefined.
    assert math.isnan(metrics["spearman"])
    assert math.isnan(metrics["macro_auc_ovr"])
    assert math.isnan(metrics["brier"])


def test_classification_metrics_with_probabilities() -> None:
    metrics = classification_metrics(Y_TRUE, Y_PRED, y_score=Y_SCORE)

    assert metrics["macro_auc_ovr"] == pytest.approx(0.9583333333333334)
    assert metrics["brier"] == pytest.approx(0.2833333333333334)
    assert math.isfinite(metrics["spearman"])


def test_classification_metrics_spearman_uses_fused_score() -> None:
    scores = np.array([0.0, 10.0, 50.0, 60.0, 100.0, 90.0])
    metrics = classification_metrics(
        Y_TRUE, Y_PRED, ordinal_scores=scores
    )
    expected = spearmanr(Y_TRUE, scores).statistic
    assert metrics["spearman"] == pytest.approx(expected)


def test_classification_metrics_handles_constant_labels() -> None:
    metrics = classification_metrics([1, 1, 1], [1, 1, 1])

    assert metrics["accuracy"] == pytest.approx(1.0)
    # Three-class macro average: classes 0 and 2 have F1=0, so 1/3.
    assert metrics["macro_f1"] == pytest.approx(1 / 3)
    assert metrics["ordinal_mae"] == pytest.approx(0.0)
    assert math.isnan(metrics["spearman"])
    assert math.isnan(metrics["macro_auc_ovr"])
    assert math.isnan(metrics["brier"])


def test_classification_metrics_constant_labels_still_brier() -> None:
    metrics = classification_metrics(
        [1, 1, 1],
        [1, 1, 1],
        y_score=np.array(
            [
                [0.1, 0.8, 0.1],
                [0.1, 0.9, 0.0],
                [0.2, 0.7, 0.1],
            ]
        ),
    )

    assert math.isnan(metrics["macro_auc_ovr"])
    assert metrics["brier"] == pytest.approx(0.07333333333333333)


def test_classification_metrics_handles_single_sample() -> None:
    metrics = classification_metrics([2], [2])

    assert metrics["accuracy"] == pytest.approx(1.0)
    assert metrics["macro_f1"] == pytest.approx(1 / 3)
    assert metrics["ordinal_mae"] == pytest.approx(0.0)
    assert math.isnan(metrics["spearman"])


def test_classification_metrics_positive_class_for_1d_scores() -> None:
    metrics = classification_metrics([0, 1, 2], [0, 1, 2], y_score=[0.1, 0.5, 0.9])

    # Positive class is the highest label (2); 1-D scores are P(y == 2).
    assert metrics["brier"] == pytest.approx(0.09)
    assert metrics["macro_auc_ovr"] == pytest.approx(1.0)


def test_classification_metrics_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        classification_metrics([], [])


def test_classification_metrics_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError):
        classification_metrics([0, 1], [0])
    with pytest.raises(ValueError):
        classification_metrics([0, 1], [0, 1], y_score=np.zeros((3, 3)))


def test_classification_metrics_rejects_bad_score_shape() -> None:
    with pytest.raises(ValueError):
        classification_metrics([0, 1, 2], [0, 1, 2], y_score=np.zeros((3, 2)))


def test_grouped_bootstrap_ci_is_reproducible() -> None:
    y_true = [0, 0, 1, 1, 2, 2, 0, 1, 2, 2]
    y_pred = [0, 1, 1, 1, 2, 0, 0, 1, 1, 2]
    groups = ["g1"] * 5 + ["g2"] * 5

    first = grouped_bootstrap_ci(
        y_true, y_pred, groups, n_boot=50, seed=42, ordinal_scores=y_pred
    )
    second = grouped_bootstrap_ci(
        y_true, y_pred, groups, n_boot=50, seed=42, ordinal_scores=y_pred
    )

    assert first == second
    for metric in ("accuracy", "macro_f1", "ordinal_mae", "spearman"):
        entry = first[metric]
        assert entry["ci_lower"] <= entry["ci_upper"]
        assert math.isfinite(entry["mean"])
        assert entry["point"] == pytest.approx(
            classification_metrics(
                y_true, y_pred, ordinal_scores=y_pred
            )[metric]
        )
    assert first["n_groups"] == 2
    assert first["n_boot"] == 50


def test_grouped_bootstrap_ci_reflects_group_level_variation() -> None:
    # Group g_bad is always wrong, g_good always right; resampling whole
    # groups changes accuracy, so the interval must not collapse.
    y_true = [0, 0, 1, 1, 2, 2, 2, 2]
    y_pred = [0, 0, 1, 1, 0, 0, 0, 0]
    groups = ["g_good"] * 4 + ["g_bad"] * 4

    result = grouped_bootstrap_ci(y_true, y_pred, groups, n_boot=200, seed=7)
    accuracy = result["accuracy"]
    assert accuracy["ci_lower"] < accuracy["ci_upper"]
    assert 0.0 <= accuracy["ci_lower"] <= 1.0
    assert 0.0 <= accuracy["ci_upper"] <= 1.0


def test_grouped_bootstrap_ci_handles_constant_predictions() -> None:
    result = grouped_bootstrap_ci(
        [1, 1, 1, 1], [1, 1, 1, 1], ["a", "a", "b", "b"], n_boot=20, seed=1
    )

    assert result["accuracy"]["ci_lower"] == pytest.approx(1.0)
    assert result["accuracy"]["ci_upper"] == pytest.approx(1.0)
    assert math.isnan(result["spearman"]["mean"])
    assert math.isnan(result["spearman"]["ci_lower"])
    assert math.isnan(result["spearman"]["ci_upper"])


def test_grouped_bootstrap_ci_validates_input() -> None:
    with pytest.raises(ValueError):
        grouped_bootstrap_ci([0, 1], [0], ["g1", "g2"])
    with pytest.raises(ValueError):
        grouped_bootstrap_ci([], [], [])
    with pytest.raises(ValueError):
        grouped_bootstrap_ci([0, 1], [0, 1], ["g1", "g2"], n_boot=0)
