"""MACT_AI_DEVICE must actually reach ultralytics.

Before this was wired up the setting was dead config: YOLO always let
ultralytics pick the device, which means a CPU-only deployment silently ran on
CUDA when a GPU was present, and a "cuda" config on a GPU-less machine failed
inside the per-frame try/except and produced empty observations.
"""
from __future__ import annotations

import pytest

from app.workers import vision


@pytest.fixture(autouse=True)
def _restore_device(monkeypatch):
    original = vision.settings.device
    yield
    vision.settings.device = original


def test_auto_keeps_ultralytics_default(monkeypatch):
    monkeypatch.setattr(vision.settings, "device", "auto")
    assert vision._resolve_yolo_device() is None


def test_empty_value_keeps_ultralytics_default(monkeypatch):
    monkeypatch.setattr(vision.settings, "device", "")
    assert vision._resolve_yolo_device() is None


def test_cpu_is_passed_through(monkeypatch):
    monkeypatch.setattr(vision.settings, "device", "cpu")
    assert vision._resolve_yolo_device() == "cpu"


def test_cuda_is_kept_when_a_gpu_is_available(monkeypatch):
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(vision.settings, "device", "cuda")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    assert vision._resolve_yolo_device() == "cuda"


def test_cuda_degrades_to_cpu_when_no_gpu(monkeypatch):
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(vision.settings, "device", "cuda")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    assert vision._resolve_yolo_device() == "cpu"


def test_device_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setattr(vision.settings, "device", "  CUDA:0 ")
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

    assert vision._resolve_yolo_device() == "cuda:0"
