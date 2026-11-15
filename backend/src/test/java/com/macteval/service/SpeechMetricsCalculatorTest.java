package com.macteval.service;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class SpeechMetricsCalculatorTest {

    private List<Map<String, Object>> segments() {
        return List.of(
                Map.of("start", 0.0, "end", 30.0, "text", "今天我们学习第三章"),
                Map.of("start", 35.0, "end", 65.0, "text", "这个问题谁来回答？"),
                Map.of("start", 70.0, "end", 80.0, "text", "好的，同学们认真思考")
        );
    }

    @Test
    void coverageAndQuestionCountForFullWindow() {
        var m = SpeechMetricsCalculator.windowMetrics(segments(), 0.0, 60.0);
        // 窗口内重叠: 30 + 25 = 55 秒
        assertEquals(55.0 / 60.0, m.speechCoverage(), 1e-9);
        assertEquals(1, m.questionCount()); // "这个问题谁来回答？"
    }

    @Test
    void segmentCrossingWindowBoundaryCountedProportionally() {
        // [0,60) 窗口: 段2 [35,65) 只算 25 秒
        var m = SpeechMetricsCalculator.windowMetrics(segments(), 0.0, 60.0);
        assertEquals(25.0 / 60.0, m.speechCoverage() - 30.0 / 60.0, 1e-9);
    }

    @Test
    void emptySegmentsYieldsZeroMetrics() {
        var m = SpeechMetricsCalculator.windowMetrics(List.of(), 0.0, 60.0);
        assertEquals(0.0, m.speechCoverage());
        assertEquals(0, m.questionCount());
        assertEquals(0.0, m.speechRate());
    }

    @Test
    void nonOverlappingSegmentsIgnored() {
        var m = SpeechMetricsCalculator.windowMetrics(segments(), 100.0, 160.0);
        assertEquals(0.0, m.speechCoverage());
        assertEquals(0, m.questionCount());
    }

    @Test
    void questionDetectionCoversChineseMarkers() {
        List<Map<String, Object>> segs = List.of(
                Map.of("start", 0.0, "end", 10.0, "text", "为什么会出现这个结果？"),
                Map.of("start", 10.0, "end", 20.0, "text", "大家知道吗"),
                Map.of("start", 20.0, "end", 30.0, "text", "谁来回答？")
        );
        var m = SpeechMetricsCalculator.windowMetrics(segs, 0.0, 30.0);
        assertEquals(3, m.questionCount());
    }

    @Test
    void speechRateMeasuredInCharsPerMinute() {
        // 10 秒 20 字 → 120 字/分钟
        List<Map<String, Object>> segs = List.of(Map.of("start", 0.0, "end", 10.0, "text", "一二三四五六七八九十一二三四五六七八九十"));
        var m = SpeechMetricsCalculator.windowMetrics(segs, 0.0, 10.0);
        assertEquals(120.0, m.speechRate(), 1e-9);
    }
}
