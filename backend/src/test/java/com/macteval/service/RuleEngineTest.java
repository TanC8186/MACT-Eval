package com.macteval.service;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * 规则引擎单元测试。
 * <p>
 * RuleEngine 为无状态 Spring Component，无外部依赖，
 * 使用纯 JUnit 5 测试即可。
 */
class RuleEngineTest {

    private final RuleEngine engine = new RuleEngine();

    // ========================================================================
    // calcBehavioralEngagement
    // ========================================================================

    @Nested
    @DisplayName("calcBehavioralEngagement — 行为参与度计算")
    class BehavioralEngagementTest {

        @Test
        @DisplayName("null 或空列表应返回默认分 50")
        void nullOrEmpty_returnsDefault() {
            assertEquals(50.0, engine.calcBehavioralEngagement(null), 1e-9);
            assertEquals(50.0, engine.calcBehavioralEngagement(List.of()), 1e-9);
        }

        @Test
        @DisplayName("全积极行为应得高分")
        void allPositive_returnsHighScore() {
            List<String> behaviors = List.of("raising_hand", "discussing", "reading", "writing");
            double score = engine.calcBehavioralEngagement(behaviors);
            // 50 + 8 + 10 + 5 + 6 = 79
            assertEquals(79.0, score, 1e-9);
        }

        @Test
        @DisplayName("全消极行为应得低分")
        void allNegative_returnsLowScore() {
            List<String> behaviors = List.of("looking_away", "sleeping", "using_phone");
            double score = engine.calcBehavioralEngagement(behaviors);
            // 50 + (-5) + (-10) + (-8) = 27
            assertEquals(27.0, score, 1e-9);
        }

        @Test
        @DisplayName("混合行为应正确加权求和")
        void mixedBehaviors_sumWeightsCorrectly() {
            List<String> behaviors = List.of("raising_hand", "sleeping", "discussing", "looking_away");
            double score = engine.calcBehavioralEngagement(behaviors);
            // 50 + 8 + (-10) + 10 + (-5) = 53
            assertEquals(53.0, score, 1e-9);
        }

        @Test
        @DisplayName("包含未知行为应跳过（权重不受影响）")
        void unknownBehavior_skipped() {
            List<String> behaviors = List.of("raising_hand", "unknown_action", "writing");
            double score = engine.calcBehavioralEngagement(behaviors);
            // 仅计算 raising_hand(8) + writing(6): 50 + 8 + 6 = 64
            assertEquals(64.0, score, 1e-9);
        }

        @Test
        @DisplayName("总分应被 clamp 到 [0, 100] 区间")
        void score_clampedBetweenZeroAndHundred() {
            // 远超上限: 50 + 8*10 = 130 -> clamp=100
            List<String> manyPositive = List.of("raising_hand", "raising_hand", "raising_hand",
                    "raising_hand", "raising_hand", "raising_hand", "raising_hand", "raising_hand",
                    "raising_hand", "raising_hand");
            assertEquals(100.0, engine.calcBehavioralEngagement(manyPositive), 1e-9);

            // 远超下限: 50 + (-10)*10 = -50 -> clamp=0
            List<String> manyNegative = List.of("sleeping", "sleeping", "sleeping",
                    "sleeping", "sleeping", "sleeping", "sleeping", "sleeping",
                    "sleeping", "sleeping");
            assertEquals(0.0, engine.calcBehavioralEngagement(manyNegative), 1e-9);
        }

        @Test
        @DisplayName("单个行为不影响总分（仅有 base 50）")
        void singleWeightlessBehavior_returnsBase() {
            // 未知行为不会被计入，相当于空列表
            List<String> unknownOnly = List.of("some_random_behavior");
            assertEquals(50.0, engine.calcBehavioralEngagement(unknownOnly), 1e-9);
        }
    }

    // ========================================================================
    // calcInteractionQuality
    // ========================================================================

    @Nested
    @DisplayName("calcInteractionQuality — 互动质量计算")
    class InteractionQualityTest {

        @Test
        @DisplayName("零输入应返回基础分")
        void zeroInputs_returnsBaseScore() {
            double score = engine.calcInteractionQuality(0, 0, 0);
            assertEquals(50.0, score, 1e-9);
        }

        @Test
        @DisplayName("理想参数应得较高分")
        void idealInputs_returnsHighScore() {
            // 20 questions * 1.5 = 30 -> clamp 20
            // 4 sec * 3 = 12 -> clamp 15
            // 0.8 * 15 = 12 -> clamp 15
            // total: 50 + 20 + 12 + 12 = 94, clamp -> 94
            double score = engine.calcInteractionQuality(20, 4.0, 0.8);
            assertEquals(94.0, score, 1e-9);
        }

        @Test
        @DisplayName("最大参数应被 clamp")
        void maxInputs_clamped() {
            double score = engine.calcInteractionQuality(100, 10.0, 2.0);
            // 100*1.5=150->clamp 20, 10*3=30->clamp 15, 2.0*15=30->clamp 15
            // 50 + 20 + 15 + 15 = 100
            assertEquals(100.0, score, 1e-9);
        }

        @Test
        @DisplayName("各部分贡献独立 clamp")
        void eachContributionClampedIndependently() {
            double score = engine.calcInteractionQuality(100, 0, 0);
            // 提问 100*1.5 -> clamp(150,0,20)=20
            // 候答 0 -> 0, 应答率 0 -> 0
            // 50+20=70
            assertEquals(70.0, score, 1e-9);
        }

        @Test
        @DisplayName("分数不会低于 0")
        void scoreNeverBelowZero() {
            double score = engine.calcInteractionQuality(0, 0, -1.0);
            // 50 + 0 + 0 + (-15) -> 35, 不会是负值(因为clamp最后输出)
            assertTrue(score >= 0);
        }
    }

    // ========================================================================
    // calcParticipationGini
    // ========================================================================

    @Nested
    @DisplayName("calcParticipationGini — 参与度 Gini 系数")
    class ParticipationGiniTest {

        @Test
        @DisplayName("null 或少于 2 个学生应返回 0")
        void insufficientData_returnsZero() {
            assertEquals(0.0, engine.calcParticipationGini(null), 1e-9);
            assertEquals(0.0, engine.calcParticipationGini(List.of()), 1e-9);
            assertEquals(0.0, engine.calcParticipationGini(List.of(5)), 1e-9);
        }

        @Test
        @DisplayName("完全均匀分布应返回 0")
        void uniformDistribution_returnsZero() {
            // 所有人参与次数相同 -> Gini = 0
            double score = engine.calcParticipationGini(List.of(3, 3, 3, 3));
            assertEquals(0.0, score, 1e-9);
        }

        @Test
        @DisplayName("极端不均匀分布应接近 1")
        void skewedDistribution_approachesOne() {
            // 一个学生参与全部 (10次), 其他人不参与 (0次)
            // Gini = (2*(1*0+2*0+3*0+4*10))/(4*10) - 5/4 = 80/40 - 1.25 = 0.75
            double gini = engine.calcParticipationGini(List.of(0, 0, 0, 10));
            assertEquals(0.75, gini, 1e-9);
        }

        @Test
        @DisplayName("中等均匀度应返回中间值")
        void moderateSkew_returnsModerateValue() {
            // Gini = (2*(1*1+2*2+3*3+4*4+5*5))/(5*15) - 6/5 = 110/75 - 1.2 ≈ 0.2667
            double gini = engine.calcParticipationGini(List.of(1, 2, 3, 4, 5));
            assertEquals(0.26666666666666666, gini, 1e-9);
        }

        @Test
        @DisplayName("所有值均为 0 应返回 0")
        void allZeros_returnsZero() {
            double gini = engine.calcParticipationGini(List.of(0, 0, 0, 0));
            assertEquals(0.0, gini, 1e-9);
        }

        @Test
        @DisplayName("输入顺序不影响结果")
        void orderIndependence() {
            double gini1 = engine.calcParticipationGini(List.of(1, 5, 2, 8));
            double gini2 = engine.calcParticipationGini(List.of(8, 1, 5, 2));
            assertEquals(gini1, gini2, 1e-9);
        }
    }

    // ========================================================================
    // calcCallEquityScore
    // ========================================================================

    @Nested
    @DisplayName("calcCallEquityScore — 叫答公平性计算")
    class CallEquityScoreTest {

        @Test
        @DisplayName("null 或少于 2 个区域应返回默认分 80")
        void insufficientData_returnsDefault() {
            assertEquals(80.0, engine.calcCallEquityScore(null), 1e-9);
            assertEquals(80.0, engine.calcCallEquityScore(Map.of("front", 10.0)), 1e-9);
        }

        @Test
        @DisplayName("完全均衡的参与度应得满分")
        void balancedParticipation_returnsFullScore() {
            Map<String, Double> zones = Map.of("front", 50.0, "middle", 50.0, "back", 50.0);
            double score = engine.calcCallEquityScore(zones);
            // std=0 -> 100 - 0 = 100
            assertEquals(100.0, score, 1e-9);
        }

        @Test
        @DisplayName("不均匀分布应扣分")
        void imbalancedParticipation_deductsPoints() {
            // 只有两个区域时更容易计算
            Map<String, Double> zones = Map.of("front", 100.0, "back", 0.0);
            double score = engine.calcCallEquityScore(zones);
            // mean=50, variance=2500, std=50, score=100-100=0
            assertEquals(0.0, score, 1e-9);
        }

        @Test
        @DisplayName("轻微不均匀应轻微扣分")
        void slightImbalance_slightDeduction() {
            Map<String, Double> zones = Map.of("front", 55.0, "middle", 50.0, "back", 45.0);
            double score = engine.calcCallEquityScore(zones);
            assertTrue(score > 85, "轻微不均应仍有高分, 实际: " + score);
            assertTrue(score < 100, "不应满分");
        }

        @Test
        @DisplayName("分数不会低于 0")
        void scoreNeverBelowZero() {
            Map<String, Double> zones = Map.of("front", 100.0, "back", 0.0);
            double score = engine.calcCallEquityScore(zones);
            assertTrue(score >= 0);
        }
    }

    // ========================================================================
    // calcPhaseDistributionScore
    // ========================================================================

    @Nested
    @DisplayName("calcPhaseDistributionScore — 教学节奏合理性")
    class PhaseDistributionScoreTest {

        @Test
        @DisplayName("完全匹配理想值应得满分")
        void exactMatch_returnsFullScore() {
            Map<String, Double> actual = Map.of(
                    "导入", 0.08, "讲授", 0.35, "互动", 0.25,
                    "练习", 0.22, "总结", 0.10
            );
            assertEquals(100.0, engine.calcPhaseDistributionScore(actual), 1e-9);
        }

        @Test
        @DisplayName("偏离理想值应扣分")
        void deviation_deductsPoints() {
            Map<String, Double> actual = Map.of(
                    "导入", 0.10, "讲授", 0.40, "互动", 0.20,
                    "练习", 0.20, "总结", 0.10
            );
            // 导入 +0.02 -> 扣2, 讲授 +0.05 -> 扣5, 互动 -0.05 -> 扣5, 练习 -0.02 -> 扣2
            // 100 - 2 - 5 - 5 - 2 = 86
            assertEquals(86.0, engine.calcPhaseDistributionScore(actual), 1e-9);
        }

        @Test
        @DisplayName("缺失阶段按 0 处理并扣分")
        void missingPhase_penalized() {
            Map<String, Double> actual = Map.of(
                    "导入", 0.50, "讲授", 0.50
            );
            double score = engine.calcPhaseDistributionScore(actual);
            // 导入 +0.42 -> 扣42, 讲授 +0.15 -> 扣15, 互动 -0.25 -> 扣25, 练习 -0.22 -> 扣22, 总结 -0.10 -> 扣10
            // 100 - 42 - 15 - 25 - 22 - 10 = -14 -> clamp 0
            assertEquals(0.0, score, 1e-9);
        }
    }

    // ========================================================================
    // summarizeBehaviors
    // ========================================================================

    @Nested
    @DisplayName("summarizeBehaviors — 行为统计摘要")
    class SummarizeBehaviorsTest {

        @Test
        @DisplayName("应正确统计频次和总数")
        void countsTotalAndRatio() {
            List<String> behaviors = List.of("raising_hand", "raising_hand", "sleeping");
            Map<String, Object> summary = engine.summarizeBehaviors(behaviors);

            assertEquals(2L, summary.get("raising_hand"));
            assertEquals(1L, summary.get("sleeping"));
            assertEquals(3, summary.get("total"));
            assertEquals(2.0 / 3.0, (Double) summary.get("positive_ratio"), 1e-9);
        }

        @Test
        @DisplayName("全积极行为正比应为 1")
        void allPositive_ratioIsOne() {
            List<String> behaviors = List.of("raising_hand", "discussing");
            Map<String, Object> summary = engine.summarizeBehaviors(behaviors);
            assertEquals(1.0, (Double) summary.get("positive_ratio"), 1e-9);
        }

        @Test
        @DisplayName("全消极行为正比应为 0")
        void allNegative_ratioIsZero() {
            List<String> behaviors = List.of("sleeping", "looking_away");
            Map<String, Object> summary = engine.summarizeBehaviors(behaviors);
            assertEquals(0.0, (Double) summary.get("positive_ratio"), 1e-9);
        }
    }

    // ========================================================================
    // clamp (通过公共方法间接验证)
    // ========================================================================

    @Nested
    @DisplayName("clamp — 数值边界约束")
    class ClampTest {

        @Test
        @DisplayName("完全匹配理想比例应得 100 分")
        void exactMatch_returnsFullScore() {
            assertEquals(100.0, engine.calcPhaseDistributionScore(Map.of(
                    "导入", 0.08, "讲授", 0.35, "互动", 0.25,
                    "练习", 0.22, "总结", 0.10
            )), 1e-9);
        }

        @Test
        @DisplayName("负值被截断为 0")
        void negative_clampedToZero() {
            // 极大偏离 -> 负分 -> clamp 为 0
            Map<String, Double> actual = Map.of(
                    "导入", 1.0, "讲授", 0.0, "互动", 0.0,
                    "练习", 0.0, "总结", 0.0
            );
            assertEquals(0.0, engine.calcPhaseDistributionScore(actual), 1e-9);
        }

        @Test
        @DisplayName("超大值被截断为 100")
        void overshoot_clampedToHundred() {
            List<String> veryHigh = List.of("raising_hand", "discussing", "raising_hand", "discussing",
                    "raising_hand", "discussing", "raising_hand", "discussing");
            double score = engine.calcBehavioralEngagement(veryHigh);
            assertEquals(100.0, score, 1e-9);
        }
    }
}
