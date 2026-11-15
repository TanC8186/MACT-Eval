"""Model-backed, frame-local classroom action evidence (not identity tracking).

This detector is separate from the existing scoring proxy. Only classes actually
declared by a classroom checkpoint are shown as model action predictions.
"""
from __future__ import annotations

import hashlib
import logging
import math
from pathlib import Path
import threading

logger = logging.getLogger(__name__)

ACTION_LABELS = {
    "hand-raising": ("hand_raising", "举手"),
    "hand_raising": ("hand_raising", "举手"),
    "reading": ("reading", "阅读"),
    "writing": ("writing", "书写"),
    "discussing": ("discussing", "讨论"),
    "bow-head": ("bow_head", "低头"),
    "turn-head": ("turn_head", "转头"),
    "using phone": ("using_phone", "使用手机"),
    "using_phone": ("using_phone", "使用手机"),
    "leaning over the table": ("leaning_over_table", "趴桌"),
    "leaning_over_table": ("leaning_over_table", "趴桌"),
    "standing": ("standing", "站立"),
    "leave-seat": ("leave_seat", "离座"),
}
_cache: dict[tuple, "ActionEvidenceDetector"] = {}
_cache_lock = threading.Lock()


def resolve_behavior_weights(settings, root: Path) -> str:
    explicit = str(getattr(settings, "behavior_weights", "") or "").strip()
    if explicit:
        # A configured-but-missing model must not silently select another one.
        return str(Path(explicit).resolve()) if Path(explicit).is_file() else ""
    for relative in (
        "models/classroom-behavior.pt",
        "docs/experiments/runs/scb5-yolo/train/weights/best.pt",
        "docs/experiments/runs/scb-yolo/train/weights/best.pt",
    ):
        # Local checkout and Docker both mount their model directory relative
        # to the process working directory; the repository root is a fallback.
        for base in (root, Path.cwd()):
            candidate = base / relative
            if candidate.is_file():
                return str(candidate.resolve())
    return ""


class ActionEvidenceDetector:
    def __init__(self, model, weights: str, device: str | None, confidence: float,
                 image_size: int = 640, max_detections: int = 100):
        self.model = model
        self.device = device
        self.confidence = confidence
        self.image_size = image_size
        self.max_detections = max_detections
        self.lock = threading.Lock()
        names = getattr(model, "names", {})
        self.names = dict(enumerate(names)) if isinstance(names, list) else names
        if not any(str(name).lower() in ACTION_LABELS for name in self.names.values()):
            raise ValueError("checkpoint has no supported classroom action classes")
        checkpoint = Path(weights)
        model_version = (checkpoint.parent.parent.parent.name + "/best"
                         if checkpoint.stem == "best" else checkpoint.stem)
        self.context = {
            "detectionSource": "behavior_model",
            "modelVersion": model_version,
            "modelDigest": hashlib.sha256(Path(weights).read_bytes()).hexdigest(),
            "confidenceThreshold": confidence,
            "inferenceImageSize": image_size,
            "maxDetections": max_detections,
            "supportedActions": [ACTION_LABELS[str(n).lower()][1]
                                 for n in self.names.values() if str(n).lower() in ACTION_LABELS],
        }

    def detect(self, frame) -> list[dict]:
        height, width = frame.shape[:2]
        if width <= 0 or height <= 0:
            return []
        kwargs = {"verbose": False, "conf": self.confidence,
                  "imgsz": self.image_size, "max_det": self.max_detections}
        if self.device:
            kwargs["device"] = self.device
        # Ultralytics predictor instances carry mutable inference state.
        with self.lock:
            results = self.model(frame, **kwargs)
        detections = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box, conf, cls_id in zip(boxes.xyxy, boxes.conf, boxes.cls):
                name = str(self.names.get(int(cls_id.item()), "")).lower()
                if name not in ACTION_LABELS:
                    continue  # COCO person/class 0 is never a classroom action.
                confidence = float(conf.item())
                values = [float(x) for x in box]
                if not all(math.isfinite(v) for v in values + [confidence]):
                    continue
                if not self.confidence <= confidence <= 1:
                    continue
                x1, y1, x2, y2 = values
                x1, x2 = max(0., min(width, x1)), max(0., min(width, x2))
                y1, y2 = max(0., min(height, y1)), max(0., min(height, y2))
                if x2 <= x1 or y2 <= y1:
                    continue
                code, label = ACTION_LABELS[name]
                detections.append({
                    "code": code, "label": label, "className": name,
                    "confidence": round(confidence, 4),
                    "bbox": [x1 / width, y1 / height,
                             (x2 - x1) / width, (y2 - y1) / height],
                })
        # Bounded output, stable spatial numbering within this frame only.
        detections = sorted(detections, key=lambda d: d["confidence"], reverse=True)[:self.max_detections]
        detections.sort(key=lambda d: (d["bbox"][0], d["bbox"][1]))
        for index, detection in enumerate(detections, 1):
            detection["subjectId"] = f"target-{index}"
        return detections


def get_action_detector(settings, root: Path, device: str | None):
    weights = resolve_behavior_weights(settings, root)
    if not weights:
        return None
    stat = Path(weights).stat()
    confidence = max(0.1, min(0.99, float(getattr(settings, "behavior_confidence", 0.4))))
    image_size = max(320, min(1536, int(getattr(settings, "behavior_imgsz", 640))))
    max_detections = max(1, min(300, int(getattr(settings, "behavior_max_detections", 100))))
    key = (weights, stat.st_size, stat.st_mtime_ns, device, confidence, image_size, max_detections)
    with _cache_lock:
        if key not in _cache:
            try:
                from ultralytics import YOLO
                detector = ActionEvidenceDetector(YOLO(weights), weights, device, confidence,
                                                  image_size, max_detections)
                # One currently configured behavior checkpoint per process.
                _cache.clear()
                _cache[key] = detector
                logger.info("Action evidence model loaded: %s", detector.context)
            except Exception as exc:
                logger.warning("Action evidence model unavailable: %s", exc)
                return None
        return _cache[key]
