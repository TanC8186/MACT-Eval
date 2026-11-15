package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.SuggestionDTO;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;

/**
 * 教学建议规则引擎。
 *
 * <p>正式模式只依据已观测字段（视频规则分、语音规则分、融合分、窗口质量、
 * 话语指标）触发规则；不再用固定倍数派生认知/情感/公平性等未测量构念。
 * 每条建议都携带 ruleId、规则版本与证据引用，报告、待办、采纳、效果追踪
 * 使用同一个稳定 ID。</p>
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class SuggestionEngine {

    public static final String RULE_VERSION = "1.0";

    private final ObjectMapper objectMapper;

    /**
     * 报告证据快照：所有字段都来自窗口特征或转写聚合，可直接回溯。
     */
    public record ReportEvidence(
            Double videoScore,
            Double speechScore,
            Double climateScore,
            int windowCount,
            int usableWindows,
            int limitedWindows,
            int unavailableWindows,
            int lowVideoWindows,
            int lowSpeechWindows,
            Double avgUpright,
            Double avgSpeechCoverage,
            Double avgQuestionDensity,
            Double avgSpeechRate,
            boolean videoAvailable,
            boolean speechAvailable
    ) {}

    public List<SuggestionDTO> generateForReport(ReportEvidence e) {
        List<SuggestionDTO> suggestions = new ArrayList<>();

        // 1. 学生参与度（视频规则分）
        if (e.videoScore() != null) {
            String priority = e.videoScore() < 60 ? "high" : e.videoScore() < 75 ? "medium" : "low";
            String content = switch (priority) {
                case "high" -> String.format(Locale.ROOT,
                        "视频规则分 %.1f 偏低，建议增加互动环节并关注可观测座位内的参与状态", e.videoScore());
                case "medium" -> String.format(Locale.ROOT,
                        "视频规则分 %.1f，有提升空间，可考虑加入更多随堂练习", e.videoScore());
                default -> String.format(Locale.ROOT,
                        "视频规则分 %.1f 表现良好，建议保持当前课堂组织方式", e.videoScore());
            };
            String evidence = String.format(Locale.ROOT,
                    "视频规则分 %.1f（端正坐姿比例均值 %s；%d 个窗口，其中 %d 个低于 60 分）",
                    e.videoScore(), format(e.avgUpright()), e.windowCount(), e.lowVideoWindows());
            suggestions.add(build("video_participation_v1", "学生参与度", priority, content, evidence,
                    evidenceRefs("video", nullableMap(
                            "video_score", e.videoScore(),
                            "avg_upright_proportion", e.avgUpright(),
                            "window_count", e.windowCount(),
                            "low_video_windows", e.lowVideoWindows()))));
        }

        // 2. 教学质量（语音话语规则分）
        if (e.speechScore() != null) {
            String priority = e.speechScore() < 65 ? "high" : e.speechScore() < 80 ? "medium" : "low";
            String content = switch (priority) {
                case "high" -> String.format(Locale.ROOT,
                        "语音规则分 %.1f 偏低，建议检查讲解节奏并增加提问互动", e.speechScore());
                case "medium" -> String.format(Locale.ROOT,
                        "语音规则分 %.1f，建议针对提问密度或语速做小幅调整", e.speechScore());
                default -> String.format(Locale.ROOT,
                        "语音规则分 %.1f 良好，讲解与提问节奏稳定", e.speechScore());
            };
            String evidence = String.format(Locale.ROOT,
                    "语音规则分 %.1f（话语覆盖率 %s；提问密度 %s 问/分钟；语速 %s 字/分钟；%d 个窗口）",
                    e.speechScore(), formatPercent(e.avgSpeechCoverage()),
                    format(e.avgQuestionDensity()), format(e.avgSpeechRate()), e.windowCount());
            suggestions.add(build("speech_teaching_v1", "教学质量", priority, content, evidence,
                    evidenceRefs("speech", nullableMap(
                            "speech_score", e.speechScore(),
                            "avg_speech_coverage", e.avgSpeechCoverage(),
                            "avg_question_density", e.avgQuestionDensity(),
                            "avg_speech_rate", e.avgSpeechRate(),
                            "window_count", e.windowCount(),
                            "low_speech_windows", e.lowSpeechWindows()))));
        }

        // 3. 课堂氛围（视频 + 语音晚期融合分）
        if (e.climateScore() != null) {
            String priority = e.climateScore() < 60 ? "high" : e.climateScore() < 75 ? "medium" : "low";
            String content = switch (priority) {
                case "high" -> String.format(Locale.ROOT,
                        "课堂氛围融合分 %.1f 偏低，建议增加鼓励性评价并关注课堂节奏", e.climateScore());
                case "medium" -> String.format(Locale.ROOT,
                        "课堂氛围融合分 %.1f，建议关注低分窗口的教学安排", e.climateScore());
                default -> String.format(Locale.ROOT,
                        "课堂氛围融合分 %.1f 表现良好，建议保持", e.climateScore());
            };
            List<String> lowModalities = new ArrayList<>();
            if (e.videoScore() != null && e.videoScore() < 60) lowModalities.add("视频");
            if (e.speechScore() != null && e.speechScore() < 60) lowModalities.add("语音");
            String modalityText = lowModalities.isEmpty()
                    ? "融合分偏低但单模态规则分均未低于 60，需结合低分窗口人工复核"
                    : "主要来源：" + String.join("、", lowModalities) + "规则分低于 60";
            String evidence = String.format(Locale.ROOT,
                    "融合分 %.1f（视频 %s，语音 %s；%d 个窗口）；%s",
                    e.climateScore(), format(e.videoScore()), format(e.speechScore()),
                    e.windowCount(), modalityText);
            suggestions.add(build("climate_fusion_v1", "课堂氛围", priority, content, evidence,
                    evidenceRefs("fusion", nullableMap(
                            "climate_score", e.climateScore(),
                            "video_score", e.videoScore(),
                            "speech_score", e.speechScore(),
                            "window_count", e.windowCount()))));
        }

        // 4. 数据质量/覆盖（工程提示，不冒充教学构念）
        int degraded = e.limitedWindows() + e.unavailableWindows();
        if (e.windowCount() > 0 && degraded * 2 >= e.windowCount()) {
            String evidence = String.format(Locale.ROOT,
                    "%d/%d 个窗口质量受限或不可用；视频可用=%s，语音可用=%s",
                    degraded, e.windowCount(), e.videoAvailable(), e.speechAvailable());
            suggestions.add(build("data_quality_v1", "数据质量", "medium",
                    "本次分析中超过一半窗口质量受限，建议调整摄像头/麦克风位置或重新采集后再解读评分",
                    evidence,
                    evidenceRefs("quality", nullableMap(
                            "window_count", e.windowCount(),
                            "degraded_windows", degraded,
                            "video_available", e.videoAvailable(),
                            "speech_available", e.speechAvailable()))));
        }

        if (suggestions.isEmpty()) {
            suggestions.add(build("insufficient_evidence_v1", "数据覆盖", "low",
                    "当前可观测证据不足以生成针对性教学建议，请补充视频/语音后重新生成",
                    "没有可用的视频或语音规则分窗口",
                    evidenceRefs("coverage", nullableMap(
                            "window_count", e.windowCount(),
                            "video_available", e.videoAvailable(),
                            "speech_available", e.speechAvailable()))));
        }

        suggestions.sort((a, b) -> priorityRank(a.priority()) - priorityRank(b.priority()));
        return suggestions;
    }

    /**
     * 仅用于 DEMO 模式的旧接口；正式模式禁止使用未测量的派生维度。
     */
    public List<SuggestionDTO> generate(Map<String, Map<String, Double>> dimensionScores) {
        List<SuggestionDTO> suggestions = new ArrayList<>();
        for (var dimEntry : dimensionScores.entrySet()) {
            String dimension = dimEntry.getKey();
            double avg = dimEntry.getValue().values().stream()
                    .mapToDouble(Double::doubleValue).average().orElse(0);
            if (dimension.equals("classroom_climate") && avg < 60) {
                suggestions.add(build("demo_climate_v1", "课堂氛围", "high",
                        "课堂氛围需要改善（DEMO），建议增加鼓励性评价和积极反馈",
                        "DEMO 固定演示数据，不代表真实观测", null));
            }
        }
        return suggestions;
    }

    public List<SuggestionDTO> getDefaultSuggestions() {
        return List.of(
                build("demo_default_v1", "学生参与度", "medium", "建议增加互动环节", "默认演示建议", null),
                build("demo_default_v2", "教学质量", "low", "当前教学节奏合理", "默认演示建议", null)
        );
    }

    private SuggestionDTO build(String ruleId, String category, String priority,
                                String content, String evidence, String evidenceRefs) {
        return new SuggestionDTO(
                UUID.randomUUID().toString(),
                category,
                priority,
                content,
                evidence,
                "pending",
                null,
                null,
                ruleId,
                RULE_VERSION,
                evidenceRefs
        );
    }

    /** 允许 null 值的 Map 构造器（Map.of 不允许 null，但缺失模态需要显式记录）。 */
    private static Map<String, Object> nullableMap(Object... keyValues) {
        Map<String, Object> map = new LinkedHashMap<>();
        for (int i = 0; i + 1 < keyValues.length; i += 2) {
            map.put(String.valueOf(keyValues[i]), keyValues[i + 1]);
        }
        return map;
    }

    private String evidenceRefs(String kind, Map<String, Object> values) {
        try {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("kind", kind);
            payload.put("fields", values);
            return objectMapper.writeValueAsString(payload);
        } catch (Exception e) {
            log.debug("Could not serialize suggestion evidence refs", e);
            return null;
        }
    }

    private static int priorityRank(String priority) {
        return switch (priority == null ? "" : priority) {
            case "high" -> 0;
            case "medium" -> 1;
            default -> 2;
        };
    }

    private static String format(Double value) {
        return value == null ? "不可用" : String.format(Locale.ROOT, "%.2f", value);
    }

    private static String formatPercent(Double value) {
        return value == null ? "不可用" : String.format(Locale.ROOT, "%.0f%%", value * 100);
    }
}
