"""Tests for the late-fusion experiment engine (spec section 6).

All fixtures are synthetic three-class data; no public dataset is required.
The tests cover the fixed model pipeline, one-class fallbacks, probability
fusion, weight and threshold grid searches, cross-validation strategy
selection, nested selection (no outer-test leakage), sensitivity, missing
modality, ablation and the CLI output contract.
"""
from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")
pytest.importorskip("sklearn")
pytest.importorskip("scipy")

from sklearn.model_selection import GroupKFold

from public_data import fusion
from public_data.experiment import (
    ExperimentConfig,
    _aggregate_to_windows,
    render_summary_markdown,
    run_cv,
)
from public_data.learn_fusion_weights import main as cli_main

#: 测试临时目录必须落在项目内（系统 TEMP 在 C 盘，本项目禁止写那里）。
#: 这些夹具只写几 KB 的 CSV/JSON，但"小"不是理由——约束是位置不是大小。
_TMP_ROOT = Path(__file__).resolve().parents[3] / ".cache" / "tmp" / "public-data-tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)





# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _three_class_labels(per_class: int = 20) -> np.ndarray:
    return np.repeat([0, 1, 2], per_class)


def _one_hot(labels: np.ndarray) -> np.ndarray:
    return np.eye(3)[labels]


def _synthetic_frame(
    per_class: int = 20,
    n_groups: int = 5,
    seed: int = 13,
    rule_columns: bool = False,
) -> pd.DataFrame:
    """Three-class frame with an informative video signal and noise speech."""
    rng = np.random.default_rng(seed)
    labels = _three_class_labels(per_class)
    n_samples = labels.size
    if n_samples % n_groups:
        raise ValueError("per_class * 3 must be divisible by n_groups")
    video_signal = labels + rng.normal(0.0, 0.55, n_samples)
    speech_noise = rng.normal(0.0, 1.0, n_samples)

    data = {
        "clip_id": [f"clip{index:04d}" for index in range(n_samples)],
        "label": labels,
        "lesson_id": np.repeat(np.arange(n_groups), n_samples // n_groups),
    }
    for index in range(3):
        data[f"video_f{index}"] = video_signal + rng.normal(0.0, 0.25, n_samples)
        data[f"speech_f{index}"] = speech_noise + rng.normal(0.0, 0.25, n_samples)
    frame = pd.DataFrame(data)

    if rule_columns:
        frame["video_posture_score"] = np.clip(
            video_signal / 2.0, 0.0, 1.0
        )
        frame["video_stability_score"] = np.clip(
            video_signal / 2.0 + rng.normal(0.0, 0.1, n_samples), 0.0, 1.0
        )
        frame["video_positive_behavior_ratio"] = np.clip(
            video_signal / 2.0 + rng.normal(0.0, 0.1, n_samples), 0.0, 1.0
        )
        frame["speech_coverage"] = np.clip(
            speech_noise / 3.0 + 0.5, 0.0, 1.0
        )
        frame["speech_question_count"] = np.maximum(
            0, np.round(labels + rng.normal(0.0, 1.0, n_samples))
        )
        frame["speech_rate"] = 220.0 + rng.normal(0.0, 20.0, n_samples)
    return frame


def _fast_config(**overrides) -> ExperimentConfig:
    values = {
        "n_boot": 0,
        "mlp_max_iter": 300,
        "missing_modality_repeats": 1,
        "run_ablation": False,
        "run_missing_modality": False,
        "run_weight_sensitivity": False,
        "run_statistical_tests": False,
        "run_window_sensitivity": False,
        "run_rule_threshold_sensitivity": False,
    }
    values.update(overrides)
    return ExperimentConfig(**values)


# ---------------------------------------------------------------------------
# Modality models and probability utilities
# ---------------------------------------------------------------------------


def test_fit_modality_model_pipeline_matches_protocol() -> None:
    rng = np.random.default_rng(0)
    labels = _three_class_labels(30)
    features = np.column_stack(
        [
            3.0 * labels + rng.normal(0.0, 0.4, labels.size),
            rng.normal(size=labels.size),
        ]
    )
    features[0, 0] = np.nan

    model = fusion.fit_modality_model(features, labels)

    steps = dict(model.named_steps)
    assert set(steps) == {"imputer", "scaler", "classifier"}
    assert steps["imputer"].strategy == "median"
    assert steps["imputer"].add_indicator is True
    assert steps["classifier"].max_iter == 2000
    assert steps["classifier"].class_weight == "balanced"
    assert steps["classifier"].random_state == 42

    probabilities = fusion.predict_proba(model, features)
    assert probabilities.shape == (labels.size, 3)
    np.testing.assert_allclose(probabilities.sum(axis=1), 1.0)
    assert np.mean(np.argmax(probabilities, axis=1) == labels) >= 0.9


def test_predict_proba_expands_missing_classes_to_three_columns() -> None:
    rng = np.random.default_rng(1)
    labels = _three_class_labels(20)
    features = np.column_stack(
        [labels + rng.normal(0.0, 0.4, labels.size), rng.normal(size=labels.size)]
    )

    two_class = labels < 2
    model = fusion.fit_modality_model(features[two_class], labels[two_class])
    probabilities = fusion.predict_proba(model, features)

    assert probabilities.shape == (labels.size, 3)
    assert np.all(probabilities[:, 2] == 0.0)
    np.testing.assert_allclose(probabilities.sum(axis=1), 1.0)

    constant = fusion.ConstantProbabilityModel([2])
    constant_probabilities = fusion.predict_proba(constant, features)
    assert constant_probabilities.shape == (labels.size, 3)
    np.testing.assert_allclose(constant_probabilities[:, 2], 1.0)


def test_fit_modality_model_one_class_fallback_is_constant() -> None:
    features = np.zeros((12, 3))
    labels = np.zeros(12, dtype=int)

    model = fusion.fit_modality_model(features, labels)

    assert isinstance(model, fusion.ConstantProbabilityModel)
    probabilities = fusion.predict_proba(model, features)
    assert probabilities.shape == (12, 3)
    np.testing.assert_allclose(probabilities[:, 0], 1.0)


def test_predict_proba_rejects_bad_shapes() -> None:
    model = fusion.ConstantProbabilityModel([0, 1, 2])
    with pytest.raises(ValueError):
        fusion.predict_proba(model, np.zeros((3, 2, 1)))


def test_fuse_proba_is_weighted_sum() -> None:
    video = np.array([[0.7, 0.2, 0.1], [0.1, 0.2, 0.7]])
    speech = np.array([[0.1, 0.2, 0.7], [0.7, 0.2, 0.1]])

    fused = fusion.fuse_proba(video, speech, 0.25)

    np.testing.assert_allclose(fused, 0.25 * video + 0.75 * speech)
    np.testing.assert_allclose(fusion.fuse_proba(video, speech, 1.0), video)
    np.testing.assert_allclose(fusion.fuse_proba(video, speech, 0.0), speech)

    with pytest.raises(ValueError):
        fusion.fuse_proba(video, speech, 1.5)
    with pytest.raises(ValueError):
        fusion.fuse_proba(video, speech, float("nan"))
    with pytest.raises(ValueError):
        fusion.fuse_proba(video, speech[:1], 0.5)


# ---------------------------------------------------------------------------
# Weight selection
# ---------------------------------------------------------------------------


def test_choose_weights_favors_informative_modality() -> None:
    labels = _three_class_labels(20)
    perfect = _one_hot(labels)
    always_wrong = _one_hot((labels + 1) % 3)

    w_video, w_speech = fusion.choose_weights(perfect, always_wrong, labels)

    assert w_video > 0.5
    assert w_speech < 0.5
    assert w_video + w_speech == pytest.approx(1.0)


def test_choose_weights_ties_to_equal_weights() -> None:
    labels = _three_class_labels(20)
    rng = np.random.default_rng(7)
    probabilities = rng.dirichlet(np.ones(3), size=labels.size)

    assert fusion.choose_weights(
        probabilities, probabilities.copy(), labels
    ) == pytest.approx((0.5, 0.5))


def test_choose_weights_respects_custom_grid() -> None:
    labels = _three_class_labels(20)
    perfect = _one_hot(labels)
    always_wrong = _one_hot((labels + 1) % 3)

    weights = fusion.choose_weights(
        perfect, always_wrong, labels, grid=[0.0, 0.25, 0.75, 1.0]
    )

    assert weights == pytest.approx((0.75, 0.25))


def test_choose_weights_rejects_invalid_grid() -> None:
    labels = _three_class_labels(3)
    probabilities = _one_hot(labels)
    with pytest.raises(ValueError):
        fusion.choose_weights(probabilities, probabilities, labels, grid=[-0.1])
    with pytest.raises(ValueError):
        fusion.choose_weights(probabilities, probabilities, labels, grid=[])


def test_weights_from_auc_prefers_perfect_modality() -> None:
    labels = _three_class_labels(20)
    perfect = _one_hot(labels)
    always_wrong = _one_hot((labels + 1) % 3)

    w_video, w_speech = fusion.weights_from_auc(perfect, always_wrong, labels)

    assert w_video == pytest.approx(1.0)
    assert w_speech == pytest.approx(0.0)


def test_weights_from_auc_falls_back_to_equal_when_both_below_chance() -> None:
    labels = _three_class_labels(20)
    always_wrong = _one_hot((labels + 1) % 3)

    assert fusion.weights_from_auc(
        always_wrong, always_wrong.copy(), labels
    ) == pytest.approx((0.5, 0.5))


@pytest.mark.parametrize("constant_scores", [True, False])
def test_weights_from_auc_gives_no_excess_to_chance_scores(
    constant_scores: bool,
) -> None:
    labels = np.repeat(np.arange(3), 5)
    if constant_scores:
        chance_scores = np.full((labels.size, 3), 1.0 / 3.0)
    else:
        # Every class has the same empirical score distribution, so scores
        # are independent of the labels despite varying across observations.
        scores = np.random.default_rng(17).dirichlet(np.ones(3), size=5)
        chance_scores = np.tile(scores, (3, 1))

    assert fusion.macro_auc(labels, chance_scores) == pytest.approx(0.5)
    assert fusion.weights_from_auc(
        chance_scores, _one_hot(labels), labels
    ) == pytest.approx((0.0, 1.0))
    assert fusion.weights_from_auc(
        chance_scores, chance_scores.copy(), labels
    ) == pytest.approx((0.5, 0.5))


def _binary_probability_ranking(negatives: list[float], positives: list[float]):
    # A two-class fold of the three-class task. Ranking class-1 probability
    # reverses the class-0 ranking, so both OvR AUCs equal the pairwise AUC.
    score = np.asarray(negatives + positives)
    return np.column_stack((1.0 - score, score, np.zeros(score.size)))


def test_weights_from_auc_does_not_reward_auc_point_four() -> None:
    labels = np.repeat([0, 1], 5)
    below_chance = _binary_probability_ranking(
        [0.1, 0.2, 0.7, 0.8, 0.9], [0.3, 0.35, 0.4, 0.45, 0.5]
    )
    better = _binary_probability_ranking(
        [0.1, 0.2, 0.3, 0.4, 0.9], [0.5, 0.55, 0.6, 0.65, 0.7]
    )
    assert fusion.macro_auc(labels, below_chance) == pytest.approx(0.4)
    assert fusion.macro_auc(labels, better) == pytest.approx(0.8)
    assert fusion.weights_from_auc(
        below_chance, better, labels
    ) == pytest.approx((0.0, 1.0))


def test_weights_from_auc_normalizes_excess_above_half() -> None:
    labels = np.repeat([0, 1], 5)
    video = _binary_probability_ranking(
        [0.1, 0.2, 0.3, 0.4, 0.9], [0.5, 0.55, 0.6, 0.65, 0.7]
    )
    speech = _binary_probability_ranking(
        [0.1, 0.2, 0.3, 0.8, 0.9], [0.4, 0.45, 0.5, 0.55, 0.6]
    )
    assert fusion.macro_auc(labels, video) == pytest.approx(0.8)
    assert fusion.macro_auc(labels, speech) == pytest.approx(0.6)
    assert fusion.weights_from_auc(video, speech, labels) == pytest.approx((0.75, 0.25))


def test_macro_auc_handles_missing_classes() -> None:
    labels = np.array([0, 0, 1, 1, 2, 2])
    probabilities = _one_hot(labels)
    assert fusion.macro_auc(labels, probabilities) == pytest.approx(1.0)

    only_one_class = np.zeros(4, dtype=int)
    assert math.isnan(fusion.macro_auc(only_one_class, np.eye(3)[only_one_class]))


# ---------------------------------------------------------------------------
# Ordinal thresholds
# ---------------------------------------------------------------------------


def test_learn_thresholds_recovers_separable_ordinal_data() -> None:
    labels = _three_class_labels(10)
    scores = np.concatenate(
        [
            np.linspace(0.05, 0.15, 10),
            np.linspace(0.45, 0.55, 10),
            np.linspace(0.85, 0.95, 10),
        ]
    )

    t1, t2 = fusion.learn_thresholds(scores, labels)

    assert 0.15 < t1 < 0.45
    assert 0.55 < t2 < 0.85
    np.testing.assert_array_equal(
        fusion.apply_thresholds(scores, (t1, t2)), labels
    )


def test_learn_thresholds_constant_scores_returns_ordered_pair() -> None:
    labels = _three_class_labels(10)
    scores = np.ones(labels.size)

    t1, t2 = fusion.learn_thresholds(scores, labels)

    assert math.isfinite(t1) and math.isfinite(t2)
    assert t1 < t2


def test_apply_thresholds_uses_half_open_intervals() -> None:
    predictions = fusion.apply_thresholds([0.0, 0.5, 1.0], (0.5, 1.0))

    np.testing.assert_array_equal(predictions, [0, 1, 2])
    with pytest.raises(ValueError):
        fusion.apply_thresholds([0.0, 1.0], (1.0, 0.5))
    with pytest.raises(ValueError):
        fusion.apply_thresholds([0.0, float("nan")], (0.5, 1.0))


def test_ordinal_score_is_expected_class() -> None:
    probabilities = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    np.testing.assert_allclose(
        fusion.ordinal_score(probabilities), [0.0, 50.0, 100.0]
    )


# ---------------------------------------------------------------------------
# Cross-validated experiment
# ---------------------------------------------------------------------------


def test_run_cv_uses_group_kfold_and_reports_all_schemes() -> None:
    frame = _synthetic_frame()

    result = run_cv(frame, _fast_config(n_boot=5))

    assert result["cv"]["strategy"] == "GroupKFold"
    assert result["cv"]["n_splits"] == 5
    assert result["cv"]["groups_available"] is True
    assert result["dataset"]["n_samples"] == len(frame)

    schemes = set(result["metrics"]["scheme"])
    assert {
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
    } <= schemes
    assert "R0_rule_equal" not in schemes

    assert len(result["predictions"]) == len(frame) * len(schemes)
    assert set(result["predictions"]["prediction"]) <= {0, 1, 2}
    learned = result["weights_by_fold"].query("scheme == 'W4_learned'")
    assert len(learned) == result["cv"]["n_splits"]
    assert result["bootstrap"]["W4_learned"] is not None
    assert set(result["schemes"]) == schemes
    assert (
        result["schemes"]["W4_learned"]["metrics"]["macro_f1"]
        == result["metrics"].set_index("scheme").loc["W4_learned", "macro_f1"]
    )


def test_run_cv_falls_back_to_stratified_without_groups() -> None:
    frame = _synthetic_frame().drop(columns=["lesson_id"])

    result = run_cv(frame, _fast_config())

    assert result["cv"]["strategy"] == "StratifiedKFold"
    assert result["cv"]["n_splits"] == 5
    assert result["cv"]["groups_available"] is False
    assert any("StratifiedKFold" in warning for warning in result["warnings"])


def test_run_cv_is_reproducible() -> None:
    frame = _synthetic_frame()
    config = _fast_config()

    first = run_cv(frame, config)
    second = run_cv(frame, config)

    pd.testing.assert_frame_equal(first["metrics"], second["metrics"])
    pd.testing.assert_frame_equal(
        first["predictions"], second["predictions"]
    )
    pd.testing.assert_frame_equal(
        first["weights_by_fold"], second["weights_by_fold"]
    )


def test_run_cv_learned_weights_favor_informative_modality() -> None:
    frame = _synthetic_frame()

    result = run_cv(frame, _fast_config())

    stability = result["weight_stability"]["W4_learned"]
    assert stability["mean"] > 0.5
    assert len(stability["fold_weights"]) == result["cv"]["n_splits"]


def test_outer_test_labels_do_not_influence_fold_selection() -> None:
    frame = _synthetic_frame()
    config = _fast_config()

    splits = list(
        GroupKFold(n_splits=5).split(
            frame, frame["label"], frame["lesson_id"]
        )
    )
    corrupted_group = 0
    target_fold = next(
        fold
        for fold, (_, test_index) in enumerate(splits)
        if corrupted_group
        in set(frame["lesson_id"].to_numpy()[test_index])
    )

    corrupted = frame.copy()
    mask = corrupted["lesson_id"] == corrupted_group
    corrupted.loc[mask, "label"] = (corrupted.loc[mask, "label"] + 1) % 3

    baseline = run_cv(frame, config)
    perturbed = run_cv(corrupted, config)

    columns = ["w_video", "w_speech", "threshold_1", "threshold_2"]

    def selected(result: dict) -> pd.DataFrame:
        frame = result["weights_by_fold"]
        mask = (frame["scheme"] == "W4_learned") & (frame["fold"] == target_fold)
        return frame.loc[mask, columns].reset_index(drop=True)

    pd.testing.assert_frame_equal(selected(baseline), selected(perturbed))


def test_rule_baseline_scheme_uses_rule_component_columns() -> None:
    frame = _synthetic_frame(rule_columns=True)

    result = run_cv(frame, _fast_config(n_boot=5))

    assert "R0_rule_equal" in set(result["metrics"]["scheme"])
    rule_metrics = result["metrics"].set_index("scheme").loc["R0_rule_equal"]
    assert rule_metrics["n_unavailable"] == 0
    assert math.isnan(rule_metrics["macro_auc_ovr"])
    rule_predictions = result["predictions"].query("scheme == 'R0_rule_equal'")
    assert np.all(np.isnan(rule_predictions["p0"]))


def test_weight_sensitivity_and_missing_modality_outputs() -> None:
    frame = _synthetic_frame()
    config = ExperimentConfig(
        n_boot=0,
        mlp_max_iter=300,
        run_ablation=False,
        run_weight_sensitivity=True,
        run_missing_modality=True,
        missing_modality_rates=(0.2, 0.5),
        missing_modality_repeats=2,
    )

    result = run_cv(frame, config)

    sensitivity = result["weight_sensitivity"]
    assert len(sensitivity) == 21
    np.testing.assert_allclose(
        sensitivity["w_video"].to_numpy(), np.linspace(0.0, 1.0, 21)
    )
    assert sensitivity["macro_f1"].notna().all()
    assert 0.0 <= sensitivity.attrs["best_w_video"] <= 1.0

    missing = result["missing_modality"]
    # 2 schemes x 2 rates x 2 repeats x 2 treatments
    assert len(missing) == 2 * 2 * 2 * 2
    assert set(missing["scheme"]) == {"W1_equal", "W4_learned"}
    assert set(missing["treatment"]) == {"neutral_fill", "available_only"}
    assert (missing["n_masked"] >= 0).all()
    assert missing["macro_f1"].notna().all()


def test_ablation_reports_requested_feature_groups() -> None:
    frame = _synthetic_frame(rule_columns=True)
    config = _fast_config(run_ablation=True)

    result = run_cv(frame, config)

    ablation = result["ablation"]
    assert {
        "video_pose_only",
        "video_detection_only",
        "speech_coverage_only",
        "speech_question_only",
        "all_video",
        "all_speech",
    } <= set(ablation["group"])
    assert (ablation["n_features"] > 0).all()
    assert ablation["macro_f1"].notna().all()


def test_summary_json_serialisable_and_markdown_renders() -> None:
    result = run_cv(_synthetic_frame(), _fast_config(n_boot=5))

    payload = json.dumps(result["summary"], ensure_ascii=False, allow_nan=False)
    assert "W4_learned" in payload

    markdown = render_summary_markdown(result)
    assert "# Late-fusion experiment summary" in markdown
    assert "W4_learned" in markdown
    assert "Out-of-fold scheme metrics" in markdown


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_writes_expected_outputs() -> None:
    frame = _synthetic_frame(per_class=15, n_groups=5)
    with tempfile.TemporaryDirectory(dir=str(_TMP_ROOT)) as temporary:
        root = Path(temporary)
        features = root / "features.csv"
        output = root / "run"
        frame.to_csv(features, index=False)

        exit_code = cli_main(
            [
                "--features",
                str(features),
                "--out",
                str(output),
                "--n-boot",
                "5",
                "--missing-repeats",
                "1",
                "--mlp-max-iter",
                "200",
                "--no-ablation",
                "--no-missing-modality",
                "--no-weight-sensitivity",
                "--no-statistical-tests",
                "--no-window-sensitivity",
                "--no-rule-threshold-sensitivity",
            ]
        )

        assert exit_code == 0
        for name in (
            "metrics.csv",
            "predictions.csv",
            "weights_by_fold.csv",
            "weights.csv",
            "config.json",
            "per_fold_metrics.csv",
            "confusion_matrix.csv",
            "per_class_metrics.csv",
            "statistical_tests.csv",
            "window_sensitivity.csv",
            "holdout_metrics.csv",
            "rule_threshold_sensitivity.csv",
            "weight_sensitivity.csv",
            "missing_modality.csv",
            "ablation.csv",
            "summary.json",
            "summary.md",
        ):
            assert (output / name).exists(), name

        metrics = pd.read_csv(output / "metrics.csv")
        assert {
            "scheme",
            "accuracy",
            "macro_f1",
            "macro_f1_ci_lower",
            "macro_f1_ci_upper",
        } <= set(metrics.columns)
        summary = json.loads(
            (output / "summary.json").read_text(encoding="utf-8")
        )
        assert summary["dataset"]["n_samples"] == len(frame)
        assert summary["cv"]["strategy"] == "GroupKFold"


def test_cli_reports_failure_instead_of_fabricating_results() -> None:
    frame = _synthetic_frame(per_class=15, n_groups=5)
    frame["label"] = 0  # a single class cannot be cross-validated
    with tempfile.TemporaryDirectory(dir=str(_TMP_ROOT)) as temporary:
        root = Path(temporary)
        features = root / "features.csv"
        output = root / "run"
        frame.to_csv(features, index=False)

        exit_code = cli_main(
            [
                "--features",
                str(features),
                "--out",
                str(output),
                "--n-boot",
                "0",
            ]
        )

        assert exit_code == 1
        # Failed runs are preserved (spec section 7): the directory contains a
        # FAILED.json record, never fabricated metrics.
        assert (output / "FAILED.json").is_file()
        failure = json.loads((output / "FAILED.json").read_text(encoding="utf-8"))
        assert failure["status"] == "FAILED"
        assert "At least two classes" in failure["error"]
        assert not (output / "metrics.csv").exists()


# ---------------------------------------------------------------------------
# Robustness / split options
# ---------------------------------------------------------------------------


def test_loso_uses_leave_one_group_out() -> None:
    frame = _synthetic_frame()
    result = run_cv(frame, _fast_config(loso=True))
    assert result["cv"]["strategy"] == "LeaveOneGroupOut"
    assert result["cv"]["n_splits"] == frame["lesson_id"].nunique()


def test_all_missing_group_column_falls_back_to_stratified() -> None:
    frame = _synthetic_frame()
    frame["lesson_id"] = np.nan
    result = run_cv(frame, _fast_config())
    assert result["cv"]["strategy"] == "StratifiedKFold"
    assert any("entirely missing" in warning for warning in result["warnings"])


def test_official_split_holdout_is_evaluated() -> None:
    frame = _synthetic_frame()
    lessons = sorted(frame["lesson_id"].unique())
    test_lessons = set(lessons[-2:])
    frame["split"] = np.where(frame["lesson_id"].isin(test_lessons), "test", "train")
    result = run_cv(frame, _fast_config(split_col="split", holdout_split="test"))
    holdout = result["holdout_metrics"]
    assert not holdout.empty
    assert "W1_equal" in set(holdout["scheme"])
    assert int(holdout.iloc[0]["n_test"]) > 0


def test_window_sensitivity_runs_for_each_window() -> None:
    frame = _synthetic_frame()
    frame["window_seconds"] = np.where(np.arange(len(frame)) % 2 == 0, 5.0, 60.0)
    config = ExperimentConfig(
        n_boot=0,
        mlp_max_iter=150,
        run_ablation=False,
        run_missing_modality=False,
        run_weight_sensitivity=False,
        run_statistical_tests=False,
        run_window_sensitivity=True,
        run_rule_threshold_sensitivity=False,
    )
    result = run_cv(frame, config)
    sensitivity = result["window_sensitivity"]
    assert set(sensitivity.query("status == 'OK'")["window_seconds"]) == {5.0, 60.0}


def test_rule_threshold_sensitivity_runs() -> None:
    frame = _synthetic_frame(rule_columns=True)
    config = ExperimentConfig(
        n_boot=0,
        mlp_max_iter=150,
        run_ablation=False,
        run_missing_modality=False,
        run_weight_sensitivity=False,
        run_statistical_tests=False,
        run_window_sensitivity=False,
        run_rule_threshold_sensitivity=True,
    )
    result = run_cv(frame, config)
    sensitivity = result["rule_threshold_sensitivity"]
    assert len(sensitivity) == 9
    assert set(sensitivity["status"]) == {"OK"}


def test_missing_modality_summary_has_h5_verdict() -> None:
    frame = _synthetic_frame()
    config = ExperimentConfig(
        n_boot=0,
        mlp_max_iter=150,
        run_ablation=False,
        run_weight_sensitivity=False,
        run_missing_modality=True,
        missing_modality_rates=(0.2,),
        missing_modality_repeats=1,
    )
    result = run_cv(frame, config)
    summary = result["summary"]["missing_modality"]
    assert summary
    assert all("h5_verdict" in row for row in summary)
    assert {row["treatment"] for row in summary} == {"neutral_fill", "available_only"}


def test_all_nan_rule_columns_do_not_fabricate_r0_results() -> None:
    frame = _synthetic_frame(rule_columns=True)
    for column in (
        "posture_score",
        "stability_score",
        "positive_behavior_ratio",
        "video_posture_score",
        "video_stability_score",
        "video_positive_behavior_ratio",
        "speech_coverage",
        "speech_question_count",
        "speech_rate",
    ):
        if column in frame.columns:
            frame[column] = np.nan
    frame["split"] = np.where(frame["lesson_id"] <= 2, "test", "train")
    config = ExperimentConfig(
        n_boot=0,
        mlp_max_iter=100,
        run_ablation=False,
        run_missing_modality=False,
        run_weight_sensitivity=False,
        run_statistical_tests=False,
        run_window_sensitivity=False,
        run_rule_threshold_sensitivity=True,
        split_col="split",
        holdout_split="test",
    )
    result = run_cv(frame, config)
    sensitivity = result["rule_threshold_sensitivity"]
    assert not sensitivity.empty
    assert set(sensitivity["status"]) == {"SKIPPED"}
    holdout = result["holdout_metrics"]
    r0 = holdout[holdout["scheme"] == "R0_rule_equal"]
    assert r0.empty or set(r0["status"]) == {"SKIPPED"}
    assert "R0_rule_equal" not in set(result["metrics"]["scheme"])


def test_aggregate_to_windows_sums_counts_and_averages_scores() -> None:
    frame = pd.DataFrame(
        {
            "lesson_id": [1] * 12,
            "clip_order": list(range(12)),
            "label": [1] * 12,
            "posture_score": np.arange(12, dtype=float) / 12.0,
            "question_count": np.ones(12, dtype=float),
            "speech_seconds": np.full(12, 5.0),
        }
    )
    aggregated = _aggregate_to_windows(frame, window_seconds=60.0, clip_seconds=5.0)
    assert aggregated is not None
    assert len(aggregated) == 1
    row = aggregated.iloc[0]
    assert row["window_seconds"] == 60.0
    assert row["question_count"] == pytest.approx(12.0)
    assert row["speech_seconds"] == pytest.approx(60.0)
    assert row["posture_score"] == pytest.approx(np.arange(12).mean() / 12.0)
    assert row["label"] == 1


def test_window_sensitivity_aggregates_when_clip_order_exists() -> None:
    # 180 rows = 5 lessons x 36 clips; 3 complete 60s windows per lesson.
    frame = _synthetic_frame(per_class=60, n_groups=5)
    frame["clip_order"] = np.tile(np.arange(36), 5)
    config = ExperimentConfig(
        n_boot=0,
        mlp_max_iter=120,
        run_ablation=False,
        run_missing_modality=False,
        run_weight_sensitivity=False,
        run_statistical_tests=False,
        run_window_sensitivity=True,
        run_rule_threshold_sensitivity=False,
    )
    result = run_cv(frame, config)
    sensitivity = result["window_sensitivity"]
    assert set(sensitivity.query("status == 'OK'")["window_seconds"]) == {5.0, 60.0}
