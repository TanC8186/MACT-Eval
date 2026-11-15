package com.macteval.service;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * 规则基线测试：实现 spec 第 5 节公式与缺失分量重归一化。
 * 参见 docs/experiments/rule-baseline-protocol.md。
 */
class RuleBaselineServiceTest {

    private final RuleBaselineService service = new RuleBaselineService();

    // ---- 视频规则分 ----

    @Test
    void videoScoreUsesWeightedMean() {
        // 100 × (0.5×0.8 + 0.2×0.6 + 0.3×0.5) = 67
        Double score = service.videoScore(0.8, 0.6, 0.5);
        assertNotNull(score);
        assertEquals(67.0, score, 1e-9);
    }

    @Test
    void videoScoreRenormalizesWhenStabilityMissing() {
        // (0.5×0.8 + 0.3×0.5) / (0.5+0.3) = 0.6875 → 68.75
        Double score = service.videoScore(0.8, null, 0.5);
        assertNotNull(score);
        assertEquals(68.75, score, 1e-9);
    }

    @Test
    void videoScoreRenormalizesWhenOnlyPostureAvailable() {
        Double score = service.videoScore(0.7, null, null);
        assertNotNull(score);
        assertEquals(70.0, score, 1e-9);
    }

    @Test
    void videoScoreNullWhenAllComponentsMissing() {
        assertNull(service.videoScore(null, null, null));
    }

    @Test
    void videoScoreClampsComponentsToZeroOne() {
        // posture 超出上界按 1，positive 为负按 0：
        // (0.5×1 + 0.2×1 + 0.3×0) / 1.0 = 0.7 → 70
        Double score = service.videoScore(1.5, 1.0, -0.5);
        assertNotNull(score);
        assertEquals(70.0, score, 1e-9);
    }

    // ---- 语音规则分 ----

    @Test
    void speechScoreUsesWeightedMean() {
        // coverage 0.8, 2 问/60s → density 1, rate 200 → rate_score 1
        // 100 × (0.5×0.8 + 0.3×1 + 0.2×1) = 90
        Double score = service.speechScore(0.8, 2, 200.0, 60.0);
        assertNotNull(score);
        assertEquals(90.0, score, 1e-9);
    }

    @Test
    void speechScoreRenormalizesWhenCoverageMissing() {
        // (0.3×1 + 0.2×1) / (0.3+0.2) = 1 → 100
        Double score = service.speechScore(null, 1, 200.0, 60.0);
        assertNotNull(score);
        assertEquals(100.0, score, 1e-9);
    }

    @Test
    void speechScoreQuestionDensitySaturatesAtOnePerMinute() {
        // 3 问/60s = 3 问/分钟，min(1, 3/1) = 1
        Double score = service.speechScore(1.0, 3, 200.0, 60.0);
        assertNotNull(score);
        assertEquals(100.0, score, 1e-9);
    }

    @Test
    void speechScoreQuestionDensityBelowSaturation() {
        // 1 问/120s = 0.5 问/分钟 → density 0.5
        // 100 × (0.5×1 + 0.3×0.5 + 0.2×1) = 85
        Double score = service.speechScore(1.0, 1, 200.0, 120.0);
        assertNotNull(score);
        assertEquals(85.0, score, 1e-9);
    }

    @Test
    void speechScoreRateScoreBoundaryAndDecay() {
        // 150-300 字/分钟为满分；两侧线性衰减。
        assertEquals(100.0, service.speechScore(1.0, 1, 150.0, 60.0), 1e-9);
        assertEquals(100.0, service.speechScore(1.0, 1, 300.0, 60.0), 1e-9);
        // rate 75 → 1 - (150-75)/150 = 0.5
        // 100 × (0.5×1 + 0.3×1 + 0.2×0.5) = 90
        assertEquals(90.0, service.speechScore(1.0, 1, 75.0, 60.0), 1e-9);
        // rate 450 → 1 - (450-300)/300 = 0.5 → 90
        assertEquals(90.0, service.speechScore(1.0, 1, 450.0, 60.0), 1e-9);
        // rate 0 → 0；rate ≥ 600 → 0
        // 100 × (0.5×1 + 0.3×1 + 0.2×0) = 80
        assertEquals(80.0, service.speechScore(1.0, 1, 0.0, 60.0), 1e-9);
        assertEquals(80.0, service.speechScore(1.0, 1, 600.0, 60.0), 1e-9);
    }

    @Test
    void speechScoreNullWhenAllComponentsMissing() {
        assertNull(service.speechScore(null, null, null, 60.0));
    }

    @Test
    void speechScoreNullWhenWindowInvalid() {
        // 窗口长度非正属于非法输入：返回 null，不能把问题密度当作缺失后
        // 用 coverage/rate 继续计算（协议 4.4）。
        assertNull(service.speechScore(0.6, 5, null, 0.0));
        assertNull(service.speechScore(0.6, 5, 200.0, -1.0));
        assertNull(service.speechScore(0.6, 5, 200.0, Double.NaN));
    }

    // ---- 融合 ----

    @Test
    void fuseNormalizesWeights() {
        // (0.6×80 + 0.4×60) / 1 = 72
        assertEquals(72.0, service.fuse(80, 60, 0.6, 0.4), 1e-9);
        // 3:1 等价于 0.75/0.25 → 0.75×80 + 0.25×60 = 75
        assertEquals(75.0, service.fuse(80, 60, 3, 1), 1e-9);
    }

    @Test
    void fuseFallsBackToEqualWhenWeightsUnusable() {
        assertEquals(70.0, service.fuse(80, 60, 0, 0), 1e-9);
        assertEquals(70.0, service.fuse(80, 60, -1, Double.NaN), 1e-9);
    }

    @Test
    void fuseClampsScoresToZeroHundred() {
        // video 120 → 100，(100+30)/2 = 65
        assertEquals(65.0, service.fuse(120, 30, 0.5, 0.5), 1e-9);
        // video -20 → 0，(0+80)/2 = 40
        assertEquals(40.0, service.fuse(-20, 80, 0.5, 0.5), 1e-9);
    }
}
