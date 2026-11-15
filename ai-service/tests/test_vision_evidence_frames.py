"""Evidence-frame selection: bounded, labeled thumbnails for human verification."""
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.config import settings
from app.workers import vision


def _observation(**overrides):
    values = dict(
        timestamp_ms=12_000, person_count=10, in_seat_count=8,
        leave_seat_count=0, standing_count=0, upright_count=8,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_missing_behavior_model_collects_only_overview(monkeypatch):
    monkeypatch.setattr(settings, "evidence_frames_enabled", True)
    monkeypatch.setattr(settings, "evidence_frame_max_per_window", 2)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    candidates = {}

    vision._collect_evidence_frame(candidates, 0, frame, _observation(timestamp_ms=1000))
    vision._collect_evidence_frame(candidates, 0, frame, _observation(timestamp_ms=2000, leave_seat_count=1))
    vision._collect_evidence_frame(candidates, 0, frame, _observation(timestamp_ms=3000, standing_count=2))

    assert [item["code"] for item in candidates[0]] == ["overview"]
    assert all(item["jpeg"].startswith(bytes([0xFF, 0xD8])) for item in candidates[0])
    assert candidates[0][0]["label"] == "课堂全景"


def test_locator_ratios_never_create_behavior_evidence(monkeypatch):
    monkeypatch.setattr(settings, "evidence_frames_enabled", True)
    monkeypatch.setattr(settings, "evidence_frame_max_per_window", 2)
    frame = np.zeros((60, 80, 3), dtype=np.uint8)
    candidates = {}

    vision._collect_evidence_frame(
        candidates, 1, frame, _observation(upright_count=2, person_count=10))

    assert [item["code"] for item in candidates[1]] == ["overview"]
    assert candidates[1][0]["metadata"]["detections"] == []


def test_disabled_evidence_frames_are_not_collected(monkeypatch):
    monkeypatch.setattr(settings, "evidence_frames_enabled", False)
    candidates = {}
    vision._collect_evidence_frame(candidates, 0, np.zeros((60, 80, 3), dtype=np.uint8), _observation())
    assert candidates == {}


def test_model_actions_replace_lower_priority_frame_and_preserve_boxes(monkeypatch):
    monkeypatch.setattr(settings, "evidence_frame_max_per_window", 2)
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    candidates = {}
    reading = dict(subjectId="target-1", code="reading", label="阅读", confidence=0.99,
                   bbox=[0.1, 0.2, 0.3, 0.4])
    hand = dict(subjectId="target-2", code="hand_raising", label="举手", confidence=0.8,
                bbox=[0.5, 0.1, 0.2, 0.5])
    context = dict(modelVersion="scb-test", modelDigest="abc")
    vision._collect_evidence_frame(candidates, 0, frame, _observation(), [reading], context)
    vision._collect_evidence_frame(candidates, 0, frame, _observation(timestamp_ms=15_000), [hand], context)
    assert [item["code"] for item in candidates[0]] == ["overview", "hand_raising"]
    evidence = candidates[0][1]
    assert evidence["metadata"]["detectionSource"] == "behavior_model"
    assert evidence["metadata"]["detections"] == [hand]
    assert evidence["metadata"]["identityScope"] == "frame_local"
    assert evidence["timestamp_ms"] == 15_000


def test_empty_behavior_prediction_never_becomes_rule_action():
    candidates = {}
    vision._collect_evidence_frame(candidates, 0, np.zeros((120, 160, 3), dtype=np.uint8),
                                   _observation(leave_seat_count=3), [], {"modelVersion": "scb"})
    assert [item["code"] for item in candidates[0]] == ["overview"]


@pytest.mark.parametrize("code,label", [("using_phone", "使用手机"), ("leaning_over_table", "趴桌")])
def test_new_case_actions_can_be_selected_for_review(monkeypatch, code, label):
    monkeypatch.setattr(settings, "evidence_frame_max_per_window", 2)
    candidates = {}
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    reading = dict(subjectId="target-1", code="reading", label="阅读", confidence=.99)
    action = dict(subjectId="target-2", code=code, label=label, confidence=.7)
    vision._collect_evidence_frame(candidates, 0, frame, _observation(), [reading])
    vision._collect_evidence_frame(candidates, 0, frame, _observation(), [action])
    assert [item["code"] for item in candidates[0]] == ["overview", code]
    assert candidates[0][1]["metadata"]["detections"] == [action]


def test_primary_target_matches_strongest_prediction_for_the_action():
    candidates = {}
    detections = [dict(subjectId="target-1", code="hand_raising", label="举手", confidence=.45),
                  dict(subjectId="target-2", code="hand_raising", label="举手", confidence=.90)]
    vision._collect_evidence_frame(candidates, 0, np.zeros((120, 160, 3), dtype=np.uint8),
                                   _observation(), detections, {"modelVersion": "scb"})
    assert candidates[0][1]["metadata"]["primarySubjectId"] == "target-2"


def test_weight_digest_changes_when_same_file_is_replaced(tmp_path):
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"first")
    first = vision._file_sha256(str(weights))
    weights.write_bytes(b"different checkpoint")
    assert first != vision._file_sha256(str(weights))


def test_loaded_model_cache_reloads_when_weight_file_changes(tmp_path, monkeypatch):
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"first")
    monkeypatch.setattr(vision, "_yolo_cache", {})
    monkeypatch.setattr(vision, "_resolve_yolo_device", lambda: "cpu")
    monkeypatch.setattr(vision, "YoloDetector", lambda **kwargs: SimpleNamespace(is_available=lambda: True))
    first = vision._get_yolo_detector(str(weights))
    assert vision._get_yolo_detector(str(weights)) is first
    weights.write_bytes(b"replacement checkpoint")
    assert vision._get_yolo_detector(str(weights)) is not first
