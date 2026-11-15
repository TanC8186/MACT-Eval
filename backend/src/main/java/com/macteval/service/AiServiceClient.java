package com.macteval.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.annotation.PostConstruct;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.*;
import org.springframework.stereotype.Service;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.RestTemplate;

import java.util.*;
import java.util.concurrent.atomic.AtomicLong;

/**
 * AI Service REST 客户端 — 调用 Python AI 服务 (ai-service/app/main.py)。
 * <p>
 * 真实覆盖的 AI 能力:
 * <ul>
 *   <li>OpenCV 视频行为分析 — {@code POST /analyze/video}</li>
 *   <li>faster-whisper 语音转写 — {@code POST /asr}</li>
 * </ul>
 * 尚未实现的 AI 能力（{@code /analyze_face}、{@code /diarize}、{@code /encode_text}）
 * 在 Python 端显式返回 HTTP 501；本客户端在调用失败时记录 ERROR 日志并注明
 * "路由未实现"，绝不静默返回假数据。
 * <p>
 * 可用性不启动时锁死：后端先起、ai-service 后起的场景下，后续调用会按冷却
 * 周期懒探测 /health，恢复后自动继续工作。
 */
@Service
@Slf4j
public class AiServiceClient {

    /** 懒探测冷却（毫秒），避免 AI 服务宕机时每次调用都打 health */
    private static final long PROBE_COOLDOWN_MS = 5_000L;

    @Value("${mact.ai-service.url:http://localhost:8001}")
    private String aiServiceUrl;

    @Value("${mact.service-token:mact-eval-dev-token}")
    private String serviceToken;

    private RestTemplate restTemplate;
    private ObjectMapper objectMapper;
    private volatile boolean available = false;
    private final AtomicLong lastProbeMs = new AtomicLong(0L);

    @PostConstruct
    public void init() {
        // 视频分析可能很慢：连接超时短、读超时放宽，避免工作线程被无限挂起。
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(3_000);
        factory.setReadTimeout(120_000);
        this.restTemplate = new RestTemplate(factory);
        this.objectMapper = new ObjectMapper();
        available = probeHealth();
        if (!available) {
            log.warn(
                    "AI Service unavailable at startup ({}); calls will lazy-reprobe every {}ms. "
                            + "Start: cd ai-service && uvicorn app.main:app --host 0.0.0.0 --port 8001",
                    aiServiceUrl, PROBE_COOLDOWN_MS);
        }
    }

    /** 懒探测：启动不可用后仍允许后续恢复，不在启动时永久锁死。 */
    private boolean ensureAvailable() {
        if (available) {
            return true;
        }
        long now = System.currentTimeMillis();
        long last = lastProbeMs.get();
        if (now - last < PROBE_COOLDOWN_MS) {
            return false;
        }
        if (!lastProbeMs.compareAndSet(last, now)) {
            return available;
        }
        available = probeHealth();
        return available;
    }

    private boolean probeHealth() {
        try {
            var headers = new HttpHeaders();
            headers.set("X-Service-Token", serviceToken != null ? serviceToken : "");
            var resp = restTemplate.exchange(
                    aiServiceUrl + "/health", HttpMethod.GET,
                    new HttpEntity<>(headers), Map.class);
            if (resp.getStatusCode().is2xxSuccessful()) {
                log.info("AI Service connected at {}", aiServiceUrl);
                return true;
            }
        } catch (Exception e) {
            log.debug("AI Service health probe failed: {}", e.getMessage());
        }
        return false;
    }

    private HttpHeaders jsonHeaders() {
        var headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        if (serviceToken != null && !serviceToken.isBlank()) {
            headers.set("X-Service-Token", serviceToken);
        }
        return headers;
    }

    private HttpHeaders multipartHeaders() {
        var headers = new HttpHeaders();
        headers.setContentType(MediaType.MULTIPART_FORM_DATA);
        if (serviceToken != null && !serviceToken.isBlank()) {
            headers.set("X-Service-Token", serviceToken);
        }
        return headers;
    }

    /**
     * 人脸关键点检测 — Python 端尚未实现（{@code POST /analyze_face} 返回 HTTP 501）。
     * 调用失败时记录 ERROR 日志并返回 null，不构造假的人脸数据。
     */
    public Map<String, Object> analyzeFace(byte[] imageData, String fileName) {
        if (!ensureAvailable()) return null;
        try {
            var headers = multipartHeaders();
            var body = new LinkedMultiValueMap<String, Object>();
            body.add("file", new ByteArrayResource(imageData) {
                @Override
                public String getFilename() { return fileName != null ? fileName : "face.jpg"; }
            });
            var resp = restTemplate.postForEntity(aiServiceUrl + "/analyze_face",
                    new HttpEntity<>(body, headers), Map.class);
            return resp.getBody();
        } catch (HttpStatusCodeException e) {
            logRouteNotImplemented("analyzeFace", "/analyze_face", e);
            return null;
        } catch (Exception e) {
            log.error("analyzeFace failed: {}", e.getMessage());
            available = false;
            return null;
        }
    }

    /**
     * faster-whisper ASR — 语音转写（{@code POST /asr}，multipart file）。
     * 返回转写文本；当 AI 服务未实现该能力或转写失败时返回 null。
     */
    public String transcribe(byte[] audioData, String fileName) {
        if (!ensureAvailable()) return null;
        try {
            var headers = multipartHeaders();
            var body = new LinkedMultiValueMap<String, Object>();
            body.add("file", new ByteArrayResource(audioData) {
                @Override
                public String getFilename() { return fileName != null ? fileName : "audio.wav"; }
            });
            var resp = restTemplate.postForEntity(aiServiceUrl + "/asr",
                    new HttpEntity<>(body, headers), Map.class);
            return resp.getBody() != null ? (String) resp.getBody().get("text") : null;
        } catch (HttpStatusCodeException e) {
            logRouteNotImplemented("transcribe", "/asr", e);
            return null;
        } catch (Exception e) {
            log.error("ASR failed: {}", e.getMessage());
            available = false;
            return null;
        }
    }

    /**
     * 说话人分离 — Python 端尚未实现（{@code POST /diarize} 返回 HTTP 501）。
     * 调用失败时记录 ERROR 日志并返回空列表，不构造假的说话人分段。
     */
    public List<Map<String, Object>> diarize(byte[] audioData, String fileName) {
        if (!ensureAvailable()) return List.of();
        try {
            var headers = multipartHeaders();
            var body = new LinkedMultiValueMap<String, Object>();
            body.add("file", new ByteArrayResource(audioData) {
                @Override
                public String getFilename() { return fileName != null ? fileName : "audio.wav"; }
            });
            var resp = restTemplate.postForEntity(aiServiceUrl + "/diarize",
                    new HttpEntity<>(body, headers), Map.class);
            var speakers = resp.getBody() != null ? resp.getBody().get("speakers") : null;
            if (speakers instanceof List) {
                return objectMapper.convertValue(speakers, new TypeReference<List<Map<String, Object>>>() {});
            }
            return List.of();
        } catch (HttpStatusCodeException e) {
            logRouteNotImplemented("diarize", "/diarize", e);
            return List.of();
        } catch (Exception e) {
            log.error("Diarization failed: {}", e.getMessage());
            available = false;
            return List.of();
        }
    }

    /**
     * BERT 文本编码 — Python 端尚未实现（{@code POST /encode_text} 返回 HTTP 501）。
     * 调用失败时记录 ERROR 日志并返回 null，不构造假的嵌入向量。
     */
    public double[] encodeText(String text) {
        if (!ensureAvailable()) return null;
        try {
            var headers = jsonHeaders();
            var resp = restTemplate.postForEntity(aiServiceUrl + "/encode_text",
                    new HttpEntity<>(Map.of("text", text), headers), Map.class);
            var emb = resp.getBody() != null ? (List<Double>) resp.getBody().get("embedding") : null;
            if (emb == null) return null;
            return emb.stream().mapToDouble(Double::doubleValue).toArray();
        } catch (HttpStatusCodeException e) {
            logRouteNotImplemented("encodeText", "/encode_text", e);
            return null;
        } catch (Exception e) {
            log.error("Text encoding failed: {}", e.getMessage());
            available = false;
            return null;
        }
    }

    /**
     * OpenCV 视频行为分析（{@code POST /analyze/video}）。
     * 提交视频文件在 AI 服务端的本地路径（JSON body），返回按时间窗聚合的行为观测。
     * <p>
     * Python 端返回 JSON，形如:
     * <pre>
     * {"status":"succeeded","mode":"real","window_count":N,
     *  "observations":[{"window_start_ms":0,"window_end_ms":60000,
     *    "visible_seat_count":40,"hand_raised_count":2, ...}], ...}
     * </pre>
     * 返回 null 表示调用失败（服务不可用或路由未实现）。
     */
    public Map<String, Object> analyzeVideo(String videoPath, String lessonSessionId, int totalSeats) {
        if (!ensureAvailable()) return null;
        try {
            var headers = jsonHeaders();
            var body = Map.of(
                    "video_path", videoPath,
                    "lesson_session_id", lessonSessionId,
                    "total_seats", totalSeats
            );
            var resp = restTemplate.postForEntity(aiServiceUrl + "/analyze/video",
                    new HttpEntity<>(body, headers), Map.class);
            return resp.getBody();
        } catch (HttpStatusCodeException e) {
            logRouteNotImplemented("analyzeVideo", "/analyze/video", e);
            return null;
        } catch (Exception e) {
            log.error("Video analysis failed: {}", e.getMessage());
            available = false;
            return null;
        }
    }

    public boolean isAvailable() {
        return available || ensureAvailable();
    }

    /**
     * 记录 "路由未实现" 的明确错误日志。HTTP 404（路由不存在）与
     * HTTP 501（显式未实现）都说明该 AI 能力当前不可用。
     */
    private void logRouteNotImplemented(String capability, String route, HttpStatusCodeException e) {
        if (e.getStatusCode() == HttpStatus.NOT_FOUND || e.getStatusCode() == HttpStatus.NOT_IMPLEMENTED) {
            log.error("{}: route {} is NOT implemented on the AI service (HTTP {}). {} is unavailable.",
                    capability, route, e.getStatusCode().value(), capability);
        } else {
            log.error("{} failed: HTTP {} - {}", capability, e.getStatusCode().value(), e.getMessage());
        }
    }
}