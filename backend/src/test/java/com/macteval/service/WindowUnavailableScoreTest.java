package com.macteval.service;

import com.macteval.model.entity.BehaviorObservationEntity;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertNull;

class WindowUnavailableScoreTest {
    @Test
    void unavailableVideoWindowHasNoVideoScore() {
        BehaviorObservationEntity observation = new BehaviorObservationEntity();
        observation.setAggregatedUprightProportion("0.9");
        observation.setDataQuality("unavailable");
        assertNull(WindowAlignmentService.videoScoreOrNull(new RuleBaselineService(), List.of(observation)));
    }
}
