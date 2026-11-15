"""vision.py 后端选择契约测试：显式配置优先、自动探测、model_version 标识、YOLO 不可用回退 Haar。

观测契约（analyze_video 返回结构）不依赖具体后端，仅 model_version 值变化。
"""
from pathlib import Path

import cv2

from app.core.config import settings
from app.workers import vision


def _make_video(path: Path) -> Path:
    """生成 10 帧 160x120 合成 AVI（writer 打不开时留空文件，下游仍可走 0 帧成功路径）。"""
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 120))
        if writer.isOpened():
            frame = np.zeros((120, 160, 3), dtype=np.uint8)
            for _ in range(10):
                writer.write(frame)
            writer.release()
    except Exception:
        pass
    return path


def _upload_root() -> Path:
    root = Path("data/uploads").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


class _FakeSettings:
    def __init__(self, face_weights: str = ""):
        self.face_weights = face_weights


def test_resolve_face_weights_explicit_wins(tmp_path):
    weights = tmp_path / "custom-face.pt"
    weights.write_bytes(b"fake")
    assert vision._resolve_face_weights(_FakeSettings(str(weights))) == str(weights)


def test_resolve_face_weights_auto_detect_absolute_or_empty():
    # 自动探测：命中（cwd 或仓库根）则返回绝对路径，否则返回空串（回退 Haar 契约）
    resolved = vision._resolve_face_weights(_FakeSettings(""))
    if resolved:
        assert Path(resolved).is_absolute()
        assert Path(resolved).exists()


def test_analyze_video_yolo_unavailable_falls_back_haar(tmp_path, monkeypatch):
    video = _make_video(_upload_root() / "fallback-haar.avi")
    monkeypatch.setattr(settings, "vision_backend", "yolo")
    monkeypatch.setattr(settings, "face_weights", str(tmp_path / "missing.pt"))  # 显式但无效权重

    result = vision.analyze_video({
        "video_path": str(video),
        "lesson_session_id": "lesson-backend-test",
        "total_seats": 30,
        "sample_interval": 1,
        "window_duration": 2,
        # Synthetic 1-second clip: five samples per 2s window is physically
        # impossible, so use a valid minimum rather than expecting zero windows.
        "min_observations": 1,
    })

    assert result["status"] == "succeeded"
    assert result["model_version"] == "haar"


def test_analyze_video_forced_haar_backend(tmp_path, monkeypatch):
    video = _make_video(_upload_root() / "forced-haar.avi")
    monkeypatch.setattr(settings, "vision_backend", "haar")
    monkeypatch.setattr(settings, "face_weights", "")

    result = vision.analyze_video({
        "video_path": str(video),
        "lesson_session_id": "lesson-backend-test",
        "total_seats": 30,
        "sample_interval": 1,
        "window_duration": 2,
        # Synthetic 1-second clip: five samples per 2s window is physically
        # impossible, so use a valid minimum rather than expecting zero windows.
        "min_observations": 1,
    })

    assert result["status"] == "succeeded"
    assert result["model_version"] == "haar"
