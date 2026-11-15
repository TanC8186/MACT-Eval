package com.macteval.dto;

import java.util.List;

/**
 * 学生导入的结构化结果：成功/幂等跳过/校验错误逐行可定位。
 */
public record StudentImportResult(
        int imported,
        int skipped,
        List<Row> rows
) {
    public record Row(
            int rowNumber,
            String studentId,
            String status,
            String message
    ) {}
}
