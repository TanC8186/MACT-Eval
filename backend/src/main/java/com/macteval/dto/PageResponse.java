package com.macteval.dto;

import java.util.List;

/** 统一分页响应（服务端分页，避免列表全量传输）。 */
public record PageResponse<T>(
        List<T> items,
        int page,
        int size,
        long total
) {}
