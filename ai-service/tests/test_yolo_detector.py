"""YoloDetector 单元测试：注入假检测结果，验证行为分类与窗口聚合契约。"""
import numpy as np
import pytest

from app.models.detector import YoloDetector, Behavior, FrameObservation


class FakeYolo:
    """模拟 ultralytics 推理输出：bbox (xyxy) + conf + cls。

    boxes 元素为 5 元组 (x1, y1, x2, y2, conf)（cls 恒 0，兼容既有测试）
    或 6 元组 (x1, y1, x2, y2, conf, cls)（指定类别，用于类别过滤用例）。
    """

    def __init__(self, boxes):
        self.boxes = boxes

    def __call__(self, frame, verbose=False, conf=0.4):
        import types
        res = types.SimpleNamespace()
        res.boxes = types.SimpleNamespace()
        xyxy, confs, clses = [], [], []
        for box in self.boxes:
            x1, y1, x2, y2, c = box[:5]
            cls_ = box[5] if len(box) > 5 else 0
            xyxy.append(np.array([x1, y1, x2, y2], dtype=float))
            confs.append(np.array([c]))
            clses.append(np.array([cls_]))
        res.boxes.xyxy, res.boxes.conf, res.boxes.cls = xyxy, confs, clses
        return [res]


def make_frame(h=480, w=640):
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_face_location_does_not_imply_posture_or_leaving_seat():
    det = YoloDetector(weights_path="models/yolov8n-face.pt", model_factory=FakeYolo)
    det.model = FakeYolo([(100, 100, 160, 160, 0.9), (100, 380, 160, 430, 0.8)])
    obs = det.process_frame(make_frame(), 0, 0)
    assert obs.person_count == 2
    assert obs.upright_count is None
    assert obs.leave_seat_count is None
    assert all(not d.behaviors for d in obs.detections)


def test_person_box_does_not_imply_seat_occupancy():
    det = YoloDetector(weights_path="models/yolov8n.pt", model_factory=FakeYolo)
    det.model = FakeYolo([(100, 100, 200, 220, 0.9)])  # 高120
    obs = det.process_frame(make_frame(), 0, 0)
    assert obs.person_count == 1
    assert obs.upright_count is None
    assert obs.in_seat_count is None


def test_yolo_detector_filters_non_person_class():
    # COCO 80 类模型：person 模式仅计 class 0（person），cls=15（cat 等）即使 conf≥阈值也忽略
    det = YoloDetector(weights_path="models/yolov8n.pt", model_factory=FakeYolo)
    det.model = FakeYolo([
        (100, 100, 160, 160, 0.9, 0),    # person → 计入
        (200, 200, 260, 260, 0.8, 15),   # cat → 忽略
        (300, 300, 360, 360, 0.7, 0),    # person → 计入
        (400, 100, 460, 160, 0.95, 2),   # car → 忽略
    ])
    obs = det.process_frame(make_frame(), 0, 0)
    assert obs.person_count == 2


def test_yolo_detector_filters_non_face_class_face_mode():
    # face 权重 class 0 即 face：非 0 类同样被忽略
    det = YoloDetector(weights_path="models/yolov8n-face.pt", model_factory=FakeYolo)
    det.model = FakeYolo([
        (100, 100, 160, 160, 0.9, 0),   # face → 计入
        (200, 200, 260, 260, 0.8, 15),  # 非 face → 忽略
    ])
    obs = det.process_frame(make_frame(), 0, 0)
    assert obs.person_count == 1


def test_yolo_detector_is_available_without_weights():
    det = YoloDetector(weights_path="/nonexistent.pt", model_factory=FakeYolo)
    assert det.is_available() is False
    assert det.process_frame(make_frame(), 0, 0).person_count == 0  # 空检测不崩溃


def test_yolo_detector_window_aggregation_contract():
    det = YoloDetector(weights_path="models/yolov8n.pt", model_factory=FakeYolo)
    det.model = FakeYolo([(100, 100, 200, 220, 0.9)])
    obs = det.aggregate_window([det.process_frame(make_frame(), i, i * 2000) for i in range(6)], 45)
    assert obs is not None
    assert obs.upright_posture_count is None
    assert obs.upright_proportion is None
    assert obs.data_quality == 'unavailable'


def test_many_locator_detections_do_not_make_behavior_scores_available():
    det = YoloDetector(weights_path='/missing.pt')
    frames = [FrameObservation(i, i * 2000, person_count=45) for i in range(5)]
    window = det.aggregate_window(frames, 45)
    assert window.coverage == 1.0
    assert window.data_quality == 'unavailable'
    assert window.upright_proportion is None
    assert window.hand_raised_count is None


class RecordingYolo(FakeYolo):
    """FakeYolo that records the device argument of every inference call."""

    def __init__(self, boxes):
        super().__init__(boxes)
        self.device_args = []

    def __call__(self, frame, verbose=False, conf=0.4, device=None):
        self.device_args.append(device)
        return super().__call__(frame, verbose=verbose, conf=conf)


def test_yolo_detector_forwards_configured_device():
    det = YoloDetector(weights_path="models/yolov8n.pt", model_factory=FakeYolo, device="cpu")
    det.model = RecordingYolo([(100, 100, 160, 160, 0.9)])
    det.process_frame(make_frame(), 0, 0)
    assert det.model.device_args == ["cpu"]


def test_yolo_detector_omits_device_when_unset():
    # device=None must keep ultralytics' own selection (cuda if available).
    det = YoloDetector(weights_path="models/yolov8n.pt", model_factory=FakeYolo)
    det.model = RecordingYolo([(100, 100, 160, 160, 0.9)])
    det.process_frame(make_frame(), 0, 0)
    assert det.model.device_args == [None]
