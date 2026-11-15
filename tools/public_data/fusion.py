"""Late-fusion primitives for the video/speech classroom-climate experiment.

Implements the decision-level protocol of spec section 6:

* one logistic-regression model per modality (median imputation with missing
  indicators, standardisation, balanced class weights);
* probability fusion ``w * p_video + (1 - w) * p_speech``;
* a 0.00-1.00 weight grid search maximising macro-F1 (ties prefer 0.5);
* a two-threshold ordinal decision rule ``t1 < t2`` searched on a scalar
  score and maximising macro-F1;
* performance-derived weights ``w_i ~ max(0, AUC_i - chance)``.

Every helper is pure and deterministic for a fixed input and seed.  Folds
with only one class cannot train a classifier, so
:class:`ConstantProbabilityModel` reproduces the observed class with
probability 1.0 instead of fabricating a multi-class distribution.
"""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

#: Ordinal climate labels used throughout the experiment.
LABELS: tuple[int, int, int] = (0, 1, 2)
N_CLASSES = len(LABELS)

#: Chance-level macro one-vs-rest ROC AUC, independent of class count.
CHANCE_AUC = 0.5

#: Default step of the video-weight grid (0.00, 0.05, ..., 1.00).
DEFAULT_WEIGHT_STEP = 0.05

#: Cap on candidate thresholds; 120 candidates give 7,140 threshold pairs.
DEFAULT_MAX_THRESHOLD_CANDIDATES = 120


class ConstantProbabilityModel:
    """Fallback estimator for folds that contain fewer than two classes.

    ``predict_proba`` returns a constant distribution that puts all mass on
    the observed class (or equal mass on the observed classes if there is more
    than one, which should not happen for a real one-class fallback).
    """

    def __init__(self, classes: Iterable[int]) -> None:
        observed = np.unique(np.asarray(list(classes), dtype=int))
        if observed.size == 0:
            raise ValueError("ConstantProbabilityModel needs at least one class")
        unknown = set(int(value) for value in observed) - set(LABELS)
        if unknown:
            raise ValueError(f"Unknown classes: {sorted(unknown)}")
        self.classes_ = observed
        self._probabilities = np.full(observed.size, 1.0 / observed.size)

    def predict_proba(self, X: object) -> np.ndarray:
        # Sklearn semantics: columns follow ``classes_``; the module-level
        # ``predict_proba`` expands them to the canonical n x 3 matrix.
        n_samples = np.shape(X)[0] if np.ndim(X) > 0 else 0
        return np.tile(self._probabilities, (n_samples, 1))

    def predict(self, X: object) -> np.ndarray:
        n_samples = np.shape(X)[0] if np.ndim(X) > 0 else 0
        return np.full(n_samples, int(self.classes_[0]), dtype=int)


def fit_modality_model(
    X_train: object,
    y_train: object,
) -> Pipeline | ConstantProbabilityModel:
    """Fit one modality classifier on a training fold.

    The pipeline is fixed by the experiment protocol: median imputation with
    missing-value indicators, standardisation, and a balanced multinomial
    logistic regression with ``max_iter=2000`` and ``random_state=42``.

    A fold with a single class returns :class:`ConstantProbabilityModel`
    instead of raising, so grouped folds that happen to miss a class still
    produce a well-defined probability matrix.

    Args:
        X_train: Feature matrix of shape ``(n_samples, n_features)``.  NaNs
            are allowed and imputed.
        y_train: Ordinal labels (0/1/2).

    Returns:
        A fitted ``sklearn`` pipeline or the constant fallback.
    """
    X = _as_feature_matrix(X_train, "X_train")
    y = _as_labels(y_train, expected_length=X.shape[0], name="y_train")

    observed = np.unique(y)
    if observed.size < 2:
        return ConstantProbabilityModel(observed)

    model = Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(strategy="median", add_indicator=True),
            ),
            ("scaler", StandardScaler()),
            (
                "classifier",
                LogisticRegression(
                    max_iter=2000,
                    class_weight="balanced",
                    random_state=42,
                ),
            ),
        ]
    )
    model.fit(X, y)
    return model


def predict_proba(estimator: object, X: object) -> np.ndarray:
    """Return an ``(n_samples, 3)`` class-probability matrix.

    Estimators trained on a subset of the three classes (a common outcome of
    grouped folds) return fewer columns from ``predict_proba``.  This helper
    expands them back to the canonical 0/1/2 columns, filling absent classes
    with zero, and renormalises rows so they sum to one.

    Args:
        estimator: Fitted estimator exposing ``predict_proba`` and
            ``classes_``.
        X: Feature matrix.

    Returns:
        ``numpy.ndarray`` of shape ``(n_samples, 3)``.
    """
    X = _as_feature_matrix(X, "X")
    if not hasattr(estimator, "predict_proba"):
        raise TypeError("estimator must implement predict_proba")
    classes = getattr(estimator, "classes_", None)
    if classes is None:
        raise TypeError("estimator must expose classes_")

    raw = np.asarray(estimator.predict_proba(X), dtype=float)
    if raw.ndim != 2 or raw.shape[0] != X.shape[0]:
        raise ValueError(
            "predict_proba returned an unexpected shape: "
            f"{raw.shape} for {X.shape[0]} row(s)"
        )
    if raw.shape[1] != len(classes):
        raise ValueError(
            "predict_proba column count does not match classes_: "
            f"{raw.shape[1]} != {len(classes)}"
        )
    if not np.all(np.isfinite(raw)):
        raise ValueError("predict_proba returned non-finite values")
    if np.any(raw < -1e-9):
        raise ValueError("predict_proba returned negative probabilities")

    probabilities = np.zeros((X.shape[0], N_CLASSES), dtype=float)
    for column, label in enumerate(classes):
        try:
            class_index = int(label)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Non-integer class label: {label!r}") from exc
        if class_index != label or class_index not in LABELS:
            raise ValueError(f"Unknown class label: {label!r}")
        probabilities[:, class_index] = raw[:, column]

    row_sums = probabilities.sum(axis=1)
    if np.any(row_sums <= 0):
        raise ValueError("predict_proba rows must sum to a positive value")
    probabilities /= row_sums[:, np.newaxis]
    return probabilities


def fuse_proba(
    p_video: object,
    p_speech: object,
    w: float,
) -> np.ndarray:
    """Late-fuse two probability matrices.

    ``fused = w * p_video + (1 - w) * p_speech``.

    Args:
        p_video: ``(n, 3)`` video probabilities.
        p_speech: ``(n, 3)`` speech probabilities.
        w: Video weight in ``[0, 1]``.

    Returns:
        ``(n, 3)`` fused probabilities.
    """
    video = _as_probability_matrix(p_video, "p_video")
    speech = _as_probability_matrix(p_speech, "p_speech")
    if video.shape[0] != speech.shape[0]:
        raise ValueError(
            "p_video and p_speech must have the same row count: "
            f"{video.shape[0]} != {speech.shape[0]}"
        )
    weight = _as_weight(w)
    fused = weight * video + (1.0 - weight) * speech
    return fused


def choose_weights(
    p_video: object,
    p_speech: object,
    y: object,
    grid: Sequence[float] | np.ndarray | None = None,
    *,
    step: float = DEFAULT_WEIGHT_STEP,
) -> tuple[float, float]:
    """Choose ``(w_video, w_speech)`` on a weight grid by macro-F1.

    Predictions for each candidate weight are the arg-max of the fused
    probability matrix.  Ties are broken towards equal weights: the candidate
    closest to ``0.5`` wins (and a symmetric tie picks the lower weight, which
    keeps the result deterministic).

    Args:
        p_video: ``(n, 3)`` video probabilities.
        p_speech: ``(n, 3)`` speech probabilities.
        y: Ordinal labels.
        grid: Optional video-weight grid.  Defaults to
            ``0.00, 0.05, ..., 1.00``.
        step: Grid step used only when ``grid`` is omitted.

    Returns:
        ``(w_video, w_speech)`` summing to one.
    """
    video = _as_probability_matrix(p_video, "p_video")
    speech = _as_probability_matrix(p_speech, "p_speech")
    labels = _as_labels(y, expected_length=video.shape[0], name="y")
    if video.shape[0] != speech.shape[0]:
        raise ValueError(
            "p_video and p_speech must have the same row count: "
            f"{video.shape[0]} != {speech.shape[0]}"
        )

    candidates = _weight_grid(grid, step)
    scores = np.empty(candidates.size, dtype=float)
    for index, weight in enumerate(candidates):
        fused = weight * video + (1.0 - weight) * speech
        predictions = np.argmax(fused, axis=1)
        scores[index] = f1_score(
            labels,
            predictions,
            labels=list(LABELS),
            average="macro",
            zero_division=0,
        )

    best = float(np.max(scores))
    # Protocol 3.5: macro-F1 differences <= 1e-6 count as ties.
    tied = np.flatnonzero(np.isclose(scores, best, rtol=0.0, atol=1e-6))
    distances = np.abs(candidates[tied] - 0.5)
    # Sort by distance to 0.5 (primary) then by weight (secondary).
    order = np.lexsort((candidates[tied], distances))
    chosen = float(candidates[tied[order[0]]])
    return chosen, float(np.round(1.0 - chosen, 12))


def weights_from_auc(
    p_video: object,
    p_speech: object,
    y: object,
) -> tuple[float, float]:
    """Performance-derived weights ``w_i ~ max(0, AUC_i - chance)``.

    ``chance`` is ``0.5`` for each one-vs-rest ROC AUC and therefore also
    for their macro average. If both modalities are at or below chance,
    the weights fall back to ``(0.5, 0.5)``.
    """
    video = _as_probability_matrix(p_video, "p_video")
    speech = _as_probability_matrix(p_speech, "p_speech")
    labels = _as_labels(y, expected_length=video.shape[0], name="y")
    if video.shape[0] != speech.shape[0]:
        raise ValueError(
            "p_video and p_speech must have the same row count: "
            f"{video.shape[0]} != {speech.shape[0]}"
        )

    video_auc = macro_auc(labels, video)
    speech_auc = macro_auc(labels, speech)
    video_evidence = max(0.0, video_auc - CHANCE_AUC) if np.isfinite(video_auc) else 0.0
    speech_evidence = (
        max(0.0, speech_auc - CHANCE_AUC) if np.isfinite(speech_auc) else 0.0
    )
    total = video_evidence + speech_evidence
    if total <= 0:
        return 0.5, 0.5
    w_video = float(video_evidence / total)
    return w_video, float(1.0 - w_video)


def macro_auc(y: object, probabilities: object) -> float:
    """Return macro one-vs-rest AUC, or ``NaN`` when it is undefined."""
    labels = _as_labels(y, expected_length=np.shape(probabilities)[0], name="y")
    matrix = _as_probability_matrix(probabilities, "probabilities")
    if np.unique(labels).size < 2:
        return float("nan")

    aucs: list[float] = []
    for label in np.unique(labels):
        binary = (labels == label).astype(int)
        if binary.min() == binary.max():
            continue
        try:
            aucs.append(float(roc_auc_score(binary, matrix[:, int(label)])))
        except ValueError:
            continue
    if not aucs:
        return float("nan")
    return float(np.mean(aucs))


def ordinal_score(probabilities: object) -> np.ndarray:
    """Map class probabilities to an expected ordinal score in ``[0, 100]``.

    ``score = 0 * p_negative + 50 * p_neutral + 100 * p_positive``, matching
    the system's 0/50/100 climate scale and the rule-baseline scores.  Using
    the same scale for both score sources keeps thresholds comparable.
    """
    matrix = _as_probability_matrix(probabilities, "probabilities")
    return matrix @ np.asarray([0.0, 50.0, 100.0], dtype=float)


def learn_thresholds(
    scores: object,
    y: object,
    *,
    max_candidates: int = DEFAULT_MAX_THRESHOLD_CANDIDATES,
) -> tuple[float, float]:
    """Search two ordinal thresholds ``t1 < t2`` maximising macro-F1.

    A sample is predicted as:

    * class 0 when ``score < t1``;
    * class 1 when ``t1 <= score < t2``;
    * class 2 when ``score >= t2``.

    Candidate thresholds are midpoints between consecutive distinct scores, so
    every candidate lies strictly inside an observed gap.  When there are more
    distinct scores than ``max_candidates``, evenly spaced quantiles of the
    distinct values are used instead.  Tied threshold pairs are broken by the
    largest margin to the nearest observed score, which places the cut in the
    widest gap and makes the choice robust to small score perturbations.

    Args:
        scores: One-dimensional ordinal scores (e.g. :func:`ordinal_score`).
        y: Ordinal labels (0/1/2).
        max_candidates: Upper bound on threshold candidates (at least 2).

    Returns:
        ``(t1, t2)`` with ``t1 < t2``.
    """
    values = np.asarray(scores, dtype=float).ravel()
    labels = _as_labels(y, expected_length=values.size, name="y")
    if values.size == 0:
        raise ValueError("scores must not be empty")
    if not np.all(np.isfinite(values)):
        raise ValueError("scores must be finite")

    candidates = _threshold_candidates(values, max_candidates)
    pair_left, pair_right = np.triu_indices(candidates.size, k=1)

    ordered = np.argsort(values, kind="stable")
    sorted_values = values[ordered]
    sorted_labels = labels[ordered]
    one_hot = np.zeros((values.size, N_CLASSES), dtype=float)
    one_hot[np.arange(values.size), sorted_labels] = 1.0
    cumulative = np.vstack(
        [np.zeros((1, N_CLASSES), dtype=float), np.cumsum(one_hot, axis=0)]
    )
    cut_positions = np.searchsorted(sorted_values, candidates, side="left")
    below = cumulative[cut_positions]  # (n_candidates, 3) counts of score < t
    totals = cumulative[-1]

    predicted_0 = below[pair_left]
    predicted_1 = below[pair_right] - below[pair_left]
    predicted_2 = totals[np.newaxis, :] - below[pair_right]
    # Shape (n_pairs, predicted_class, true_class).
    counts = np.stack([predicted_0, predicted_1, predicted_2], axis=1)

    true_positives = np.stack(
        [counts[:, 0, 0], counts[:, 1, 1], counts[:, 2, 2]], axis=1
    )
    predicted_totals = counts.sum(axis=2)
    false_positives = predicted_totals - true_positives
    false_negatives = totals[np.newaxis, :] - true_positives
    denominators = 2.0 * true_positives + false_positives + false_negatives
    f1 = np.divide(
        2.0 * true_positives,
        denominators,
        out=np.zeros_like(true_positives),
        where=denominators > 0,
    )
    macro_f1 = f1.mean(axis=1)

    best = float(np.max(macro_f1))
    tied = np.flatnonzero(np.isclose(macro_f1, best, rtol=0.0, atol=1e-12))
    chosen = _widest_margin_pair(
        tied, pair_left, pair_right, candidates, values
    )
    return (
        float(candidates[pair_left[chosen]]),
        float(candidates[pair_right[chosen]]),
    )


def apply_thresholds(
    scores: object,
    thresholds: tuple[float, float] | Sequence[float],
) -> np.ndarray:
    """Apply two ordinal thresholds to a score vector.

    Returns integer labels 0/1/2.  Non-finite scores are rejected: silently
    mapping them to a class would fabricate predictions for missing data.
    """
    values = np.asarray(scores, dtype=float).ravel()
    if not np.all(np.isfinite(values)):
        raise ValueError("scores must be finite")
    if len(thresholds) != 2:
        raise ValueError("thresholds must contain exactly two values")
    t1, t2 = float(thresholds[0]), float(thresholds[1])
    if not (np.isfinite(t1) and np.isfinite(t2)):
        raise ValueError("thresholds must be finite")
    if t1 > t2:
        raise ValueError(f"thresholds must satisfy t1 <= t2: {t1} > {t2}")

    predictions = np.ones(values.size, dtype=int)
    predictions[values < t1] = 0
    predictions[values >= t2] = 2
    return predictions


def _threshold_candidates(
    scores: np.ndarray, max_candidates: int
) -> np.ndarray:
    if int(max_candidates) < 2:
        raise ValueError("max_candidates must be at least 2")
    unique = np.unique(scores)
    if unique.size == 1:
        value = float(unique[0])
        epsilon = max(1e-9, abs(value) * 1e-9)
        return np.asarray([value - epsilon, value + epsilon], dtype=float)

    if unique.size <= int(max_candidates) + 1:
        candidates = (unique[:-1] + unique[1:]) / 2.0
    else:
        indices = np.unique(
            np.linspace(0, unique.size - 1, int(max_candidates) + 1).round()
        ).astype(int)
        sampled = unique[indices]
        candidates = (sampled[:-1] + sampled[1:]) / 2.0

    if candidates.size < 2:
        span = max(float(unique[-1] - unique[0]), 1.0)
        epsilon = span * 1e-6
        candidates = np.concatenate(
            (
                np.asarray([unique[0] - epsilon]),
                candidates,
                np.asarray([unique[-1] + epsilon]),
            )
        )
    return candidates


def _widest_margin_pair(
    tied: np.ndarray,
    pair_left: np.ndarray,
    pair_right: np.ndarray,
    candidates: np.ndarray,
    scores: np.ndarray,
) -> int:
    """Pick the tied threshold pair furthest from any observed score."""
    if tied.size == 1:
        return int(tied[0])

    margins = np.empty(tied.size, dtype=float)
    chunk_size = 64
    for start in range(0, tied.size, chunk_size):
        chunk = tied[start : start + chunk_size]
        left = candidates[pair_left[chunk]][:, np.newaxis]
        right = candidates[pair_right[chunk]][:, np.newaxis]
        distances = np.minimum(
            np.abs(scores[np.newaxis, :] - left),
            np.abs(scores[np.newaxis, :] - right),
        )
        margins[start : start + chunk_size] = distances.min(axis=1)
    return int(tied[int(np.argmax(margins))])


def _weight_grid(
    grid: Sequence[float] | np.ndarray | None, step: float
) -> np.ndarray:
    if grid is None:
        if not np.isfinite(step) or step <= 0 or step > 1:
            raise ValueError("step must be in (0, 1]")
        count = int(round(1.0 / step))
        candidates = np.linspace(0.0, 1.0, count + 1)
    else:
        candidates = np.asarray(list(grid), dtype=float).ravel()
    if candidates.size == 0:
        raise ValueError("weight grid must not be empty")
    if not np.all(np.isfinite(candidates)):
        raise ValueError("weight grid contains non-finite values")
    if np.any((candidates < 0.0) | (candidates > 1.0)):
        raise ValueError("weight grid values must lie in [0, 1]")
    return candidates


def _as_feature_matrix(values: object, name: str) -> np.ndarray:
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2:
        raise ValueError(f"{name} must be a 2-D array")
    return matrix


def _as_probability_matrix(values: object, name: str) -> np.ndarray:
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] != N_CLASSES:
        raise ValueError(f"{name} must have shape (n, {N_CLASSES})")
    if not np.all(np.isfinite(matrix)):
        raise ValueError(f"{name} contains non-finite values")
    if np.any(matrix < -1e-9):
        raise ValueError(f"{name} contains negative probabilities")
    return matrix


def _as_labels(values: object, *, expected_length: int, name: str) -> np.ndarray:
    labels = np.asarray(values).ravel()
    if labels.size != expected_length:
        raise ValueError(
            f"{name} length mismatch: {labels.size} != {expected_length}"
        )
    if labels.size == 0:
        raise ValueError(f"{name} must not be empty")
    try:
        numeric = labels.astype(float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain integer labels") from exc
    if not np.all(np.isfinite(numeric)) or np.any(numeric != np.round(numeric)):
        raise ValueError(f"{name} must contain integer labels")
    integers = numeric.astype(int)
    unknown = set(np.unique(integers)) - set(LABELS)
    if unknown:
        raise ValueError(f"{name} contains unknown labels: {sorted(unknown)}")
    return integers


def _as_weight(value: object) -> float:
    weight = float(value)
    if not np.isfinite(weight) or weight < 0.0 or weight > 1.0:
        raise ValueError(f"weight must be in [0, 1], got {value!r}")
    return weight


__all__ = [
    "CHANCE_AUC",
    "DEFAULT_MAX_THRESHOLD_CANDIDATES",
    "DEFAULT_WEIGHT_STEP",
    "LABELS",
    "N_CLASSES",
    "ConstantProbabilityModel",
    "apply_thresholds",
    "choose_weights",
    "fit_modality_model",
    "fuse_proba",
    "learn_thresholds",
    "macro_auc",
    "ordinal_score",
    "predict_proba",
    "weights_from_auc",
]
