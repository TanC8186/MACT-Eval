package com.macteval.service;

import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * 语音话语指标计算（规则可计算、可复核，导师建议核心模态二落点）。
 * 从 Whisper 转写分段 [{start, end, text}] 按统一时间窗聚合：
 * 话语覆盖率、提问句数、平均语速。
 */
public final class SpeechMetricsCalculator {

    /** 统一时间窗粒度（秒）——导师建议的统一时间窗设计 */
    public static final double WINDOW_SECONDS = 60.0;

    /** 中文疑问句标记（句末标点 + 常见疑问词） */
    private static final Pattern QUESTION_MARKER = Pattern.compile("[？?]|吗|呢|吧|谁|什么|怎么|为什么|如何|几|多少|哪");

    private SpeechMetricsCalculator() {
    }

    public record WindowSpeechMetrics(
            double speechCoverage,  // 0-1：窗口内语音时长占比
            int questionCount,      // 窗口内含疑问标记的转写分段数
            double speechRate       // 字/分钟：窗口内平均语速（无语音为 0）
    ) {
    }

    /**
     * 计算窗口内的语音话语指标。跨窗口边界的分段按重叠时长比例计入。
     *
     * @param segments       转写分段 [{start, end, text}]（秒）
     * @param windowStartSec 窗口起点（秒）
     * @param windowEndSec   窗口终点（秒）
     */
    public static WindowSpeechMetrics windowMetrics(List<Map<String, Object>> segments, double windowStartSec, double windowEndSec) {
        if (segments == null || segments.isEmpty()) {
            return new WindowSpeechMetrics(0.0, 0, 0.0);
        }
        double overlapSeconds = 0;
        int questionCount = 0;
        int totalChars = 0;
        for (Map<String, Object> seg : segments) {
            double start = num(seg.get("start"));
            double end = num(seg.get("end"));
            if (end <= windowStartSec || start >= windowEndSec) continue;
            double overlap = Math.min(end, windowEndSec) - Math.max(start, windowStartSec);
            if (overlap <= 0) continue;
            overlapSeconds += overlap;
            String text = String.valueOf(seg.getOrDefault("text", ""));
            totalChars += text.length();
            if (QUESTION_MARKER.matcher(text).find()) questionCount++;
        }
        double windowDuration = windowEndSec - windowStartSec;
        double coverage = windowDuration <= 0 ? 0.0 : Math.min(1.0, overlapSeconds / windowDuration);
        double rate = overlapSeconds <= 0 ? 0.0 : totalChars / (overlapSeconds / 60.0);
        return new WindowSpeechMetrics(coverage, questionCount, rate);
    }

    private static double num(Object v) {
        if (v == null) return 0;
        return v instanceof Number n ? n.doubleValue() : Double.parseDouble(String.valueOf(v));
    }
}
