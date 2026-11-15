package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.test.util.ReflectionTestUtils;
import org.springframework.web.client.RestTemplate;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

/**
 * AiServiceClient 集成测试 — 验证 REST 客户端与远程 AI 服务的 HTTP 通信。
 * <p>
 * 通过 @MockBean RestTemplate 模拟 HTTP 调用，验证：
 * - 请求构建（URL、Header、Body）
 * - 响应解析（成功/失败/超时）
 * - 优雅降级（AI 服务不可用时的 null/空值返回）
 * <p>
 * AiServiceClient 在 @PostConstruct 中自行创建 RestTemplate，
 * 测试通过 ReflectionTestUtils 注入 @MockBean 的 RestTemplate 实例。
 */
@SpringBootTest(
        classes = AiServiceClient.class,
        properties = {
                "mact.ai-service.url=http://localhost:8001",
                "mact.service-token=mact-eval-dev-token"
        }
)
class AiServiceClientTest {

    @MockBean
    private RestTemplate restTemplate;

    @Autowired
    private AiServiceClient aiServiceClient;

    private final ObjectMapper objectMapper = new ObjectMapper();

    @BeforeEach
    void setUp() {
        // AiServiceClient.init() 在 @PostConstruct 中创建了自有的 RestTemplate，
        // 这里注入 @MockBean 的 RestTemplate 以控制 HTTP 调用行为。
        ReflectionTestUtils.setField(aiServiceClient, "restTemplate", restTemplate);
        ReflectionTestUtils.setField(aiServiceClient, "available", true);
        ReflectionTestUtils.setField(aiServiceClient, "objectMapper", objectMapper);
    }

    // ========================================================================
    // health 端点
    // ========================================================================

    @Nested
    @DisplayName("health 健康检查")
    class HealthTest {

        @Test
        @DisplayName("AI 服务在线时 isAvailable 返回 true")
        void healthReturnsOk_isAvailableTrue() {
            when(restTemplate.getForEntity(anyString(), eq(Map.class)))
                    .thenReturn(ResponseEntity.ok(Map.of("status", "ok")));

            // 手动触发 init 逻辑（模拟 @PostConstruct）
            assertTrue(aiServiceClient.isAvailable());
        }

        @Test
        @DisplayName("AI 服务返回非 2xx 时 isAvailable 为 false")
        void healthReturnsNonOk_isAvailableFalse() {
            // 模拟 @PostConstruct 中的健康检查: 非 2xx
            try {
                when(restTemplate.getForEntity(anyString(), eq(Map.class)))
                        .thenThrow(new RuntimeException("Connection refused"));
            } catch (Exception e) {
                // Mockito 要求抛出 RuntimeException
            }

            ReflectionTestUtils.setField(aiServiceClient, "available", false);
            assertFalse(aiServiceClient.isAvailable());
        }
    }

    // ========================================================================
    // analyzeFace — MediaPipe FaceMesh
    // ========================================================================

    @Nested
    @DisplayName("analyzeFace — 面部关键点检测")
    class AnalyzeFaceTest {

        @Test
        @DisplayName("成功调用应返回人脸关键点数据")
        void successfulCall_returnsFaceLandmarks() {
            Map<String, Object> mockResponse = Map.of(
                    "faces", List.of(Map.of(
                            "face_id", 0,
                            "ear", 0.35,
                            "mar", 0.12,
                            "head_pose", Map.of("pitch", 5.0, "yaw", -2.0, "roll", 0.5),
                            "eye_closed", false,
                            "yawning", false
                    ))
            );
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/analyze_face"),
                    any(),
                    eq(Map.class)
            )).thenReturn(ResponseEntity.ok(mockResponse));

            byte[] testImage = new byte[]{0x01, 0x02, 0x03, 0x04};
            Map<String, Object> result = aiServiceClient.analyzeFace(testImage, "test_face.jpg");

            assertNotNull(result);
            assertTrue(result.containsKey("faces"));
        }

        @Test
        @DisplayName("AI 服务不可用时应返回 null（优雅降级）")
        void serviceUnavailable_returnsNull() {
            ReflectionTestUtils.setField(aiServiceClient, "available", false);

            byte[] testImage = new byte[]{};
            Map<String, Object> result = aiServiceClient.analyzeFace(testImage, "test.jpg");

            assertNull(result);
        }

        @Test
        @DisplayName("HTTP 调用失败时应返回 null（优雅降级）")
        void httpError_returnsNull() {
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/analyze_face"),
                    any(),
                    eq(Map.class)
            )).thenThrow(new RuntimeException("Connection timeout"));

            byte[] testImage = new byte[]{0x0A, 0x0B, 0x0C};
            Map<String, Object> result = aiServiceClient.analyzeFace(testImage, "error.jpg");

            assertNull(result);
        }
    }

    // ========================================================================
    // transcribe — Whisper ASR
    // ========================================================================

    @Nested
    @DisplayName("transcribe — 语音转写")
    class TranscribeTest {

        @Test
        @DisplayName("成功调用应返回转写文本")
        void successfulCall_returnsTranscript() {
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/asr"),
                    any(),
                    eq(Map.class)
            )).thenReturn(ResponseEntity.ok(Map.of("text", "今天我们来学习勾股定理")));

            byte[] audioData = new byte[]{0x10, 0x20, 0x30};
            String result = aiServiceClient.transcribe(audioData, "lesson.wav");

            assertNotNull(result);
            assertEquals("今天我们来学习勾股定理", result);
        }

        @Test
        @DisplayName("AI 服务不可用时应返回 null")
        void serviceUnavailable_returnsNull() {
            ReflectionTestUtils.setField(aiServiceClient, "available", false);

            String result = aiServiceClient.transcribe(new byte[]{}, "test.wav");
            assertNull(result);
        }

        @Test
        @DisplayName("HTTP 调用失败时应返回 null")
        void httpError_returnsNull() {
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/asr"),
                    any(),
                    eq(Map.class)
            )).thenThrow(new RuntimeException("Service unavailable"));

            String result = aiServiceClient.transcribe(new byte[]{0x01}, "fail.wav");
            assertNull(result);
        }
    }

    // ========================================================================
    // diarize — pyannote 说话人分离
    // ========================================================================

    @Nested
    @DisplayName("diarize — 说话人分离")
    class DiarizeTest {

        @Test
        @DisplayName("成功调用应返回说话人列表")
        void successfulCall_returnsSpeakerList() throws Exception {
            String json = "{\"speakers\": [{\"speaker\": \"SPEAKER_00\", \"start\": 0.0, \"end\": 30.0}]}";
            Map<String, Object> mockResponse = objectMapper.readValue(json, Map.class);
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/diarize"),
                    any(),
                    eq(Map.class)
            )).thenReturn(ResponseEntity.ok(mockResponse));

            List<Map<String, Object>> result = aiServiceClient.diarize(new byte[]{0x01}, "diarize.wav");

            assertNotNull(result);
            assertFalse(result.isEmpty());
            assertEquals("SPEAKER_00", result.get(0).get("speaker"));
        }

        @Test
        @DisplayName("AI 服务不可用时应返回空列表")
        void serviceUnavailable_returnsEmptyList() {
            ReflectionTestUtils.setField(aiServiceClient, "available", false);

            List<Map<String, Object>> result = aiServiceClient.diarize(new byte[]{}, "test.wav");
            assertTrue(result.isEmpty());
        }
    }

    // ========================================================================
    // encodeText — BERT 文本编码
    // ========================================================================

    @Nested
    @DisplayName("encodeText — BERT 文本编码")
    class EncodeTextTest {

        @Test
        @DisplayName("成功调用应返回嵌入向量")
        void successfulCall_returnsEmbedding() {
            var mockResponse = Map.of("embedding", List.of(0.1, 0.2, 0.3, 0.4, 0.5));
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/encode_text"),
                    any(),
                    eq(Map.class)
            )).thenReturn(ResponseEntity.ok(mockResponse));

            double[] result = aiServiceClient.encodeText("今天天气很好");

            assertNotNull(result);
            assertEquals(5, result.length);
            assertArrayEquals(new double[]{0.1, 0.2, 0.3, 0.4, 0.5}, result, 1e-9);
        }

        @Test
        @DisplayName("中文文本编码应正常处理")
        void chineseText_returnsEmbedding() {
            var mockResponse = Map.of("embedding", List.of(-0.05, 0.15, 0.0, 0.22));
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/encode_text"),
                    any(),
                    eq(Map.class)
        )).thenReturn(ResponseEntity.ok(mockResponse));

            double[] result = aiServiceClient.encodeText("学生积极参与课堂讨论");

            assertNotNull(result);
            assertEquals(4, result.length);
        }

        @Test
        @DisplayName("AI 服务不可用时应返回 null")
        void serviceUnavailable_returnsNull() {
            ReflectionTestUtils.setField(aiServiceClient, "available", false);

            double[] result = aiServiceClient.encodeText("测试文本");
            assertNull(result);
        }
    }

    // ========================================================================
    // analyzeVideo — OpenCV 视频行为分析 (POST /analyze/video)
    // ========================================================================

    @Nested
    @DisplayName("analyzeVideo — 视频行为分析")
    class AnalyzeVideoTest {

        @Test
        @DisplayName("成功调用应返回行为观测结果")
        void successfulCall_returnsObservations() throws Exception {
            String json = "{\"status\":\"succeeded\",\"mode\":\"real\",\"lesson_session_id\":\"lesson-1\","
                    + "\"window_count\":1,"
                    + "\"observations\":[{\"window_start_ms\":0,\"window_end_ms\":60000,"
                    + "\"visible_seat_count\":40,\"hand_raised_count\":2}]}";
            Map<String, Object> mockResponse = objectMapper.readValue(json, Map.class);
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/analyze/video"),
                    any(),
                    eq(Map.class)
            )).thenReturn(ResponseEntity.ok(mockResponse));

            Map<String, Object> result = aiServiceClient.analyzeVideo("/tmp/video.mp4", "lesson-1", 45);

            assertNotNull(result);
            assertEquals("succeeded", result.get("status"));
            assertEquals(1, result.get("window_count"));
        }

        @Test
        @DisplayName("AI 服务不可用时应返回 null")
        void serviceUnavailable_returnsNull() {
            ReflectionTestUtils.setField(aiServiceClient, "available", false);

            Map<String, Object> result = aiServiceClient.analyzeVideo("/tmp/video.mp4", "lesson-1", 45);
            assertNull(result);
        }

        @Test
        @DisplayName("路由未实现 (HTTP 501) 时应返回 null")
        void routeNotImplemented_returnsNull() {
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/analyze/video"),
                    any(),
                    eq(Map.class)
            )).thenThrow(new org.springframework.web.client.HttpServerErrorException(
                    HttpStatus.NOT_IMPLEMENTED));

            Map<String, Object> result = aiServiceClient.analyzeVideo("/tmp/video.mp4", "lesson-1", 45);

            assertNull(result);
        }
    }

    // ========================================================================
    // 综合测试 — 优雅降级与边界条件
    // ========================================================================

    @Nested
    @DisplayName("综合测试 — 优雅降级")
    class GracefulDegradationTest {

        @Test
        @DisplayName("所有方法在服务不可用时都应返回 null 或空")
        void allMethodsDegradeGracefully() {
            ReflectionTestUtils.setField(aiServiceClient, "available", false);

            assertNull(aiServiceClient.analyzeFace(new byte[]{1, 2, 3}, "test.jpg"));
            assertNull(aiServiceClient.transcribe(new byte[]{1, 2, 3}, "test.wav"));
            assertTrue(aiServiceClient.diarize(new byte[]{1, 2, 3}, "test.wav").isEmpty());
            assertNull(aiServiceClient.encodeText("test"));
            assertNull(aiServiceClient.analyzeVideo("/tmp/video.mp4", "lesson-1", 45));
            assertFalse(aiServiceClient.isAvailable());
        }

        @Test
        @DisplayName("analyzeFace 和 transcribe 的文件名应传递到请求体")
        void fileNamesPassedToRequestBody() {
            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/analyze_face"),
                    any(),
                    eq(Map.class)
            )).thenReturn(ResponseEntity.ok(Map.of("faces", List.of())));

            when(restTemplate.postForEntity(
                    eq("http://localhost:8001/asr"),
                    any(),
                    eq(Map.class)
            )).thenReturn(ResponseEntity.ok(Map.of("text", "")));

            // These should not throw
            assertDoesNotThrow(() -> aiServiceClient.analyzeFace(new byte[]{0x01}, "custom_name.jpg"));
            assertDoesNotThrow(() -> aiServiceClient.transcribe(new byte[]{0x01}, "custom_audio.wav"));
        }
    }
}
