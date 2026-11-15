package com.macteval.service;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class AudioAnalysisServiceTest {

    private AiServiceClient aiClient;
    private AudioAnalysisService service;

    @BeforeEach
    void setUp() {
        aiClient = mock(AiServiceClient.class);
        service = new AudioAnalysisService(aiClient);
    }

    @Test
    void transcribeRejectsUnavailableAiServiceInsteadOfReturningMockText() {
        var error = assertThrows(
                IllegalStateException.class,
                () -> service.transcribe(new byte[]{1}, "lesson.wav")
        );

        assertEquals(
                "Audio transcription is unavailable because the AI service is offline",
                error.getMessage()
        );
    }

    @Test
    void transcribeRejectsFailedAiRequestInsteadOfReturningMockText() {
        when(aiClient.isAvailable()).thenReturn(true);
        when(aiClient.transcribe(any(byte[].class), eq("lesson.wav"))).thenReturn(null);

        var error = assertThrows(
                IllegalStateException.class,
                () -> service.transcribe(new byte[]{1}, "lesson.wav")
        );

        assertEquals("Audio transcription is currently unavailable", error.getMessage());
    }

    @Test
    void diarizeKeepsEmptyRealResultInsteadOfCreatingSpeakerSegments() {
        when(aiClient.isAvailable()).thenReturn(true);
        when(aiClient.diarize(any(byte[].class), eq("lesson.wav"))).thenReturn(List.of());

        assertEquals(List.of(), service.diarize(new byte[]{1}, "lesson.wav"));
    }
}
