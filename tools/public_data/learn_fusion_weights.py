#!/usr/bin/env python
"""Run the W0-W5 late-fusion experiment from a feature CSV.

Example::

    python -m public_data.learn_fusion_weights \
        --features data/processed/pri_mccd_features.csv \
        --out docs/experiments/runs/20261101-120000-fusion \
        --label-col label \
        --group-col lesson_id

Outputs (all written to ``--out``):

* ``metrics.csv`` - out-of-fold metrics per scheme with bootstrap CIs;
* ``predictions.csv`` - long-format out-of-fold predictions and probabilities;
* ``weights_by_fold.csv`` / ``weights.csv`` - chosen weights and thresholds;
* ``per_fold_metrics.csv`` - per-fold metrics for paired tests;
* ``confusion_matrix.csv`` / ``per_class_metrics.csv`` - error analysis;
* ``statistical_tests.csv`` - Wilcoxon / paired bootstrap / Holm;
* ``weight_sensitivity.csv`` - macro-F1/MAE for w_video in 0.00..1.00;
* ``missing_modality.csv`` - neutral-fill and available-only simulations;
* ``window_sensitivity.csv`` - 5s/60s window comparison;
* ``holdout_metrics.csv`` - official split holdout;
* ``rule_threshold_sensitivity.csv`` - rule threshold sensitivity;
* ``ablation.csv`` - feature-group ablation;
* ``config.json`` - run configuration and environment;
* ``summary.json`` / ``summary.md`` - machine/human-readable summary.

The command never fabricates missing inputs: if the feature file or required
columns are absent it exits non-zero with the exact error.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd
import scipy
import sklearn

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from public_data.experiment import (
        ExperimentConfig,
        render_summary_markdown,
        run_cv,
    )
else:
    from .experiment import ExperimentConfig, render_summary_markdown, run_cv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare video/speech late-fusion schemes W0-W5 and the rule "
            "baseline with grouped cross-validation."
        )
    )
    parser.add_argument(
        "--features",
        required=True,
        type=Path,
        help="Feature CSV with one row per clip/window.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output directory (created if missing).",
    )
    parser.add_argument(
        "--label-col",
        default="label",
        help="Ordinal label column (0/1/2). Default: label",
    )
    parser.add_argument(
        "--group-col",
        default=None,
        help=(
            "Group column for GroupKFold. Default: auto-detect lesson_id when "
            "present. Pass 'none' to force StratifiedKFold."
        ),
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Random seed. Default: 42"
    )
    parser.add_argument(
        "--n-splits", type=int, default=5, help="Outer folds. Default: 5"
    )
    parser.add_argument(
        "--inner-splits", type=int, default=3, help="Inner folds. Default: 3"
    )
    parser.add_argument(
        "--n-boot",
        type=int,
        default=1000,
        help="Group-bootstrap replicates for 95%% CIs (0 disables). Default: 1000",
    )
    parser.add_argument(
        "--loso",
        action="store_true",
        help="Use LeaveOneGroupOut for the outer folds when groups are available.",
    )
    parser.add_argument(
        "--split-col",
        default=None,
        help="Optional official split column (e.g. split) for holdout evaluation.",
    )
    parser.add_argument(
        "--holdout-split",
        default=None,
        help="Value of --split-col used as the test set (e.g. test).",
    )
    parser.add_argument(
        "--gbdt-max-iter",
        type=int,
        default=200,
        help="Max iterations for the W5 GBDT upper bound. Default: 200",
    )
    parser.add_argument(
        "--gbdt-learning-rate",
        type=float,
        default=0.05,
        help="Learning rate for the W5 GBDT upper bound. Default: 0.05",
    )
    parser.add_argument(
        "--no-statistical-tests",
        action="store_true",
        help="Skip paired Wilcoxon/bootstrap tests.",
    )
    parser.add_argument(
        "--no-window-sensitivity",
        action="store_true",
        help="Skip 5s/60s window-length sensitivity.",
    )
    parser.add_argument(
        "--no-rule-threshold-sensitivity",
        action="store_true",
        help="Skip rule-threshold sensitivity.",
    )
    parser.add_argument(
        "--missing-repeats",
        type=int,
        default=20,
        help="Repeats per missing-modality rate. Default: 20",
    )
    parser.add_argument(
        "--weight-step",
        type=float,
        default=0.05,
        help="Weight-grid step. Default: 0.05",
    )
    parser.add_argument(
        "--video-prefix",
        default="video_",
        help="Prefix used to auto-detect video features. Default: video_",
    )
    parser.add_argument(
        "--speech-prefix",
        default="speech_",
        help="Prefix used to auto-detect speech features. Default: speech_",
    )
    parser.add_argument(
        "--video-cols",
        default=None,
        help="Comma-separated explicit video feature columns.",
    )
    parser.add_argument(
        "--speech-cols",
        default=None,
        help="Comma-separated explicit speech feature columns.",
    )
    parser.add_argument(
        "--mlp-max-iter",
        type=int,
        default=2000,
        help="Maximum MLP iterations for the W5 upper bound. Default: 2000",
    )
    parser.add_argument(
        "--no-ablation",
        action="store_true",
        help="Skip the feature-group ablation.",
    )
    parser.add_argument(
        "--no-missing-modality",
        action="store_true",
        help="Skip the missing-modality simulation.",
    )
    parser.add_argument(
        "--no-weight-sensitivity",
        action="store_true",
        help="Skip the weight-sensitivity curve.",
    )
    parser.add_argument(
        "--rule-window-seconds",
        type=float,
        default=None,
        help=(
            "Window length the rule baseline assumes, in seconds. Default: read "
            "the window_seconds column when present, else 60. MCCD clips are 5 s, "
            "so pass 5 there; leaving the 60 s default would understate question "
            "density by 12x."
        ),
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress the success message."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    for path, name in ((args.features, "features"),):
        if not path.exists():
            parser.error(f"{name} file does not exist: {path}")
        if not path.is_file():
            parser.error(f"{name} path is not a file: {path}")

    group_col = args.group_col
    auto_group_col: str | None = "lesson_id"
    if group_col is not None and group_col.strip().lower() in {"none", "null", ""}:
        group_col = None
        auto_group_col = None
    elif group_col is not None:
        auto_group_col = None

    config = ExperimentConfig(
        label_col=args.label_col,
        group_col=group_col,
        auto_group_col=auto_group_col,
        video_features=_split_columns(args.video_cols),
        speech_features=_split_columns(args.speech_cols),
        video_prefix=args.video_prefix,
        speech_prefix=args.speech_prefix,
        n_splits=args.n_splits,
        inner_splits=args.inner_splits,
        n_boot=args.n_boot,
        seed=args.seed,
        weight_step=args.weight_step,
        missing_modality_repeats=args.missing_repeats,
        mlp_max_iter=args.mlp_max_iter,
        gbdt_max_iter=args.gbdt_max_iter,
        gbdt_learning_rate=args.gbdt_learning_rate,
        loso=args.loso,
        split_col=args.split_col,
        holdout_split=args.holdout_split,
        run_ablation=not args.no_ablation,
        run_missing_modality=not args.no_missing_modality,
        run_weight_sensitivity=not args.no_weight_sensitivity,
        run_statistical_tests=not args.no_statistical_tests,
        run_window_sensitivity=not args.no_window_sensitivity,
        run_rule_threshold_sensitivity=not args.no_rule_threshold_sensitivity,
        rule_window_seconds=(
            args.rule_window_seconds
            if args.rule_window_seconds is not None
            else ExperimentConfig.rule_window_seconds
        ),
    )

    output_dir = Path(args.out)
    try:
        frame = pd.read_csv(args.features)
        result = run_cv(frame, config)
        result["summary"]["input"] = {"features": str(args.features)}
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_outputs(result, output_dir)
    except Exception as exc:  # noqa: BLE001 - CLI must report the exact failure
        output_dir.mkdir(parents=True, exist_ok=True)
        failure = {
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "stage": "learn_fusion_weights",
            "features": str(args.features),
            "config": {
                "label_col": args.label_col,
                "group_col": args.group_col,
                "seed": args.seed,
                "n_splits": args.n_splits,
                "inner_splits": args.inner_splits,
            },
            "timestamp_utc": _timestamp_utc(),
        }
        (output_dir / "FAILED.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(
            f"Wrote fusion experiment outputs to {output_dir} "
            f"({len(result['metrics'])} schemes, "
            f"{result['dataset']['n_samples']} samples)."
        )
    return 0


def _split_columns(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    columns = tuple(item.strip() for item in value.split(",") if item.strip())
    return columns or None


def _write_outputs(result: dict[str, Any], output_dir: Path) -> None:
    metrics = _metrics_with_bootstrap(result)
    metrics.to_csv(output_dir / "metrics.csv", index=False, encoding="utf-8")

    predictions = result["predictions"]
    predictions.to_csv(
        output_dir / "predictions.csv", index=False, encoding="utf-8"
    )

    weights = result["weights_by_fold"]
    weights.to_csv(
        output_dir / "weights_by_fold.csv", index=False, encoding="utf-8"
    )

    # Protocol section 8: weights.csv must record the selected W3/W4 weights
    # and the selection metric.  Keep weights_by_fold.csv for all schemes.
    weights_protocol = weights[
        weights["scheme"].isin(["W3_auc", "W4_learned"])
    ].copy()
    if weights_protocol.empty:
        weights_protocol = pd.DataFrame(
            columns=[
                "fold",
                "scheme",
                "w_video",
                "w_speech",
                "threshold_1",
                "threshold_2",
                "train_size",
                "test_size",
                "inner_strategy",
                "selection_metric",
            ]
        )
    else:
        weights_protocol["selection_metric"] = "macro_f1"
    weights_protocol.to_csv(
        output_dir / "weights.csv", index=False, encoding="utf-8"
    )

    per_fold = result.get("per_fold_metrics", pd.DataFrame())
    per_fold.to_csv(
        output_dir / "per_fold_metrics.csv", index=False, encoding="utf-8"
    )
    confusion = result.get("confusion_matrix", pd.DataFrame())
    confusion.to_csv(
        output_dir / "confusion_matrix.csv", index=False, encoding="utf-8"
    )
    per_class = result.get("per_class_metrics", pd.DataFrame())
    per_class.to_csv(
        output_dir / "per_class_metrics.csv", index=False, encoding="utf-8"
    )
    statistical_tests = result.get("statistical_tests", pd.DataFrame())
    statistical_tests.to_csv(
        output_dir / "statistical_tests.csv", index=False, encoding="utf-8"
    )
    window_sensitivity = result.get("window_sensitivity", pd.DataFrame())
    window_sensitivity.to_csv(
        output_dir / "window_sensitivity.csv", index=False, encoding="utf-8"
    )
    holdout = result.get("holdout_metrics", pd.DataFrame())
    holdout.to_csv(
        output_dir / "holdout_metrics.csv", index=False, encoding="utf-8"
    )
    rule_sensitivity = result.get("rule_threshold_sensitivity", pd.DataFrame())
    rule_sensitivity.to_csv(
        output_dir / "rule_threshold_sensitivity.csv",
        index=False,
        encoding="utf-8",
    )

    config_record = {
        "status": "OK",
        "timestamp_utc": _timestamp_utc(),
        "input": result["summary"].get("input", {}),
        "config": result["config"],
        "model_defaults": {
            "modality_logistic": {
                "C": 1.0,
                "max_iter": 2000,
                "class_weight": "balanced",
                "random_state": 42,
            },
            "w5_mlp": {
                "hidden_layer_sizes": result["config"].get(
                    "mlp_hidden_layer_sizes"
                ),
                "max_iter": result["config"].get("mlp_max_iter"),
                "alpha": 1e-3,
            },
            "w5_gbdt": {
                "max_leaf_nodes": 15,
                "learning_rate": result["config"].get("gbdt_learning_rate"),
                "max_iter": result["config"].get("gbdt_max_iter"),
            },
        },
        "rule_protocol": "v1",
        "cv": result["cv"],
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
            "scipy": scipy.__version__,
        },
        "git_commit": _git_commit(),
    }
    (output_dir / "config.json").write_text(
        json.dumps(config_record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    sensitivity = result["weight_sensitivity"]
    if sensitivity.empty:
        sensitivity = pd.DataFrame(
            columns=[
                "w_video",
                "w_speech",
                "accuracy",
                "macro_f1",
                "ordinal_mae",
                "spearman",
                "macro_auc_ovr",
                "brier",
                "log_loss",
                "n",
            ]
        )
    sensitivity.to_csv(
        output_dir / "weight_sensitivity.csv", index=False, encoding="utf-8"
    )

    missing = result["missing_modality"]
    if missing.empty:
        missing = pd.DataFrame(
            columns=[
                "scheme",
                "treatment",
                "rate",
                "repeat",
                "accuracy",
                "macro_f1",
                "ordinal_mae",
                "log_loss",
                "n",
                "n_masked",
                "n_video_masked",
                "n_speech_masked",
            ]
        )
    missing.to_csv(
        output_dir / "missing_modality.csv", index=False, encoding="utf-8"
    )

    ablation = result["ablation"]
    if ablation.empty:
        ablation = pd.DataFrame(
            columns=[
                "group",
                "modality",
                "n_features",
                "features",
                "accuracy",
                "macro_f1",
                "ordinal_mae",
                "spearman",
                "macro_auc_ovr",
                "brier",
                "log_loss",
                "n",
            ]
        )
    ablation.to_csv(
        output_dir / "ablation.csv", index=False, encoding="utf-8"
    )

    summary_json = json.dumps(
        result["summary"], ensure_ascii=False, indent=2, allow_nan=False
    )
    (output_dir / "summary.json").write_text(summary_json + "\n", encoding="utf-8")
    (output_dir / "summary.md").write_text(
        render_summary_markdown(result), encoding="utf-8"
    )


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_commit() -> str | None:
    try:
        value = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = value.stdout.strip()
    return commit or None


def _metrics_with_bootstrap(result: dict[str, Any]) -> pd.DataFrame:
    bootstrap = result["bootstrap"]
    rows: list[dict[str, Any]] = []
    for record in result["metrics"].to_dict("records"):
        scheme = record["scheme"]
        scheme_bootstrap = bootstrap.get(scheme)
        row = dict(record)
        for metric in ("accuracy", "macro_f1", "ordinal_mae", "spearman"):
            entry = (
                scheme_bootstrap.get(metric)
                if isinstance(scheme_bootstrap, dict)
                else None
            )
            row[f"{metric}_ci_lower"] = (
                float(entry["ci_lower"]) if entry else float("nan")
            )
            row[f"{metric}_ci_upper"] = (
                float(entry["ci_upper"]) if entry else float("nan")
            )
        rows.append(row)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    raise SystemExit(main())
