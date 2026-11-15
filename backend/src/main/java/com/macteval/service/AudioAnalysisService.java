package com.macteval.service;

import jakarta.annotation.PostConstruct;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * Audio analysis facade backed by the Python AI service.
 *
 * <p>This service never fabricates transcripts or speaker segments. Callers
 * receive an explicit failure when the AI service cannot execute the request.
 */
@Service
@Slf4j
public class AudioAnalysisService {

    private final AiServiceClient aiClient;

    public AudioAnalysisService(AiServiceClient aiClient) {
        this.aiClient = aiClient;
    }

    @PostConstruct
    public void init() {
        log.info("AudioAnalysisService initialized (AI service available: {})", aiClient.isAvailable());
    }

    /**
     * Transcribe audio through the configured ASR service.
     *
     * @throws IllegalStateException when the AI service is unavailable or the
     *                               request fails
     */
    public String transcribe(byte[] audioData, String fileName) {
        requireAiService("Audio transcription");

        String result = aiClient.transcribe(audioData, fileName);
        if (result == null) {
            throw new IllegalStateException("Audio transcription is currently unavailable");
        }

        log.info("ASR completed: {} chars", result.length());
        return result;
    }

    /**
     * Separate speakers through the configured diarization service.
     *
     * <p>An empty list is a valid real result when no speaker segment is
     * detected; it is not replaced with sample data.
     */
    public List<Map<String, Object>> diarize(byte[] audioData, String fileName) {
        requireAiService("Speaker diarization");

        List<Map<String, Object>> result = aiClient.diarize(audioData, fileName);
        log.info("Diarization completed: {} segments", result.size());
        return result;
    }

    public byte[] readAudioFile(String filePath) {
        try {
            return Files.readAllBytes(Path.of(filePath));
        } catch (Exception e) {
            log.error("Failed to read audio file: {}", e.getMessage());
            return new byte[0];
        }
    }

    private void requireAiService(String capability) {
        if (!aiClient.isAvailable()) {
            throw new IllegalStateException(capability + " is unavailable because the AI service is offline");
        }
    }
}
