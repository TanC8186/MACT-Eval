package com.macteval.service;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class VideoAnalysisServiceTest {

    private AiServiceClient aiClient;
    private VideoAnalysisService service;

    @BeforeEach
    void setUp() {
        aiClient = mock(AiServiceClient.class);
        service = new VideoAnalysisService(aiClient);
    }

    @Test
    void detectFacesRejectsUnavailableAiServiceInsteadOfReturningMockFace() {
        assertThrows(IllegalStateException.class, () -> service.detectFaces(new byte[]{1}));
    }

    @Test
    void detectFacesKeepsEmptyRealResult() {
        when(aiClient.isAvailable()).thenReturn(true);
        when(aiClient.analyzeFace(any(byte[].class), eq("frame.jpg")))
                .thenReturn(Map.of("faces", List.of()));

        assertEquals(List.of(), service.detectFaces(new byte[]{1}));
    }
}
