package com.macteval.dto;

import java.time.LocalDateTime;

public record BehaviorDTO(
        String id,
        String lessonSessionId,
        LocalDateTime windowStart,
        LocalDateTime windowEnd,
        Integer visibleSeatCount,
        Integer totalConfiguredSeats,
        Double coverage,
        String dataQuality,
        Integer inSeatCount,
        Integer leaveSeatCount,
        Integer handRaisedCount,
        Integer standingCount,
        Integer uprightPostureCount,
        Double uprightProportion,
        LocalDateTime createdAt
) {}
