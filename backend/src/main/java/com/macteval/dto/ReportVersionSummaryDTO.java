package com.macteval.dto;

import java.time.LocalDateTime;

/**
 * 已发布报告版本摘要（不含快照正文）。
 *
 * @param dataSource   该版本的数据来源（REAL / DEMO / EMPTY），取自快照正文；
 *                     旧快照缺失该字段时为 {@code null}，不回填默认值
 * @param climateScore 该版本的课堂氛围融合分，取自快照正文；
 *                     旧快照缺失时为 {@code null}，不回填默认值
 */
public record ReportVersionSummaryDTO(
        Integer version,
        String status,
        LocalDateTime publishedAt,
        String publishedBy,
        String contentHash,
        String analysisVideoJobId,
        String analysisAudioJobId,
        String dataSource,
        Double climateScore
) {}
