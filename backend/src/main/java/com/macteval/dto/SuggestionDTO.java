package com.macteval.dto;

import java.time.LocalDateTime;

/**
 * 教学建议 DTO — 包含分类、优先级、内容、数据依据和状态追踪。
 * ruleId/ruleVersion/evidenceRefs 用于把每条建议回溯到具体规则与观测字段。
 */
public record SuggestionDTO(
        String id,
        String category,
        String priority,
        String content,
        String evidence,
        String status,
        LocalDateTime createdAt,
        LocalDateTime resolvedAt,
        String ruleId,
        String ruleVersion,
        String evidenceRefs
) {}
