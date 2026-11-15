"""
Classroom vision detector using OpenCV.
Locates visible people/faces. Bounding-box position is not a behavior measurement.

This module only supplies localization. It does NOT infer:
- Attention, concentration, or engagement
- Emotions or mental states
- Identity (no face recognition)
"""
from __future__ import annotations

import cv2
import numpy as np
import logging
import os
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class Behavior(str, Enum):
    IN_SEAT = "in-seat"
    LEAVE_SEAT = "leave-seat"
    HAND_RAISED = "hand-raised"
    STANDING = "standing"
    UPRIGHT_POSTURE = "upright-posture"


@dataclass
class PersonDetection:
    bbox: tuple  # x, y, w, h
    confidence: float = 0.0
    body_bbox: tuple = None  # full body bbox if detected
    behaviors: list = field(default_factory=list)


@dataclass
class FrameObservation:
    frame_index: int
    timestamp_ms: int
    person_count: int
    detections: list = field(default_factory=list)
    in_seat_count: int | None = None
    leave_seat_count: int | None = None
    hand_raised_count: int | None = None
    standing_count: int | None = None
    upright_count: int | None = None


@dataclass
class WindowObservation:
    window_start_ms: int
    window_end_ms: int
    frame_count: int
    visible_seat_count: int
    total_configured_seats: int
    coverage: float
    data_quality: str
    in_seat_count: int | None
    leave_seat_count: int | None
    hand_raised_count: int | None
    standing_count: int | None
    upright_posture_count: int | None
    upright_proportion: float | None


def aggregate_window(observations: list, total_seats: int = 45) -> WindowObservation:
    """
    Aggregate frame-level observations into a time-window summary.

    Boundary rules:
    - Minimum 3 frames in window (worker separately enforces configured minimum)
    - Coverage < 60% -> 'limited' quality
    - Visible persons < 5 -> 'unavailable'
    """
    if not observations or len(observations) < 3:
        return None

    frame_count = len(observations)
    person_counts = [max(0, o.person_count) for o in observations]
    avg_visible = sum(person_counts) / len(person_counts) if person_counts else 0
    coverage = min(1.0, avg_visible / total_seats) if total_seats > 0 else 0

    def known_max(name):
        values = [getattr(o, name) for o in observations if getattr(o, name) is not None]
        return max(values) if values else None

    posture_ratios = [min(1.0, o.upright_count / o.person_count)
                      for o in observations if o.upright_count is not None and o.person_count > 0]
    # Locator-only frames cannot supply the posture/positive-behavior score.
    if not posture_ratios and known_max("hand_raised_count") is None:
        data_quality = "unavailable"
    elif avg_visible < 5:
        data_quality = "unavailable"
    elif coverage < 0.6:
        data_quality = "limited"
    else:
        data_quality = "usable"

    # Take max counts across the window (most conservative)
    return WindowObservation(
        window_start_ms=observations[0].timestamp_ms,
        window_end_ms=observations[-1].timestamp_ms,
        frame_count=frame_count,
        visible_seat_count=int(avg_visible),
        total_configured_seats=total_seats,
        coverage=round(coverage, 2),
        data_quality=data_quality,
        in_seat_count=known_max("in_seat_count"),
        leave_seat_count=known_max("leave_seat_count"),
        hand_raised_count=known_max("hand_raised_count"),
        standing_count=known_max("standing_count"),
        upright_posture_count=known_max("upright_count"),
        upright_proportion=(round(sum(posture_ratios) / len(posture_ratios), 2)
                            if posture_ratios else None),
    )


class ClassroomDetector:
    """
    Locates visible faces/upper bodies in classroom video.

    Uses OpenCV Haar cascades for localization only.
    Designed for single front-facing classroom camera scenarios.
    """

    def __init__(self, min_detection_confidence: float = 0.5, total_seats: int = 45):
        self.min_confidence = min_detection_confidence
        self.total_seats = total_seats

        # Load OpenCV pre-trained classifiers
        self.face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        )
        self.upper_body_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_upperbody.xml'
        )

        # Tracking state for temporal stability
        self._stable_count = {}
        self._frame_history = []

    def process_frame(self, frame: np.ndarray, frame_index: int, timestamp_ms: int) -> FrameObservation:
        """
        Process a single video frame and return structured observations.
        """
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)  # Improve contrast for detection

        detections = []

        # 1. Detect faces
        faces = self.face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30), flags=cv2.CASCADE_SCALE_IMAGE
        )

        # 2. Detect upper bodies (for seated students where face may be occluded)
        upper_bodies = self.upper_body_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=3, minSize=(60, 60), flags=cv2.CASCADE_SCALE_IMAGE
        )

        # 3. Merge face and body detections (avoid double-counting)
        for (fx, fy, fw, fh) in faces:
            # Check if this face is inside an upper body detection
            merged = False
            for (bx, by, bw, bh) in upper_bodies:
                if bx <= fx <= bx + bw and by <= fy <= by + bh:
                    # Face is within body - use body bbox for full person
                    person = PersonDetection(
                        bbox=(bx, by, bw, bh),
                        confidence=0.8,
                        body_bbox=(bx, by, bw, bh),
                    )
                    detections.append(person)
                    merged = True
                    break
            if not merged:
                person = PersonDetection(bbox=(fx, fy, fw, fh), confidence=0.7)
                detections.append(person)

        # 4. Add unmatched upper body detections
        for (bx, by, bw, bh) in upper_bodies:
            matched = False
            for (fx, fy, fw, fh) in faces:
                if bx <= fx <= bx + bw and by <= fy <= by + bh:
                    matched = True
                    break
            if not matched:
                person = PersonDetection(bbox=(bx, by, bw, bh), confidence=0.6)
                detections.append(person)

        # 5. Aggregate counts
        obs = FrameObservation(
            frame_index=frame_index,
            timestamp_ms=timestamp_ms,
            person_count=len(detections),
            detections=detections,
        )

        return obs

    def aggregate_window(self, observations: list, total_seats: int = 45) -> WindowObservation:
        return aggregate_window(observations, total_seats)

    def reset_tracking(self):
        """Discard temporal state after a camera/viewpoint change."""
        self._stable_count.clear()
        self._frame_history.clear()

    def close(self):
        pass  # OpenCV Haar cascades don't need explicit cleanup


class YoloDetector:
    """
    YOLOv8-face 人脸检测器（真实模型后端，替换 Haar 原型）。

    只输出人员/人脸位置；不使用框的位置或大小推断坐姿、站立或离座。
    课堂行为由独立的专用行为模型提供。
    权重缺失时 is_available() 返回 False，process_frame 返回空观测（优雅降级）。
    """

    def __init__(self, weights_path=None, min_confidence=0.4, total_seats=45, model_factory=None, mode=None,
                 device=None):
        self.min_confidence = min_confidence
        self.total_seats = total_seats
        self.model = None
        self.weights_path = weights_path
        # None 表示交给 ultralytics 自行选择（cuda 可用则 cuda）; "cpu"/"cuda" 强制指定。
        self.device = device or None
        # 模式推断：权重文件名含 "face" → face（框即人头）；否则 person（框上部 30% 为人头）
        self.mode = mode or ("face" if weights_path and "face" in os.path.basename(weights_path).lower() else "person")
        if model_factory is not None:
            # 测试注入：权重文件存在时采用注入模型；权重缺失时保持不可用（优雅降级）
            if weights_path and os.path.exists(weights_path):
                self.model = model_factory
            else:
                logger.warning("YOLO weights not found at %s; detector unavailable", weights_path)
        else:
            self._load_model()

    def _load_model(self):
        path = self.weights_path
        if not path or not os.path.exists(path):
            logger.warning("YOLO weights not found at %s; detector unavailable", path)
            return
        try:
            from ultralytics import YOLO
            self.model = YOLO(path)
            logger.info("YOLOv8 detector loaded from %s (mode=%s)", path, self.mode)
        except Exception as e:
            logger.warning("Failed to load YOLO model from %s: %s", path, e)
            self.model = None

    def is_available(self) -> bool:
        return self.model is not None

    def process_frame(self, frame: np.ndarray, frame_index: int, timestamp_ms: int) -> FrameObservation:
        h, w = frame.shape[:2]
        detections = []
        if self.model is not None:
            try:
                infer_kwargs = {"verbose": False, "conf": self.min_confidence}
                if self.device:
                    infer_kwargs["device"] = self.device
                results = self.model(frame, **infer_kwargs)
                for r in results:
                    if r.boxes is None or r.boxes.xyxy is None:
                        continue
                    # COCO 80 类模型：person 模式仅计 class 0（person）；face 权重 class 0 即 face。
                    # cls 为 ultralytics 张量或 mock 的 shape-(1,) np.array，统一经 .item() 取标量。
                    cls_ids = getattr(r.boxes, "cls", None)
                    if cls_ids is None:
                        cls_ids = [None] * len(r.boxes.xyxy)
                    for box, conf, cls_id in zip(r.boxes.xyxy, r.boxes.conf, cls_ids):
                        if cls_id is not None and cls_id.item() != 0:
                            continue  # 非目标类（椅子/显示器等 conf≥阈值）不计入人数
                        x1, y1, x2, y2 = [float(v) for v in box]
                        bw, bh = x2 - x1, y2 - y1
                        if bw <= 0 or bh <= 0:
                            continue
                        person = PersonDetection(
                            bbox=(int(x1), int(y1), int(bw), int(bh)),
                            confidence=float(conf.item()),
                        )
                        detections.append(person)
            except Exception as e:
                logger.warning("YOLO inference failed: %s", e)

        obs = FrameObservation(
            frame_index=frame_index, timestamp_ms=timestamp_ms,
            person_count=len(detections), detections=detections,
        )
        return obs

    def aggregate_window(self, observations: list, total_seats: int = 45) -> WindowObservation:
        return aggregate_window(observations, total_seats)

    def reset_tracking(self):
        """Uniform detector API; YOLO currently has no temporal state."""
        return None

    def close(self):
        # 共享缓存场景下由 vision.py 控制生命周期；此处仅清引用，不强制卸载
        return None
