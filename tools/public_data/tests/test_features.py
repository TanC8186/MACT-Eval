"""Tests for Pri-MCCD pose/speech/detection feature extraction (spec 5)."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pytest

from public_data import extract_features, features
from public_data.features import (
    contains_question_marker,
    count_characters,
    detection_features,
    parse_pose_file,
    speech_features_from_segments,
)

MANIFEST_FIELDS = [
    "clip_id",
    "label",
    "label_name",
    "video_path",
    "audio_path",
    "lesson_id",
    "split",
]


# ---------------------------------------------------------------------------
# Pose features
# ---------------------------------------------------------------------------


def _frame(pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0) -> dict:
    return {
        "aggregated_pose": {
            "avg_pitch": pitch,
            "avg_yaw": yaw,
            "avg_roll": roll,
        }
    }


def _write_pose(path: Path, frames: list) -> Path:
    path.write_text(json.dumps(frames), encoding="utf-8")
    return path


def test_parse_pose_file_straight_head_is_perfect(tmp_path: Path) -> None:
    path = _write_pose(
        tmp_path / "neg0001_head_pose_features_mtcnn_aggregated.json",
        [_frame() for _ in range(5)],
    )

    result = parse_pose_file(path)

    assert result is not None
    assert result["frame_count"] == 5
    assert result["skipped_frame_count"] == 0
    assert result["posture_score"] == pytest.approx(1.0)
    assert result["stability_score"] == pytest.approx(1.0)
    assert result["head_motion"] == pytest.approx(0.0)
    assert result["mean_pitch"] == pytest.approx(0.0)
    assert result["std_pitch"] == pytest.approx(0.0)


def test_parse_pose_file_45_degree_pitch_has_zero_posture(tmp_path: Path) -> None:
    path = _write_pose(
        tmp_path / "pos0003_head_pose_features_mtcnn_aggregated.json",
        [_frame(pitch=45.0) for _ in range(3)],
    )

    result = parse_pose_file(path)

    assert result is not None
    assert result["posture_score"] == pytest.approx(0.0)
    # A constant angle has zero variance, so stability stays perfect.
    assert result["stability_score"] == pytest.approx(1.0)


def test_parse_pose_file_uses_population_std_and_clipped_components(
    tmp_path: Path,
) -> None:
    path = _write_pose(
        tmp_path / "neu0002_head_pose_features_mtcnn_aggregated.json",
        [
            _frame(0.0, 10.0, 0.0),
            _frame(30.0, 20.0, 0.0),
            _frame(60.0, 30.0, 0.0),
        ],
    )

    result = parse_pose_file(path)

    assert result is not None
    assert result["frame_count"] == 3
    assert result["mean_pitch"] == pytest.approx(30.0)
    assert result["mean_yaw"] == pytest.approx(20.0)
    assert result["mean_abs_pitch"] == pytest.approx(30.0)
    # ddof=0: sqrt(600) for pitch, sqrt(200/3) for yaw.
    assert result["std_pitch"] == pytest.approx(math.sqrt(600.0))
    assert result["std_yaw"] == pytest.approx(math.sqrt(200.0 / 3.0))
    assert result["std_roll"] == pytest.approx(0.0)
    head_motion = math.sqrt(600.0) + math.sqrt(200.0 / 3.0)
    assert result["head_motion"] == pytest.approx(head_motion)
    assert result["posture_score"] == pytest.approx(1.0 - 30.0 / 45.0)
    assert result["stability_score"] == pytest.approx(1.0 - head_motion / 90.0)


def test_parse_pose_file_missing_file_is_none(tmp_path: Path) -> None:
    assert parse_pose_file(tmp_path / "missing.json") is None


def test_parse_pose_file_empty_or_unusable_is_none(tmp_path: Path) -> None:
    empty = _write_pose(tmp_path / "empty.json", [])
    assert parse_pose_file(empty) is None

    unusable = _write_pose(
        tmp_path / "unusable.json",
        [{"frame": 0}, {"aggregated_pose": {"avg_pitch": 1.0}}],
    )
    assert parse_pose_file(unusable) is None


def test_parse_pose_file_skips_frames_without_usable_pose(tmp_path: Path) -> None:
    path = _write_pose(
        tmp_path / "partial.json",
        [
            {"frame": 0},
            _frame(0.0, 0.0, 0.0),
            {"aggregated_pose": {"avg_pitch": "not-a-number", "avg_yaw": 0, "avg_roll": 0}},
        ],
    )

    result = parse_pose_file(path)

    assert result is not None
    assert result["frame_count"] == 1
    assert result["skipped_frame_count"] == 2


def test_parse_pose_file_accepts_frames_object(tmp_path: Path) -> None:
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({"frames": [_frame()]}), encoding="utf-8")

    result = parse_pose_file(path)

    assert result is not None
    assert result["frame_count"] == 1


def test_parse_pose_file_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_pose_file(path)


def test_parse_pose_file_rejects_unsupported_structure(tmp_path: Path) -> None:
    path = tmp_path / "unsupported.json"
    path.write_text(json.dumps({"unexpected": []}), encoding="utf-8")

    with pytest.raises(ValueError):
        parse_pose_file(path)


# ---------------------------------------------------------------------------
# Speech features
# ---------------------------------------------------------------------------


def test_speech_features_example_from_plan() -> None:
    # 60 s clip, 30 s of speech, two question segments, 300 characters.
    segments = [
        {"start": 0.0, "end": 10.0, "text": "这是什么？"},
        {"start": 20.0, "end": 40.0, "text": "为什么这样呢？"},
    ]

    result = speech_features_from_segments(segments, 60.0, "字" * 300)

    assert result["speech_seconds"] == pytest.approx(30.0)
    assert result["speech_coverage"] == pytest.approx(0.5)
    assert result["coverage"] == pytest.approx(0.5)
    assert result["question_count"] == 2
    assert result["speech_rate"] == pytest.approx(600.0)
    assert result["pause_ratio"] == pytest.approx(0.5)
    assert result["char_count"] == 300


def test_speech_features_clips_intervals_and_merges_overlaps() -> None:
    segments = [
        {"start": -5.0, "end": 20.0, "text": ""},
        {"start": 10.0, "end": 30.0, "text": ""},
        {"start": 50.0, "end": 70.0, "text": ""},
    ]

    result = speech_features_from_segments(segments, 60.0)

    # [0, 20] + [10, 30] -> [0, 30]; [50, 70] clipped to [50, 60] -> 10 s.
    assert result["speech_seconds"] == pytest.approx(40.0)
    assert result["speech_coverage"] == pytest.approx(40.0 / 60.0)
    assert result["pause_ratio"] == pytest.approx(20.0 / 60.0)


def test_speech_features_empty_segments_are_safe() -> None:
    result = speech_features_from_segments([], 60.0, "你好吗？")

    assert result["speech_seconds"] == pytest.approx(0.0)
    assert result["speech_coverage"] == pytest.approx(0.0)
    assert result["speech_rate"] == pytest.approx(0.0)
    assert result["pause_ratio"] == pytest.approx(1.0)
    assert result["question_count"] == 1
    assert result["char_count"] == 3


def test_speech_features_none_segments_and_text_are_safe() -> None:
    result = speech_features_from_segments(None, 60.0, None)

    assert result["speech_coverage"] == pytest.approx(0.0)
    assert result["speech_rate"] == pytest.approx(0.0)
    assert result["question_count"] == 0
    assert result["char_count"] == 0


def test_speech_features_counts_segments_not_marker_occurrences() -> None:
    segments = [{"start": 0.0, "end": 1.0, "text": "谁在哪里？"}]

    result = speech_features_from_segments(segments, 10.0, "谁在哪里？")

    assert result["question_count"] == 1


def test_speech_features_uses_segment_texts_for_questions() -> None:
    # The full transcript has an extra question, but only segment texts are
    # the protocol's counting unit.
    segments = [{"start": 0.0, "end": 1.0, "text": "我们开始吧。"}]

    result = speech_features_from_segments(segments, 10.0, "我们开始吧。为什么？")

    assert result["question_count"] == 1


def test_speech_features_ignores_non_mapping_segments() -> None:
    segments = ["not-a-segment", {"start": 0.0, "end": 30.0, "text": "你好"}]

    result = speech_features_from_segments(segments, 60.0, "你好")

    assert result["speech_coverage"] == pytest.approx(0.5)


@pytest.mark.parametrize("duration", [0.0, -1.0, float("nan"), float("inf"), None])
def test_speech_features_rejects_invalid_duration(duration) -> None:
    with pytest.raises(ValueError):
        speech_features_from_segments([], duration)


def test_character_counting_ignores_whitespace_and_punctuation() -> None:
    assert count_characters("你 好，世界！") == 4
    assert count_characters("Hello, world!") == 10
    assert count_characters("") == 0


def test_question_markers_match_protocol() -> None:
    for marker in ("？", "?", "吗", "呢", "吧", "谁", "什么", "怎么", "为什么", "如何", "几", "多少", "哪"):
        assert contains_question_marker(f"前缀{marker}后缀")
    assert not contains_question_marker("这是陈述句。")


# ---------------------------------------------------------------------------
# Detection features (fake OpenCV capture and fake YOLO model, no real video)
# ---------------------------------------------------------------------------


class _FakeBoxes:
    def __init__(
        self,
        confidences: list[float],
        class_ids: list[int],
        boxes: list[list[float]] | None = None,
    ) -> None:
        self.conf = confidences
        self.cls = class_ids
        self.xyxy = boxes if boxes is not None else [[0.0, 0.0, 1.0, 1.0]] * len(confidences)


class _FakeResult:
    def __init__(
        self,
        names: dict[int, str],
        confidences: list[float] | None = None,
        class_ids: list[int] | None = None,
        boxes: list[list[float]] | None = None,
        *,
        conf: list[float] | None = None,
        cls: list[int] | None = None,
    ) -> None:
        self.names = names
        confidences = confidences if confidences is not None else (conf or [])
        class_ids = class_ids if class_ids is not None else (cls or [])
        self.boxes = _FakeBoxes(confidences, class_ids, boxes)


class _FakeModel:
    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = results
        self.calls = 0

    def predict(self, frame, conf: float = 0.25, verbose: bool = False):
        result = self._results[self.calls]
        self.calls += 1
        return [result]


class _FakeCapture:
    def __init__(self, frame_count: int, fps: float) -> None:
        self._frame_count = frame_count
        self._fps = fps
        self._index = 0

    def isOpened(self) -> bool:
        return True

    def get(self, prop: int) -> float:
        return self._fps if prop == 5 else float(self._frame_count)

    def read(self):
        if self._index >= self._frame_count:
            return False, None
        self._index += 1
        return True, object()

    def release(self) -> None:
        pass


class _FakeCV2:
    CAP_PROP_FPS = 5

    def __init__(self, capture: _FakeCapture) -> None:
        self._capture = capture

    def VideoCapture(self, path: str) -> _FakeCapture:
        return self._capture


def _install_fake_capture(
    monkeypatch: pytest.MonkeyPatch, frame_count: int, fps: float
) -> None:
    capture = _FakeCapture(frame_count, fps)
    monkeypatch.setattr(features, "_import_cv2", lambda: _FakeCV2(capture))


def _empty_result(names: dict[int, str]) -> _FakeResult:
    return _FakeResult(names, [], [])


def test_detection_features_aggregates_counts_and_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {0: "hand-raising", 1: "reading", 2: "writing", 3: "person"}
    model = _FakeModel(
        [
            _FakeResult(names, [0.9, 0.5, 0.1], [0, 3, 2]),
            _FakeResult(names, [0.8], [1]),
            _empty_result(names),
        ]
    )
    _install_fake_capture(monkeypatch, frame_count=5, fps=2.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    result = detection_features(model, video, sample_fps=1.0, conf=0.25)

    # 2 fps video at 1 sample/s -> frames 0, 2, 4 are sampled.
    assert model.calls == 3
    assert result["sampled_frames"] == 3
    assert result["person_count"] == 1
    assert result["hand_raising_count"] == 1
    assert result["reading_count"] == 1
    assert result["writing_count"] == 0
    # The 0.1-confidence writing box is below the threshold.
    assert result["total_detections"] == 3
    assert result["detection_coverage"] == pytest.approx(2.0 / 3.0)
    assert result["mean_confidence"] == pytest.approx((0.9 + 0.5 + 0.8) / 3.0)
    assert result["positive_behavior_ratio"] == pytest.approx(2.0 / 3.0)


def test_detection_features_counts_six_class_taxonomy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {
        0: "hand-raising",
        1: "reading",
        2: "writing",
        3: "discussing",
        4: "bow-head",
        5: "turn-head",
    }
    boxes = [[float(i * 100), 0.0, float(i * 100 + 40), 60.0] for i in range(6)]
    model = _FakeModel([
        _FakeResult(names, [0.9, 0.8, 0.7, 0.6, 0.5, 0.4], [0, 1, 2, 3, 4, 5], boxes)
    ])
    _install_fake_capture(monkeypatch, frame_count=1, fps=1.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    result = detection_features(model, video, sample_fps=1.0, conf=0.25)

    assert result["hand_raising_count"] == 1
    assert result["reading_count"] == 1
    assert result["writing_count"] == 1
    assert result["discussing_count"] == 1
    assert result["bow_head_count"] == 1
    assert result["turn_head_count"] == 1
    assert result["behaviour_total"] == 6
    # Only hand-raising / reading / writing count as positive.
    assert result["positive_behavior_ratio"] == pytest.approx(3.0 / 6.0)


def test_positive_ratio_has_variance_across_off_task_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The six-class model must not reproduce the all-ones ratio."""
    names = {0: "hand-raising", 4: "bow-head"}
    # Frame 0: one positive box.  Frame 1: one off-task box.
    model = _FakeModel([
        _FakeResult(names, [0.9], [0]),
        _FakeResult(names, [0.9], [4]),
    ])
    _install_fake_capture(monkeypatch, frame_count=2, fps=1.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    result = detection_features(model, video, sample_fps=1.0, conf=0.25)

    assert result["behaviour_total"] == 2
    assert result["positive_behavior_ratio"] == pytest.approx(0.5)


def test_behaviour_aliases_cover_scb5_names() -> None:
    assert features._behaviour_kind("discussing") == "discussing"
    assert features._behaviour_kind("bow-head") == "bow_head"
    assert features._behaviour_kind("turn-head") == "turn_head"
    assert features._behaviour_kind("discuss") == "discussing"


def test_overlapping_behaviour_boxes_are_deduped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two boxes on the same student must count once, keeping the confident one."""
    names = {0: "hand-raising"}
    model = _FakeModel([
        _FakeResult(
            names,
            conf=[0.60, 0.30],
            cls=[0, 0],
            boxes=[[10.0, 10.0, 50.0, 90.0], [12.0, 11.0, 51.0, 91.0]],
        )
    ])
    _install_fake_capture(monkeypatch, frame_count=1, fps=1.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    result = detection_features(model, video, sample_fps=1.0, conf=0.25)

    assert result["hand_raising_count"] == 1
    assert result["behaviour_total"] == 1
    assert result["mean_confidence"] == pytest.approx(0.60)


def test_non_overlapping_boxes_are_both_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {0: "hand-raising", 1: "reading"}
    model = _FakeModel([
        _FakeResult(
            names,
            conf=[0.60, 0.55],
            cls=[0, 1],
            boxes=[[10.0, 10.0, 50.0, 90.0], [200.0, 10.0, 260.0, 95.0]],
        )
    ])
    _install_fake_capture(monkeypatch, frame_count=1, fps=1.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    result = detection_features(model, video, sample_fps=1.0, conf=0.25)

    assert result["hand_raising_count"] == 1
    assert result["reading_count"] == 1
    assert result["behaviour_total"] == 2


def test_iou_threshold_is_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {0: "hand-raising"}
    # IoU is roughly 0.68: kept at 0.9, merged at 0.5.
    def new_model() -> _FakeModel:
        return _FakeModel([
            _FakeResult(
                names,
                conf=[0.60, 0.30],
                cls=[0, 0],
                boxes=[[10.0, 10.0, 50.0, 90.0], [16.0, 10.0, 56.0, 90.0]],
            )
        ])

    _install_fake_capture(monkeypatch, frame_count=1, fps=1.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    lenient = detection_features(new_model(), video, sample_fps=1.0, conf=0.25, iou_threshold=0.9)
    # The fake capture is consumed by the first pass; install a fresh one.
    _install_fake_capture(monkeypatch, frame_count=1, fps=1.0)
    strict = detection_features(new_model(), video, sample_fps=1.0, conf=0.25, iou_threshold=0.5)

    assert lenient["behaviour_total"] == 2
    assert strict["behaviour_total"] == 1


def test_detection_features_no_detections_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {0: "hand-raising", 1: "reading", 2: "writing"}
    model = _FakeModel([_empty_result(names) for _ in range(3)])
    _install_fake_capture(monkeypatch, frame_count=3, fps=1.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    result = detection_features(model, video)

    assert result["sampled_frames"] == 3
    assert result["total_detections"] == 0
    assert result["detection_coverage"] == pytest.approx(0.0)
    assert result["mean_confidence"] is None
    assert result["positive_behavior_ratio"] == pytest.approx(0.0)
    assert result["hand_raising_count"] == 0


def test_detection_features_missing_video_raises(tmp_path: Path) -> None:
    model = _FakeModel([])

    with pytest.raises(FileNotFoundError):
        detection_features(model, tmp_path / "missing.mp4")


def test_detection_features_rejects_invalid_sampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_capture(monkeypatch, frame_count=1, fps=1.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")

    with pytest.raises(ValueError):
        detection_features(_FakeModel([]), video, sample_fps=0.0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_manifest(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in MANIFEST_FIELDS})
    return path


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _manifest_rows(count: int = 3) -> list[dict[str, str]]:
    rows = [
        {
            "clip_id": "neg0001",
            "label": "0",
            "label_name": "negative",
            "video_path": "negative/neg0001_0.mp4",
            "audio_path": "negative/neg0001_0.wav",
            "split": "train",
        },
        {
            "clip_id": "neu0002",
            "label": "1",
            "label_name": "neutral",
            "video_path": "neutral/neu0002_1.mp4",
            "audio_path": "neutral/neu0002_1.wav",
            "split": "train",
        },
        {
            "clip_id": "pos0003",
            "label": "2",
            "label_name": "positive",
            "video_path": "positive/pos0003_2.mp4",
            "audio_path": "positive/pos0003_2.wav",
            "split": "valid",
        },
    ]
    return rows[:count]


def test_cli_extracts_pose_features_and_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pose_root = tmp_path / "pose"
    pose_root.mkdir()
    _write_pose(
        pose_root / "neg0001_head_pose_features_mtcnn_aggregated.json",
        [_frame(0.0, 0.0, 0.0) for _ in range(3)],
    )
    _write_pose(
        pose_root / "pos0003_head_pose_features_mtcnn_aggregated.json",
        [_frame(45.0, 0.0, 0.0) for _ in range(3)],
    )
    manifest = _write_manifest(tmp_path / "manifest.csv", _manifest_rows())
    out = tmp_path / "features.csv"

    code = extract_features.main(
        [
            "--manifest",
            str(manifest),
            "--pose-root",
            str(pose_root),
            "--out",
            str(out),
            "--limit",
            "2",
        ]
    )

    assert code == 0
    rows = _read_csv(out)
    assert [row["clip_id"] for row in rows] == ["neg0001", "neu0002"]
    assert rows[0]["pose_available"] == "True"
    assert float(rows[0]["posture_score"]) == pytest.approx(1.0)
    assert rows[1]["pose_available"] == ""
    assert rows[1]["posture_score"] == ""
    assert _read_csv(tmp_path / "features_failures.csv") == []

    # Resume processes only the remaining clip even with --limit 2.
    code = extract_features.main(
        [
            "--manifest",
            str(manifest),
            "--pose-root",
            str(pose_root),
            "--out",
            str(out),
            "--limit",
            "2",
            "--resume",
        ]
    )

    assert code == 0
    rows = _read_csv(out)
    assert [row["clip_id"] for row in rows] == ["neg0001", "neu0002", "pos0003"]
    assert float(rows[2]["posture_score"]) == pytest.approx(0.0)
    assert "detection features are skipped" in capsys.readouterr().err


def test_cli_records_pose_failure_without_zero_filling(tmp_path: Path) -> None:
    pose_root = tmp_path / "pose"
    pose_root.mkdir()
    (pose_root / "neg0001_head_pose_features_mtcnn_aggregated.json").write_text(
        "{broken", encoding="utf-8"
    )
    manifest = _write_manifest(tmp_path / "manifest.csv", _manifest_rows(1))
    out = tmp_path / "features.csv"

    code = extract_features.main(
        [
            "--manifest",
            str(manifest),
            "--pose-root",
            str(pose_root),
            "--out",
            str(out),
        ]
    )

    assert code == 1
    rows = _read_csv(out)
    assert len(rows) == 1
    assert rows[0]["pose_available"] == ""
    assert rows[0]["posture_score"] == ""
    failures = _read_csv(tmp_path / "features_failures.csv")
    assert len(failures) == 1
    assert failures[0]["clip_id"] == "neg0001"
    assert failures[0]["stage"] == "pose"
    assert "JSONDecodeError" in failures[0]["error"]


def test_cli_writes_jsonl(tmp_path: Path) -> None:
    pose_root = tmp_path / "pose"
    pose_root.mkdir()
    _write_pose(pose_root / "neg0001_pose.json", [_frame()])
    manifest = _write_manifest(tmp_path / "manifest.csv", _manifest_rows(1))
    out = tmp_path / "features.jsonl"

    code = extract_features.main(
        [
            "--manifest",
            str(manifest),
            "--pose-root",
            str(pose_root),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    record = json.loads(out.read_text(encoding="utf-8").strip())
    assert record["clip_id"] == "neg0001"
    assert record["pose_available"] is True
    assert record["posture_score"] == pytest.approx(1.0)
    assert record["speech_coverage"] is None


def test_cli_rejects_manifest_without_clip_id(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    manifest.write_text("label,split\n0,train\n", encoding="utf-8")
    pose_root = tmp_path / "pose"
    pose_root.mkdir()

    code = extract_features.main(
        [
            "--manifest",
            str(manifest),
            "--pose-root",
            str(pose_root),
            "--out",
            str(tmp_path / "features.csv"),
        ]
    )

    assert code == 2


def test_cli_rejects_unknown_output_suffix(tmp_path: Path) -> None:
    pose_root = tmp_path / "pose"
    pose_root.mkdir()
    manifest = _write_manifest(tmp_path / "manifest.csv", _manifest_rows(1))

    code = extract_features.main(
        [
            "--manifest",
            str(manifest),
            "--pose-root",
            str(pose_root),
            "--out",
            str(tmp_path / "features.txt"),
        ]
    )

    assert code == 2
