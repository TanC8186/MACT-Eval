package com.macteval.service;

import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * 两模态晚期融合测试：仅 video/speech，权重来源 EQUAL/PRIOR/CONFIG，
 * 缺失模态中性填充 50，双缺失返回 null。
 */
class FusionServiceTest {

    private final FusionService equalService = new FusionService();

    @Test
    void equalWeightsAreHalfAndTotalIsWeightedSum() {
        FusionService.FusedScore r = equalService.lateFuse(Map.of(
                FusionService.VIDEO, 100.0, FusionService.SPEECH, 0.0));
        assertEquals(50.0, r.total(), 1e-9);
        assertEquals(2, r.weights().size());
        assertEquals(0.5, r.weights().get(FusionService.VIDEO), 1e-9);
        assertEquals(0.5, r.weights().get(FusionService.SPEECH), 1e-9);
        assertEquals(FusionService.WeightSource.EQUAL, r.weightSource());
    }

    @Test
    void missingSpeechFilledWithNeutralScore() {
        FusionService.FusedScore r = equalService.lateFuse(Map.of(FusionService.VIDEO, 80.0));
        assertEquals(FusionService.NEUTRAL_SCORE, r.scores().get(FusionService.SPEECH), 1e-9);
        // (80 + 50) / 2 = 65
        assertEquals(65.0, r.total(), 1e-9);
    }

    @Test
    void explicitNullValueTreatedAsMissing() {
        Map<String, Double> input = new HashMap<>();
        input.put(FusionService.VIDEO, 90.0);
        input.put(FusionService.SPEECH, null);
        FusionService.FusedScore r = equalService.lateFuse(input);
        assertEquals(FusionService.NEUTRAL_SCORE, r.scores().get(FusionService.SPEECH), 1e-9);
        assertEquals(70.0, r.total(), 1e-9);
    }

    @Test
    void missingVideoFilledWithNeutralScore() {
        FusionService.FusedScore r = equalService.lateFuse(Map.of(FusionService.SPEECH, 100.0));
        assertEquals(FusionService.NEUTRAL_SCORE, r.scores().get(FusionService.VIDEO), 1e-9);
        assertEquals(75.0, r.total(), 1e-9);
    }

    @Test
    void allModalitiesMissingReturnsNull() {
        assertNull(equalService.lateFuse(Map.of()));
        assertNull(equalService.lateFuse(null));
    }

    @Test
    void scoreClampedToZeroHundred() {
        FusionService.FusedScore r = equalService.lateFuse(Map.of(
                FusionService.VIDEO, 150.0, FusionService.SPEECH, -20.0));
        assertEquals(100.0, r.scores().get(FusionService.VIDEO), 1e-9);
        assertEquals(0.0, r.scores().get(FusionService.SPEECH), 1e-9);
        assertEquals(50.0, r.total(), 1e-9);
    }

    @Test
    void contributionsSumToTotal() {
        FusionService.FusedScore r = equalService.lateFuse(Map.of(
                FusionService.VIDEO, 60.0, FusionService.SPEECH, 90.0));
        double sum = r.contributions().values().stream().mapToDouble(Double::doubleValue).sum();
        assertEquals(r.total(), sum, 1e-9);
        assertEquals(75.0, r.total(), 1e-9);
    }

    @Test
    void priorWeightsUseSixtyForty() {
        FusionService service = new FusionService(
                FusionService.WeightSource.PRIOR, 0.6, 0.4, null, null);
        FusionService.FusedScore r = service.lateFuse(Map.of(
                FusionService.VIDEO, 100.0, FusionService.SPEECH, 0.0));
        assertEquals(0.6, r.weights().get(FusionService.VIDEO), 1e-9);
        assertEquals(0.4, r.weights().get(FusionService.SPEECH), 1e-9);
        assertEquals(60.0, r.total(), 1e-9);
        assertEquals(FusionService.WeightSource.PRIOR, r.weightSource());
    }

    @Test
    void priorFallsBackToDefaultSixtyFortyWhenConfigMissing() {
        FusionService service = new FusionService(
                FusionService.WeightSource.PRIOR, null, null, null, null);
        FusionService.FusedScore r = service.lateFuse(Map.of(
                FusionService.VIDEO, 80.0, FusionService.SPEECH, 30.0));
        assertEquals(0.6, r.weights().get(FusionService.VIDEO), 1e-9);
        assertEquals(0.4, r.weights().get(FusionService.SPEECH), 1e-9);
        // 0.6*80 + 0.4*30 = 60
        assertEquals(60.0, r.total(), 1e-9);
        assertEquals(FusionService.WeightSource.PRIOR, r.weightSource());
    }

    @Test
    void configWeightsAreNormalized() {
        FusionService service = new FusionService(
                FusionService.WeightSource.CONFIG, 0.6, 0.4, 0.3, 0.1);
        FusionService.FusedScore r = service.lateFuse(Map.of(
                FusionService.VIDEO, 100.0, FusionService.SPEECH, 0.0));
        assertEquals(0.75, r.weights().get(FusionService.VIDEO), 1e-9);
        assertEquals(0.25, r.weights().get(FusionService.SPEECH), 1e-9);
        assertEquals(75.0, r.total(), 1e-9);
        assertEquals(FusionService.WeightSource.CONFIG, r.weightSource());
    }

    @Test
    void configWithoutUsableWeightsFallsBackToEqual() {
        FusionService service = new FusionService(
                FusionService.WeightSource.CONFIG, 0.6, 0.4, 0.0, null);
        FusionService.FusedScore r = service.lateFuse(Map.of(
                FusionService.VIDEO, 100.0, FusionService.SPEECH, 0.0));
        assertEquals(0.5, r.weights().get(FusionService.VIDEO), 1e-9);
        assertEquals(0.5, r.weights().get(FusionService.SPEECH), 1e-9);
        assertEquals(FusionService.WeightSource.EQUAL, r.weightSource());
    }

    @Test
    void configWithOnlyOneWeightFallsBackToEqual() {
        FusionService service = new FusionService(
                FusionService.WeightSource.CONFIG, 0.6, 0.4, 0.7, null);
        FusionService.FusedScore r = service.lateFuse(Map.of(
                FusionService.VIDEO, 100.0, FusionService.SPEECH, 0.0));
        assertEquals(0.5, r.weights().get(FusionService.VIDEO), 1e-9);
        assertEquals(0.5, r.weights().get(FusionService.SPEECH), 1e-9);
        assertEquals(FusionService.WeightSource.EQUAL, r.weightSource());
    }

    @Test
    void unknownWeightSourceFallsBackToEqual() {
        FusionService service = new FusionService("bogus", 0.6, 0.4, null, null);
        FusionService.FusedScore r = service.lateFuse(Map.of(
                FusionService.VIDEO, 100.0, FusionService.SPEECH, 0.0));
        assertEquals(0.5, r.weights().get(FusionService.VIDEO), 1e-9);
        assertEquals(FusionService.WeightSource.EQUAL, r.weightSource());
    }

    @Test
    void learnedWeightsUseConfigAndKeepLearnedSource() {
        FusionService service = new FusionService(
                FusionService.WeightSource.LEARNED, 0.6, 0.4, 0.7, 0.3);
        FusionService.FusedScore r = service.lateFuse(Map.of(
                FusionService.VIDEO, 100.0, FusionService.SPEECH, 0.0));
        assertEquals(0.7, r.weights().get(FusionService.VIDEO), 1e-9);
        assertEquals(0.3, r.weights().get(FusionService.SPEECH), 1e-9);
        assertEquals(70.0, r.total(), 1e-9);
        assertEquals(FusionService.WeightSource.LEARNED, r.weightSource());
    }

    @Test
    void learnedWeightsWithoutConfigFallBackToEqual() {
        FusionService service = new FusionService(
                FusionService.WeightSource.LEARNED, 0.6, 0.4, null, null);
        FusionService.FusedScore r = service.lateFuse(Map.of(
                FusionService.VIDEO, 100.0, FusionService.SPEECH, 0.0));
        assertEquals(FusionService.WeightSource.EQUAL, r.weightSource());
    }
}
