from types import SimpleNamespace

import numpy as np
import pytest

from app.models.action_evidence import ActionEvidenceDetector, resolve_behavior_weights


class Model:
    names = {0: "hand-raising", 1: "reading", 2: "writing"}

    def __call__(self, frame, **kwargs):
        return [SimpleNamespace(boxes=SimpleNamespace(
            xyxy=np.array([[10, 20, 50, 80], [60, 10, 100, 90], [-20, 0, 20, 50]]),
            conf=np.array([0.91, 0.85, 0.1]), cls=np.array([0, 2, 1]))) ]


def test_action_classes_boxes_confidences_and_frame_local_ids(tmp_path):
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"checkpoint")
    detector = ActionEvidenceDetector(Model(), str(weights), "cpu", .4)
    detections = detector.detect(np.zeros((100, 100, 3), dtype=np.uint8))
    assert [d["code"] for d in detections] == ["hand_raising", "writing"]
    assert detections[0]["bbox"] == [0.1, 0.2, 0.4, 0.6]
    assert detections[0]["confidence"] == .91
    assert detections[1]["subjectId"] == "target-2"
    assert len(detector.context["modelDigest"]) == 64


def test_generic_person_model_is_not_treated_as_behavior_classifier(tmp_path):
    weights = tmp_path / "person.pt"
    weights.write_bytes(b"checkpoint")
    model = Model()
    model.names = {0: "person", 1: "bicycle"}
    with pytest.raises(ValueError, match="classroom action"):
        ActionEvidenceDetector(model, str(weights), "cpu", .4)


def test_case_eight_classes_use_model_names_without_relabeling_scb_ids(tmp_path):
    class CaseModel:
        names = {3: "discussing", 5: "turn-head", 6: "using phone", 7: "leaning over the table"}

        def __call__(self, frame, **kwargs):
            return [SimpleNamespace(boxes=SimpleNamespace(
                xyxy=np.array([[i * 20, 0, i * 20 + 10, 20] for i in range(4)]),
                conf=np.full(4, .85), cls=np.array([3, 5, 6, 7]))) ]

    weights = tmp_path / "case8.pt"
    weights.write_bytes(b"candidate checkpoint")
    detector = ActionEvidenceDetector(CaseModel(), str(weights), "cpu", .4)
    detections = detector.detect(np.zeros((100, 100, 3), dtype=np.uint8))
    assert [d["code"] for d in detections] == ["discussing", "turn_head", "using_phone", "leaning_over_table"]
    assert [d["label"] for d in detections] == ["讨论", "转头", "使用手机", "趴桌"]
    assert detector.context["supportedActions"] == ["讨论", "转头", "使用手机", "趴桌"]


def test_crowded_classroom_is_not_silently_truncated_to_twenty_targets(tmp_path):
    class CrowdedModel:
        names = {0: 'reading'}
        kwargs = None

        def __call__(self, frame, **kwargs):
            self.kwargs = kwargs
            return [SimpleNamespace(boxes=SimpleNamespace(
                xyxy=np.array([[i * 3, 0, i * 3 + 2, 10] for i in range(40)]),
                conf=np.full(40, .8), cls=np.zeros(40)))]

    weights = tmp_path / 'classroom.pt'
    weights.write_bytes(b'checkpoint')
    model = CrowdedModel()
    detector = ActionEvidenceDetector(model, str(weights), 'cpu', .4, image_size=960)
    assert len(detector.detect(np.zeros((100, 200, 3), dtype=np.uint8))) == 40
    assert model.kwargs['max_det'] == 100
    assert model.kwargs['imgsz'] == 960


def test_missing_explicit_weights_does_not_silently_change_model(tmp_path):
    default = tmp_path / "models/classroom-behavior.pt"
    default.parent.mkdir()
    default.write_bytes(b"fallback")
    assert resolve_behavior_weights(SimpleNamespace(behavior_weights="missing-test-model.pt"), tmp_path) == ""
    assert resolve_behavior_weights(SimpleNamespace(behavior_weights=""), tmp_path) == str(default.resolve())


def test_container_mount_discovered_from_working_directory(tmp_path, monkeypatch):
    weights = tmp_path / "models/classroom-behavior.pt"
    weights.parent.mkdir()
    weights.write_bytes(b"container checkpoint")
    monkeypatch.chdir(tmp_path)
    assert resolve_behavior_weights(SimpleNamespace(behavior_weights=""), tmp_path / "missing-root") == str(weights)
