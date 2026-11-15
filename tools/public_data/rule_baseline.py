"""Video/speech rule baseline (spec section 5).

Mirrors the Java ``RuleBaselineService`` so offline experiments and the online
report pipeline compute the same scores:

* every component is a 0-1 score;
* missing components (``None`` or non-finite) are dropped and the remaining
  weights are renormalised;
* a modality with no usable component is ``None`` (never 0);
* modality scores are returned on the 0-100 scale and clamped to it.

The thresholds below are engineering thresholds documented in
``docs/experiments/rule-baseline-protocol.md``; they are meant to be checked
with sensitivity analysis, not treated as absolute educational standards.
"""
from __future__ import annotations

import math
from typing import Mapping

#: Video component weights (spec 5.1 C).
VIDEO_WEIGHTS: dict[str, float] = {
    "posture_score": 0.5,
    "stability_score": 0.2,
    "positive_behavior_ratio": 0.3,
}

#: Speech component weights (spec 5.2).
SPEECH_WEIGHTS: dict[str, float] = {
    "speech_coverage": 0.5,
    "question_density": 0.3,
    "rate_score": 0.2,
}

#: ``question_density`` saturates at one question per minute.
QUESTION_DENSITY_SATURATION_PER_MINUTE = 1.0

#: Comfortable speech-rate band in characters per minute.
SPEECH_RATE_MIN = 150.0
SPEECH_RATE_MAX = 300.0


def video_score(
    posture: float | None,
    stability: float | None,
    positive_behavior_ratio: float | None,
) -> float | None:
    """Return the 0-100 video rule score, or ``None`` if every part is missing.

    ``100 * weighted_mean(posture 0.5, stability 0.2, behavior 0.3)``.
    """
    normalized = _weighted_mean(
        {
            "posture_score": posture,
            "stability_score": stability,
            "positive_behavior_ratio": positive_behavior_ratio,
        },
        VIDEO_WEIGHTS,
    )
    return None if normalized is None else 100.0 * normalized


def question_density(
    question_count: int | float | None,
    window_seconds: float | None,
) -> float | None:
    """Return ``min(1, questions_per_minute / 1.0)``.

    ``None`` when the question count is missing or the window length is not
    positive (the rate would be undefined).
    """
    count = _as_float(question_count)
    window = _as_float(window_seconds)
    if count is None or window is None or window <= 0:
        return None
    questions_per_minute = count / (window / 60.0)
    return _clamp01(
        min(1.0, questions_per_minute / QUESTION_DENSITY_SATURATION_PER_MINUTE)
    )


def rate_score(speech_rate: float | None) -> float | None:
    """Return the 0-1 speech-rate score.

    ``1`` inside [150, 300] characters/minute, linear decay to 0 outside:
    ``1 - (150 - rate) / 150`` below and ``1 - (rate - 300) / 300`` above,
    clipped to [0, 1].  ``None`` for missing/non-finite input.
    """
    rate = _as_float(speech_rate)
    if rate is None:
        return None
    if SPEECH_RATE_MIN <= rate <= SPEECH_RATE_MAX:
        return 1.0
    if rate < SPEECH_RATE_MIN:
        return _clamp01(1.0 - (SPEECH_RATE_MIN - rate) / SPEECH_RATE_MIN)
    return _clamp01(1.0 - (rate - SPEECH_RATE_MAX) / SPEECH_RATE_MAX)


def speech_score(
    coverage: float | None,
    question_count: int | float | None,
    speech_rate: float | None,
    window_seconds: float | None,
) -> float | None:
    """Return the 0-100 speech rule score, or ``None`` if every part is missing.

    ``100 * weighted_mean(coverage 0.5, question_density 0.3, rate_score 0.2)``.

    A non-positive or non-finite ``window_seconds`` is invalid input: the
    whole speech score is ``None`` (protocol section 4.4), not a renormalised
    score over coverage/rate.
    """
    window = _as_float(window_seconds)
    if window is None or window <= 0:
        return None
    normalized = _weighted_mean(
        {
            "speech_coverage": coverage,
            "question_density": question_density(question_count, window),
            "rate_score": rate_score(speech_rate),
        },
        SPEECH_WEIGHTS,
    )
    return None if normalized is None else 100.0 * normalized


def fuse(
    video: float | None,
    speech: float | None,
    w_video: float = 0.5,
    w_speech: float = 0.5,
) -> float | None:
    """Late-fuse two 0-100 modality scores with weight renormalisation.

    * both missing -> ``None``;
    * one missing -> the available score (scaled about its own weight);
    * both present -> normalised weighted mean, clamped to [0, 100].

    Non-positive or non-finite weights are treated as unusable; if both are
    unusable the fusion falls back to equal weights.
    """
    video_value = _as_float(video)
    speech_value = _as_float(speech)
    if video_value is None and speech_value is None:
        return None

    if video_value is not None:
        video_value = _clamp(video_value, 0.0, 100.0)
    if speech_value is not None:
        speech_value = _clamp(speech_value, 0.0, 100.0)

    if video_value is None:
        return speech_value
    if speech_value is None:
        return video_value

    video_weight = _usable_weight(w_video)
    speech_weight = _usable_weight(w_speech)
    weight_sum = video_weight + speech_weight
    if weight_sum <= 0:
        video_weight = speech_weight = 0.5

    fused = (
        video_weight * video_value + speech_weight * speech_value
    ) / (video_weight + speech_weight)
    return _clamp(fused, 0.0, 100.0)


def _weighted_mean(
    components: Mapping[str, float | None],
    weights: Mapping[str, float],
) -> float | None:
    total = 0.0
    weight_sum = 0.0
    for name, weight in weights.items():
        value = _as_float(components.get(name))
        if value is None:
            continue
        total += weight * _clamp(value, 0.0, 1.0)
        weight_sum += weight
    if weight_sum <= 0:
        return None
    return _clamp(total / weight_sum, 0.0, 1.0)


def _usable_weight(value: float | None) -> float:
    weight = _as_float(value)
    return weight if weight is not None and weight > 0 else 0.0


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _clamp01(value: float) -> float:
    return _clamp(value, 0.0, 1.0)


__all__ = [
    "QUESTION_DENSITY_SATURATION_PER_MINUTE",
    "SPEECH_RATE_MAX",
    "SPEECH_RATE_MIN",
    "SPEECH_WEIGHTS",
    "VIDEO_WEIGHTS",
    "fuse",
    "question_density",
    "rate_score",
    "speech_score",
    "video_score",
]
