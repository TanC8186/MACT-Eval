"""窗口级场景质量门控测试。"""
from app.models.detector import ClassroomDetector
from app.workers.vision import (
    _build_scene_window_metadata,
    _build_window_scene_metadata,
    _should_reset_tracking,
)


def test_should_reset_tracking_on_viewpoint_change():
    assert _should_reset_tracking("classroom_student", "classroom_teacher") is True
    assert _should_reset_tracking("classroom_student", "slides_or_screen") is True
    assert _should_reset_tracking("transition", "classroom_student") is True


def test_should_not_reset_tracking_within_same_scene():
    assert _should_reset_tracking("classroom_student", "classroom_student") is False
    assert _should_reset_tracking(None, "classroom_student") is False


def test_detector_reset_tracking_clears_state():
    detector = ClassroomDetector()
    detector._stable_count = {"0": 5}
    detector._frame_history = [1, 2, 3]
    detector.reset_tracking()
    assert detector._stable_count == {}
    assert detector._frame_history == []


def test_scene_window_metadata_marks_mixed_view_as_limited():
    frames = ([{"scene_type": "classroom_student", "usable": True}] * 3
              + [{"scene_type": "classroom_teacher", "usable": False}] * 2)
    result = _build_scene_window_metadata(frames)
    assert result["usable_frame_ratio"] == 0.6
    assert result["data_quality"] == "limited"
    assert result["dominant_scene"] == "classroom_student"
    assert result["view_switch_count"] == 1


def test_scene_window_metadata_counts_transition_and_screen():
    frames = [
        {"scene_type": "classroom_student", "usable": True},
        {"scene_type": "transition", "usable": False},
        {"scene_type": "slides_or_screen", "usable": False},
    ]
    result = _build_scene_window_metadata(frames)
    assert result["view_switch_count"] == 2
    assert result["dominant_scene"] == "classroom_student"


def test_window_scene_metadata_groups_by_time_window():
    sampled = [
        {"timestamp_ms": 0, "scene_type": "classroom_student", "usable": True},
        {"timestamp_ms": 2000, "scene_type": "classroom_student", "usable": True},
        {"timestamp_ms": 4000, "scene_type": "slides_or_screen", "usable": False},
        {"timestamp_ms": 60000, "scene_type": "classroom_teacher", "usable": False},
    ]
    result = _build_window_scene_metadata(sampled, window_duration=60_000)
    assert len(result) == 2
    assert result[0]["window_index"] == 0
    assert result[0]["usable_frame_ratio"] == 0.67
    assert result[1]["dominant_scene"] == "classroom_teacher"
    assert result[1]["data_quality"] == "unavailable"
