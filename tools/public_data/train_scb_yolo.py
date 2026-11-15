"""Train and evaluate YOLOv8n on SCB-Dataset3-S (experiment E1).

The script deliberately imports ``ultralytics`` lazily so that the rest of the
public-data test suite works in environments without the heavy dependency.
Training uses the official train/val split, seeds every random process with
``--seed``, validates the best checkpoint after training, and writes:

    <out>/metrics.json           summary: mAP50, mAP50-95, per-class P/R,
                                 confusion-matrix paths, argument snapshot
    <out>/confusion_matrix.csv   raw confusion matrix saved by this script
    <out>/train/                 Ultralytics training artifacts
    <out>/val/                   Ultralytics validation artifacts

A failed run still writes ``metrics.json`` with ``"status": "FAILED"`` and a
traceback, then exits non-zero.  Metrics are never fabricated.

Examples
--------
Full run::

    python tools/public_data/train_scb_yolo.py \
        --data data/raw/public/scb/data.yaml \
        --model yolov8n.pt --epochs 100 --imgsz 640 --batch 16 \
        --device auto --out docs/experiments/runs/scb-yolo

Smoke run (GPU)::

    python tools/public_data/train_scb_yolo.py --epochs 1 --imgsz 320 \
        --batch 8 --out docs/experiments/runs/scb-yolo-smoke
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Train YOLOv8n on SCB-Dataset3-S and validate the best checkpoint, "
            "writing metrics.json with per-class precision/recall."
        )
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("data/raw/public/scb/data.yaml"),
        help="Ultralytics data.yaml (default: data/raw/public/scb/data.yaml).",
    )
    parser.add_argument(
        "--model",
        default="yolov8n.pt",
        help="Initial weights or model name (default: yolov8n.pt).",
    )
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument(
        "--device",
        default="auto",
        help="'auto' selects CUDA device 0 when available, else CPU.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/experiments/runs/scb-yolo"),
        help="Run output directory (default: docs/experiments/runs/scb-yolo).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for training, validation and dataloading (default: 42).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "DataLoader workers. Defaults to 0 on Windows and 8 elsewhere to "
            "avoid spawn-related failures."
        ),
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        default=None,
        help=(
            "Continue an interrupted run from its last.pt. The optimizer, "
            "scheduler and epoch counter are restored, so the remaining epochs "
            "run with the original schedule instead of restarting at lr0."
        ),
    )
    return parser


def run_training(args: argparse.Namespace) -> dict[str, Any]:
    """Run training + validation and return the metrics summary.

    Raises any exception from Ultralytics; :func:`main` records the failure and
    converts it to a non-zero exit code.
    """
    YOLO, ultralytics, torch = _import_ultralytics()

    data_path = Path(args.data)
    if not data_path.is_file():
        raise FileNotFoundError(
            f"SCB data.yaml not found: {data_path}. "
            "Run tools/public_data/prepare_scb.py first."
        )
    data_document = _load_data_yaml(data_path)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    device = _resolve_device(args.device, torch)
    workers = _resolve_workers(args.workers)
    seed = args.seed

    if args.resume_from is not None:
        resume_path = Path(args.resume_from)
        if not resume_path.is_file():
            raise FileNotFoundError(f"resume checkpoint not found: {resume_path}")
        # Ultralytics restores optimizer state, scheduler and epoch from the
        # checkpoint's embedded args; passing the other knobs again would
        # silently override the original run configuration.
        model = YOLO(str(resume_path))
        model.train(resume=True)
    else:
        model = YOLO(str(args.model))
        model.train(
            data=str(data_path),
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            device=device,
            project=str(out.resolve()),
            name="train",
            exist_ok=True,
            seed=seed,
            workers=workers,
            plots=True,
            val=True,
            verbose=True,
        )

    train_dir = Path(getattr(model.trainer, "save_dir", out / "train"))
    weights_dir = train_dir / "weights"
    best_weights = weights_dir / "best.pt"
    if not best_weights.is_file():
        last_weights = weights_dir / "last.pt"
        if last_weights.is_file():
            best_weights = last_weights
        else:
            raise FileNotFoundError(
                f"Training finished but no checkpoint was found in {weights_dir}"
            )

    eval_model = YOLO(str(best_weights))
    validation = eval_model.val(
        data=str(data_path),
        imgsz=args.imgsz,
        batch=args.batch,
        device=device,
        split="val",
        project=str(out.resolve()),
        name="val",
        exist_ok=True,
        plots=True,
    )

    class_names = _class_names(validation, data_document)
    metrics = _extract_metrics(validation, class_names)
    confusion = _save_confusion_matrix(validation, out, class_names)

    return {
        "status": "OK",
        "created_at": _utc_now(),
        "args": _args_snapshot(args),
        "resolved": {"device": device, "workers": workers, "seed": seed},
        "environment": _environment(ultralytics, torch),
        "data": {
            "path": str(data_path.resolve()),
            "names": class_names,
            "train": data_document.get("train"),
            "val": data_document.get("val"),
            "root": data_document.get("path"),
        },
        "model": str(args.model),
        "weights": str(best_weights.resolve()),
        "run_dir": str(train_dir.resolve()),
        "val_dir": str(_validation_dir(validation, out).resolve()),
        "metrics": metrics,
        "confusion_matrix": confusion,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Path(args.out)

    try:
        summary = run_training(args)
    except Exception as exc:  # noqa: BLE001 - recorded and reported verbatim
        failure = {
            "status": "FAILED",
            "created_at": _utc_now(),
            "args": _args_snapshot(args),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        try:
            out.mkdir(parents=True, exist_ok=True)
            _write_json(out / "metrics.json", failure)
        except OSError as write_exc:
            print(
                f"ERROR: could not write failure metrics to {out}: {write_exc}",
                file=sys.stderr,
            )
        print(f"ERROR: SCB YOLO training failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    _write_json(out / "metrics.json", summary)
    print(
        json.dumps(
            {
                "status": summary["status"],
                "metrics": summary["metrics"],
                "confusion_matrix": summary["confusion_matrix"],
                "metrics_json": str((out / "metrics.json").resolve()),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _import_ultralytics():
    # Anaconda environments on Windows may load both Intel MKL and PyTorch's
    # bundled libiomp5md.dll, which aborts training with "OMP: Error #15".
    # The official workaround is to allow the duplicate OpenMP runtime; an
    # explicit environment value takes precedence.
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    try:
        import ultralytics
        import torch
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError(
            "ultralytics (and PyTorch) are required for SCB YOLOv8n training; "
            "install them with `pip install ultralytics`."
        ) from exc
    return YOLO, ultralytics, torch


def _load_data_yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid data.yaml {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"Invalid data.yaml {path}: expected a mapping")
    if "names" not in document:
        raise ValueError(f"Invalid data.yaml {path}: missing 'names'")
    return document


def _resolve_device(value: str, torch) -> str:
    if str(value).strip().lower() != "auto":
        return str(value)
    return "0" if torch.cuda.is_available() else "cpu"


def _resolve_workers(value: int | None) -> int:
    if value is not None:
        return value
    return 0 if os.name == "nt" else 8


def _class_names(validation, data_document: dict[str, Any]) -> dict[int, str]:
    names = getattr(validation, "names", None)
    if isinstance(names, dict):
        return {int(key): str(value) for key, value in names.items()}
    raw = data_document.get("names", {})
    if isinstance(raw, dict):
        return {int(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return {index: str(value) for index, value in enumerate(raw)}
    raise ValueError("Could not determine class names from validation or data.yaml")


def _extract_metrics(
    validation, class_names: dict[int, str]
) -> dict[str, Any]:
    box = validation.box
    class_index = [int(index) for index in getattr(box, "ap_class_index", [])]

    per_class: dict[str, dict[str, Any]] = {}
    for position, class_id in enumerate(class_index):
        label = class_names.get(class_id, str(class_id))
        per_class[label] = {
            "class_id": class_id,
            "precision": _as_float(box.p[position]),
            "recall": _as_float(box.r[position]),
            "mAP50": _as_float(box.ap50[position]),
            "mAP50-95": _as_float(box.ap[position]),
        }

    speed = getattr(validation, "speed", None)
    return {
        "mAP50": _as_float(box.map50),
        "mAP50-95": _as_float(box.map),
        "precision": _as_float(box.mp),
        "recall": _as_float(box.mr),
        "per_class": per_class,
        "classes_evaluated": class_index,
        "speed_ms": (
            {key: _as_float(value) for key, value in speed.items()}
            if isinstance(speed, dict)
            else None
        ),
    }


def _save_confusion_matrix(
    validation, out: Path, class_names: dict[int, str]
) -> dict[str, Any]:
    wrapper = getattr(validation, "confusion_matrix", None)
    matrix = getattr(wrapper, "matrix", None)
    if matrix is None:
        return {"csv": None, "png": None, "png_normalized": None}

    csv_path = out / "confusion_matrix.csv"
    row_count = len(matrix)
    class_count = max(0, row_count - 1)
    labels = [
        class_names.get(index, str(index)) for index in range(class_count)
    ] + ["background"]

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["actual\\predicted", *labels])
        for index, row in enumerate(matrix):
            actual = labels[index] if index < len(labels) else str(index)
            writer.writerow([actual, *(int(value) for value in row)])

    save_dir = _validation_dir(validation, out)
    png = save_dir / "confusion_matrix.png"
    png_normalized = save_dir / "confusion_matrix_normalized.png"
    return {
        "csv": str(csv_path.resolve()),
        "png": str(png.resolve()) if png.is_file() else None,
        "png_normalized": (
            str(png_normalized.resolve()) if png_normalized.is_file() else None
        ),
    }


def _validation_dir(validation, out: Path) -> Path:
    save_dir = getattr(validation, "save_dir", None)
    return Path(save_dir) if save_dir is not None else out / "val"


def _environment(ultralytics, torch) -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "ultralytics": getattr(ultralytics, "__version__", None),
        "torch": getattr(torch, "__version__", None),
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_version": getattr(torch.version, "cuda", None),
    }


def _args_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key, value in vars(args).items():
        snapshot[key] = str(value) if isinstance(value, Path) else value
    return snapshot


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
