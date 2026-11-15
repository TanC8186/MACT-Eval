package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.BehaviorObservationEntity;
import com.macteval.model.entity.LessonWindowFeatureEntity;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class WindowQualityMetadataTest {
    @Test
    void videoFeatureMetadataIncludesWorstWindowQuality() throws Exception {
        BehaviorObservationEntity first = new BehaviorObservationEntity();
        first.setAggregatedUprightProportion("0.8");
        first.setDataQuality("usable");
        BehaviorObservationEntity second = new BehaviorObservationEntity();
        second.setAggregatedUprightProportion("0.6");
        second.setDataQuality("unavailable");

        Map<String, Object> metadata = WindowAlignmentService.videoFeatureMetadata(List.of(first, second));
        assertEquals("unavailable", metadata.get("data_quality"));
        assertEquals(2, metadata.get("observation_count"));
    }
}
