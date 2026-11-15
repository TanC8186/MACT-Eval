"""Tests for the video/speech rule baseline (spec section 5)."""
from __future__ import annotations

import pytest

from public_data.rule_baseline import (
    QUESTION_DENSITY_SATURATION_PER_MINUTE,
    SPEECH_RATE_MAX,
    SPEECH_RATE_MIN,
    SPEECH_WEIGHTS,
    VIDEO_WEIGHTS,
    fuse,
    question_density,
    rate_score,
    speech_score,
    video_score,
)


def test_rule_weights_match_spec() -> None:
    assert VIDEO_WEIGHTS == {
        "posture_score": 0.5,
        "stability_score": 0.2,
        "positive_behavior_ratio": 0.3,
    }
    assert SPEECH_WEIGHTS == {
        "speech_coverage": 0.5,
        "question_density": 0.3,
        "rate_score": 0.2,
    }
    assert QUESTION_DENSITY_SATURATION_PER_MINUTE == 1.0
    assert SPEECH_RATE_MIN == 150.0
    assert SPEECH_RATE_MAX == 300.0


def test_video_score_weighted_sum() -> None:
    # 100 * (0.5*0.8 + 0.2*0.6 + 0.3*0.5) = 67.0
    assert video_score(0.8, 0.6, 0.5) == pytest.approx(67.0)


def test_video_score_renormalizes_missing_components() -> None:
    # stability missing: (0.5*0.8 + 0.3*0.5) / (0.5 + 0.3) = 0.6875
    assert video_score(0.8, None, 0.5) == pytest.approx(68.75)
    # posture missing: (0.2*0.6 + 0.3*0.5) / (0.2 + 0.3) = 0.54
    assert video_score(None, 0.6, 0.5) == pytest.approx(54.0)
    # behavior missing: (0.5*0.8 + 0.2*0.6) / (0.5 + 0.2) = 0.742857...
    assert video_score(0.8, 0.6, None) == pytest.approx(74.2857142857)
    # single available component
    assert video_score(None, 0.5, None) == pytest.approx(50.0)


def test_video_score_all_missing_is_none() -> None:
    assert video_score(None, None, None) is None
    assert video_score(float("nan"), None, float("inf")) is None


def test_video_score_treats_non_finite_as_missing() -> None:
    assert video_score(float("nan"), 0.6, 0.5) == pytest.approx(54.0)
    assert video_score(0.8, float("inf"), 0.5) == pytest.approx(68.75)


def test_video_score_clamps_to_0_100() -> None:
    assert video_score(2.0, 2.0, 2.0) == pytest.approx(100.0)
    assert video_score(-1.0, -1.0, -1.0) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        (0.0, 0.0),
        (75.0, 0.5),
        (149.0, 1 - 1 / 150),
        (150.0, 1.0),
        (225.0, 1.0),
        (300.0, 1.0),
        (301.0, 1 - 1 / 300),
        (450.0, 0.5),
        (600.0, 0.0),
        (-50.0, 0.0),
    ],
)
def test_rate_score_bounds_and_linear_decay(rate: float, expected: float) -> None:
    assert rate_score(rate) == pytest.approx(expected)


def test_rate_score_missing_values() -> None:
    assert rate_score(None) is None
    assert rate_score(float("nan")) is None


@pytest.mark.parametrize(
    ("question_count", "window_seconds", "expected"),
    [
        (0, 60.0, 0.0),
        (1, 60.0, 1.0),
        (2, 60.0, 1.0),
        (1, 120.0, 0.5),
        (3, 60.0, 1.0),
        (-1, 60.0, 0.0),
    ],
)
def test_question_density(
    question_count: int, window_seconds: float, expected: float
) -> None:
    assert question_density(question_count, window_seconds) == pytest.approx(expected)


def test_question_density_invalid_window_is_missing() -> None:
    assert question_density(1, 0.0) is None
    assert question_density(1, -60.0) is None
    assert question_density(None, 60.0) is None


def test_speech_score_weighted_sum() -> None:
    # coverage 0.5, 2 questions/min -> density 1, rate 300 -> 1
    # 100 * (0.5*0.5 + 0.3*1 + 0.2*1) = 75.0
    assert speech_score(0.5, 2, 300, 60) == pytest.approx(75.0)
    assert speech_score(0.4, 1, 150, 60) == pytest.approx(70.0)


def test_speech_score_low_rate_and_no_questions() -> None:
    # coverage 0, density 0, rate 75 -> rate_score 0.5 -> 10.0
    assert speech_score(0.0, 0, 75, 60) == pytest.approx(10.0)


def test_speech_score_uses_questions_per_minute() -> None:
    # 1 question in 120 s -> 0.5/min -> density 0.5; rate 600 -> 0
    # 100 * (0.5*0.6 + 0.3*0.5 + 0.2*0) = 45.0
    assert speech_score(0.6, 1, 600, 120) == pytest.approx(45.0)


def test_speech_score_renormalizes_missing_components() -> None:
    # coverage missing: (0.3*1 + 0.2*1) / (0.3 + 0.2) = 1.0
    assert speech_score(None, 2, 300, 60) == pytest.approx(100.0)
    # only coverage available
    assert speech_score(0.5, None, None, 60) == pytest.approx(50.0)
    # invalid window -> whole speech score is unavailable (protocol 4.4)
    assert speech_score(0.5, 2, 300, 0) is None


def test_speech_score_all_missing_is_none() -> None:
    assert speech_score(None, None, None, 60) is None


def test_speech_score_clamps_components_and_total() -> None:
    assert speech_score(5.0, 2, 300, 60) == pytest.approx(100.0)
    assert speech_score(-1.0, -1, -100, 60) == pytest.approx(0.0)


def test_fuse_weighted_sum_and_normalization() -> None:
    assert fuse(80, 60) == pytest.approx(70.0)
    assert fuse(80, 60, w_video=0.6, w_speech=0.4) == pytest.approx(72.0)


def test_fuse_uses_available_modality_when_other_missing() -> None:
    assert fuse(80, None) == pytest.approx(80.0)
    assert fuse(None, 60) == pytest.approx(60.0)
    assert fuse(80, None, w_video=0.9, w_speech=0.1) == pytest.approx(80.0)


def test_fuse_both_missing_is_none() -> None:
    assert fuse(None, None) is None
    assert fuse(float("nan"), None) is None


def test_fuse_normalizes_weights_and_falls_back_to_equal() -> None:
    # A non-positive weight drops that modality, the other weight renormalizes.
    assert fuse(80, 60, w_video=0.0, w_speech=0.5) == pytest.approx(60.0)
    # Both weights unusable -> equal weights.
    assert fuse(80, 60, w_video=-1.0, w_speech=-1.0) == pytest.approx(70.0)
    assert fuse(80, 60, w_video=float("nan"), w_speech=0.0) == pytest.approx(70.0)
    # Weights do not need to sum to one.
    assert fuse(80, 60, w_video=2.0, w_speech=2.0) == pytest.approx(70.0)


def test_fuse_clamps_scores_to_0_100() -> None:
    assert fuse(120, -20) == pytest.approx(50.0)
    assert fuse(120, None) == pytest.approx(100.0)


def test_matches_java_rule_baseline_service() -> None:
    """Same synthetic inputs as RuleBaselineServiceTest (tolerance 1e-9)."""
    tolerance = {"abs": 1e-9}
    assert video_score(0.7, None, None) == pytest.approx(70.0, **tolerance)
    assert video_score(1.5, 1.0, -0.5) == pytest.approx(70.0, **tolerance)
    assert speech_score(0.8, 2, 200.0, 60.0) == pytest.approx(90.0, **tolerance)
    assert speech_score(None, 1, 200.0, 60.0) == pytest.approx(100.0, **tolerance)
    assert speech_score(1.0, 3, 200.0, 60.0) == pytest.approx(100.0, **tolerance)
    assert speech_score(1.0, 1, 200.0, 120.0) == pytest.approx(85.0, **tolerance)
    assert speech_score(1.0, 1, 150.0, 60.0) == pytest.approx(100.0, **tolerance)
    assert speech_score(1.0, 1, 450.0, 60.0) == pytest.approx(90.0, **tolerance)
    assert speech_score(1.0, 1, 0.0, 60.0) == pytest.approx(80.0, **tolerance)
    assert speech_score(1.0, 1, 600.0, 60.0) == pytest.approx(80.0, **tolerance)
    assert speech_score(0.6, 5, None, 0.0) is None
    assert fuse(80, 60, 0.6, 0.4) == pytest.approx(72.0, **tolerance)
    assert fuse(80, 60, 3, 1) == pytest.approx(75.0, **tolerance)
    assert fuse(80, 60, 0, 0) == pytest.approx(70.0, **tolerance)
    assert fuse(80, 60, -1, float("nan")) == pytest.approx(70.0, **tolerance)
    assert fuse(120, 30, 0.5, 0.5) == pytest.approx(65.0, **tolerance)
    assert fuse(-20, 80, 0.5, 0.5) == pytest.approx(40.0, **tolerance)
