"""镜头场景门控的最小行为契约。"""
from __future__ import annotations

import cv2
import numpy as np

from app.models.scene_classifier import SceneType, classify_scene


def _solid_frame(value: int = 128) -> np.ndarray:
    return np.full((240, 320, 3), value, dtype=np.uint8)


def _student_frame() -> np.ndarray:
    frame = _solid_frame(90)
    # 测试替身：三个高对比圆形，模拟检测到的学生脸区域。
    for center in ((80, 120), (160, 110), (240, 125)):
        cv2.circle(frame, center, 28, (220, 220, 220), -1)
        cv2.circle(frame, (center[0] - 9, center[1] - 5), 4, (30, 30, 30), -1)
        cv2.circle(frame, (center[0] + 9, center[1] - 5), 4, (30, 30, 30), -1)
    return frame


def _slide_frame() -> np.ndarray:
    frame = np.full((240, 320, 3), 245, dtype=np.uint8)
    for y in range(35, 210, 22):
        cv2.rectangle(frame, (35, y), (285, y + 7), (35, 35, 35), -1)
    return frame


def test_black_frame_is_transition():
    result = classify_scene(_solid_frame(0), previous_frame=_solid_frame(220))
    assert result.scene_type is SceneType.TRANSITION
    assert result.usable is False


def test_text_heavy_frame_is_slides_or_screen():
    result = classify_scene(_slide_frame(), previous_frame=_slide_frame())
    assert result.scene_type is SceneType.SLIDES_OR_SCREEN
    assert result.usable is False


def test_multi_face_frame_is_student_classroom():
    result = classify_scene(_student_frame(), previous_frame=_student_frame(), face_count=3)
    assert result.scene_type is SceneType.CLASSROOM_STUDENT
    assert result.usable is True
