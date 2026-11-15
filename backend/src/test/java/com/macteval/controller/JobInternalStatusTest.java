package com.macteval.controller;

import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.security.ServiceTokenValidator;
import com.macteval.service.AnalysisJobService;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * {@code GET /jobs/{jobId}/internal-status} 的契约（供 ai-service 判断终态）。
 *
 * <p>结果对象损坏时，worker 必须先问后端“这个 attempt 是否已终态”：终态才能清理
 * 消息与索引；非终态/查不到状态时不能冒然 ACK，也不能据此重新推理。</p>
 */
class JobInternalStatusTest {

    private static final String TOKEN = "test-service-token";

    private final AnalysisJobService analysisJobService = mock(AnalysisJobService.class);
    private final ServiceTokenValidator tokenValidator = mock(ServiceTokenValidator.class);
    private final JobController controller =
            new JobController(analysisJobService, tokenValidator);

    private static AnalysisJobEntity job(String id, AnalysisJobEntity.JobStatus status, int attempt) {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId(id);
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(status);
        job.setAttempt(attempt);
        return job;
    }

    @Test
    void terminalJobReportsTerminalTrue() {
        when(analysisJobService.findJob("job-failed"))
                .thenReturn(job("job-failed", AnalysisJobEntity.JobStatus.FAILED, 1));

        ResponseEntity<com.macteval.dto.ApiResponse<Map<String, Object>>> response =
                controller.internalStatus("job-failed", TOKEN);

        assertEquals(200, response.getStatusCode().value());
        Map<String, Object> data = response.getBody().data();
        assertEquals("FAILED", data.get("status"));
        assertEquals(1, data.get("attempt"));
        assertEquals(true, data.get("terminal"));
        verify(tokenValidator).validate(TOKEN);
    }

    @Test
    void succeededAndPartialAreAlsoTerminal() {
        when(analysisJobService.findJob("job-ok"))
                .thenReturn(job("job-ok", AnalysisJobEntity.JobStatus.SUCCEEDED, 2));
        when(analysisJobService.findJob("job-partial"))
                .thenReturn(job("job-partial", AnalysisJobEntity.JobStatus.PARTIAL, 1));

        assertEquals(true, controller.internalStatus("job-ok", TOKEN).getBody().data().get("terminal"));
        assertEquals(true,
                controller.internalStatus("job-partial", TOKEN).getBody().data().get("terminal"));
    }

    @Test
    void runningJobIsNotTerminal() {
        when(analysisJobService.findJob("job-running"))
                .thenReturn(job("job-running", AnalysisJobEntity.JobStatus.RUNNING, 1));

        ResponseEntity<com.macteval.dto.ApiResponse<Map<String, Object>>> response =
                controller.internalStatus("job-running", TOKEN);

        assertEquals(200, response.getStatusCode().value());
        assertEquals(false, response.getBody().data().get("terminal"),
                "非终态时 worker 不能清理消息索引");
    }

    @Test
    void missingJobReturns404InsteadOf500() {
        when(analysisJobService.findJob(anyString()))
                .thenThrow(new java.util.NoSuchElementException("任务不存在: job-gone"));

        ResponseEntity<com.macteval.dto.ApiResponse<Map<String, Object>>> response =
                controller.internalStatus("job-gone", TOKEN);

        assertEquals(404, response.getStatusCode().value());
        assertEquals(404, response.getBody().code());
        assertEquals("任务不存在", response.getBody().message());
        assertNull(response.getBody().data());
    }

    @Test
    void missingOrInvalidTokenIsRejectedBeforeAnyLookup() {
        org.mockito.Mockito.doThrow(new org.springframework.security.access.AccessDeniedException(
                        "无效的服务令牌"))
                .when(tokenValidator).validate(org.mockito.ArgumentMatchers.isNull());

        org.junit.jupiter.api.Assertions.assertThrows(
                org.springframework.security.access.AccessDeniedException.class,
                () -> controller.internalStatus("job-failed", null));

        org.mockito.Mockito.doThrow(new org.springframework.security.access.AccessDeniedException(
                        "无效的服务令牌"))
                .when(tokenValidator).validate("wrong-token");
        org.junit.jupiter.api.Assertions.assertThrows(
                org.springframework.security.access.AccessDeniedException.class,
                () -> controller.internalStatus("job-failed", "wrong-token"));

        org.mockito.Mockito.verifyNoInteractions(analysisJobService);
    }

    /**
     * 契约锁定：路径与方法签名不得漂移（AI 组按此实现）。
     */
    @Test
    void endpointPathIsStable() throws Exception {
        var method = JobController.class.getMethod("internalStatus", String.class, String.class);
        var mapping = method.getAnnotation(
                org.springframework.web.bind.annotation.GetMapping.class);

        assertTrue(mapping != null, "必须是 GET 映射");
        assertEquals("/{jobId}/internal-status", mapping.value()[0],
                "AI 组依赖该路径，不得改动");
        assertEquals("X-Service-Token", method.getParameters()[1]
                .getAnnotation(org.springframework.web.bind.annotation.RequestHeader.class).value());
    }
}
