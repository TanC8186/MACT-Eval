"""Lightweight, explainable scene gating for classroom video analysis."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np


class SceneType(str, Enum):
    CLASSROOM_STUDENT = "classroom_student"
    CLASSROOM_TEACHER = "classroom_teacher"
    SLIDES_OR_SCREEN = "slides_or_screen"
    TRANSITION = "transition"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SceneResult:
    scene_type: SceneType
    usable: bool
    confidence: float
    reason: str


def _frame_difference(current: np.ndarray, previous: np.ndarray | None) -> float:
    if previous is None or current.shape != previous.shape:
        return 0.0
    a = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
    return float(np.mean(cv2.absdiff(a, b)) / 255.0)


def _is_slide_like(frame: np.ndarray) -> bool:
    """Detect a bright, text/line-heavy presentation screen without OCR."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    bright_ratio = float(np.mean(gray > 205))
    edges = cv2.Canny(gray, 80, 180)
    horizontal = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=max(20, frame.shape[1] // 8),
        minLineLength=max(40, frame.shape[1] // 3), maxLineGap=8,
    )
    horizontal_count = 0
    if horizontal is not None:
        # OpenCV may return Hough segments as (N, 1, 4) or (N, 4)
        # depending on the build. Normalize both layouts before iterating;
        # indexing ``horizontal[:, 0]`` on the latter yields scalar values.
        for line in horizontal.reshape(-1, 4):
            x1, y1, x2, y2 = [int(value) for value in line]
            if abs(y2 - y1) <= 3 and abs(x2 - x1) >= frame.shape[1] * 0.35:
                horizontal_count += 1
    return bright_ratio >= 0.62 and horizontal_count >= 3


def classify_scene(
    frame: np.ndarray,
    previous_frame: np.ndarray | None = None,
    face_count: int | None = None,
) -> SceneResult:
    """Classify a sampled frame before student behavior inference.

    The classifier is intentionally conservative: uncertain frames are unusable.
    ``face_count`` can be supplied by the detector to avoid running a second face
    detector. When absent, this function only makes screen/transition decisions.
    """
    if frame is None or frame.size == 0:
        return SceneResult(SceneType.UNKNOWN, False, 0.0, "empty frame")

    difference = _frame_difference(frame, previous_frame)
    mean_luma = float(np.mean(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
    if mean_luma < 8 or difference >= 0.42:
        return SceneResult(SceneType.TRANSITION, False, 0.96, "black or abrupt scene change")

    if _is_slide_like(frame):
        return SceneResult(SceneType.SLIDES_OR_SCREEN, False, 0.88, "bright text/line-heavy screen")

    if face_count is not None:
        if face_count >= 2:
            return SceneResult(SceneType.CLASSROOM_STUDENT, True, 0.82, "multiple visible student faces")
        if face_count == 1:
            return SceneResult(SceneType.CLASSROOM_TEACHER, False, 0.62, "single face; student view not confirmed")

    return SceneResult(SceneType.UNKNOWN, False, 0.35, "student classroom view not confirmed")
