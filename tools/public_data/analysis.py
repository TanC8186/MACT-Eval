"""Post-hoc analysis for the late-fusion experiment.

The main cross-validation engine (``experiment.run_cv``) produces out-of-fold
predictions per scheme.  This module turns those predictions into the
artifacts required by the protocol:

* per-fold metrics (paired inputs for statistical tests);
* aggregated confusion matrices and per-class precision/recall/F1;
* paired Wilcoxon / paired-bootstrap tests with Holm correction;
* the H5 missing-modality verdict (neutral fill and available-only arms).

Everything is deterministic for a fixed input and seed.  Undefined
statistics (fewer than five folds, zero differences, constant labels) are
reported as ``None`` instead of being fabricated.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.stats import wilcoxon
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
)

from .metrics import classification_metrics

LABELS = (0, 1, 2)


def per_fold_metrics(
    fold_records: Sequence[Mapping[str, Any]],
    y: np.ndarray,
    schemes: Iterable[str],
) -> pd.DataFrame:
    """Return one metric row per (fold, scheme)."""
    rows: list[dict[str, Any]] = []
    scheme_list = list(schemes)
    for record in fold_records:
        test_index = np.asarray(record["test_index"], dtype=int)
        for scheme in scheme_list:
            scheme_record = record["schemes"].get(scheme)
            if scheme_record is None:
                continue
            prediction = np.asarray(scheme_record["prediction"], dtype=int)
            valid = prediction >= 0
            if not valid.any():
                continue
            probabilities = scheme_record.get("probabilities")
            has_probabilities = (
                probabilities is not None
                and np.asarray(probabilities).ndim == 2
                and np.all(np.isfinite(np.asarray(probabilities, dtype=float)))
            )
            scores = scheme_record.get("score")
            labels = y[test_index][valid]
            metrics = classification_metrics(
                labels,
                prediction[valid],
                y_score=np.asarray(probabilities, dtype=float)[valid]
                if has_probabilities
                else None,
                ordinal_scores=np.asarray(scores, dtype=float)[valid]
                if scores is not None
                else None,
            )
            rows.append(
                {
                    "fold": int(record["fold"]),
                    "scheme": scheme,
                    "n": int(valid.sum()),
                    "n_unavailable": int((~valid).sum()),
                    "n_class_0": int(np.sum(labels == 0)),
                    "n_class_1": int(np.sum(labels == 1)),
                    "n_class_2": int(np.sum(labels == 2)),
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def confusion_tables(
    y: np.ndarray,
    predictions: Mapping[str, np.ndarray],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return long-format confusion matrices and per-class metrics."""
    confusion_rows: list[dict[str, Any]] = []
    per_class_rows: list[dict[str, Any]] = []
    for scheme, prediction in predictions.items():
        values = np.asarray(prediction, dtype=int)
        if values.size != len(y) or np.any(values < 0):
            continue
        matrix = confusion_matrix(y, values, labels=list(LABELS))
        for true_label in LABELS:
            for pred_label in LABELS:
                confusion_rows.append(
                    {
                        "scheme": scheme,
                        "true_label": true_label,
                        "pred_label": pred_label,
                        "count": int(matrix[true_label, pred_label]),
                    }
                )
        precision, recall, f1, support = precision_recall_fscore_support(
            y, values, labels=list(LABELS), zero_division=0
        )
        for class_index, label in enumerate(LABELS):
            per_class_rows.append(
                {
                    "scheme": scheme,
                    "class": label,
                    "precision": float(precision[class_index]),
                    "recall": float(recall[class_index]),
                    "f1": float(f1[class_index]),
                    "support": int(support[class_index]),
                }
            )
    return pd.DataFrame(confusion_rows), pd.DataFrame(per_class_rows)


def paired_tests(
    per_fold: pd.DataFrame,
    reference_scheme: str,
    *,
    metric: str = "macro_f1",
    alpha: float = 0.05,
    n_boot: int = 1000,
    seed: int = 42,
) -> pd.DataFrame:
    """Paired Wilcoxon and paired-bootstrap tests versus one reference scheme.

    Each non-reference scheme is compared on the same folds.  The returned
    ``p_holm`` column is the Holm-adjusted Wilcoxon p-value (bootstrap p-value
    is used when Wilcoxon is undefined).  ``significant_holm`` is True when
    ``p_holm < alpha``.
    """
    if per_fold.empty:
        return pd.DataFrame(
            columns=[
                "scheme",
                "reference",
                "metric",
                "n_folds",
                "mean_diff",
                "wilcoxon_stat",
                "p_value",
                "bootstrap_p",
                "p_holm",
                "significant_holm",
            ]
        )
    pivot = per_fold.pivot_table(
        index="fold", columns="scheme", values=metric, aggfunc="mean"
    )
    if reference_scheme not in pivot.columns:
        return pd.DataFrame()

    reference = pivot[reference_scheme]
    rows: list[dict[str, Any]] = []
    for scheme in pivot.columns:
        if scheme == reference_scheme:
            continue
        differences = (pivot[scheme] - reference).dropna()
        if differences.size < 2:
            continue
        values = differences.to_numpy(dtype=float)
        mean_diff = float(np.mean(values))

        statistic = float("nan")
        p_value = float("nan")
        if values.size >= 5 and not np.allclose(values, 0.0):
            try:
                result = wilcoxon(values, alternative="two-sided")
                statistic = float(result.statistic)
                p_value = float(result.pvalue)
            except ValueError:
                pass

        rng = np.random.default_rng(seed)
        boot_means = np.empty(int(n_boot), dtype=float)
        for index in range(int(n_boot)):
            sample = rng.choice(values, size=values.size, replace=True)
            boot_means[index] = float(np.mean(sample))
        if mean_diff == 0.0:
            bootstrap_p = 1.0
        else:
            bootstrap_p = 2.0 * min(
                float(np.mean(boot_means <= 0.0)),
                float(np.mean(boot_means >= 0.0)),
            )
            bootstrap_p = min(1.0, bootstrap_p)

        rows.append(
            {
                "scheme": scheme,
                "reference": reference_scheme,
                "metric": metric,
                "n_folds": int(values.size),
                "mean_diff": mean_diff,
                "wilcoxon_stat": statistic if math.isfinite(statistic) else None,
                "p_value": p_value if math.isfinite(p_value) else None,
                "bootstrap_p": float(bootstrap_p),
            }
        )

    table = pd.DataFrame(rows)
    if table.empty:
        return table
    raw_p = table["p_value"].fillna(table["bootstrap_p"]).to_numpy(dtype=float)
    order = np.argsort(raw_p, kind="stable")
    adjusted = np.empty_like(raw_p)
    previous = 0.0
    total = len(raw_p)
    for rank, index in enumerate(order):
        value = min(1.0, (total - rank) * raw_p[index])
        value = max(value, previous)
        previous = value
        adjusted[index] = value
    table["p_holm"] = adjusted
    table["significant_holm"] = table["p_holm"] < alpha
    return table


def paired_bootstrap_ci(
    y_true: np.ndarray,
    pred_reference: np.ndarray,
    pred_scheme: np.ndarray,
    groups: np.ndarray,
    *,
    metric: str = "macro_f1",
    n_boot: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """Group-level paired bootstrap for the difference scheme - reference.

    Whole groups (lessons) are resampled with replacement, and the metric is
    recomputed for both schemes on the same resampled rows.  This is the
    protocol's paired bootstrap; it is not a bootstrap of fold-level metric
    differences.
    """
    true = np.asarray(y_true).ravel()
    reference = np.asarray(pred_reference).ravel()
    scheme = np.asarray(pred_scheme).ravel()
    group_values = np.asarray(groups).ravel()
    if not (true.shape == reference.shape == scheme.shape == group_values.shape):
        raise ValueError("all inputs must have the same length")
    if int(n_boot) <= 0:
        raise ValueError("n_boot must be positive")

    def score(a: np.ndarray, b: np.ndarray) -> float:
        if metric == "macro_f1":
            return float(
                f1_score(a, b, labels=list(LABELS), average="macro", zero_division=0)
            )
        if metric == "accuracy":
            return float(np.mean(a == b))
        if metric == "ordinal_mae":
            return float(np.mean(np.abs(a.astype(float) - b.astype(float))))
        raise ValueError(f"Unknown metric: {metric!r}")

    point = score(true, scheme) - score(true, reference)
    unique_groups, inverse = np.unique(group_values, return_inverse=True)
    group_indices = [
        np.flatnonzero(inverse == index) for index in range(unique_groups.size)
    ]
    rng = np.random.default_rng(seed)
    differences = np.empty(int(n_boot), dtype=float)
    for index in range(int(n_boot)):
        picked = rng.integers(0, unique_groups.size, size=unique_groups.size)
        rows = np.concatenate([group_indices[item] for item in picked])
        differences[index] = score(true[rows], scheme[rows]) - score(
            true[rows], reference[rows]
        )
    if point == 0 and np.allclose(differences, 0.0):
        p_value = 1.0
    else:
        p_value = 2.0 * min(
            float(np.mean(differences <= 0.0)),
            float(np.mean(differences >= 0.0)),
        )
        p_value = min(1.0, p_value)
    return {
        "point_diff": float(point),
        "ci_lower": float(np.percentile(differences, 2.5)),
        "ci_upper": float(np.percentile(differences, 97.5)),
        "group_bootstrap_p": float(p_value),
        "n_boot": int(n_boot),
        "n_groups": int(unique_groups.size),
    }


def missing_modality_summary(
    missing: pd.DataFrame,
    metrics: pd.DataFrame,
    *,
    stable_rates: float = 0.3,
    macro_f1_drop: float = 0.02,
    mae_increase: float = 0.05,
) -> list[dict[str, Any]]:
    """Summarise missing-modality runs and apply the H5 stability rule.

    The baseline for each scheme comes from ``metrics`` (the no-masking
    out-of-fold result).  H5 is evaluated only on rates up to ``stable_rates``:
    Macro-F1 mean drop <= ``macro_f1_drop`` and Ordinal MAE mean increase
    <= ``mae_increase`` means "stable".
    """
    if missing.empty or metrics.empty:
        return []
    baseline = metrics.set_index("scheme")
    rows: list[dict[str, Any]] = []
    grouped = missing.groupby(["scheme", "treatment", "rate"], as_index=False).agg(
        macro_f1_mean=("macro_f1", "mean"),
        macro_f1_std=("macro_f1", "std"),
        accuracy_mean=("accuracy", "mean"),
        ordinal_mae_mean=("ordinal_mae", "mean"),
    )
    for (scheme, treatment), group in grouped.groupby(["scheme", "treatment"]):
        if scheme not in baseline.index:
            continue
        base = baseline.loc[scheme]
        base_f1 = float(base["macro_f1"])
        base_mae = float(base["ordinal_mae"])
        ordered = group.sort_values("rate")
        for record in ordered.to_dict("records"):
            rows.append(
                {
                    "scheme": scheme,
                    "treatment": treatment,
                    "rate": float(record["rate"]),
                    "macro_f1_mean": float(record["macro_f1_mean"]),
                    "macro_f1_std": float(record["macro_f1_std"])
                    if math.isfinite(float(record["macro_f1_std"]))
                    else None,
                    "accuracy_mean": float(record["accuracy_mean"]),
                    "ordinal_mae_mean": float(record["ordinal_mae_mean"]),
                    "macro_f1_drop": base_f1 - float(record["macro_f1_mean"]),
                    "ordinal_mae_increase": float(record["ordinal_mae_mean"])
                    - base_mae,
                }
            )
        applicable = ordered[ordered["rate"] <= stable_rates + 1e-12]
        if applicable.empty:
            verdict = "not_evaluated"
        else:
            max_drop = max(
                base_f1 - float(value) for value in applicable["macro_f1_mean"]
            )
            max_increase = max(
                float(value) - base_mae for value in applicable["ordinal_mae_mean"]
            )
            verdict = (
                "stable"
                if max_drop <= macro_f1_drop and max_increase <= mae_increase
                else "unstable"
            )
        for row in rows:
            if row["scheme"] == scheme and row["treatment"] == treatment:
                row["h5_verdict"] = verdict
                row["h5_baseline_macro_f1"] = base_f1
                row["h5_baseline_ordinal_mae"] = base_mae
    return rows


__all__ = [
    "confusion_tables",
    "missing_modality_summary",
    "paired_tests",
    "per_fold_metrics",
]
