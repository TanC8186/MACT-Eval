#!/usr/bin/env python
"""Unified entry point for the public-data experiment stages.

Examples::

    python tools/public_data/run_experiments.py prepare-scb --images-zip ... --labels-zip ... --out data/raw/public/scb
    python tools/public_data/run_experiments.py scb-train --epochs 100 --imgsz 640
    python tools/public_data/run_experiments.py asr --thchs-root data/raw/public/thchs30 --out docs/experiments/runs/asr-thchs
    python tools/public_data/run_experiments.py features --manifest ... --pose-root ... --out ...
    python tools/public_data/run_experiments.py fusion --features ... --out ...
    python tools/public_data/run_experiments.py all --manifest ... --pose-root ... --out docs/experiments/runs/pri-mccd-fusion

Each stage delegates to the stage script and propagates its exit code.  A
missing input or failed stage is reported exactly; no stage is silently
skipped and no result is fabricated.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYTHON = sys.executable

STAGE_SCRIPTS = {
    "prepare-scb": HERE / "prepare_scb.py",
    "scb-train": HERE / "train_scb_yolo.py",
    "asr": HERE / "evaluate_asr.py",
    "features": HERE / "extract_features.py",
    "fusion": HERE / "learn_fusion_weights.py",
}


def _run(script: Path, args: list[str]) -> int:
    if not script.is_file():
        print(f"error: stage script not found: {script}", file=sys.stderr)
        return 2
    command = [PYTHON, str(script), *args]
    print("+ " + " ".join(command), file=sys.stderr)
    completed = subprocess.run(command, cwd=HERE.parents[1])
    return int(completed.returncode)


def _run_all(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="run_experiments.py all",
        description=(
            "Run feature extraction followed by the fusion experiment. "
            "ASR/detection are used only when --asr-model/--weights are given."
        ),
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--pose-root", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--features", default=None)
    parser.add_argument("--weights", default=None)
    parser.add_argument("--asr-model", default=None)
    parser.add_argument("--label-col", default="label")
    parser.add_argument("--group-col", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-statistical-tests", action="store_true")
    parser.add_argument("--no-window-sensitivity", action="store_true")
    parser.add_argument("--no-rule-threshold-sensitivity", action="store_true")
    options = parser.parse_args(args)

    output = Path(options.out)
    output.mkdir(parents=True, exist_ok=True)
    features = (
        Path(options.features)
        if options.features
        else output / "pri_mccd_features.csv"
    )
    failed = output / "_stage_failed.json"
    if failed.exists():
        failed.unlink()

    feature_args = [
        "--manifest",
        options.manifest,
        "--out",
        str(features),
    ]
    if options.pose_root:
        feature_args.extend(["--pose-root", options.pose_root])
    if options.weights:
        feature_args.extend(["--weights", options.weights])
    if options.asr_model:
        feature_args.extend(["--asr-model", options.asr_model])
    code = _run(STAGE_SCRIPTS["features"], feature_args)
    if code != 0:
        failed.write_text(
            json.dumps(
                {
                    "status": "FAILED",
                    "stage": "features",
                    "returncode": code,
                    "manifest": options.manifest,
                    "features": str(features),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return code

    fusion_args = [
        "--features",
        str(features),
        "--out",
        str(output / "fusion"),
        "--label-col",
        options.label_col,
        "--seed",
        str(options.seed),
    ]
    if options.group_col:
        fusion_args.extend(["--group-col", options.group_col])
    if options.no_statistical_tests:
        fusion_args.append("--no-statistical-tests")
    if options.no_window_sensitivity:
        fusion_args.append("--no-window-sensitivity")
    if options.no_rule_threshold_sensitivity:
        fusion_args.append("--no-rule-threshold-sensitivity")
    code = _run(STAGE_SCRIPTS["fusion"], fusion_args)
    if code != 0:
        failed.write_text(
            json.dumps(
                {
                    "status": "FAILED",
                    "stage": "fusion",
                    "returncode": code,
                    "features": str(features),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=[*STAGE_SCRIPTS, "all"],
        help="Experiment stage to run",
    )
    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the stage script",
    )
    options = parser.parse_args(argv)

    if options.stage == "all":
        return _run_all(options.args)

    # Accept both `run_experiments.py fusion --features ...` and the more
    # explicit `run_experiments.py fusion -- --features ...` forms.
    forwarded = list(options.args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    return _run(STAGE_SCRIPTS[options.stage], forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
