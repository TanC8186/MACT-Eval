#!/usr/bin/env python
"""导出**固定时间戳抽样**的真实证据帧预测（任务书 §D 的第 1 步）。

    python -I tools/public_data/export_fixed_sample_predictions.py \\
        --manifest data/processed/classroom_windows/window_manifest.csv \\
        --weights  models/classroom-behavior.pt \\
        --out      data/action-eval/predictions_fixed_sample.json

### 这一步在整条链路里的位置

`evaluate_actions.py` 只做"标注 × 预测 → 指标"。它要求预测文件里的每一帧都带
`detectionSource=fixed_timestamp_sample`；否则 `recall_estimable` 恒为 **no**
（模型自选帧里不可能出现漏检，recall 是循环的）。此前仓库里**没有任何脚本能
产出这种帧**——`docs/experiments/action-eval-2026-10-28.md` 只描述了做法，
缺的是可执行的那一步。本文件补上它。

### 抽样规则（与标注协议 §3.1 逐字一致）

对 `window_manifest.csv` 的每一行，在 `start_sec + {10, 30, 50}` 处取帧，
每帧跑一次部署 checkpoint，记录**全部**检测（**空检测帧必须保留**——丢掉空帧
等于只评估模型自己挑中的帧，recall 立刻失效）。

### `target-N` 的含义（不得混淆）

`subjectId=target-N` 是检测器在**这一帧内**按 `(bbox.x, bbox.y)` 排序后的第 N 个框，
没有 track id，下一帧的 `target-3` 与上一帧无关。帧数不是事件数，不是学生数。

### 边界

* 只导出**预测**；标注（`action-annotation/1.0`）需要两名人类标注者，本脚本不生成。
* 需要 ultralytics + 权重文件；缺任一项时返回 2，不伪造空预测。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 与标注协议 §3.1 相同的三个固定偏移（秒），在分析开始前确定，与模型无关。
OFFSETS = (10, 30, 50)

#: 与 ai-service 的 `ActionEvidenceDetector` 保持同一份码表。
ACTION_LABELS = {
    "hand-raising": ("hand_raising", "举手"),
    "hand_raising": ("hand_raising", "举手"),
    "reading": ("reading", "阅读"),
    "writing": ("writing", "书写"),
    "discussing": ("discussing", "讨论"),
    "bow-head": ("bow_head", "低头"),
    "turn-head": ("turn_head", "转头"),
    "standing": ("standing", "站立"),
    "leave-seat": ("leave_seat", "离座"),
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_detector(weights: Path, confidence: float):
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit(
            f"需要 ultralytics：{exc}\n  装到项目内：pip install -r ai-service/requirements.txt"
            "（或复用 ai-service/.venv-test 里已有的版本）")
    model = YOLO(str(weights))
    names = getattr(model, "names", {})
    names = dict(enumerate(names)) if isinstance(names, list) else names
    if not any(str(n).lower() in ACTION_LABELS for n in names.values()):
        raise SystemExit(
            f"{weights} 的类名里没有受支持的课堂动作：{list(names.values())[:8]}")
    return model, names


def detect(model, names, frame, confidence: float) -> list[dict]:
    """一次推理，返回与 ai-service 完全同构的检测列表（含 target-N）。"""
    import math

    height, width = frame.shape[:2]
    results = model(frame, verbose=False, conf=confidence)
    detections: list[dict] = []
    for result in results:
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        for box, conf, cls_id in zip(boxes.xyxy, boxes.conf, boxes.cls):
            name = str(names.get(int(cls_id.item()), "")).lower()
            if name not in ACTION_LABELS:
                continue
            score = float(conf.item())
            values = [float(x) for x in box]
            if not all(math.isfinite(v) for v in values + [score]):
                continue
            if not confidence <= score <= 1:
                continue
            x1, y1, x2, y2 = values
            x1, x2 = max(0.0, min(width, x1)), max(0.0, min(width, x2))
            y1, y2 = max(0.0, min(height, y1)), max(0.0, min(height, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            code, label = ACTION_LABELS[name]
            detections.append({
                "code": code, "label": label, "className": name,
                "confidence": round(score, 4),
                "bbox": [x1 / width, y1 / height,
                         (x2 - x1) / width, (y2 - y1) / height],
            })
    detections = sorted(detections, key=lambda d: d["confidence"], reverse=True)[:20]
    detections.sort(key=lambda d: (d["bbox"][0], d["bbox"][1]))
    for index, detection in enumerate(detections, 1):
        detection["subjectId"] = f"target-{index}"
    return detections


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest",
                    default="data/processed/classroom_windows/window_manifest.csv")
    ap.add_argument("--weights", default="models/classroom-behavior.pt")
    ap.add_argument("--out", default="data/action-eval/predictions_fixed_sample.json")
    ap.add_argument("--confidence", type=float, default=0.40,
                    help="部署默认 behavior_confidence")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个窗口（调试）")
    args = ap.parse_args()

    manifest = Path(args.manifest)
    weights = Path(args.weights)
    if not manifest.is_file():
        raise SystemExit(f"manifest 不存在：{manifest}")
    if not weights.is_file():
        raise SystemExit(f"权重不存在：{weights}")

    try:
        import cv2
    except ImportError as exc:
        raise SystemExit(f"需要 opencv：{exc}")

    model, names = load_detector(weights, args.confidence)
    model_digest = sha256_of(weights)
    model_version = (weights.parent.parent.parent.name + "/best"
                     if weights.stem == "best" else weights.stem)

    rows = list(csv.DictReader(manifest.open(encoding="utf-8-sig")))
    if args.limit:
        rows = rows[: args.limit]
    print(f"==> {len(rows)} 个窗口 × {len(OFFSETS)} 个固定时刻 = "
          f"{len(rows) * len(OFFSETS)} 帧")

    frames: list[dict] = []
    empty_frames = 0
    total_detections = 0
    for index, row in enumerate(rows, 1):
        clip = Path(row["window_path"])
        start = float(row["start_sec"])
        lesson = row["lesson_id"]
        window_index = int(row["clip_id"].rsplit("w", 1)[-1])
        if not clip.is_file():
            print(f"  !! 缺窗口切片：{clip}", file=sys.stderr)
            continue
        cap = cv2.VideoCapture(str(clip))
        if not cap.isOpened():
            print(f"  !! 打不开：{clip}", file=sys.stderr)
            continue
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps if fps else 0
        for offset in OFFSETS:
            # 偏移相对窗口起点；窗口切片不足 60s 时跳过越界的时刻。
            local = min(offset, max(0.0, duration - 1.0))
            cap.set(cv2.CAP_PROP_POS_MSEC, local * 1000.0)
            ok, frame = cap.read()
            if not ok:
                continue
            detections = detect(model, names, frame, args.confidence)
            if not detections:
                empty_frames += 1
            total_detections += len(detections)
            frames.append({
                "lesson_id": lesson,
                "window_index": window_index,
                "code": "fixed_sample",
                "timestamp_ms": int(round((start + local) * 1000)),
                "frame_id": f"{lesson}:w{window_index:04d}:fixed_sample",
                "metadata": {
                    "detectionSource": "fixed_timestamp_sample",
                    "modelVersion": model_version,
                    "modelDigest": model_digest,
                    "identityScope": "frame_local",
                    "detections": detections,
                },
            })
        cap.release()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sampling": {
            "offsetsSeconds": list(OFFSETS),
            "manifest": str(manifest),
            "manifestSha256": sha256_of(manifest),
            "weights": str(weights),
            "weightsSha256": model_digest,
            "confidence": args.confidence,
        },
        "frames": frames,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"==> 导出 {len(frames)} 帧（空检测帧 {empty_frames}，"
          f"检测总数 {total_detections}）-> {out}")
    print(f"    空检测帧必须保留：丢掉它们等于只评估模型自己挑中的帧，recall 立刻失效。")
    print(f"    注意 target-N 是帧内序号，不是学生身份；帧数不是动作次数。")
    print(f"    下一步：按 docs/experiments/action-annotation-protocol-2026-10-28.md "
          f"做双人盲标，再用 evaluate_actions.py 出指标。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
