package com.macteval.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.util.*;

/**
 * 评价规则引擎 — 基于教育领域专家知识的指标计算。
 * <p>
 * 规则引擎处理定义明确的教学行为指标，输出结构化数值。
 * 与 MLP 评分模型互补：规则引擎负责可精确计算的指标，
 * MLP 负责需要综合判断的抽象维度。
 */
@Component
@Slf4j
public class RuleEngine {

    /** 行为权重配置 */
    private static final Map<String, Double> BEHAVIOR_WEIGHTS = Map.of(
            "raising_hand", 8.0, "reading", 5.0, "writing", 6.0,
            "discussing", 10.0, "looking_away", -5.0, "sleeping", -10.0,
            "using_phone", -8.0
    );

    /** 教学环节标准时长占比 (理想值) */
    private static final Map<String, Double> PHASE_RATIO_IDEAL = Map.of(
            "导入", 0.08, "讲授", 0.35, "互动", 0.25,
            "练习", 0.22, "总结", 0.10
    );

    /**
     * 计算行为参与度 (0-100)。
     * 基于课堂中检测到的学生行为序列加权平均。
     */
    public double calcBehavioralEngagement(List<String> behaviors) {
        if (behaviors == null || behaviors.isEmpty()) return 50.0;
        double score = 50.0;
        int count = 0;
        for (String b : behaviors) {
            Double weight = BEHAVIOR_WEIGHTS.get(b);
            if (weight != null) {
                score += weight;
                count++;
            }
        }
        return clamp(score);
    }

    /**
     * 计算互动质量 (0-100)。
     */
    public double calcInteractionQuality(int questionCount, double avgWaitTimeSec,
                                          double studentResponseRate) {
        double score = 50.0;
        // 提问频次: 理想值 15-25 次/节
        score += clamp(questionCount * 1.5, 0, 20);
        // 候答时间: 理想值 3-5 秒
        score += clamp(avgWaitTimeSec * 3, 0, 15);
        // 应答率: 理想值 > 70%
        score += clamp(studentResponseRate * 15, 0, 15);
        return clamp(score);
    }

    /**
     * 计算参与度的 Gini 系数 (衡量参与均匀度)。
     * 0 = 完全均匀, 1 = 完全不均匀。
     * <p>
     * 使用相对平均绝对离差公式: G = (2 * Σ(i+1)*x_i) / (n * Σx_i) - (n+1)/n
     * 其中 x_i 为升序排列后的参与次数。
     */
    public double calcParticipationGini(List<Integer> participationCounts) {
        if (participationCounts == null || participationCounts.size() < 2) return 0;
        List<Integer> sorted = new ArrayList<>(participationCounts);
        Collections.sort(sorted);
        int n = sorted.size();
        double sum = sorted.stream().mapToInt(Integer::intValue).sum();
        if (sum == 0) return 0;
        double weightedSum = 0;
        for (int i = 0; i < n; i++) {
            weightedSum += (long) (i + 1) * sorted.get(i);
        }
        return (2.0 * weightedSum) / (n * sum) - (double) (n + 1) / n;
    }

    /**
     * 计算教学环节时间分布合理性。
     */
    public double calcPhaseDistributionScore(Map<String, Double> actualRatios) {
        double score = 100.0;
        for (var entry : PHASE_RATIO_IDEAL.entrySet()) {
            double actual = actualRatios.getOrDefault(entry.getKey(), 0.0);
            double diff = Math.abs(actual - entry.getValue());
            score -= diff * 100; // 每偏离 1% 扣 1 分
        }
        return clamp(score);
    }

    /**
     * 计算叫答公平性 (基于座位区域参与度标准差)。
     */
    public double calcCallEquityScore(Map<String, Double> zoneParticipation) {
        if (zoneParticipation == null || zoneParticipation.size() < 2) return 80.0;
        Collection<Double> values = zoneParticipation.values();
        double mean = values.stream().mapToDouble(Double::doubleValue).average().orElse(0);
        double variance = values.stream()
                .mapToDouble(v -> Math.pow(v - mean, 2))
                .average().orElse(0);
        double std = Math.sqrt(variance);
        return clamp(100 - std * 2); // 标准差越小越公平
    }

    /**
     * 生成行为统计摘要。
     */
    public Map<String, Object> summarizeBehaviors(List<String> behaviors) {
        Map<String, Long> counts = new HashMap<>();
        behaviors.forEach(b -> counts.merge(b, 1L, Long::sum));
        Map<String, Object> summary = new LinkedHashMap<>();
        counts.forEach((k, v) -> summary.put(k, v));
        summary.put("total", behaviors.size());
        summary.put("positive_ratio", behaviors.stream()
                .filter(b -> BEHAVIOR_WEIGHTS.getOrDefault(b, 0.0) > 0)
                .count() / (double) Math.max(behaviors.size(), 1));
        return summary;
    }

    private double clamp(double v) { return Math.max(0, Math.min(100, v)); }
    private double clamp(double v, double min, double max) { return Math.max(min, Math.min(max, v)); }
}
