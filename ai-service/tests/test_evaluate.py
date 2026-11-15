"""评估计算函数单元测试（不加载模型）。"""
from scripts.evaluate_face import compute_ap, _iou


def test_iou_basic():
    assert _iou((0, 0, 10, 10), (5, 5, 15, 15)) == 25 / 175  # 25 / 175 ≈ 0.1429


def test_compute_ap_perfect_match():
    gt = [(0, 0, 10, 10, 1.0)]
    pred = [(0, 0, 10, 10, 0.9)]
    p, r, ap = compute_ap(gt, pred)
    assert p == 1.0 and r == 1.0 and ap == 1.0


def test_compute_ap_empty():
    assert compute_ap([], []) == (0.0, 0.0, 0.0)
    assert compute_ap([(0, 0, 10, 10, 1.0)], []) == (0.0, 0.0, 0.0)
