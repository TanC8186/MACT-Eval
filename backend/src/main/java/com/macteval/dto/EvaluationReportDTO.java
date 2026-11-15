package com.macteval.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

/**
 * 课堂评价报告 DTO — 使用 Java 21 Record 实现不可变数据传输。
 * Record 模式相比 Lombok @Data：天然不可变、更安全、更简洁、自动支持 equals/hashCode/toString。
 *
 * <p>每个维度分数通过 {@link #evidence()} 溯源到数据来源、模型版本、覆盖率与窗口数，
 * 满足「每个报告指标都可追溯到输入/模型版本/覆盖率」的审计验收标准。
 */
public record EvaluationReportDTO(
        String lessonId,
        LocalDateTime timestamp,
        @Valid Map<String, DimensionScore> dimensions,
        @Valid List<TimelineEvent> timeline,
        List<String> suggestions,
        String dataSource,
        @Valid Evidence evidence
) {
    /**
     * 证据面板 — 描述报告分数是从哪些输入、哪个模型版本、多少覆盖率计算出来的。
     *
     * @param dataSource    数据来源: REAL(真实课堂证据) / DEMO(演示数据) / EMPTY(无数据)
     * @param modelVersion  使用的模型版本, 如 "rule-engine-1.0" / "opencv-haar-0.1"
     * @param coverage      检测覆盖率, 取值 0-1
     * @param windowCount   分析的时间窗口数
     * @param featureCount  参与计算的模态特征/信号数
     */
    public record Evidence(
            String dataSource,
            String modelVersion,
            @Min(0) @Max(1) double coverage,
            @Min(0) int windowCount,
            @Min(0) int featureCount
    ) {}

    public record DimensionScore(
            @Min(0) @Max(100) double score,
            List<String> highlights,
            String evidenceNote
    ) {}

    public record TimelineEvent(
            @Min(0) int minute,
            @NotBlank String phase,
            @Min(0) @Max(100) double engagementScore,
            String note
    ) {}
}
