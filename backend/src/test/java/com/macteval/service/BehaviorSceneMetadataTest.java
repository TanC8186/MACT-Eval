package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.model.entity.BehaviorObservationEntity;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;

class BehaviorSceneMetadataTest {
    @Test
    void usableSceneNeverUpgradesUnsupportedBehaviorMeasurements() {
        BehaviorObservationEntity observation = new BehaviorObservationEntity();
        observation.setDataQuality("unavailable");
        Map<String, Object> result = Map.of("window_scene_metadata", java.util.List.of(
                Map.of("window_index", 0, "data_quality", "usable")));
        BehaviorService.applySceneQuality(observation, 0L, result);
        assertEquals("unavailable", observation.getDataQuality());
    }

    @Test
    void videoObservationUsesWindowSceneQualityWhenPresent() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        Map<String, Object> result = mapper.readValue("{\"window_scene_metadata\":[{\"window_index\":2,\"data_quality\":\"unavailable\",\"dominant_scene\":\"slides_or_screen\"}]}", Map.class);
        BehaviorObservationEntity observation = new BehaviorObservationEntity();
        BehaviorService.applySceneQuality(observation, 120000L, result);
        assertEquals("unavailable", observation.getDataQuality());
    }

    @Test
    void explicitWindowIndexWinsOverStartOffset() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        Map<String, Object> result = mapper.readValue(
                "{\"window_scene_metadata\":[{\"window_index\":1,\"data_quality\":\"limited\"}]}", Map.class);
        BehaviorObservationEntity observation = new BehaviorObservationEntity();
        // 重叠窗口边界时，startMs 反推可能得到相邻索引；显式 window_index 必须优先。
        observation.setDataQuality("usable");
        BehaviorService.applySceneQuality(observation, 30_000L, 1, 30_000L, result);
        assertEquals("limited", observation.getDataQuality());
    }

    @Test
    void qualityIndexUsesConfiguredWindowDuration() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        Map<String, Object> result = mapper.readValue(
                "{\"window_scene_metadata\":[{\"window_index\":3,\"data_quality\":\"unavailable\"}]}", Map.class);
        BehaviorObservationEntity observation = new BehaviorObservationEntity();
        // 30s 窗口下，90s 对应索引 3；若固定按 60s 换算会得到索引 1 而漏映射。
        BehaviorService.applySceneQuality(observation, 90_000L, null, 30_000L, result);
        assertEquals("unavailable", observation.getDataQuality());
    }

    @Test
    void missingSceneMetadataKeepsDetectorQuality() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        Map<String, Object> result = mapper.readValue("{\"window_scene_metadata\":[]}", Map.class);
        BehaviorObservationEntity observation = new BehaviorObservationEntity();
        observation.setDataQuality("usable");
        BehaviorService.applySceneQuality(observation, 0L, result);
        assertEquals("usable", observation.getDataQuality());
    }
}
