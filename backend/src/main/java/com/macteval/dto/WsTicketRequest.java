package com.macteval.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * WebSocket 一次性票据申请（SEC-02，2026-10-28）。
 *
 * @param lessonSessionId 目标课堂。票据会与该课堂绑定，握手时 URL 里的
 *                       lessonId 必须与之一致，否则 403——不能用一个课堂的
 *                       票据去连另一个课堂。
 */
public record WsTicketRequest(
        @NotBlank @Size(max = 64) String lessonSessionId
) {}
