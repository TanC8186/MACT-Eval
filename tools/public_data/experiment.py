"""Cross-validated late-fusion experiment engine (spec sections 6 and 7).

The engine compares the fusion schemes W0-W5 plus the rule baseline on a
feature CSV produced by ``extract_features.py`` (or any equivalent table):

======  =====================================================================
W0      video-only and speech-only logistic models
W1      equal weights (0.5 / 0.5)
W2      prior weights (0.5 / 0.5 and 0.6 / 0.4), reported separately
W3      performance-derived weights ``max(0, AUC - 1/3)``
W4      weights learned by a 0.05-step grid search on inner folds
W5      early feature concatenation with logistic regression, a small MLP,
        and a GBDT robustness upper bound
R0      video/speech rule baseline from :mod:`public_data.rule_baseline`
======  =====================================================================

Grouped cross-validation is used whenever the group column contains at least
two groups; otherwise the engine falls back to stratified 5-fold CV and
records that limitation.  Weight and threshold selection happen exclusively on
inner folds of the outer training data, so the outer test fold never
participates in any choice.

The returned dictionary contains pandas DataFrames for metrics, predictions,
per-fold weights, weight sensitivity, missing-modality simulations, ablation
rows and a JSON-ready summary.  Nothing requires network access or real data;
the unit tests drive the whole engine with synthetic three-class frames.
"""
from __future__ import annotations

import math
import platform
import re
import warnings
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.model_selection import (
    GroupKFold,
    KFold,
    LeaveOneGroupOut,
    StratifiedKFold,
)
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import analysis, fusion, rule_baseline
from .metrics import classification_metrics, grouped_bootstrap_ci

SCHEME_ORDER: tuple[str, ...] = (
    "W0_video_only",
    "W0_speech_only",
    "W1_equal",
    "W2_prior_balanced",
    "W2_prior_visual",
    "W3_auc",
    "W4_learned",
    "W5_early_logistic",
    "W5_mlp",
    "W5_early_gbdt",
    "R0_rule_equal",
)

PROBABILITY_SCHEMES: tuple[str, ...] = SCHEME_ORDER[:-1]

FIXED_WEIGHTS: dict[str, tuple[float, float]] = {
    "W0_video_only": (1.0, 0.0),
    "W0_speech_only": (0.0, 1.0),
    "W1_equal": (0.5, 0.5),
    "W2_prior_balanced": (0.5, 0.5),
    "W2_prior_visual": (0.6, 0.4),
}

VARIABLE_WEIGHT_SCHEMES: tuple[str, ...] = ("W3_auc", "W4_learned")

_RULE_VIDEO_ALIASES: dict[str, tuple[str, ...]] = {
    "posture_score": ("video_posture_score", "posture_score"),
    "stability_score": ("video_stability_score", "stability_score"),
    "positive_behavior_ratio": (
        "video_positive_behavior_ratio",
        "positive_behavior_ratio",
    ),
}

_RULE_SPEECH_ALIASES: dict[str, tuple[str, ...]] = {
    "coverage": ("speech_coverage", "coverage"),
    "question_count": ("speech_question_count", "question_count"),
    "speech_rate": ("speech_rate", "speech_rate_cpm"),
}

_RULE_WINDOW_ALIASES: tuple[str, ...] = (
    "speech_window_seconds",
    "window_seconds",
)

#: Numeric video columns emitted by ``extract_features.py``.  Auto-detection
#: accepts these unprefixed names in addition to columns with the configured
#: prefix, so the engine runs directly on the repository's feature table.
DEFAULT_VIDEO_FEATURES: tuple[str, ...] = (
    "frame_count",
    "skipped_frame_count",
    "posture_score",
    "stability_score",
    "head_motion",
    "mean_pitch",
    "mean_yaw",
    "mean_roll",
    "mean_abs_pitch",
    "std_pitch",
    "std_yaw",
    "std_roll",
    "sampled_frames",
    "person_count",
    "hand_raising_count",
    "reading_count",
    "writing_count",
    "total_detections",
    "detection_coverage",
    "mean_confidence",
    "positive_behavior_ratio",
    "upright_ratio",
)

#: Numeric speech/ASR columns emitted by ``extract_features.py``.
DEFAULT_SPEECH_FEATURES: tuple[str, ...] = (
    "speech_seconds",
    "speech_coverage",
    "question_count",
    "speech_rate",
    "pause_ratio",
    "asr_confidence",
    "char_count",
    "questions_per_minute",
)


@dataclass(frozen=True)
class ExperimentConfig:
    """Configuration for :func:`run_cv`.

    ``group_col=None`` auto-detects ``lesson_id`` when present, matching the
    Pri-MCCD manifest.  Set ``auto_group_col=None`` to disable detection.
    """

    label_col: str = "label"
    group_col: str | None = None
    auto_group_col: str | None = "lesson_id"
    video_features: tuple[str, ...] | None = None
    speech_features: tuple[str, ...] | None = None
    video_prefix: str = "video_"
    speech_prefix: str = "speech_"
    n_splits: int = 5
    inner_splits: int = 3
    n_boot: int = 1000
    seed: int = 42
    weight_step: float = fusion.DEFAULT_WEIGHT_STEP
    missing_modality_rates: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5)
    missing_modality_repeats: int = 20
    mlp_hidden_layer_sizes: tuple[int, ...] = (16,)
    mlp_max_iter: int = 2000
    gbdt_max_iter: int = 200
    gbdt_learning_rate: float = 0.05
    rule_window_seconds: float = 60.0
    loso: bool = False
    split_col: str | None = None
    holdout_split: str | None = None
    run_ablation: bool = True
    run_missing_modality: bool = True
    run_weight_sensitivity: bool = True
    run_statistical_tests: bool = True
    run_window_sensitivity: bool = True
    run_rule_threshold_sensitivity: bool = True
    max_threshold_candidates: int = fusion.DEFAULT_MAX_THRESHOLD_CANDIDATES


def run_cv(df: pd.DataFrame, config: ExperimentConfig | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run the full W0-W5 + rule-baseline comparison.

    Args:
        df: Feature table with a label column, optional group column, and
            numeric video/speech feature columns (``video_*`` / ``speech_*``
            by default).  Non-numeric and all-missing feature columns are
            dropped with a recorded warning.
        config: :class:`ExperimentConfig`, a mapping of its fields, or
            ``None`` for defaults.

    Returns:
        A dictionary with ``config``, ``dataset``, ``cv``, ``folds``,
        ``metrics`` (DataFrame), ``schemes`` (per-scheme metrics, bootstrap
        and fold selections), ``bootstrap``, ``predictions`` (DataFrame),
        ``weights_by_fold`` (DataFrame), ``weight_sensitivity`` (DataFrame),
        ``missing_modality`` (DataFrame), ``ablation`` (DataFrame),
        ``weight_stability``, ``summary`` and ``warnings``.
    """
    cfg = _coerce_config(config)
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame")
    if df.empty:
        raise ValueError("df must not be empty")
    if cfg.n_splits < 2 or cfg.inner_splits < 2:
        raise ValueError("n_splits and inner_splits must be at least 2")
    if cfg.n_boot < 0:
        raise ValueError("n_boot must be non-negative")
    if cfg.missing_modality_repeats < 0:
        raise ValueError("missing_modality_repeats must be non-negative")

    run_warnings: list[str] = []
    y = _resolve_labels(df, cfg)

    feature_sets, feature_warnings = _resolve_features(df, cfg)
    run_warnings.extend(feature_warnings)
    Xv = feature_sets["video"]["matrix"]
    Xs = feature_sets["speech"]["matrix"]
    Xe = np.hstack([Xv, Xs])

    groups, group_column, n_groups, group_warning = _resolve_groups(df, cfg)
    if group_warning:
        run_warnings.append(group_warning)

    outer = _make_splits(y, groups, cfg.n_splits, cfg.seed, loso=cfg.loso)
    if outer["strategy"] not in {"GroupKFold", "LeaveOneGroupOut"}:
        run_warnings.append(
            "No usable group column: using "
            f"{outer['strategy']} with {outer['n_splits']} splits; clips from "
            "the same lesson may cross folds (record this limitation)."
        )
    if outer["n_splits"] < cfg.n_splits:
        run_warnings.append(
            f"Requested {cfg.n_splits} outer folds but only "
            f"{outer['n_splits']} are feasible."
        )

    rule_video, rule_speech, rule_note = _rule_scores(df, cfg)
    if rule_note:
        run_warnings.append(rule_note)
    rule_available = (
        (rule_video is not None and np.isfinite(rule_video).any())
        or (rule_speech is not None and np.isfinite(rule_speech).any())
    )
    if rule_available and not any(alias in df.columns for alias in _RULE_WINDOW_ALIASES):
        run_warnings.append(
            "Rule scores assumed a 60s window because no window_seconds column "
            "was present; record this assumption."
        )

    n_samples = len(df)
    fold_of_row = np.full(n_samples, -1, dtype=int)
    fold_records: list[dict[str, Any]] = []

    for fold_index, (train_index, test_index) in enumerate(outer["splits"]):
        fold_of_row[test_index] = fold_index
        y_train = y[train_index]
        inner_groups = groups[train_index] if groups is not None else None
        inner = _inner_oof_all(
            Xv[train_index],
            Xs[train_index],
            Xe[train_index],
            y_train,
            inner_groups,
            cfg,
        )

        pv_test = _fit_predict(Xv[train_index], y_train, Xv[test_index])
        ps_test = _fit_predict(Xs[train_index], y_train, Xs[test_index])
        pe_test = _fit_predict(Xe[train_index], y_train, Xe[test_index])
        pm_test = _fit_mlp_predict(Xe[train_index], y_train, Xe[test_index], cfg)
        pg_test = _fit_gbdt_predict(Xe[train_index], y_train, Xe[test_index], cfg)

        scheme_records: dict[str, dict[str, Any]] = {}
        for scheme in PROBABILITY_SCHEMES:
            if scheme == "W5_early_logistic":
                inner_fused = inner["early"]
                test_fused = pe_test
                weights = (float("nan"), float("nan"))
            elif scheme == "W5_mlp":
                inner_fused = inner["mlp"]
                test_fused = pm_test
                weights = (float("nan"), float("nan"))
            elif scheme == "W5_early_gbdt":
                inner_fused = inner["gbdt"]
                test_fused = pg_test
                weights = (float("nan"), float("nan"))
            else:
                weights = _scheme_weights(scheme, inner, y_train, cfg)
                inner_fused = fusion.fuse_proba(
                    inner["video"], inner["speech"], weights[0]
                )
                test_fused = fusion.fuse_proba(pv_test, ps_test, weights[0])

            thresholds = fusion.learn_thresholds(
                fusion.ordinal_score(inner_fused),
                y_train,
                max_candidates=cfg.max_threshold_candidates,
            )
            score = fusion.ordinal_score(test_fused)
            prediction = fusion.apply_thresholds(score, thresholds)
            scheme_records[scheme] = {
                "weights": weights,
                "thresholds": thresholds,
                "prediction": prediction,
                "probabilities": test_fused,
                "score": score,
                "n_unavailable": 0,
            }

        if rule_available:
            rule_record = _rule_scheme_fold(
                rule_video, rule_speech, train_index, test_index, y_train, cfg
            )
            scheme_records["R0_rule_equal"] = rule_record

        fold_records.append(
            {
                "fold": fold_index,
                "train_index": train_index,
                "test_index": test_index,
                "y_train": y_train,
                "inner_strategy": inner["strategy"],
                "inner": inner,
                "pv_test": pv_test,
                "ps_test": ps_test,
                "schemes": scheme_records,
            }
        )

    present_schemes = [
        scheme
        for scheme in SCHEME_ORDER
        if any(scheme in record["schemes"] for record in fold_records)
    ]

    bootstrap_groups = groups if groups is not None else np.arange(n_samples)
    metrics_rows: list[dict[str, Any]] = []
    bootstrap_results: dict[str, Any] = {}
    scheme_predictions: dict[str, np.ndarray] = {}
    scheme_probabilities: dict[str, np.ndarray] = {}
    scheme_scores: dict[str, np.ndarray] = {}
    scheme_thresholds: dict[str, np.ndarray] = {}
    scheme_weights: dict[str, np.ndarray] = {}

    for scheme in present_schemes:
        collected = _collect_scheme(
            fold_records, scheme, n_samples, probability_scheme=scheme != "R0_rule_equal"
        )
        has_probabilities = bool(np.all(np.isfinite(collected["probabilities"])))
        valid = collected["prediction"] >= 0
        if not valid.any():
            metrics = {
                key: float("nan")
                for key in (
                    "accuracy",
                    "macro_f1",
                    "ordinal_mae",
                    "spearman",
                    "macro_auc_ovr",
                    "brier",
                    "log_loss",
                )
            }
        else:
            metrics = classification_metrics(
                y[valid],
                collected["prediction"][valid],
                y_score=collected["probabilities"][valid]
                if has_probabilities
                else None,
                ordinal_scores=collected["scores"][valid],
            )
        metrics_rows.append(
            {
                "scheme": scheme,
                "n": int(valid.sum()),
                "n_unavailable": int(collected["n_unavailable"]),
                **metrics,
            }
        )
        scheme_predictions[scheme] = collected["prediction"]
        scheme_probabilities[scheme] = collected["probabilities"]
        scheme_scores[scheme] = collected["scores"]
        scheme_thresholds[scheme] = collected["thresholds"]
        scheme_weights[scheme] = collected["weights"]
        if cfg.n_boot > 0 and valid.any():
            bootstrap_results[scheme] = grouped_bootstrap_ci(
                y[valid],
                collected["prediction"][valid],
                bootstrap_groups[valid],
                n_boot=cfg.n_boot,
                seed=cfg.seed,
                ordinal_scores=collected["scores"][valid],
            )
        else:
            bootstrap_results[scheme] = None

    metrics_df = pd.DataFrame(metrics_rows)
    predictions_df = _predictions_frame(
        df, y, groups, fold_of_row, present_schemes,
        scheme_predictions, scheme_probabilities, scheme_scores,
        scheme_thresholds, scheme_weights,
    )
    weights_df = _weights_by_fold_frame(fold_records, present_schemes)
    weight_stability = _weight_stability(fold_records, present_schemes)
    scheme_details: dict[str, dict[str, Any]] = {}
    for row in metrics_rows:
        scheme = row["scheme"]
        scheme_details[scheme] = {
            "metrics": {
                key: value
                for key, value in row.items()
                if key not in {"scheme", "n"}
            },
            "bootstrap": bootstrap_results[scheme],
            "fold_weights": [
                float(record["schemes"][scheme]["weights"][0])
                for record in fold_records
                if scheme in record["schemes"]
            ],
            "fold_thresholds": [
                tuple(
                    float(value)
                    for value in record["schemes"][scheme]["thresholds"]
                )
                for record in fold_records
                if scheme in record["schemes"]
            ],
            "prediction": scheme_predictions[scheme],
        }
    weight_sensitivity = (
        _weight_sensitivity(fold_records, y, cfg)
        if cfg.run_weight_sensitivity
        else pd.DataFrame()
    )
    missing_modality = (
        _missing_modality_simulation(fold_records, y, cfg)
        if cfg.run_missing_modality
        else pd.DataFrame()
    )
    ablation = _ablation_table(df, feature_sets, y, outer, groups, cfg)

    single_modality = [
        scheme
        for scheme in ("W0_video_only", "W0_speech_only")
        if scheme in set(metrics_df["scheme"])
    ]
    if single_modality:
        reference_scheme = (
            metrics_df[metrics_df["scheme"].isin(single_modality)]
            .sort_values("macro_f1", ascending=False)
            .iloc[0]["scheme"]
        )
    else:
        reference_scheme = metrics_df.iloc[0]["scheme"] if not metrics_df.empty else None

    per_fold = (
        analysis.per_fold_metrics(fold_records, y, present_schemes)
        if cfg.run_statistical_tests
        else pd.DataFrame()
    )
    confusion_matrix, per_class_metrics = analysis.confusion_tables(
        y, scheme_predictions
    )
    statistical_tests = (
        analysis.paired_tests(per_fold, reference_scheme)
        if cfg.run_statistical_tests and reference_scheme is not None
        else pd.DataFrame()
    )
    if (
        cfg.run_statistical_tests
        and reference_scheme is not None
        and not statistical_tests.empty
    ):
        reference_prediction = scheme_predictions[reference_scheme]
        reference_valid = reference_prediction >= 0
        bootstrap_rows: list[dict[str, Any]] = []
        for scheme in statistical_tests["scheme"]:
            scheme_prediction = scheme_predictions.get(scheme)
            if scheme_prediction is None:
                continue
            valid = reference_valid & (scheme_prediction >= 0)
            if not valid.any():
                continue
            result = analysis.paired_bootstrap_ci(
                y[valid],
                reference_prediction[valid],
                scheme_prediction[valid],
                bootstrap_groups[valid],
                metric="macro_f1",
                n_boot=max(1, int(cfg.n_boot)) if cfg.n_boot > 0 else 1000,
                seed=cfg.seed,
            )
            bootstrap_rows.append({"scheme": scheme, **result})
        if bootstrap_rows:
            statistical_tests = statistical_tests.merge(
                pd.DataFrame(bootstrap_rows), on="scheme", how="left"
            )
    window_sensitivity = (
        _window_sensitivity(df, cfg) if cfg.run_window_sensitivity else pd.DataFrame()
    )
    holdout_metrics = _holdout_evaluation(df, feature_sets, y, cfg, groups)
    rule_threshold_sensitivity = (
        _rule_threshold_sensitivity(df, y, outer, groups, cfg)
        if cfg.run_rule_threshold_sensitivity
        else pd.DataFrame()
    )

    dataset_info = {
        "n_samples": n_samples,
        "n_features_video": int(Xv.shape[1]),
        "n_features_speech": int(Xs.shape[1]),
        "video_features": feature_sets["video"]["columns"],
        "speech_features": feature_sets["speech"]["columns"],
        "label_counts": {
            str(int(label)): int(np.sum(y == label)) for label in sorted(np.unique(y))
        },
        "group_column": group_column,
        "n_groups": int(n_groups),
    }
    cv_info = {
        "strategy": outer["strategy"],
        "n_splits": int(outer["n_splits"]),
        "inner_strategy": fold_records[0]["inner_strategy"] if fold_records else None,
        "inner_splits": int(cfg.inner_splits),
        "seed": int(cfg.seed),
        "groups_available": groups is not None,
    }

    summary = _build_summary(
        cfg, dataset_info, cv_info, metrics_df, bootstrap_results,
        weight_stability, weight_sensitivity, missing_modality, ablation,
        fold_records, run_warnings,
        per_fold=per_fold,
        confusion_matrix=confusion_matrix,
        per_class_metrics=per_class_metrics,
        statistical_tests=statistical_tests,
        reference_scheme=reference_scheme,
        window_sensitivity=window_sensitivity,
        holdout_metrics=holdout_metrics,
        rule_threshold_sensitivity=rule_threshold_sensitivity,
    )

    return {
        "config": _config_dict(cfg),
        "dataset": dataset_info,
        "cv": cv_info,
        "folds": [
            {
                "fold": int(record["fold"]),
                "train_size": int(len(record["train_index"])),
                "test_size": int(len(record["test_index"])),
                "inner_strategy": record["inner_strategy"],
                "weights": {
                    scheme: list(values) for scheme, values in record["schemes"].items()
                },
            }
            for record in fold_records
        ],
        "metrics": metrics_df,
        "schemes": scheme_details,
        "bootstrap": bootstrap_results,
        "predictions": predictions_df,
        "weights_by_fold": weights_df,
        "weight_sensitivity": weight_sensitivity,
        "missing_modality": missing_modality,
        "ablation": ablation,
        "weight_stability": weight_stability,
        "per_fold_metrics": per_fold,
        "confusion_matrix": confusion_matrix,
        "per_class_metrics": per_class_metrics,
        "statistical_tests": statistical_tests,
        "reference_scheme": reference_scheme,
        "window_sensitivity": window_sensitivity,
        "holdout_metrics": holdout_metrics,
        "rule_threshold_sensitivity": rule_threshold_sensitivity,
        "summary": summary,
        "warnings": run_warnings,
    }


def render_summary_markdown(result: Mapping[str, Any]) -> str:
    """Render a human-readable Markdown report from :func:`run_cv` output."""
    summary = result["summary"]
    metrics = result["metrics"]
    lines: list[str] = []
    lines.append("# Late-fusion experiment summary")
    lines.append("")
    lines.append("Generated by `tools/public_data/experiment.py`.")
    lines.append("")

    dataset = summary["dataset"]
    cv = summary["cv"]
    lines.append("## Dataset and cross-validation")
    lines.append("")
    lines.append(f"- Samples: {dataset['n_samples']}")
    lines.append(f"- Video features: {dataset['n_features_video']}")
    lines.append(f"- Speech features: {dataset['n_features_speech']}")
    lines.append(f"- Label counts: {dataset['label_counts']}")
    lines.append(
        f"- CV: {cv['strategy']} ({cv['n_splits']} outer, "
        f"{cv['inner_splits']} inner), seed {cv['seed']}"
    )
    lines.append(f"- Groups: {dataset['n_groups']} (column {dataset['group_column']!r})")
    if summary.get("input"):
        lines.append(f"- Input: {summary['input']}")
    environment = summary.get("environment", {})
    if environment:
        lines.append(
            "- Environment: Python {python}, numpy {numpy}, pandas {pandas}, "
            "scikit-learn {scikit_learn}".format(**environment)
        )
    lines.append("")

    lines.append("## Out-of-fold scheme metrics")
    lines.append("")
    lines.append(
        "| scheme | n | accuracy | macro-F1 | ordinal MAE | macro AUC | Brier | log loss |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for _, row in metrics.iterrows():
        lines.append(
            "| {scheme} | {n} | {accuracy} | {macro_f1} | {ordinal_mae} | "
            "{macro_auc_ovr} | {brier} | {log_loss} |".format(
                scheme=row["scheme"],
                n=int(row["n"]),
                accuracy=_fmt(row.get("accuracy")),
                macro_f1=_fmt(row.get("macro_f1")),
                ordinal_mae=_fmt(row.get("ordinal_mae")),
                macro_auc_ovr=_fmt(row.get("macro_auc_ovr")),
                brier=_fmt(row.get("brier")),
                log_loss=_fmt(row.get("log_loss")),
            )
        )
    lines.append("")

    lines.append("## 95% group-bootstrap confidence intervals")
    lines.append("")
    lines.append("| scheme | metric | point | mean | CI lower | CI upper |")
    lines.append("|---|---|---|---|---|---|")
    for scheme, metrics_boot in summary["bootstrap"].items():
        if not metrics_boot:
            continue
        for metric_name in ("accuracy", "macro_f1", "ordinal_mae"):
            entry = metrics_boot.get(metric_name)
            if entry is None:
                continue
            lines.append(
                f"| {scheme} | {metric_name} | {_fmt(entry['point'])} | "
                f"{_fmt(entry['mean'])} | {_fmt(entry['ci_lower'])} | "
                f"{_fmt(entry['ci_upper'])} |"
            )
    lines.append("")

    lines.append("## Weights by fold")
    lines.append("")
    lines.append("| fold | scheme | w_video | w_speech | t1 | t2 |")
    lines.append("|---|---|---|---|---|---|")
    for record in summary["weights_by_fold"]:
        lines.append(
            "| {fold} | {scheme} | {w_video} | {w_speech} | {t1} | {t2} |".format(
                fold=record["fold"],
                scheme=record["scheme"],
                w_video=_fmt(record["w_video"]),
                w_speech=_fmt(record["w_speech"]),
                t1=_fmt(record["threshold_1"]),
                t2=_fmt(record["threshold_2"]),
            )
        )
    lines.append("")

    lines.append("## Weight stability")
    lines.append("")
    if summary["weight_stability"]:
        lines.append("| scheme | mean w_video | std | CV | conclusion |")
        lines.append("|---|---|---|---|---|")
        for scheme, entry in summary["weight_stability"].items():
            lines.append(
                f"| {scheme} | {_fmt(entry['mean'])} | {_fmt(entry['std'])} | "
                f"{_fmt(entry['cv'])} | {entry['conclusion']} |"
            )
    else:
        lines.append("No variable-weight schemes were evaluated.")
    lines.append("")

    lines.append("## Weight sensitivity")
    lines.append("")
    sensitivity = summary["weight_sensitivity"]
    if sensitivity.get("rows"):
        lines.append(
            f"- Best grid weight: w_video={_fmt(sensitivity['best_w_video'])} "
            f"(macro-F1={_fmt(sensitivity['best_macro_f1'])})"
        )
        lines.append("")
        lines.append("| w_video | w_speech | accuracy | macro-F1 | ordinal MAE |")
        lines.append("|---|---|---|---|---|")
        for row in sensitivity["rows"]:
            lines.append(
                f"| {_fmt(row['w_video'])} | {_fmt(row['w_speech'])} | "
                f"{_fmt(row['accuracy'])} | {_fmt(row['macro_f1'])} | "
                f"{_fmt(row['ordinal_mae'])} |"
            )
    else:
        lines.append("Weight sensitivity was not evaluated.")
    lines.append("")

    lines.append("## Missing-modality robustness")
    lines.append("")
    missing = summary["missing_modality"]
    if missing:
        lines.append(
            "| scheme | treatment | rate | macro-F1 mean | macro-F1 std | "
            "ordinal MAE mean | macro-F1 drop | MAE increase | H5 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for row in missing:
            lines.append(
                f"| {row['scheme']} | {row.get('treatment', 'n/a')} | "
                f"{_fmt(row['rate'])} | {_fmt(row['macro_f1_mean'])} | "
                f"{_fmt(row['macro_f1_std'])} | {_fmt(row['ordinal_mae_mean'])} | "
                f"{_fmt(row.get('macro_f1_drop'))} | "
                f"{_fmt(row.get('ordinal_mae_increase'))} | "
                f"{row.get('h5_verdict', 'n/a')} |"
            )
    else:
        lines.append("Missing-modality simulation was not evaluated.")
    lines.append("")

    lines.append("## Feature-group ablation")
    lines.append("")
    if summary["ablation"]:
        lines.append("| group | modality | n_features | accuracy | macro-F1 | ordinal MAE |")
        lines.append("|---|---|---|---|---|---|")
        for row in summary["ablation"]:
            lines.append(
                f"| {row['group']} | {row['modality']} | {row['n_features']} | "
                f"{_fmt(row['accuracy'])} | {_fmt(row['macro_f1'])} | "
                f"{_fmt(row['ordinal_mae'])} |"
            )
    else:
        lines.append("Feature-group ablation was not evaluated.")
    lines.append("")

    lines.append("## Paired statistical tests")
    lines.append("")
    tests = summary.get("statistical_tests") or []
    if tests:
        lines.append(
            f"Reference scheme: {summary.get('reference_scheme')} "
            "(best single modality by macro-F1)."
        )
        lines.append("")
        lines.append(
            "| scheme | n folds | mean diff | group CI low | group CI high | "
            "Wilcoxon p | fold bootstrap p | group bootstrap p | Holm p | significant |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for row in tests:
            lines.append(
                f"| {row['scheme']} | {row['n_folds']} | "
                f"{_fmt(row['mean_diff'])} | {_fmt(row.get('ci_lower'))} | "
                f"{_fmt(row.get('ci_upper'))} | {_fmt(row.get('p_value'))} | "
                f"{_fmt(row.get('bootstrap_p'))} | "
                f"{_fmt(row.get('group_bootstrap_p'))} | "
                f"{_fmt(row.get('p_holm'))} | {row.get('significant_holm')} |"
            )
    else:
        lines.append("Paired tests were not evaluated.")
    lines.append("")

    lines.append("## Per-class metrics")
    lines.append("")
    per_class = summary.get("per_class_metrics") or []
    if per_class:
        lines.append("| scheme | class | precision | recall | F1 | support |")
        lines.append("|---|---|---|---|---|---|")
        for row in per_class:
            lines.append(
                f"| {row['scheme']} | {row['class']} | "
                f"{_fmt(row['precision'])} | {_fmt(row['recall'])} | "
                f"{_fmt(row['f1'])} | {row['support']} |"
            )
    else:
        lines.append("Per-class metrics were not evaluated.")
    lines.append("")

    lines.append("## Window-length sensitivity")
    lines.append("")
    window_rows = summary.get("window_sensitivity") or []
    if window_rows:
        lines.append("| window (s) | status | scheme | n | macro-F1 | ordinal MAE |")
        lines.append("|---|---|---|---|---|---|")
        for row in window_rows:
            lines.append(
                f"| {_fmt(row.get('window_seconds'))} | {row.get('status')} | "
                f"{row.get('scheme', 'n/a')} | {row.get('n', 'n/a')} | "
                f"{_fmt(row.get('macro_f1'))} | {_fmt(row.get('ordinal_mae'))} |"
            )
    else:
        lines.append("Window-length sensitivity was not evaluated.")
    lines.append("")

    lines.append("## Official split holdout")
    lines.append("")
    holdout = summary.get("holdout_metrics") or []
    if holdout:
        lines.append("| scheme | status | n train | n test | accuracy | macro-F1 | ordinal MAE |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in holdout:
            lines.append(
                f"| {row.get('scheme', 'n/a')} | {row.get('status')} | "
                f"{row.get('n_train', 'n/a')} | {row.get('n_test', 'n/a')} | "
                f"{_fmt(row.get('accuracy'))} | {_fmt(row.get('macro_f1'))} | "
                f"{_fmt(row.get('ordinal_mae'))} |"
            )
    else:
        lines.append("No official split holdout was requested or available.")
    lines.append("")

    lines.append("## Rule-threshold sensitivity")
    lines.append("")
    rule_rows = summary.get("rule_threshold_sensitivity") or []
    if rule_rows:
        lines.append(
            "| question saturation | rate min | rate max | status | macro-F1 | ordinal MAE |"
        )
        lines.append("|---|---|---|---|---|---|")
        for row in rule_rows:
            lines.append(
                f"| {_fmt(row.get('question_saturation'))} | "
                f"{_fmt(row.get('rate_min'))} | {_fmt(row.get('rate_max'))} | "
                f"{row.get('status')} | {_fmt(row.get('macro_f1'))} | "
                f"{_fmt(row.get('ordinal_mae'))} |"
            )
    else:
        lines.append("Rule-threshold sensitivity was not evaluated.")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    if summary["warnings"]:
        for warning in summary["warnings"]:
            lines.append(f"- {warning}")
    else:
        lines.append("- No warnings.")
    lines.append("")
    lines.append(
        "All weights and thresholds were selected on inner folds of each "
        "outer training split; outer test folds were never used for selection."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Configuration and input validation
# ---------------------------------------------------------------------------


def _coerce_config(
    config: ExperimentConfig | Mapping[str, Any] | None,
) -> ExperimentConfig:
    if config is None:
        return ExperimentConfig()
    if isinstance(config, ExperimentConfig):
        return config
    if not isinstance(config, Mapping):
        raise TypeError("config must be ExperimentConfig, a mapping or None")

    allowed = {field.name for field in fields(ExperimentConfig)}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"Unknown config field(s): {sorted(unknown)}")
    values = dict(config)
    for name in (
        "video_features",
        "speech_features",
        "missing_modality_rates",
        "mlp_hidden_layer_sizes",
    ):
        if values.get(name) is not None:
            values[name] = tuple(values[name])
    return ExperimentConfig(**values)


def _config_dict(config: ExperimentConfig) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields(ExperimentConfig):
        value = getattr(config, field.name)
        if isinstance(value, tuple):
            value = list(value)
        result[field.name] = value
    return result


def _resolve_labels(df: pd.DataFrame, config: ExperimentConfig) -> np.ndarray:
    if config.label_col not in df.columns:
        raise ValueError(
            f"Label column {config.label_col!r} not found; available columns: "
            f"{list(df.columns)}"
        )
    raw = df[config.label_col]
    if raw.isna().any():
        raise ValueError(
            f"Label column {config.label_col!r} contains {int(raw.isna().sum())} missing value(s)"
        )
    numeric = pd.to_numeric(raw, errors="coerce")
    if numeric.isna().any():
        raise ValueError(f"Label column {config.label_col!r} must be numeric")
    rounded = np.round(numeric.to_numpy(dtype=float))
    if not np.allclose(numeric.to_numpy(dtype=float), rounded):
        raise ValueError(f"Label column {config.label_col!r} must contain integers")
    labels = rounded.astype(int)
    unknown = sorted(set(np.unique(labels)) - set(fusion.LABELS))
    if unknown:
        raise ValueError(
            f"Label column {config.label_col!r} contains labels outside "
            f"{fusion.LABELS}: {unknown}"
        )
    if np.unique(labels).size < 2:
        raise ValueError(
            f"At least two classes are required for cross-validation; "
            f"found only {sorted(np.unique(labels))}"
        )
    return labels


def _resolve_features(
    df: pd.DataFrame, config: ExperimentConfig
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    excluded = {
        config.label_col,
        config.group_col or "",
        config.auto_group_col or "",
        "clip_id",
        "split",
        "label_name",
    }
    feature_warnings: list[str] = []
    video_columns = _feature_columns(
        df,
        config.video_features,
        config.video_prefix,
        excluded,
        DEFAULT_VIDEO_FEATURES,
        "video",
    )
    speech_columns = _feature_columns(
        df,
        config.speech_features,
        config.speech_prefix,
        excluded,
        DEFAULT_SPEECH_FEATURES,
        "speech",
    )

    video_matrix, video_kept, video_dropped = _numeric_matrix(
        df, video_columns, "video"
    )
    speech_matrix, speech_kept, speech_dropped = _numeric_matrix(
        df, speech_columns, "speech"
    )
    for modality, dropped in (("video", video_dropped), ("speech", speech_dropped)):
        if dropped:
            feature_warnings.append(
                f"Dropped non-numeric or all-missing {modality} column(s): "
                f"{dropped}"
            )

    return (
        {
            "video": {
                "columns": video_kept,
                "matrix": video_matrix,
            },
            "speech": {
                "columns": speech_kept,
                "matrix": speech_matrix,
            },
        },
        feature_warnings,
    )


def _feature_columns(
    df: pd.DataFrame,
    explicit: Sequence[str] | None,
    prefix: str,
    excluded: set[str],
    known_defaults: Sequence[str],
    modality: str,
) -> list[str]:
    if explicit is not None:
        columns = list(explicit)
        missing = [column for column in columns if column not in df.columns]
        if missing:
            raise ValueError(f"Requested feature column(s) not found: {missing}")
        if not columns:
            raise ValueError("Explicit feature list must not be empty")
        return columns

    known = set(known_defaults)
    columns = []
    for column in df.columns:
        if column in excluded:
            continue
        if not (column.startswith(prefix) or column in known):
            continue
        lowered = column.lower()
        if lowered.endswith(("_path", "_file", "_id")):
            continue
        columns.append(column)
    if not columns:
        raise ValueError(
            f"No {modality} feature columns found (prefix {prefix!r} or the "
            "canonical extract_features.py names); pass explicit "
            "video_features/speech_features in the config"
        )
    return columns


def _numeric_matrix(
    df: pd.DataFrame, columns: Sequence[str], modality: str
) -> tuple[np.ndarray, list[str], list[str]]:
    frame = df[list(columns)].apply(pd.to_numeric, errors="coerce")
    kept = [column for column in columns if frame[column].notna().any()]
    dropped = [column for column in columns if column not in kept]
    if not kept:
        raise ValueError(
            f"All {modality} feature columns are non-numeric or entirely "
            f"missing: {list(columns)}"
        )
    return frame[kept].to_numpy(dtype=float), kept, dropped


def _resolve_groups(
    df: pd.DataFrame, config: ExperimentConfig
) -> tuple[np.ndarray | None, str | None, int, str | None]:
    column = config.group_col
    if column is None and config.auto_group_col is not None:
        if config.auto_group_col in df.columns:
            column = config.auto_group_col
    if column is None:
        return None, None, 0, None
    if column not in df.columns:
        raise ValueError(f"Group column {column!r} not found")

    raw = df[column]
    values = np.empty(len(df), dtype=object)
    missing_count = 0
    for index, value in enumerate(raw):
        if pd.isna(value):
            values[index] = f"__row_{index}"
            missing_count += 1
        else:
            values[index] = str(value)

    if missing_count == len(df):
        return None, column, 0, (
            f"Group column {column!r} is entirely missing; "
            "falling back to StratifiedKFold and recording the leakage limitation"
        )

    n_groups = int(np.unique(values).size)
    warning = None
    if n_groups < 2:
        warning = (
            f"Group column {column!r} contains {n_groups} group(s); "
            "falling back to StratifiedKFold"
        )
        return None, column, n_groups, warning
    if missing_count:
        warning = (
            f"{missing_count} row(s) had a missing {column!r}; each was "
            "treated as its own group to avoid leakage"
        )
    return values, column, n_groups, warning


# ---------------------------------------------------------------------------
# Split construction and model fitting
# ---------------------------------------------------------------------------


def _make_splits(
    y: np.ndarray,
    groups: np.ndarray | None,
    requested_splits: int,
    seed: int,
    *,
    loso: bool = False,
) -> dict[str, Any]:
    if requested_splits < 2:
        raise ValueError("n_splits must be at least 2")
    n_samples = len(y)

    if groups is not None:
        n_groups = int(np.unique(groups).size)
        if n_groups >= 2 and loso:
            splitter = LeaveOneGroupOut()
            splits = list(
                splitter.split(np.zeros((n_samples, 1)), y, groups=groups)
            )
            return {
                "strategy": "LeaveOneGroupOut",
                "n_splits": n_groups,
                "splits": splits,
            }
        if n_groups >= 2:
            n_splits = min(requested_splits, n_groups)
            splitter = GroupKFold(n_splits=n_splits)
            splits = list(
                splitter.split(np.zeros((n_samples, 1)), y, groups=groups)
            )
            return {
                "strategy": "GroupKFold",
                "n_splits": n_splits,
                "splits": splits,
            }

    counts = np.bincount(y, minlength=len(fusion.LABELS))
    counts = counts[counts > 0]
    min_count = int(counts.min()) if counts.size else 0
    n_splits = min(requested_splits, min_count, n_samples)
    if n_splits >= 2:
        splitter = StratifiedKFold(
            n_splits=n_splits, shuffle=True, random_state=seed
        )
        splits = list(splitter.split(np.zeros((n_samples, 1)), y))
        return {
            "strategy": "StratifiedKFold",
            "n_splits": n_splits,
            "splits": splits,
        }

    if n_samples < 2:
        raise ValueError("At least two samples are required for cross-validation")
    splitter = KFold(n_splits=2, shuffle=True, random_state=seed)
    splits = list(splitter.split(np.zeros((n_samples, 1)), y))
    return {"strategy": "KFold", "n_splits": 2, "splits": splits}


def _fit_predict(
    X_train: np.ndarray, y_train: np.ndarray, X_test: np.ndarray
) -> np.ndarray:
    model = fusion.fit_modality_model(X_train, y_train)
    return fusion.predict_proba(model, X_test)


def _fit_gbdt_predict(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_test: np.ndarray,
    config: ExperimentConfig,
) -> np.ndarray:
    """Early-fusion GBDT upper bound (W5 robustness model)."""
    observed = np.unique(y_train)
    if observed.size < 2:
        fallback = fusion.ConstantProbabilityModel(observed)
        return fusion.predict_proba(fallback, X_test)
    model = HistGradientBoostingClassifier(
        max_iter=int(config.gbdt_max_iter),
        learning_rate=float(config.gbdt_learning_rate),
        max_leaf_nodes=15,
        random_state=int(config.seed),
        early_stopping=False,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(X_train, y_train)
    return fusion.predict_proba(model, X_test)


def _fit_mlp_predict(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_test: np.ndarray,
    config: ExperimentConfig,
) -> np.ndarray:
    observed = np.unique(y_train)
    if observed.size < 2:
        fallback = fusion.ConstantProbabilityModel(observed)
        return fusion.predict_proba(fallback, X_test)

    model = Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(strategy="median", add_indicator=True),
            ),
            ("scaler", StandardScaler()),
            (
                "mlp",
                MLPClassifier(
                    hidden_layer_sizes=tuple(config.mlp_hidden_layer_sizes),
                    max_iter=config.mlp_max_iter,
                    random_state=config.seed,
                    alpha=1e-3,
                ),
            ),
        ]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        warnings.simplefilter("ignore", UserWarning)
        model.fit(X_train, y_train)
    return fusion.predict_proba(model, X_test)


def _inner_oof_matrix(
    X: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray | None,
    config: ExperimentConfig,
    *,
    mlp: bool = False,
    gbdt: bool = False,
) -> tuple[np.ndarray, str]:
    split_info = _make_splits(y, groups, config.inner_splits, config.seed)
    n_samples = len(y)
    probabilities = np.full((n_samples, fusion.N_CLASSES), np.nan, dtype=float)
    for train_index, valid_index in split_info["splits"]:
        if gbdt:
            probabilities[valid_index] = _fit_gbdt_predict(
                X[train_index], y[train_index], X[valid_index], config
            )
        elif mlp:
            probabilities[valid_index] = _fit_mlp_predict(
                X[train_index], y[train_index], X[valid_index], config
            )
        else:
            probabilities[valid_index] = _fit_predict(
                X[train_index], y[train_index], X[valid_index]
            )
    if np.isnan(probabilities).any():
        missing = np.isnan(probabilities[:, 0])
        if gbdt:
            probabilities[missing] = _fit_gbdt_predict(X, y, X[missing], config)
        elif mlp:
            probabilities[missing] = _fit_mlp_predict(X, y, X[missing], config)
        else:
            probabilities[missing] = _fit_predict(X, y, X[missing])
    return probabilities, split_info["strategy"]


def _inner_oof_all(
    Xv: np.ndarray,
    Xs: np.ndarray,
    Xe: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray | None,
    config: ExperimentConfig,
) -> dict[str, Any]:
    video, strategy = _inner_oof_matrix(Xv, y, groups, config)
    speech, _ = _inner_oof_matrix(Xs, y, groups, config)
    early, _ = _inner_oof_matrix(Xe, y, groups, config)
    mlp, _ = _inner_oof_matrix(Xe, y, groups, config, mlp=True)
    gbdt, _ = _inner_oof_matrix(Xe, y, groups, config, gbdt=True)
    return {
        "video": video,
        "speech": speech,
        "early": early,
        "mlp": mlp,
        "gbdt": gbdt,
        "strategy": strategy,
    }


def _scheme_weights(
    scheme: str,
    inner: Mapping[str, Any],
    y_train: np.ndarray,
    config: ExperimentConfig,
) -> tuple[float, float]:
    if scheme in FIXED_WEIGHTS:
        return FIXED_WEIGHTS[scheme]
    if scheme == "W3_auc":
        return fusion.weights_from_auc(
            inner["video"], inner["speech"], y_train
        )
    if scheme == "W4_learned":
        return fusion.choose_weights(
            inner["video"],
            inner["speech"],
            y_train,
            step=config.weight_step,
        )
    raise KeyError(f"Unknown fusion scheme: {scheme!r}")


# ---------------------------------------------------------------------------
# Rule baseline
# ---------------------------------------------------------------------------


def _rule_scores(
    df: pd.DataFrame, config: ExperimentConfig
) -> tuple[np.ndarray | None, np.ndarray | None, str | None]:
    video_components = {
        name: _component_array(df, aliases)
        for name, aliases in _RULE_VIDEO_ALIASES.items()
    }
    speech_components = {
        name: _component_array(df, aliases)
        for name, aliases in _RULE_SPEECH_ALIASES.items()
    }
    if all(value is None for value in video_components.values()) and all(
        value is None for value in speech_components.values()
    ):
        return None, None, (
            "Rule baseline skipped: no posture/stability/behavior or "
            "coverage/question/rate columns were found"
        )

    window = _component_array(df, _RULE_WINDOW_ALIASES)
    if window is None:
        window = np.full(len(df), float(config.rule_window_seconds))
    else:
        window = np.where(np.isfinite(window), window, float(config.rule_window_seconds))

    video_scores = np.full(len(df), np.nan, dtype=float)
    speech_scores = np.full(len(df), np.nan, dtype=float)
    for index in range(len(df)):
        video_value = rule_baseline.video_score(
            _component_at(video_components["posture_score"], index),
            _component_at(video_components["stability_score"], index),
            _component_at(
                video_components["positive_behavior_ratio"], index
            ),
        )
        if video_value is not None:
            video_scores[index] = video_value
        speech_value = rule_baseline.speech_score(
            _component_at(speech_components["coverage"], index),
            _component_at(speech_components["question_count"], index),
            _component_at(speech_components["speech_rate"], index),
            float(window[index]),
        )
        if speech_value is not None:
            speech_scores[index] = speech_value
    if not np.isfinite(video_scores).any() and not np.isfinite(speech_scores).any():
        return None, None, (
            "Rule baseline skipped: rule component columns exist but contain "
            "no usable (finite) values"
        )
    return video_scores, speech_scores, None


def _component_array(
    df: pd.DataFrame, aliases: Sequence[str]
) -> np.ndarray | None:
    for column in aliases:
        if column in df.columns:
            return pd.to_numeric(df[column], errors="coerce").to_numpy(dtype=float)
    return None


def _component_at(values: np.ndarray | None, index: int) -> float | None:
    if values is None:
        return None
    value = float(values[index])
    return value if math.isfinite(value) else None


def _fuse_rule_pair(video: np.ndarray, speech: np.ndarray) -> np.ndarray:
    fused = np.full(len(video), np.nan, dtype=float)
    for index in range(len(video)):
        video_value = float(video[index])
        speech_value = float(speech[index])
        value = rule_baseline.fuse(
            video_value if math.isfinite(video_value) else None,
            speech_value if math.isfinite(speech_value) else None,
        )
        if value is not None:
            fused[index] = value
    return fused


def _rule_scheme_fold(
    video: np.ndarray,
    speech: np.ndarray,
    train_index: np.ndarray,
    test_index: np.ndarray,
    y_train: np.ndarray,
    config: ExperimentConfig,
) -> dict[str, Any]:
    train_fused = _fuse_rule_pair(video[train_index], speech[train_index])
    test_fused = _fuse_rule_pair(video[test_index], speech[test_index])

    # -1 means "no rule score available"; never impute a majority class.
    prediction = np.full(len(test_index), -1, dtype=int)
    thresholds = (0.0, 100.0)
    n_unavailable = int(np.sum(~np.isfinite(test_fused)))

    usable_train = np.isfinite(train_fused)
    if usable_train.any():
        thresholds = fusion.learn_thresholds(
            train_fused[usable_train],
            y_train[usable_train],
            max_candidates=config.max_threshold_candidates,
        )
        usable_test = np.isfinite(test_fused)
        prediction[usable_test] = fusion.apply_thresholds(
            test_fused[usable_test], thresholds
        )

    return {
        "weights": (0.5, 0.5),
        "thresholds": thresholds,
        "prediction": prediction,
        "probabilities": None,
        "score": test_fused,
        "n_unavailable": n_unavailable,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _collect_scheme(
    fold_records: Sequence[Mapping[str, Any]],
    scheme: str,
    n_samples: int,
    *,
    probability_scheme: bool,
) -> dict[str, np.ndarray]:
    prediction = np.full(n_samples, -1, dtype=int)
    probabilities = np.full((n_samples, fusion.N_CLASSES), np.nan, dtype=float)
    scores = np.full(n_samples, np.nan, dtype=float)
    thresholds = np.full((n_samples, 2), np.nan, dtype=float)
    weights = np.full((n_samples, 2), np.nan, dtype=float)
    n_unavailable = 0

    for record in fold_records:
        scheme_record = record["schemes"].get(scheme)
        if scheme_record is None:
            continue
        test_index = record["test_index"]
        prediction[test_index] = scheme_record["prediction"]
        if probability_scheme and scheme_record["probabilities"] is not None:
            probabilities[test_index] = scheme_record["probabilities"]
        scores[test_index] = scheme_record["score"]
        thresholds[test_index] = scheme_record["thresholds"]
        weights[test_index] = scheme_record["weights"]
        n_unavailable += int(scheme_record.get("n_unavailable", 0))

    if np.any(prediction < 0):
        raise RuntimeError(
            f"Scheme {scheme!r} did not cover every sample; this indicates a "
            "split bug"
        )
    return {
        "prediction": prediction,
        "probabilities": probabilities,
        "scores": scores,
        "thresholds": thresholds,
        "weights": weights,
        "n_unavailable": n_unavailable,
    }


def _predictions_frame(
    df: pd.DataFrame,
    y: np.ndarray,
    groups: np.ndarray | None,
    fold_of_row: np.ndarray,
    schemes: Sequence[str],
    predictions: Mapping[str, np.ndarray],
    probabilities: Mapping[str, np.ndarray],
    scores: Mapping[str, np.ndarray],
    thresholds: Mapping[str, np.ndarray],
    weights: Mapping[str, np.ndarray],
) -> pd.DataFrame:
    clip_ids = (
        df["clip_id"].astype(str).to_numpy()
        if "clip_id" in df.columns
        else np.asarray([str(index) for index in range(len(df))])
    )
    rows: list[dict[str, Any]] = []
    for scheme in schemes:
        prediction = predictions[scheme]
        probability = probabilities[scheme]
        score = scores[scheme]
        threshold = thresholds[scheme]
        weight = weights[scheme]
        has_probability = bool(np.all(np.isfinite(probability)))
        for index in range(len(df)):
            rows.append(
                {
                    "scheme": scheme,
                    "row_index": index,
                    "clip_id": clip_ids[index],
                    "label": int(y[index]),
                    "group": groups[index] if groups is not None else "",
                    "fold": int(fold_of_row[index]),
                    "prediction": int(prediction[index]),
                    "score": float(score[index]),
                    "p0": float(probability[index, 0]) if has_probability else np.nan,
                    "p1": float(probability[index, 1]) if has_probability else np.nan,
                    "p2": float(probability[index, 2]) if has_probability else np.nan,
                    "threshold_1": float(threshold[index, 0]),
                    "threshold_2": float(threshold[index, 1]),
                    "weight_video": float(weight[index, 0]),
                    "weight_speech": float(weight[index, 1]),
                }
            )
    return pd.DataFrame(rows)


def _weights_by_fold_frame(
    fold_records: Sequence[Mapping[str, Any]], schemes: Sequence[str]
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for record in fold_records:
        for scheme in schemes:
            scheme_record = record["schemes"].get(scheme)
            if scheme_record is None:
                continue
            rows.append(
                {
                    "fold": int(record["fold"]),
                    "scheme": scheme,
                    "w_video": float(scheme_record["weights"][0]),
                    "w_speech": float(scheme_record["weights"][1]),
                    "threshold_1": float(scheme_record["thresholds"][0]),
                    "threshold_2": float(scheme_record["thresholds"][1]),
                    "train_size": int(len(record["train_index"])),
                    "test_size": int(len(record["test_index"])),
                    "inner_strategy": record["inner_strategy"],
                }
            )
    return pd.DataFrame(rows)


def _weight_stability(
    fold_records: Sequence[Mapping[str, Any]], schemes: Sequence[str]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scheme in schemes:
        if scheme not in VARIABLE_WEIGHT_SCHEMES:
            continue
        values = [
            float(record["schemes"][scheme]["weights"][0])
            for record in fold_records
            if scheme in record["schemes"]
        ]
        values = [value for value in values if math.isfinite(value)]
        if not values:
            continue
        array = np.asarray(values, dtype=float)
        mean = float(array.mean())
        # Protocol 6.5 requires the sample standard deviation (ddof=1).
        std = float(array.std(ddof=1)) if array.size > 1 else 0.0
        near_zero = abs(mean) < 1e-6
        coefficient = (
            float("nan") if near_zero or mean <= 0 else float(std / mean)
        )
        unstable = math.isfinite(coefficient) and coefficient > 0.2
        if near_zero:
            conclusion = (
                "near-zero mean: report std/range; CV is undefined"
            )
        elif unstable:
            conclusion = "unstable (CV > 0.2): prior weights are more reliable"
        else:
            conclusion = "stable (CV <= 0.2)"
        result[scheme] = {
            "fold_weights": values,
            "mean": mean,
            "std": std,
            "range": [float(array.min()), float(array.max())],
            "cv": coefficient,
            "conclusion": conclusion,
        }
    return result


def _weight_sensitivity(
    fold_records: Sequence[Mapping[str, Any]],
    y: np.ndarray,
    config: ExperimentConfig,
) -> pd.DataFrame:
    if not fold_records:
        return pd.DataFrame()
    step = float(config.weight_step)
    if not math.isfinite(step) or step <= 0 or step > 1:
        raise ValueError("weight_step must be in (0, 1]")
    count = int(round(1.0 / step))
    grid = np.linspace(0.0, 1.0, count + 1)
    n_samples = len(y)
    rows: list[dict[str, Any]] = []

    for weight in grid:
        prediction = np.full(n_samples, -1, dtype=int)
        probabilities = np.full((n_samples, fusion.N_CLASSES), np.nan)
        for record in fold_records:
            inner = record["inner"]
            thresholds = fusion.learn_thresholds(
                fusion.ordinal_score(
                    fusion.fuse_proba(inner["video"], inner["speech"], weight)
                ),
                record["y_train"],
                max_candidates=config.max_threshold_candidates,
            )
            fused_test = fusion.fuse_proba(
                record["pv_test"], record["ps_test"], weight
            )
            prediction[record["test_index"]] = fusion.apply_thresholds(
                fusion.ordinal_score(fused_test), thresholds
            )
            probabilities[record["test_index"]] = fused_test

        if np.any(prediction < 0):
            raise RuntimeError(
                "Weight-sensitivity folds did not cover every sample"
            )
        metrics = classification_metrics(
            y,
            prediction,
            y_score=probabilities,
            ordinal_scores=fusion.ordinal_score(probabilities),
        )
        rows.append(
            {
                "w_video": float(weight),
                "w_speech": float(1.0 - weight),
                **metrics,
                "n": n_samples,
            }
        )

    frame = pd.DataFrame(rows)
    if not frame.empty:
        best_index = frame["macro_f1"].idxmax()
        frame.attrs["best_w_video"] = float(frame.loc[best_index, "w_video"])
        frame.attrs["best_macro_f1"] = float(frame.loc[best_index, "macro_f1"])
    return frame


def _missing_modality_simulation(
    fold_records: Sequence[Mapping[str, Any]],
    y: np.ndarray,
    config: ExperimentConfig,
) -> pd.DataFrame:
    """Simulate missing video/speech under two protocol treatments.

    * ``neutral_fill`` (primary, matches the online FusionService): the
      missing modality is replaced by a uniform class distribution, i.e. the
      probability-space analogue of a neutral 50 score;
    * ``available_only`` (secondary): the fused distribution is renormalised
      onto the available modality.

    Both arms mask only the test fold; models and thresholds are not refit.
    """
    if not fold_records:
        return pd.DataFrame()
    schemes = [
        scheme
        for scheme in ("W1_equal", "W4_learned")
        if all(scheme in record["schemes"] for record in fold_records)
    ]
    n_samples = len(y)
    rows: list[dict[str, Any]] = []
    neutral = np.full(fusion.N_CLASSES, 1.0 / fusion.N_CLASSES, dtype=float)

    for scheme in schemes:
        for rate in config.missing_modality_rates:
            rate_value = float(rate)
            if not (0.0 <= rate_value <= 1.0):
                raise ValueError(
                    f"missing_modality_rates must lie in [0, 1]: {rate}"
                )
            for repeat in range(int(config.missing_modality_repeats)):
                for treatment in ("neutral_fill", "available_only"):
                    prediction = np.full(n_samples, -1, dtype=int)
                    probabilities = np.full(
                        (n_samples, fusion.N_CLASSES), np.nan
                    )
                    masked_total = 0
                    video_masked_total = 0
                    speech_masked_total = 0
                    for record in fold_records:
                        scheme_record = record["schemes"][scheme]
                        test_index = record["test_index"]
                        pv = record["pv_test"]
                        ps = record["ps_test"]
                        weight_video, weight_speech = scheme_record["weights"]

                        rng = np.random.default_rng(
                            config.seed
                            + 104729 * record["fold"]
                            + 7919 * repeat
                        )
                        masked = rng.random(len(test_index)) < rate_value
                        video_side = rng.random(len(test_index)) < 0.5
                        video_mask = masked & video_side
                        speech_mask = masked & ~video_side

                        if treatment == "neutral_fill":
                            pv_used = pv.copy()
                            ps_used = ps.copy()
                            pv_used[video_mask] = neutral
                            ps_used[speech_mask] = neutral
                            fused = (
                                weight_video * pv_used
                                + weight_speech * ps_used
                            )
                        else:
                            fused = weight_video * pv + weight_speech * ps
                            fused[video_mask] = ps[video_mask]
                            fused[speech_mask] = pv[speech_mask]

                        prediction[test_index] = fusion.apply_thresholds(
                            fusion.ordinal_score(fused),
                            scheme_record["thresholds"],
                        )
                        probabilities[test_index] = fused
                        masked_total += int(masked.sum())
                        video_masked_total += int(video_mask.sum())
                        speech_masked_total += int(speech_mask.sum())

                    if np.any(prediction < 0):
                        raise RuntimeError(
                            "Missing-modality folds did not cover every sample"
                        )
                    metrics = classification_metrics(
                        y,
                        prediction,
                        y_score=probabilities,
                        ordinal_scores=fusion.ordinal_score(probabilities),
                    )
                    rows.append(
                        {
                            "scheme": scheme,
                            "treatment": treatment,
                            "rate": rate_value,
                            "repeat": repeat,
                            "accuracy": metrics["accuracy"],
                            "macro_f1": metrics["macro_f1"],
                            "ordinal_mae": metrics["ordinal_mae"],
                            "log_loss": metrics.get("log_loss", float("nan")),
                            "n": n_samples,
                            "n_masked": masked_total,
                            "n_video_masked": video_masked_total,
                            "n_speech_masked": speech_masked_total,
                        }
                    )
    return pd.DataFrame(rows)


def _ablation_table(
    df: pd.DataFrame,
    feature_sets: Mapping[str, Any],
    y: np.ndarray,
    outer: Mapping[str, Any],
    groups: np.ndarray | None,
    config: ExperimentConfig,
) -> pd.DataFrame:
    if not config.run_ablation:
        return pd.DataFrame()

    groups_to_test = _ablation_groups(feature_sets)
    rows: list[dict[str, Any]] = []
    for name, modality, columns in groups_to_test:
        if not columns:
            continue
        matrix, kept, _ = _numeric_matrix(df, columns, modality)
        if not kept:
            continue
        prediction = np.full(len(y), -1, dtype=int)
        probabilities = np.full((len(y), fusion.N_CLASSES), np.nan)
        for train_index, test_index in outer["splits"]:
            inner_groups = groups[train_index] if groups is not None else None
            inner_proba, _ = _inner_oof_matrix(
                matrix[train_index], y[train_index], inner_groups, config
            )
            thresholds = fusion.learn_thresholds(
                fusion.ordinal_score(inner_proba),
                y[train_index],
                max_candidates=config.max_threshold_candidates,
            )
            test_proba = _fit_predict(
                matrix[train_index], y[train_index], matrix[test_index]
            )
            prediction[test_index] = fusion.apply_thresholds(
                fusion.ordinal_score(test_proba), thresholds
            )
            probabilities[test_index] = test_proba

        if np.any(prediction < 0):
            raise RuntimeError(
                f"Ablation group {name!r} did not cover every sample"
            )
        metrics = classification_metrics(
            y,
            prediction,
            y_score=probabilities,
            ordinal_scores=fusion.ordinal_score(probabilities),
        )
        rows.append(
            {
                "group": name,
                "modality": modality,
                "n_features": len(kept),
                "features": ",".join(kept),
                **metrics,
                "n": len(y),
            }
        )
    return pd.DataFrame(rows)


def _ablation_groups(
    feature_sets: Mapping[str, Any],
) -> list[tuple[str, str, list[str]]]:
    video_columns = list(feature_sets["video"]["columns"])
    speech_columns = list(feature_sets["speech"]["columns"])

    def matching(columns: Sequence[str], pattern: str) -> list[str]:
        return [
            column
            for column in columns
            if re.search(pattern, column, flags=re.IGNORECASE)
        ]

    return [
        (
            "video_pose_only",
            "video",
            matching(video_columns, r"posture|stability|pitch|yaw|roll"),
        ),
        (
            "video_detection_only",
            "video",
            matching(
                video_columns,
                r"person|hand|reading|writing|detect|confidence|upright|behavior",
            ),
        ),
        (
            "speech_coverage_only",
            "speech",
            matching(speech_columns, r"coverage|pause"),
        ),
        (
            "speech_question_only",
            "speech",
            matching(speech_columns, r"question"),
        ),
        ("all_video", "video", video_columns),
        ("all_speech", "speech", speech_columns),
    ]


# ---------------------------------------------------------------------------
# Robustness and holdout analyses
# ---------------------------------------------------------------------------


def _aggregate_to_windows(
    df: pd.DataFrame,
    window_seconds: float = 60.0,
    clip_seconds: float = 5.0,
) -> pd.DataFrame | None:
    """Aggregate consecutive 5 s clips into fixed windows.

    Requires ``lesson_id`` and ``clip_order``.  Only complete blocks are
    emitted; an incomplete tail is dropped and reported via ``df.attrs`` so it
    is not silently treated as a full window.  Count-like columns are summed;
    score/rate/coverage columns are averaged; the label is the block majority.
    """
    if not {"lesson_id", "clip_order", "label"}.issubset(df.columns):
        return None
    block_size = int(round(float(window_seconds) / float(clip_seconds)))
    if block_size < 2:
        return None
    rows: list[dict[str, Any]] = []
    dropped = 0
    skip_columns = {
        "clip_id",
        "label",
        "label_name",
        "lesson_id",
        "clip_order",
        "window_seconds",
        "video_path",
        "audio_path",
        "split",
    }
    sum_hints = (
        "count",
        "seconds",
        "char_count",
        "total_detections",
        "sampled_frames",
        "frame_count",
    )
    for lesson, group in df.groupby("lesson_id", dropna=True):
        order = pd.to_numeric(group["clip_order"], errors="coerce")
        if order.isna().any():
            continue
        ordered = group.assign(_order=order).sort_values("_order")
        for block_index, start in enumerate(
            range(0, len(ordered), block_size)
        ):
            block = ordered.iloc[start:start + block_size]
            if len(block) < block_size:
                dropped += len(block)
                break
            labels = pd.to_numeric(block["label"], errors="coerce").dropna()
            if labels.empty:
                continue
            row: dict[str, Any] = {
                "clip_id": f"{lesson}-w{block_index}",
                "lesson_id": lesson,
                "clip_order": block_index,
                "window_seconds": float(window_seconds),
                "label": int(labels.mode().iloc[0]),
            }
            for column in block.columns:
                if column in skip_columns or column.startswith("_"):
                    continue
                values = pd.to_numeric(block[column], errors="coerce")
                if not values.notna().any():
                    continue
                if any(hint in column for hint in sum_hints):
                    row[column] = float(values.sum())
                else:
                    row[column] = float(values.mean())
            rows.append(row)
    if not rows:
        return None
    aggregated = pd.DataFrame(rows)
    aggregated.attrs["dropped_incomplete_clips"] = dropped
    return aggregated


def _window_sensitivity(
    df: pd.DataFrame, config: ExperimentConfig
) -> pd.DataFrame:
    """Run CV at 5 s and 60 s, aggregating clips when order metadata exists.

    * If the table already has two or more positive ``window_seconds`` values,
      each subset is evaluated directly.
    * Otherwise, if ``lesson_id`` and ``clip_order`` are present, 60 s windows
      are built from consecutive 5 s clips (12 clips/window).
    * Without either, the analysis is explicitly ``SKIPPED``; no random
      concatenation is used.
    """
    frames: list[tuple[float, pd.DataFrame]] = []
    if "window_seconds" in df.columns:
        numeric = pd.to_numeric(df["window_seconds"], errors="coerce")
        values = sorted(
            float(value) for value in np.unique(numeric.dropna()) if value > 0
        )
        frames = [
            (window, df[numeric == window].reset_index(drop=True))
            for window in values
        ]
    if len(frames) < 2 and {"lesson_id", "clip_order"}.issubset(df.columns):
        five = df.copy()
        five["window_seconds"] = 5.0
        sixty = _aggregate_to_windows(df, 60.0, 5.0)
        frames = [(5.0, five)]
        if sixty is not None:
            frames.append((60.0, sixty))
    if len(frames) < 2:
        return pd.DataFrame(
            [
                {
                    "status": "SKIPPED",
                    "reason": (
                        "need two window lengths or lesson_id+clip_order "
                        "for 60s aggregation"
                    ),
                }
            ]
        )

    rows: list[dict[str, Any]] = []
    for window, subset in frames:
        if len(subset) < 12 or subset[config.label_col].nunique() < 2:
            rows.append(
                {
                    "window_seconds": window,
                    "status": "SKIPPED",
                    "reason": "too few rows or fewer than two classes",
                    "n": len(subset),
                }
            )
            continue
        sub_config = replace(
            config,
            run_missing_modality=False,
            run_ablation=False,
            run_weight_sensitivity=False,
            run_statistical_tests=False,
            run_window_sensitivity=False,
            run_rule_threshold_sensitivity=False,
            loso=False,
            split_col=None,
            holdout_split=None,
        )
        try:
            result = run_cv(subset, sub_config)
        except Exception as exc:  # noqa: BLE001 - record and continue
            rows.append(
                {
                    "window_seconds": window,
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                    "n": len(subset),
                }
            )
            continue
        for record in result["metrics"].to_dict("records"):
            rows.append(
                {
                    "window_seconds": window,
                    "status": "OK",
                    "dropped_incomplete_clips": int(
                        subset.attrs.get("dropped_incomplete_clips", 0)
                    ),
                    **record,
                }
            )
    return pd.DataFrame(rows)


def _holdout_evaluation(
    df: pd.DataFrame,
    feature_sets: Mapping[str, Any],
    y: np.ndarray,
    config: ExperimentConfig,
    groups: np.ndarray | None = None,
) -> pd.DataFrame:
    """Evaluate on an official train/valid/test split when one is available.

    Rows whose ``split_col`` equals ``holdout_split`` form the test set; all
    other rows form the training set.  Weight and threshold selection use
    inner folds of the training set only.
    """
    if not config.split_col or not config.holdout_split:
        return pd.DataFrame()
    if config.split_col not in df.columns:
        return pd.DataFrame(
            [
                {
                    "status": "SKIPPED",
                    "reason": f"split column {config.split_col!r} not present",
                }
            ]
        )
    split_values = df[config.split_col].astype(str).str.strip().str.lower()
    target = str(config.holdout_split).strip().lower()
    test_mask = (split_values == target).to_numpy()
    train_mask = ~test_mask
    if not test_mask.any() or not train_mask.any():
        return pd.DataFrame(
            [
                {
                    "status": "SKIPPED",
                    "reason": (
                        f"holdout split {config.holdout_split!r} has "
                        f"{int(test_mask.sum())} test and "
                        f"{int(train_mask.sum())} training row(s)"
                    ),
                }
            ]
        )

    train_index = np.flatnonzero(train_mask)
    test_index = np.flatnonzero(test_mask)
    y_train = y[train_index]
    y_test = y[test_index]
    Xv = feature_sets["video"]["matrix"]
    Xs = feature_sets["speech"]["matrix"]
    Xe = np.hstack([Xv, Xs])

    inner = _inner_oof_all(
        Xv[train_index], Xs[train_index], Xe[train_index], y_train,
        groups[train_index] if groups is not None else None,
        config,
    )
    pv_test = _fit_predict(Xv[train_index], y_train, Xv[test_index])
    ps_test = _fit_predict(Xs[train_index], y_train, Xs[test_index])
    pe_test = _fit_predict(Xe[train_index], y_train, Xe[test_index])
    pm_test = _fit_mlp_predict(Xe[train_index], y_train, Xe[test_index], config)
    pg_test = _fit_gbdt_predict(Xe[train_index], y_train, Xe[test_index], config)

    rows: list[dict[str, Any]] = []
    for scheme in PROBABILITY_SCHEMES:
        if scheme == "W5_early_logistic":
            inner_fused, test_fused = inner["early"], pe_test
            weights = (float("nan"), float("nan"))
        elif scheme == "W5_mlp":
            inner_fused, test_fused = inner["mlp"], pm_test
            weights = (float("nan"), float("nan"))
        elif scheme == "W5_early_gbdt":
            inner_fused, test_fused = inner["gbdt"], pg_test
            weights = (float("nan"), float("nan"))
        else:
            weights = _scheme_weights(scheme, inner, y_train, config)
            inner_fused = fusion.fuse_proba(
                inner["video"], inner["speech"], weights[0]
            )
            test_fused = fusion.fuse_proba(pv_test, ps_test, weights[0])
        thresholds = fusion.learn_thresholds(
            fusion.ordinal_score(inner_fused),
            y_train,
            max_candidates=config.max_threshold_candidates,
        )
        prediction = fusion.apply_thresholds(
            fusion.ordinal_score(test_fused), thresholds
        )
        metrics = classification_metrics(
            y_test,
            prediction,
            y_score=test_fused,
            ordinal_scores=fusion.ordinal_score(test_fused),
        )
        rows.append(
            {
                "scheme": scheme,
                "status": "OK",
                "n_train": int(len(train_index)),
                "n_test": int(len(test_index)),
                **metrics,
            }
        )

    rule_video, rule_speech, _ = _rule_scores(df, config)
    rule_available = (
        (rule_video is not None and np.isfinite(rule_video).any())
        or (rule_speech is not None and np.isfinite(rule_speech).any())
    )
    if rule_available:
        video = rule_video if rule_video is not None else np.full(len(df), np.nan)
        speech = rule_speech if rule_speech is not None else np.full(len(df), np.nan)
        record = _rule_scheme_fold(
            video, speech, train_index, test_index, y_train, config
        )
        valid = record["prediction"] >= 0
        if not valid.any():
            rows.append(
                {
                    "scheme": "R0_rule_equal",
                    "status": "SKIPPED",
                    "reason": "no usable rule prediction on the holdout split",
                    "n_train": int(len(train_index)),
                    "n_test": int(len(test_index)),
                    "n_unavailable": int(len(test_index)),
                }
            )
        else:
            metrics = classification_metrics(
                y_test[valid],
                record["prediction"][valid],
                ordinal_scores=record["score"][valid],
            )
            rows.append(
                {
                    "scheme": "R0_rule_equal",
                    "status": "OK",
                    "n_train": int(len(train_index)),
                    "n_test": int(valid.sum()),
                    "n_unavailable": int((~valid).sum()),
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


def _thresholded_speech_score(
    coverage: float | None,
    question_count: float | None,
    speech_rate: float | None,
    window_seconds: float,
    *,
    question_saturation: float,
    rate_min: float,
    rate_max: float,
) -> float | None:
    """Speech rule score with alternate thresholds (sensitivity analysis)."""
    components: list[tuple[float, float]] = []
    if coverage is not None and math.isfinite(coverage):
        components.append((0.5, max(0.0, min(1.0, coverage))))
    if (
        question_count is not None
        and math.isfinite(question_count)
        and window_seconds > 0
        and question_saturation > 0
    ):
        questions_per_minute = max(0.0, question_count) / (window_seconds / 60.0)
        components.append(
            (0.3, min(1.0, questions_per_minute / question_saturation))
        )
    if speech_rate is not None and math.isfinite(speech_rate):
        rate = max(0.0, speech_rate)
        if rate < rate_min:
            value = max(0.0, 1.0 - (rate_min - rate) / rate_min)
        elif rate > rate_max:
            value = max(0.0, 1.0 - (rate - rate_max) / rate_max)
        else:
            value = 1.0
        components.append((0.2, value))
    if not components:
        return None
    weight_sum = sum(weight for weight, _ in components)
    return 100.0 * sum(weight * value for weight, value in components) / weight_sum


def _rule_threshold_sensitivity(
    df: pd.DataFrame,
    y: np.ndarray,
    outer: Mapping[str, Any],
    groups: np.ndarray | None,
    config: ExperimentConfig,
) -> pd.DataFrame:
    """Vary question saturation and speech-rate bands for the rule baseline."""
    video_components = {
        name: _component_array(df, aliases)
        for name, aliases in _RULE_VIDEO_ALIASES.items()
    }
    speech_components = {
        name: _component_array(df, aliases)
        for name, aliases in _RULE_SPEECH_ALIASES.items()
    }
    if all(value is None for value in video_components.values()) and all(
        value is None for value in speech_components.values()
    ):
        return pd.DataFrame(
            [{"status": "SKIPPED", "reason": "no rule components found"}]
        )
    if all(
        value is None or not np.isfinite(value).any()
        for value in speech_components.values()
    ) and all(
        value is None or not np.isfinite(value).any()
        for value in video_components.values()
    ):
        return pd.DataFrame(
            [
                {
                    "status": "SKIPPED",
                    "reason": "rule component columns exist but contain no finite values",
                }
            ]
        )

    video_scores = np.full(len(df), np.nan, dtype=float)
    for index in range(len(df)):
        value = rule_baseline.video_score(
            _component_at(video_components["posture_score"], index),
            _component_at(video_components["stability_score"], index),
            _component_at(video_components["positive_behavior_ratio"], index),
        )
        if value is not None:
            video_scores[index] = value

    window = _component_array(df, _RULE_WINDOW_ALIASES)
    if window is None:
        window = np.full(len(df), float(config.rule_window_seconds))
    else:
        window = np.where(np.isfinite(window), window, float(config.rule_window_seconds))

    rows: list[dict[str, Any]] = []
    for saturation in (0.5, 1.0, 2.0):
        for rate_min, rate_max in ((120.0, 270.0), (150.0, 300.0), (180.0, 360.0)):
            speech_scores = np.full(len(df), np.nan, dtype=float)
            for index in range(len(df)):
                value = _thresholded_speech_score(
                    _component_at(speech_components["coverage"], index),
                    _component_at(speech_components["question_count"], index),
                    _component_at(speech_components["speech_rate"], index),
                    float(window[index]),
                    question_saturation=saturation,
                    rate_min=rate_min,
                    rate_max=rate_max,
                )
                if value is not None:
                    speech_scores[index] = value

            if (
                not np.isfinite(video_scores).any()
                and not np.isfinite(speech_scores).any()
            ):
                rows.append(
                    {
                        "question_saturation": saturation,
                        "rate_min": rate_min,
                        "rate_max": rate_max,
                        "status": "SKIPPED",
                        "reason": "no finite rule score",
                    }
                )
                continue

            prediction = np.full(len(y), -1, dtype=int)
            scores = np.full(len(y), np.nan, dtype=float)
            for train_index, test_index in outer["splits"]:
                test_array = np.asarray(test_index)
                record = _rule_scheme_fold(
                    video_scores,
                    speech_scores,
                    np.asarray(train_index),
                    test_array,
                    y[np.asarray(train_index)],
                    config,
                )
                prediction[test_array] = record["prediction"]
                scores[test_array] = record["score"]
            valid = prediction >= 0
            if not valid.any():
                rows.append(
                    {
                        "question_saturation": saturation,
                        "rate_min": rate_min,
                        "rate_max": rate_max,
                        "status": "SKIPPED",
                        "reason": "no usable out-of-fold rule prediction",
                    }
                )
                continue
            metrics = classification_metrics(
                y[valid],
                prediction[valid],
                ordinal_scores=scores[valid],
            )
            rows.append(
                {
                    "question_saturation": saturation,
                    "rate_min": rate_min,
                    "rate_max": rate_max,
                    "status": "OK",
                    "n": int(valid.sum()),
                    "n_unavailable": int((~valid).sum()),
                    **metrics,
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Summary construction
# ---------------------------------------------------------------------------


def _build_summary(
    config: ExperimentConfig,
    dataset: Mapping[str, Any],
    cv: Mapping[str, Any],
    metrics: pd.DataFrame,
    bootstrap: Mapping[str, Any],
    weight_stability: Mapping[str, Any],
    weight_sensitivity: pd.DataFrame,
    missing_modality: pd.DataFrame,
    ablation: pd.DataFrame,
    fold_records: Sequence[Mapping[str, Any]],
    warnings_list: Sequence[str],
    *,
    per_fold: pd.DataFrame | None = None,
    confusion_matrix: pd.DataFrame | None = None,
    per_class_metrics: pd.DataFrame | None = None,
    statistical_tests: pd.DataFrame | None = None,
    reference_scheme: str | None = None,
    window_sensitivity: pd.DataFrame | None = None,
    holdout_metrics: pd.DataFrame | None = None,
    rule_threshold_sensitivity: pd.DataFrame | None = None,
) -> dict[str, Any]:
    scheme_rows = []
    for record in metrics.to_dict("records"):
        scheme_rows.append(_jsonable(record))

    sensitivity_summary: dict[str, Any] = {"rows": []}
    if not weight_sensitivity.empty:
        sensitivity_summary = {
            "best_w_video": float(
                weight_sensitivity.attrs.get("best_w_video", float("nan"))
            ),
            "best_macro_f1": float(
                weight_sensitivity.attrs.get("best_macro_f1", float("nan"))
            ),
            "rows": _jsonable(weight_sensitivity.to_dict("records")),
        }

    missing_summary: list[dict[str, Any]] = []
    if not missing_modality.empty:
        missing_summary = [
            _jsonable(record)
            for record in analysis.missing_modality_summary(
                missing_modality, metrics
            )
        ]

    weights_by_fold = _weights_by_fold_frame(
        fold_records, [row["scheme"] for row in scheme_rows]
    ).to_dict("records")

    summary_warnings = list(warnings_list)
    if not weight_sensitivity.empty:
        summary_warnings.append(
            "weight_sensitivity best_w_video is descriptive (out-of-fold "
            "optimum for plotting), not a learned weight; use W4 for the "
            "protocol's learned weights."
        )

    return {
        "dataset": _jsonable(dict(dataset)),
        "cv": _jsonable(dict(cv)),
        "config": _jsonable(_config_dict(config)),
        "rule_protocol": "v1",
        "schemes": scheme_rows,
        "bootstrap": _jsonable(dict(bootstrap)),
        "weights_by_fold": _jsonable(weights_by_fold),
        "weight_stability": _jsonable(dict(weight_stability)),
        "weight_sensitivity": sensitivity_summary,
        "missing_modality": missing_summary,
        "ablation": _jsonable(ablation.to_dict("records")),
        "per_fold_metrics": _jsonable(
            (per_fold if per_fold is not None else pd.DataFrame()).to_dict("records")
        ),
        "confusion_matrix": _jsonable(
            (confusion_matrix if confusion_matrix is not None else pd.DataFrame()).to_dict(
                "records"
            )
        ),
        "per_class_metrics": _jsonable(
            (
                per_class_metrics if per_class_metrics is not None else pd.DataFrame()
            ).to_dict("records")
        ),
        "statistical_tests": _jsonable(
            (
                statistical_tests if statistical_tests is not None else pd.DataFrame()
            ).to_dict("records")
        ),
        "reference_scheme": reference_scheme,
        "window_sensitivity": _jsonable(
            (
                window_sensitivity if window_sensitivity is not None else pd.DataFrame()
            ).to_dict("records")
        ),
        "holdout_metrics": _jsonable(
            (holdout_metrics if holdout_metrics is not None else pd.DataFrame()).to_dict(
                "records"
            )
        ),
        "rule_threshold_sensitivity": _jsonable(
            (
                rule_threshold_sensitivity
                if rule_threshold_sensitivity is not None
                else pd.DataFrame()
            ).to_dict("records")
        ),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
        },
        "warnings": summary_warnings,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return [_jsonable(item) for item in value.tolist()]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is pd.NA:
        return None
    return value


def _fmt(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{number:.4f}" if math.isfinite(number) else "n/a"


__all__ = [
    "ExperimentConfig",
    "PROBABILITY_SCHEMES",
    "SCHEME_ORDER",
    "render_summary_markdown",
    "run_cv",
]
