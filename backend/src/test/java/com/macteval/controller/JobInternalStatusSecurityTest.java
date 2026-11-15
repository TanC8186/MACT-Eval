package com.macteval.controller;

import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.security.JwtTokenProvider;
import com.macteval.security.SecurityConfig;
import com.macteval.security.ServiceTokenValidator;
import com.macteval.service.AnalysisJobService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.context.annotation.Import;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.test.web.servlet.MockMvc;

import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * {@code GET /jobs/{jobId}/internal-status} 的 MVC 层安全契约（R07）。
 *
 * <p>该路径在 {@link SecurityConfig} 中放行（不要求 JWT），身份由
 * {@code X-Service-Token} 决定：没有令牌或令牌错误一律 403，且不查任何任务。</p>
 */
@WebMvcTest(JobController.class)
@Import(SecurityConfig.class)
class JobInternalStatusSecurityTest {

    private static final String PATH = "/api/jobs/job-1/internal-status";
    private static final String TOKEN = "test-service-token";

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private AnalysisJobService analysisJobService;

    @MockBean
    private JwtTokenProvider jwtTokenProvider;

    @MockBean
    private RedisConnectionFactory redisConnectionFactory;

    @MockBean
    private ServiceTokenValidator serviceTokenValidator;

    @Test
    void validTokenReturnsTerminalStatusWithoutJwt() throws Exception {
        when(analysisJobService.findJob("job-1")).thenReturn(job(
                AnalysisJobEntity.JobStatus.FAILED, 1));

        mockMvc.perform(get(PATH).contextPath("/api")
                        .header("X-Service-Token", TOKEN))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.data.status").value("FAILED"))
                .andExpect(jsonPath("$.data.attempt").value(1))
                .andExpect(jsonPath("$.data.terminal").value(true));
    }

    @Test
    void missingTaskReturns404NotServerError() throws Exception {
        when(analysisJobService.findJob("job-1"))
                .thenThrow(new java.util.NoSuchElementException("任务不存在: job-1"));

        mockMvc.perform(get(PATH).contextPath("/api")
                        .header("X-Service-Token", TOKEN))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value(404))
                .andExpect(jsonPath("$.message").value("任务不存在"));
    }

    @Test
    void missingTokenIsForbidden() throws Exception {
        org.mockito.Mockito.doThrow(new org.springframework.security.access.AccessDeniedException(
                        "无效的服务令牌"))
                .when(serviceTokenValidator).validate(null);

        mockMvc.perform(get(PATH).contextPath("/api"))
                .andExpect(status().isForbidden());

        verifyNoInteractions(analysisJobService);
    }

    @Test
    void wrongTokenIsForbidden() throws Exception {
        org.mockito.Mockito.doThrow(new org.springframework.security.access.AccessDeniedException(
                        "无效的服务令牌"))
                .when(serviceTokenValidator).validate("bad-token");

        mockMvc.perform(get(PATH).contextPath("/api")
                        .header("X-Service-Token", "bad-token"))
                .andExpect(status().isForbidden());

        verifyNoInteractions(analysisJobService);
    }

    private static AnalysisJobEntity job(AnalysisJobEntity.JobStatus status, int attempt) {
        AnalysisJobEntity job = new AnalysisJobEntity();
        job.setId("job-1");
        job.setType(AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        job.setStatus(status);
        job.setAttempt(attempt);
        return job;
    }
}
