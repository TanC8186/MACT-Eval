package com.macteval.dto;

import java.time.LocalDateTime;
import java.util.List;

public record ReportDetailDTO(
        String id,
        String lessonSessionId,
        String courseName,
        String className,
        String teacherName,
        String title,
        String summary,
        Double engagementScore,
        Double teachingScore,
        Double climateScore,
        Double overallScore,
        String dataCoverage,
        String attendanceSummary,
        String behaviorSummary,
        String quizStatistics,
        String assignmentSummary,
        String feedbackSummary,
        List<SuggestionDTO> suggestions,
        String dataSource,
        Evidence evidence,
        String status,
        LocalDateTime publishedAt,
        LocalDateTime createdAt,
        java.util.Map<String, String> scoreValidation,
        Integer version,
        String analysisVideoJobId,
        String analysisAudioJobId,
        String publishedBy
) {
    /**
     * 证据面板 — 描述报告分数是从哪些输入、哪个模型版本、多少覆盖率计算出来的。
     *
     * @param dataSource    数据来源: REAL(真实课堂证据) / DEMO(演示数据) / EMPTY(无数据)
     * @param modelVersion  使用的模型版本, 如 "rule-engine-1.0"
     * @param coverage      检测覆盖率, 取值 0-1
     * @param windowCount   分析的时间窗口数
     * @param featureCount  参与计算的模态特征/信号数
     */
    public record Evidence(
            String dataSource,
            String modelVersion,
            Double coverage,
            Integer windowCount,
            Integer featureCount,
            java.util.Map<String, Object> traceability
    ) {
        @Deprecated
        public Evidence(String dataSource, String modelVersion, Double coverage,
                        Integer windowCount, Integer featureCount) {
            this(dataSource, modelVersion, coverage, windowCount, featureCount, java.util.Map.of());
        }
    }
}
