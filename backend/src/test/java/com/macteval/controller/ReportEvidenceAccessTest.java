package com.macteval.controller;

import com.macteval.dto.EvidenceFrameDTO;
import com.macteval.security.JwtTokenProvider;
import com.macteval.security.SecurityConfig;
import com.macteval.service.ReportEvidenceService;
import com.macteval.service.ReportService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.context.annotation.Import;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.test.context.support.WithMockUser;
import org.springframework.test.web.servlet.MockMvc;

import java.lang.reflect.Method;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * `/reports/{id}/behavior-evidence` 的 MVC 层鉴权三态：匿名 401、学生 403、教师 200。
 *
 * <p>使用真实 {@link SecurityConfig}（含 {@code @EnableMethodSecurity}），因此
 * {@code @PreAuthorize} 是在运行时被验证的，而不是只断言注解字符串。</p>
 */
@WebMvcTest(ReportController.class)
@Import(SecurityConfig.class)
class ReportEvidenceAccessTest {

    private static final String PATH = "/api/reports/lesson-1/behavior-evidence";

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private ReportService reportService;

    @MockBean
    private ReportEvidenceService reportEvidenceService;

    @MockBean
    private JwtTokenProvider jwtTokenProvider;

    @MockBean
    private RedisConnectionFactory redisConnectionFactory;

    @Test
    void anonymousIsRejectedBeforeAnyEvidenceIsLoaded() throws Exception {
        mockMvc.perform(get(PATH).contextPath("/api"))
                .andExpect(status().isUnauthorized());

        verify(reportEvidenceService, never()).getFrames(any(), any(), any());
    }

    @Test
    @WithMockUser(username = "student-1", roles = "student")
    void studentIsForbiddenByMethodSecurity() throws Exception {
        mockMvc.perform(get(PATH).contextPath("/api"))
                .andExpect(status().isForbidden());

        verify(reportEvidenceService, never()).getFrames(any(), any(), any());
    }

    @Test
    @WithMockUser(username = "teacher-1", roles = "teacher")
    void teacherSeesOnlyTheEvidenceOfThatLesson() throws Exception {
        when(reportEvidenceService.getFrames(eq("lesson-1"), any(), eq("teacher-1")))
                .thenReturn(List.of(new EvidenceFrameDTO("frame-1", 0, 12_000L,
                        "hand_raising", "举手", "https://signed.example/frame-1.jpg", Map.of())));

        mockMvc.perform(get(PATH).contextPath("/api"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data[0].id").value("frame-1"));

        verify(reportEvidenceService).getFrames("lesson-1", null, "teacher-1");
    }

    /**
     * 契约锁定：即使将来切片测试被替换，接口也不得退回到「登录即可读」。
     */
    @Test
    void endpointKeepsTeacherOrAdminPreAuthorize() throws Exception {
        Method method = ReportController.class.getMethod(
                "behaviorEvidence", String.class, Integer.class,
                org.springframework.security.core.Authentication.class);
        PreAuthorize annotation = method.getAnnotation(PreAuthorize.class);

        assertNotNull(annotation, "行为证据接口必须保留 @PreAuthorize");
        assertEquals("hasAnyRole('teacher','admin')", annotation.value());
    }
}
