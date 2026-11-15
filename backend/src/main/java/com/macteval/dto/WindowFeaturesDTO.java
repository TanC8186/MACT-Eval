package com.macteval.dto;

import java.util.List;

public record WindowFeaturesDTO(
        String lessonSessionId,
        List<WindowFeatureDTO> windows,
        SpeechMetricsDTO speechMetrics) {
}
