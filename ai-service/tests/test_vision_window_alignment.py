from types import SimpleNamespace

from app.workers.vision import _group_observations_by_window, _sampling_policy


def test_observations_keep_original_time_window_after_filtering():
    observations = [
        SimpleNamespace(timestamp_ms=0),
        SimpleNamespace(timestamp_ms=59000),
        SimpleNamespace(timestamp_ms=120000),
    ]
    groups = _group_observations_by_window(observations, window_duration=60)
    assert list(groups) == [0, 2]
    assert len(groups[0]) == 2
    assert len(groups[2]) == 1


def test_long_video_sampling_cap_keeps_windows_reachable():
    """80/120-minute lessons must not silently produce zero windows.

    A fixed 300-sample cap made the effective interval 18-26s for these
    durations, so every 60s window had fewer than five samples and was dropped.
    """
    for minutes in (80, 120):
        policy = _sampling_policy(30 * 60 * minutes, 30, 2, 60, 5)
        assert policy["max_samples"] <= policy["hard_max_samples"]
        assert policy["max_samples"] >= policy["estimated_windows"] * 7
        assert policy["effective_sample_interval_sec"] <= 10
        assert policy["estimated_samples_per_window"] >= 6


def test_short_video_keeps_existing_300_sample_cap():
    policy = _sampling_policy(30 * 120, 30, 2, 60, 5)
    assert policy["max_samples"] == 300
    assert policy["stride_mult"] == 1
    assert policy["effective_sample_interval_sec"] == 2


def test_sampling_policy_honours_non_default_min_observations():
    # min=12 needs at most a 2x stride (4 s), not the 3x stride implied by a
    # duration-only cap; the policy must raise the cap and keep 15 samples/window.
    policy = _sampling_policy(30 * 60 * 40, 30, 2, 60, 12)
    assert policy["min_observations_unreachable"] is False
    assert policy["stride_mult"] == 2
    assert policy["estimated_samples_per_window"] >= 12
    assert policy["max_samples"] >= 600

    # 60 samples per 60 s at a 2 s interval is physically impossible.
    impossible = _sampling_policy(30 * 60 * 40, 30, 2, 60, 60)
    assert impossible["min_observations_unreachable"] is True
    assert "window_capacity" in impossible["unreachable_reason"]

    # 120 min with min=20 would need the entire 3,682-frame sample stream,
    # beyond the hard cap; the job must fail rather than emit zero windows.
    capped = _sampling_policy(30 * 60 * 120, 30, 2, 60, 20)
    assert capped["min_observations_unreachable"] is True
    assert "hard_sample_cap" in capped["unreachable_reason"]


def test_sampling_policy_flags_unknown_frame_count():
    policy = _sampling_policy(0, 30, 2, 60, 5)
    assert policy["frame_count_unknown"] is True
    assert policy["estimated_windows"] == 0
    assert policy["estimated_samples_per_window"] is None
