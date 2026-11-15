package com.macteval.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

/**
 * 规则基线：视频/语音模态分数的显式公式实现（论文主方案的可解释对照）。
 * <p>
 * 公式、阈值与出处见 {@code docs/experiments/rule-baseline-protocol.md}，与
 * {@code tools/public_data/rule_baseline.py} 保持一致。所有输入分量缺失时返回 null，
 * 部分缺失时按可用分量权重重归一化——缺失不得当作 0 分。
 * <ul>
 *   <li>视频：{@code 100 × weighted_mean(posture 0.5, stability 0.2, positive_behavior_ratio 0.3)}</li>
 *   <li>语音：{@code 100 × weighted_mean(coverage 0.5, question_density 0.3, rate_score 0.2)}</li>
 * </ul>
 * 阈值（1 问/分钟、150–300 字/分钟）为可复核的工程阈值，需在实验中做敏感性分析，
 * 不作为绝对教育标准。
 */
@Service
@Slf4j
public class RuleBaselineService {

    // 视频分量权重（spec 5.1）
    public static final double VIDEO_POSTURE_WEIGHT = 0.5;
    public static final double VIDEO_STABILITY_WEIGHT = 0.2;
    public static final double VIDEO_POSITIVE_BEHAVIOR_WEIGHT = 0.3;

    // 语音分量权重（spec 5.2）
    public static final double SPEECH_COVERAGE_WEIGHT = 0.5;
    public static final double SPEECH_QUESTION_WEIGHT = 0.3;
    public static final double SPEECH_RATE_WEIGHT = 0.2;

    /** 问题密度饱和点：1 问/分钟（spec 5.2）。 */
    public static final double QUESTION_DENSITY_SATURATION_PER_MINUTE = 1.0;
    /** 语速满分区间下界：150 字/分钟（spec 5.2）。 */
    public static final double SPEECH_RATE_MIN = 150.0;
    /** 语速满分区间上界：300 字/分钟（spec 5.2）。 */
    public static final double SPEECH_RATE_MAX = 300.0;

    /**
     * 视频规则分（0-100）。
     *
     * @param posture               姿态分（0-1）；null 表示该分量不可用
     * @param stability             姿态稳定分（0-1）；null 表示该分量不可用
     * @param positiveBehaviorRatio 积极行为比例（0-1）；null 表示该分量不可用
     * @return 加权平均分；全部分量缺失返回 null
     */
    public Double videoScore(Double posture, Double stability, Double positiveBehaviorRatio) {
        WeightedComponents components = new WeightedComponents();
        components.add(posture, VIDEO_POSTURE_WEIGHT);
        components.add(stability, VIDEO_STABILITY_WEIGHT);
        components.add(positiveBehaviorRatio, VIDEO_POSITIVE_BEHAVIOR_WEIGHT);
        return components.score();
    }

    /**
     * 语音规则分（0-100）。
     *
     * @param coverage      话语覆盖率（0-1）；null 表示该分量不可用
     * @param questionCount 窗口内提问数；null 或窗口长度无效表示该分量不可用
     * @param speechRate    语速（字/分钟）；null 表示该分量不可用
     * @param windowSeconds 窗口时长（秒）
     * @return 加权平均分；全部分量缺失返回 null
     */
    public Double speechScore(Double coverage, Integer questionCount, Double speechRate, double windowSeconds) {
        if (!Double.isFinite(windowSeconds) || windowSeconds <= 0) {
            log.warn("Invalid windowSeconds={}; speech score is unavailable", windowSeconds);
            return null;
        }
        WeightedComponents components = new WeightedComponents();
        components.add(coverage, SPEECH_COVERAGE_WEIGHT);
        components.add(questionDensity(questionCount, windowSeconds), SPEECH_QUESTION_WEIGHT);
        components.add(rateScore(speechRate), SPEECH_RATE_WEIGHT);
        return components.score();
    }

    /**
     * 两模态融合：权重归一化后加权求和，输入与输出均 clamp 到 0-100。
     * 权重不可用时回退等权。
     */
    /**
     * 规则分数对照用的融合；在线窗口融合由 {@link FusionService} 完成
     * （缺失模态中性 50）。本方法在单模态缺失时返回可用模态分数，
     * 与 Python {@code rule_baseline.fuse} 一致，不与在线中性填充混用。
     */
    public double fuse(double video, double speech, double wVideo, double wSpeech) {
        double v = clampScore(video);
        double s = clampScore(speech);
        double wv = usableWeight(wVideo);
        double ws = usableWeight(wSpeech);
        double sum = wv + ws;
        if (sum <= 0) {
            wv = 0.5;
            ws = 0.5;
            sum = 1.0;
        }
        return clampScore((wv * v + ws * s) / sum);
    }

    /** question_density = min(1, questions_per_minute / 1.0)。 */
    private static Double questionDensity(Integer questionCount, double windowSeconds) {
        if (questionCount == null || !Double.isFinite(windowSeconds) || windowSeconds <= 0) {
            return null;
        }
        double questionsPerMinute = Math.max(0, questionCount) / (windowSeconds / 60.0);
        return Math.min(1.0, questionsPerMinute / QUESTION_DENSITY_SATURATION_PER_MINUTE);
    }

    /** 150–300 字/分钟满分，区间外线性衰减到 0（spec 5.2）。 */
    private static Double rateScore(Double speechRate) {
        if (speechRate == null || !Double.isFinite(speechRate)) {
            return null;
        }
        double rate = Math.max(0, speechRate);
        if (rate < SPEECH_RATE_MIN) {
            return clamp01(1.0 - (SPEECH_RATE_MIN - rate) / SPEECH_RATE_MIN);
        }
        if (rate > SPEECH_RATE_MAX) {
            return clamp01(1.0 - (rate - SPEECH_RATE_MAX) / SPEECH_RATE_MAX);
        }
        return 1.0;
    }

    private static double usableWeight(double weight) {
        return Double.isFinite(weight) && weight > 0 ? weight : 0.0;
    }

    private static double clamp01(double value) {
        return Math.max(0, Math.min(1, value));
    }

    private static double clampScore(double value) {
        if (!Double.isFinite(value)) return 0.0;
        return Math.max(0, Math.min(100, value));
    }

    /** 缺失分量重归一化的加权平均累加器。 */
    private static final class WeightedComponents {
        private double weightedSum;
        private double weightSum;

        void add(Double value, double weight) {
            if (value == null || !Double.isFinite(value)) return;
            weightedSum += weight * clamp01(value);
            weightSum += weight;
        }

        Double score() {
            return weightSum <= 0 ? null : clampScore(100.0 * weightedSum / weightSum);
        }
    }
}
