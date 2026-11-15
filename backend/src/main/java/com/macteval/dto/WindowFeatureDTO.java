package com.macteval.dto;

import java.time.LocalDateTime;
import java.util.Map;

public record WindowFeatureDTO(
        String id,
        String lessonSessionId,
        Integer windowIndex,
        LocalDateTime windowStart,
        LocalDateTime windowEnd,
        Double videoScore,
        Double speechScore,
        Double fusedScore,
        Map<String, Object> videoFeatures,
        Map<String, Object> speechFeatures,
        java.util.List<EvidenceFrameDTO> evidenceFrames) {
}
