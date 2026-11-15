#!/usr/bin/env python
"""Validate a YOLO checkpoint on a YOLO dataset and write per-class metrics.

Runs in its own file (not ``python -``) because ultralytics spawns worker
processes on Windows, and a stdin script cannot be re-imported by the child
interpreter.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--device", default="0")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from ultralytics import YOLO

    model = YOLO(str(args.weights))
    result = model.val(
        data=str(args.data),
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        workers=0,
        verbose=False,
    )

    per_class = {}
    for index, name in model.names.items():
        per_class[name] = {
            "precision": round(float(result.box.p[index]), 4),
            "recall": round(float(result.box.r[index]), 4),
            "mAP50": round(float(result.box.ap50[index]), 4),
            "mAP50_95": round(float(result.box.ap[index]), 4),
        }

    summary = {
        "weights": str(args.weights),
        "data": str(args.data),
        "images": int(getattr(result, "seen", 0)),
        "precision": round(float(result.box.mp), 4),
        "recall": round(float(result.box.mr), 4),
        "mAP50": round(float(result.box.map50), 4),
        "mAP50_95": round(float(result.box.map), 4),
        "per_class": per_class,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
