package com.macteval.service;

import jakarta.annotation.PostConstruct;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.util.List;
import java.util.Map;

/**
 * Video analysis facade backed by the Python AI service.
 *
 * <p>This service never creates placeholder face detections. An empty face
 * list means the real analyzer completed without detecting a face.
 */
@Service
@Slf4j
public class VideoAnalysisService {

    private final AiServiceClient aiClient;

    public VideoAnalysisService(AiServiceClient aiClient) {
        this.aiClient = aiClient;
    }

    @PostConstruct
    public void init() {
        log.info("VideoAnalysisService initialized (AI service available: {})", aiClient.isAvailable());
    }

    /**
     * Detect faces through the configured AI service.
     *
     * @throws IllegalStateException when the AI service is unavailable or the
     *                               request fails
     */
    @SuppressWarnings("unchecked")
    public List<Map<String, Object>> detectFaces(byte[] imageData) {
        if (!aiClient.isAvailable()) {
            throw new IllegalStateException("Face detection is unavailable because the AI service is offline");
        }

        Map<String, Object> result = aiClient.analyzeFace(imageData, "frame.jpg");
        if (result == null) {
            throw new IllegalStateException("Face detection is currently unavailable");
        }

        Object faces = result.get("faces");
        return faces instanceof List<?> ? (List<Map<String, Object>>) faces : List.of();
    }

    /**
     * Frame extraction is not implemented yet and therefore returns no data.
     */
    public List<byte[]> extractFrames(String videoPath, int maxFps) {
        log.info("extractFrames({}, {}fps) is not implemented", videoPath, maxFps);
        return List.of();
    }

    public byte[] toJpegBytes(BufferedImage img) {
        try {
            var baos = new ByteArrayOutputStream();
            ImageIO.write(img, "jpg", baos);
            return baos.toByteArray();
        } catch (Exception e) {
            log.error("Image encoding failed: {}", e.getMessage());
            return new byte[0];
        }
    }
}
