package com.macteval.dto;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

public record LessonTranscriptDTO(
        String id,
        String lessonSessionId,
        String text,
        String language,
        Double languageProbability,
        List<Map<String, Object>> segments,
        String modelVersion,
        LocalDateTime createdAt
) {}
