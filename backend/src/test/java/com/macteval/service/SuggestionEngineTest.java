package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.SuggestionDTO;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * 建议引擎：正式模式只能依据可观测字段触发规则，
 * 不得再用固定倍数派生认知/情感/公平性等未测量构念（BE08）。
 */
class SuggestionEngineTest {

    private final SuggestionEngine engine = new SuggestionEngine(new ObjectMapper());

    private SuggestionEngine.ReportEvidence evidence(
            Double video, Double speech, Double climate,
            int windows, int limited, int unavailable) {
        return new SuggestionEngine.ReportEvidence(
                video, speech, climate,
                windows, windows - limited - unavailable, limited, unavailable,
                video != null && video < 60 ? 1 : 0,
                speech != null && speech < 60 ? 1 : 0,
                0.7, 0.6, 0.4, 180.0,
                video != null, speech != null);
    }

    @Test
    @DisplayName("视频规则分低于 60 生成 high 参与度建议，并引用真实字段")
    void lowVideo_generatesEvidenceBasedSuggestion() {
        var result = engine.generateForReport(evidence(55.0, 70.0, 62.0, 10, 1, 0));
        var video = result.stream().filter(s -> "学生参与度".equals(s.category())).findFirst().orElseThrow();

        assertEquals("high", video.priority());
        assertEquals("video_participation_v1", video.ruleId());
        assertTrue(video.evidence().contains("视频规则分 55.0"));
        assertTrue(video.evidence().contains("端正坐姿比例"));
        assertNotNull(video.evidenceRefs());
    }

    @Test
    @DisplayName("语音规则分低于 65 生成教学质量建议")
    void lowSpeech_generatesTeachingSuggestion() {
        var result = engine.generateForReport(evidence(80.0, 58.0, 69.0, 10, 0, 0));
        var speech = result.stream().filter(s -> "教学质量".equals(s.category())).findFirst().orElseThrow();

        assertEquals("high", speech.priority());
        assertEquals("speech_teaching_v1", speech.ruleId());
        assertTrue(speech.evidence().contains("话语覆盖率"));
        assertTrue(speech.evidence().contains("提问密度"));
    }

    @Test
    @DisplayName("融合分低时只陈述实际偏低的模态，不推断两模态都差")
    void climateEvidenceNamesOnlyObservedLowModality() {
        var result = engine.generateForReport(evidence(55.0, 85.0, 62.0, 10, 0, 0));
        var climate = result.stream().filter(s -> "课堂氛围".equals(s.category())).findFirst().orElseThrow();

        assertTrue(climate.evidence().contains("主要来源：视频"));
        assertFalse(climate.evidence().contains("视频、语音"));
        assertTrue(climate.evidence().contains("语音 85.00"));
    }

    @Test
    @DisplayName("不生成认知/情感/公平性等未测量子维度")
    void neverEmitsUnmeasuredConstructs() {
        var result = engine.generateForReport(evidence(55.0, 58.0, 50.0, 10, 4, 2));
        var categories = result.stream().map(SuggestionDTO::category).toList();

        assertFalse(categories.contains("认知"));
        assertFalse(categories.contains("情感"));
        assertFalse(categories.contains("公平性"));
        assertFalse(categories.contains("秩序"));
        assertTrue(categories.contains("数据质量"), "质量受限应给出工程提示");
    }

    @Test
    @DisplayName("没有任何模态数据时给出证据不足提示，而不是伪造教学结论")
    void noEvidence_returnsInsufficientEvidenceSuggestion() {
        var result = engine.generateForReport(evidence(null, null, null, 0, 0, 0));

        assertEquals(1, result.size());
        assertEquals("数据覆盖", result.getFirst().category());
        assertEquals("insufficient_evidence_v1", result.getFirst().ruleId());
    }

    @Test
    @DisplayName("demo 旧接口只保留明确标注的演示建议")
    void demoGenerateIsExplicitlyDemo() {
        var result = engine.generate(Map.of("classroom_climate", Map.of("activity", 50.0)));

        assertFalse(result.isEmpty());
        assertTrue(result.getFirst().ruleId().startsWith("demo_"));
        assertTrue(result.getFirst().evidence().contains("DEMO"));
    }
}
