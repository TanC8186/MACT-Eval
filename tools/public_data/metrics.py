"""Classification/ordinal metrics and group-level bootstrap confidence intervals.

The experiment protocol (spec section 6.3) reports accuracy, macro-F1,
ordinal MAE, Spearman correlation, macro one-vs-rest AUC and Brier score.
Metrics that are mathematically undefined for a given input (e.g. AUC or
Spearman on a constant label vector) are returned as ``NaN`` rather than
raising, so small folds and degenerate synthetic fixtures still produce a
complete result row.

``grouped_bootstrap_ci`` resamples whole groups (lessons) with replacement,
which keeps clips from the same lesson together — the correct unit for the
Pri-MCCD grouped protocol.
"""
from __future__ import annotations

import warnings
from typing import Any, Callable, Iterable, Sequence

import numpy as np
from scipy.stats import spearmanr
from sklearn.metrics import accuracy_score, f1_score, log_loss, roc_auc_score

_NAN = float("nan")
_THREE_LABELS = (0, 1, 2)


def classification_metrics(
    y_true: Iterable[Any],
    y_pred: Iterable[Any],
    y_score: Iterable[Any] | np.ndarray | None = None,
    *,
    ordinal_scores: Iterable[Any] | np.ndarray | None = None,
) -> dict[str, float]:
    """Return the standard metric dictionary for one prediction set.

    Args:
        y_true: Ordinal labels (0/1/2).
        y_pred: Predicted labels.
        y_score: Optional probabilities/scores.  A 2-D array is interpreted as
            class probabilities (columns aligned to label values when labels
            are 0..K-1, otherwise to sorted unique labels); a 1-D array is
            interpreted as ``P(y == positive class)`` where the positive class
            is the highest observed label (2 for the three-class task).
        ordinal_scores: Optional fused ordinal score (0/50/100).  When given,
            Spearman is computed between ``y_true`` and this score, as the
            protocol requires.  When omitted, a 2-D probability matrix is
            converted to its expected ordinal score; a 1-D score is used as
            the ordinal score; without any score, Spearman is ``NaN``.

    Returns:
        ``{"accuracy", "macro_f1", "ordinal_mae", "spearman",
        "macro_auc_ovr", "brier", "log_loss"}``.  AUC/Brier/log-loss are
        ``NaN`` when ``y_score`` is omitted; Spearman is ``NaN`` when no
        ordinal score is available.
    """
    true = np.asarray(y_true).ravel()
    pred = np.asarray(y_pred).ravel()
    if true.size == 0:
        raise ValueError("y_true and y_pred must not be empty")
    if true.shape != pred.shape:
        raise ValueError(
            f"y_true and y_pred length mismatch: {true.size} != {pred.size}"
        )

    metrics: dict[str, float] = {
        "accuracy": float(accuracy_score(true, pred)),
        # Protocol 5.1: macro average over all three classes, not only the
        # classes present in this fold.
        "macro_f1": float(
            f1_score(
                true,
                pred,
                labels=list(range(len(_THREE_LABELS))),
                average="macro",
                zero_division=0,
            )
        ),
        "ordinal_mae": float(
            np.mean(np.abs(true.astype(float) - pred.astype(float)))
        ),
        "spearman": _ordinal_spearman(true, y_score, ordinal_scores),
    }

    if y_score is None:
        metrics["macro_auc_ovr"] = _NAN
        metrics["brier"] = _NAN
        metrics["log_loss"] = _NAN
        return metrics

    scores = np.asarray(y_score, dtype=float)
    if scores.ndim == 1:
        if scores.size != true.size:
            raise ValueError(
                "y_score length mismatch: "
                f"{scores.size} != {true.size}"
            )
        positive_label = np.max(np.unique(true))
        binary = (true == positive_label).astype(float)
        metrics["macro_auc_ovr"] = _binary_auc(binary, scores)
        metrics["brier"] = float(np.mean((scores - binary) ** 2))
        metrics["log_loss"] = _safe_log_loss(binary, scores, labels=[0, 1])
        return metrics

    if scores.ndim == 2:
        if scores.shape[0] != true.size:
            raise ValueError(
                "y_score row count mismatch: "
                f"{scores.shape[0]} != {true.size}"
            )
        classes, columns = _resolve_score_columns(true, scores)
        metrics["macro_auc_ovr"] = _macro_auc_ovr(true, scores, classes, columns)
        metrics["brier"] = _multiclass_brier(true, scores, columns)
        aligned = scores[:, [columns[label] for label in classes]]
        metrics["log_loss"] = _safe_log_loss(true, aligned, labels=classes)
        return metrics

    raise ValueError("y_score must be a 1-D or 2-D array")


def grouped_bootstrap_ci(
    y_true: Iterable[Any],
    y_pred: Iterable[Any],
    groups: Iterable[Any],
    n_boot: int = 1000,
    seed: int = 42,
    *,
    ordinal_scores: Iterable[Any] | np.ndarray | None = None,
) -> dict[str, Any]:
    """Bootstrap 95% CIs by resampling whole groups.

    Each replicate samples ``n_groups`` groups with replacement and pools all
    rows of the selected groups.  Results contain, per metric, the full-sample
    point estimate plus the bootstrap mean and the 2.5 / 97.5 percentiles.

    Args:
        y_true: Ordinal labels.
        y_pred: Predicted labels.
        groups: Group id per row (e.g. lesson id).  Rows of a group are always
            resampled together.
        n_boot: Number of bootstrap replicates (must be positive).
        seed: RNG seed; identical seeds produce identical results.
        ordinal_scores: Optional fused ordinal score (0/50/100) used for the
            Spearman bootstrap.  Without it, Spearman is ``NaN``.

    Returns:
        ``{metric: {"point", "mean", "ci_lower", "ci_upper"}, "n_groups",
        "n_boot"}`` for accuracy, macro-F1, ordinal MAE and Spearman.
    """
    true = np.asarray(y_true).ravel()
    pred = np.asarray(y_pred).ravel()
    group_values = np.asarray(groups).ravel()

    if true.size == 0:
        raise ValueError("y_true and y_pred must not be empty")
    if true.shape != pred.shape or true.shape != group_values.shape:
        raise ValueError(
            "y_true, y_pred and groups must have the same length"
        )
    ordinal_values = None
    if ordinal_scores is not None:
        ordinal_values = np.asarray(ordinal_scores, dtype=float).ravel()
        if ordinal_values.shape != true.shape:
            raise ValueError(
                "ordinal_scores must have the same length as y_true"
            )

    boot_count = int(n_boot)
    if boot_count <= 0:
        raise ValueError("n_boot must be positive")

    unique_groups, inverse = np.unique(group_values, return_inverse=True)
    group_indices = [
        np.flatnonzero(inverse == index) for index in range(unique_groups.size)
    ]
    n_groups = unique_groups.size

    metric_functions: dict[str, Callable[[np.ndarray, np.ndarray], float]] = {
        "accuracy": lambda a, b: float(accuracy_score(a, b)),
        "macro_f1": lambda a, b: float(
            f1_score(
                a,
                b,
                labels=list(_THREE_LABELS),
                average="macro",
                zero_division=0,
            )
        ),
        "ordinal_mae": lambda a, b: float(
            np.mean(np.abs(a.astype(float) - b.astype(float)))
        ),
    }

    metric_names = [*metric_functions, "spearman"]
    points = {
        name: function(true, pred)
        for name, function in metric_functions.items()
    }
    points["spearman"] = (
        _safe_spearman(true, ordinal_values)
        if ordinal_values is not None
        else _NAN
    )

    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = {name: [] for name in metric_names}
    for _ in range(boot_count):
        picked = rng.integers(0, n_groups, size=n_groups)
        indices = np.concatenate([group_indices[index] for index in picked])
        true_sample = true[indices]
        pred_sample = pred[indices]
        for name, function in metric_functions.items():
            samples[name].append(function(true_sample, pred_sample))
        samples["spearman"].append(
            _safe_spearman(true_sample, ordinal_values[indices])
            if ordinal_values is not None
            else _NAN
        )

    result: dict[str, Any] = {}
    for name, values in samples.items():
        array = np.asarray(values, dtype=float)
        finite = array[np.isfinite(array)]
        if finite.size == 0:
            result[name] = {
                "point": _finite_or_nan(points[name]),
                "mean": _NAN,
                "ci_lower": _NAN,
                "ci_upper": _NAN,
            }
            continue
        result[name] = {
            "point": _finite_or_nan(points[name]),
            "mean": float(np.mean(finite)),
            "ci_lower": float(np.percentile(finite, 2.5)),
            "ci_upper": float(np.percentile(finite, 97.5)),
        }

    result["n_groups"] = int(n_groups)
    result["n_boot"] = boot_count
    return result


def _ordinal_spearman(
    true: np.ndarray,
    y_score: Iterable[Any] | np.ndarray | None,
    ordinal_scores: Iterable[Any] | np.ndarray | None,
) -> float:
    """Spearman between true labels and the fused ordinal score.

    This is deliberately *not* the correlation with discrete predicted labels:
    the protocol specifies the fused score ``s_fused``.
    """
    if ordinal_scores is not None:
        values = np.asarray(ordinal_scores, dtype=float).ravel()
        if values.size != true.size:
            raise ValueError(
                "ordinal_scores length mismatch: "
                f"{values.size} != {true.size}"
            )
        if not np.all(np.isfinite(values)):
            return _NAN
        return _safe_spearman(true, values)

    if y_score is None:
        return _NAN
    scores = np.asarray(y_score, dtype=float)
    if scores.ndim == 1:
        if scores.size != true.size:
            raise ValueError(
                "y_score length mismatch: "
                f"{scores.size} != {true.size}"
            )
        return _safe_spearman(true, scores)
    if scores.ndim != 2:
        return _NAN
    if scores.shape[0] != true.size:
        raise ValueError(
            "y_score row count mismatch: "
            f"{scores.shape[0]} != {true.size}"
        )
    classes, columns = _resolve_score_columns(true, scores)
    label_score = {0: 0.0, 1: 50.0, 2: 100.0}
    expected = np.zeros(true.size, dtype=float)
    for label in classes:
        expected += scores[:, columns[label]] * label_score.get(int(label), float(label))
    return _safe_spearman(true, expected)


def _safe_spearman(true: np.ndarray, pred: np.ndarray) -> float:
    if true.size < 2:
        return _NAN
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            result = spearmanr(true.astype(float), pred.astype(float))
        except (TypeError, ValueError):
            return _NAN
    value = float(result.statistic)
    return value if np.isfinite(value) else _NAN


def _safe_log_loss(
    true: np.ndarray,
    scores: np.ndarray,
    *,
    labels: Sequence[Any],
) -> float:
    """Log loss, or NaN when the input is degenerate/undefined."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            value = float(log_loss(true, scores, labels=list(labels)))
    except (TypeError, ValueError):
        return _NAN
    return value if np.isfinite(value) else _NAN


def _resolve_score_columns(
    true: np.ndarray, scores: np.ndarray
) -> tuple[list[Any], dict[Any, int]]:
    """Map each observed label to its probability column.

    Prefers direct column indexing by label value (the usual 0/1/2 layout) and
    falls back to sorted-unique-label order when the matrix has exactly one
    column per observed class.
    """
    classes = list(np.unique(true))
    n_columns = scores.shape[1]

    columns: dict[Any, int] = {}
    direct = True
    for cls in classes:
        try:
            index = int(cls)
        except (TypeError, ValueError):
            direct = False
            break
        if index != cls or index < 0 or index >= n_columns:
            direct = False
            break
        columns[cls] = index

    if direct:
        return classes, columns

    if n_columns == len(classes):
        return classes, {cls: index for index, cls in enumerate(classes)}

    raise ValueError(
        f"Cannot align y_score with {n_columns} column(s) to labels {classes!r}"
    )


def _binary_auc(binary: np.ndarray, scores: np.ndarray) -> float:
    if binary.min() == binary.max():
        return _NAN
    try:
        return float(roc_auc_score(binary, scores))
    except ValueError:
        return _NAN


def _macro_auc_ovr(
    true: np.ndarray,
    scores: np.ndarray,
    classes: list[Any],
    columns: dict[Any, int],
) -> float:
    aucs: list[float] = []
    for cls in classes:
        binary = (true == cls).astype(int)
        if binary.min() == binary.max():
            # The class is absent from or covers the whole fold.
            continue
        try:
            aucs.append(float(roc_auc_score(binary, scores[:, columns[cls]])))
        except ValueError:
            continue
    if not aucs:
        return _NAN
    return float(np.mean(aucs))


def _multiclass_brier(
    true: np.ndarray,
    scores: np.ndarray,
    columns: dict[Any, int],
) -> float:
    targets = np.zeros_like(scores, dtype=float)
    for cls, column in columns.items():
        targets[true == cls, column] = 1.0
    # Columns with no observed class keep an all-zero target, which is the
    # standard multi-class Brier definition for the full label set.
    return float(np.mean(np.sum((scores - targets) ** 2, axis=1)))


def _finite_or_nan(value: float) -> float:
    return float(value) if np.isfinite(value) else _NAN


__all__ = ["classification_metrics", "grouped_bootstrap_ci"]
