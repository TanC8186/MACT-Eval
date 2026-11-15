package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.config.GlobalExceptionHandler;
import com.macteval.controller.ReportController;
import com.macteval.dto.ReportDetailDTO;
import com.macteval.dto.ReportVersionSummaryDTO;
import com.macteval.model.entity.EvaluationReportEntity;
import com.macteval.model.entity.EvaluationReportVersionEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.repository.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 报告版本 HTTP API：版本列表/版本详情/行为证据的版本选择与错误码契约。
 *
 * <p>覆盖四项此前没有测试的行为：</p>
 * <ul>
 *   <li>属主教师能拿到按版本号降序的快照列表与指定版本详情；</li>
 *   <li>其他教师被拒绝（403），{@code teacherId == null} 被拒绝（400）；</li>
 *   <li>课堂不存在 / 版本不存在都映射为 404，而不是 500；</li>
 *   <li>摘要携带快照里的 {@code dataSource}/{@code climateScore}；行为证据按
 *       “已发布快照优先” 精确选择任务，版本号撞上当前草稿也不读草稿证据。</li>
 * </ul>
 */
class ReportVersionApiTest {

    private static final String LESSON = "ls-1";
    private static final String OWNER = "teacher-1";
    private static final String OTHER_TEACHER = "teacher-2";

    private final ObjectMapper objectMapper = new ObjectMapper().findAndRegisterModules();

    private LessonSessionRepository lessonSessionRepository;
    private EvaluationReportRepository reportRepository;
    private EvaluationReportVersionRepository versionRepository;
    private CourseRepository courseRepository;
    private ClassGroupRepository classGroupRepository;
    private ReportService reportService;

    @BeforeEach
    void setUp() {
        lessonSessionRepository = mock(LessonSessionRepository.class);
        reportRepository = mock(EvaluationReportRepository.class);
        versionRepository = mock(EvaluationReportVersionRepository.class);
        courseRepository = mock(CourseRepository.class);
        classGroupRepository = mock(ClassGroupRepository.class);
        reportService = new ReportService(
                reportRepository, versionRepository, lessonSessionRepository,
                courseRepository, classGroupRepository,
                mock(BehaviorObservationRepository.class), mock(SuggestionRepository.class),
                mock(AnalysisJobRepository.class), new SuggestionEngine(objectMapper),
                mock(StudentRepository.class), mock(LessonWindowFeatureRepository.class),
                objectMapper);
    }

    // ======================================================= 版本列表 / 详情

    @Nested
    class VersionHistory {

        @Test
        void ownerReadsVersionsInDescendingOrderWithSnapshotSource() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));
            when(versionRepository.findByLessonSessionIdOrderByVersionDesc(LESSON))
                    .thenReturn(List.of(
                            version(3, "published", flatSnapshot("REAL", 82.5), "J3"),
                            version(2, "published", flatSnapshot("DEMO", 61.0), "J2"),
                            version(1, "published", flatSnapshot("EMPTY", null), null)));

            List<ReportVersionSummaryDTO> history = reportService.getVersionHistory(LESSON, OWNER);

            assertThat(history).extracting(ReportVersionSummaryDTO::version)
                    .containsExactly(3, 2, 1);
            assertThat(history).extracting(ReportVersionSummaryDTO::dataSource)
                    .containsExactly("REAL", "DEMO", "EMPTY");
            assertThat(history).extracting(ReportVersionSummaryDTO::climateScore)
                    .containsExactly(82.5, 61.0, null);
            assertThat(history).extracting(ReportVersionSummaryDTO::analysisVideoJobId)
                    .containsExactly("J3", "J2", null);
        }

        /** 现代快照把正文放在 {@code detail} 里；摘要必须从 detail 读取，而不是只看平铺字段。 */
        @Test
        void modernSnapshotReadsSourceFromDetailMap() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));
            when(versionRepository.findByLessonSessionIdOrderByVersionDesc(LESSON))
                    .thenReturn(List.of(version(1, "published",
                            detailSnapshot("REAL", 74.25), "J1")));

            ReportVersionSummaryDTO summary = reportService.getVersionHistory(LESSON, OWNER).getFirst();

            assertThat(summary.dataSource()).isEqualTo("REAL");
            assertThat(summary.climateScore()).isEqualTo(74.25);
        }

        /** 旧快照缺少字段时必须保持 null，不能用 EMPTY / 0 分伪造数据来源。 */
        @Test
        void snapshotWithoutSourceFieldsYieldsNullNotADefault() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));
            when(versionRepository.findByLessonSessionIdOrderByVersionDesc(LESSON))
                    .thenReturn(List.of(version(1, "published", "{}", null)));

            ReportVersionSummaryDTO summary = reportService.getVersionHistory(LESSON, OWNER).getFirst();

            assertThat(summary.dataSource()).isNull();
            assertThat(summary.climateScore()).isNull();
        }

        @Test
        void otherTeacherIsForbidden() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));

            assertThatThrownBy(() -> reportService.getVersionHistory(LESSON, OTHER_TEACHER))
                    .isInstanceOf(AccessDeniedException.class);
            verifyNoInteractions(versionRepository);
        }

        @Test
        void missingTeacherIdIsRejectedRatherThanSkippingTheCheck() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));

            assertThatThrownBy(() -> reportService.getVersionHistory(LESSON, null))
                    .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> reportService.getVersionHistory(LESSON, "  "))
                    .isInstanceOf(IllegalArgumentException.class);
            verifyNoInteractions(versionRepository);
        }

        @Test
        void missingLessonIsNotFound() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.empty());

            assertThatThrownBy(() -> reportService.getVersionHistory(LESSON, OWNER))
                    .isInstanceOf(NoSuchElementException.class)
                    .hasMessageContaining("课堂不存在");
        }
    }

    @Nested
    class VersionDetail {

        @Test
        void ownerReadsTheRequestedSnapshot() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));
            when(courseRepository.findById(anyString())).thenReturn(Optional.empty());
            when(classGroupRepository.findById(anyString())).thenReturn(Optional.empty());
            when(versionRepository.findByLessonSessionIdAndVersion(LESSON, 2))
                    .thenReturn(Optional.of(version(2, "published", flatSnapshot("REAL", 66.0), "J2")));

            ReportDetailDTO detail = reportService.getVersionDetail(LESSON, 2, OWNER);

            assertThat(detail.version()).isEqualTo(2);
            assertThat(detail.climateScore()).isEqualTo(66.0);
            assertThat(detail.dataSource()).isEqualTo("REAL");
            assertThat(detail.analysisVideoJobId()).isEqualTo("J2");
        }

        @Test
        void otherTeacherIsForbidden() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));

            assertThatThrownBy(() -> reportService.getVersionDetail(LESSON, 2, OTHER_TEACHER))
                    .isInstanceOf(AccessDeniedException.class);
            verifyNoInteractions(versionRepository);
        }

        @Test
        void missingTeacherIdIsRejected() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));

            assertThatThrownBy(() -> reportService.getVersionDetail(LESSON, 2, null))
                    .isInstanceOf(IllegalArgumentException.class);
        }

        @Test
        void missingLessonIsNotFoundNotServerError() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.empty());

            assertThatThrownBy(() -> reportService.getVersionDetail(LESSON, 2, OWNER))
                    .isInstanceOf(NoSuchElementException.class)
                    .hasMessageContaining("课堂不存在");
        }

        @Test
        void missingVersionIsNotFound() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));
            when(versionRepository.findByLessonSessionIdAndVersion(LESSON, 9))
                    .thenReturn(Optional.empty());

            assertThatThrownBy(() -> reportService.getVersionDetail(LESSON, 9, OWNER))
                    .isInstanceOf(NoSuchElementException.class)
                    .hasMessageContaining("报告版本不存在");
        }

        /** 证据接口用的 Optional 变体：快照缺失时返回空，而不是抛异常。 */
        @Test
        void findVersionDetailReturnsEmptyForMissingSnapshot() {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));
            when(versionRepository.findByLessonSessionIdAndVersion(LESSON, 9))
                    .thenReturn(Optional.empty());

            assertThat(reportService.findVersionDetail(LESSON, 9, OWNER)).isEmpty();
        }
    }

    // ============================================================ HTTP 语义

    /**
     * 真实 {@link GlobalExceptionHandler} 下的状态码：课堂/版本不存在必须是 404，
     * 其他教师是 403，null 主体是 400（不能被通用处理器降级成 500）。
     */
    @Nested
    class HttpStatusContract {

        private MockMvc mockMvc;

        @BeforeEach
        void setUpMvc() {
            mockMvc = MockMvcBuilders.standaloneSetup(new ReportController(reportService,
                            mock(ReportEvidenceService.class)))
                    .setControllerAdvice(new GlobalExceptionHandler())
                    .build();
        }

        @Test
        void missingLessonOnVersionListReturns404() throws Exception {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.empty());

            mockMvc.perform(get("/reports/{id}/versions", LESSON).principal(teacher(OWNER)))
                    .andExpect(status().isNotFound())
                    .andExpect(jsonPath("$.code").value(404));
        }

        @Test
        void missingLessonOnVersionDetailReturns404() throws Exception {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.empty());

            mockMvc.perform(get("/reports/{id}/versions/{version}", LESSON, 2)
                            .principal(teacher(OWNER)))
                    .andExpect(status().isNotFound())
                    .andExpect(jsonPath("$.code").value(404));
        }

        @Test
        void missingVersionReturns404() throws Exception {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));
            when(versionRepository.findByLessonSessionIdAndVersion(LESSON, 9))
                    .thenReturn(Optional.empty());

            mockMvc.perform(get("/reports/{id}/versions/{version}", LESSON, 9)
                            .principal(teacher(OWNER)))
                    .andExpect(status().isNotFound())
                    .andExpect(jsonPath("$.code").value(404));
        }

        @Test
        void otherTeacherGets403() throws Exception {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));

            mockMvc.perform(get("/reports/{id}/versions", LESSON).principal(teacher(OTHER_TEACHER)))
                    .andExpect(status().isForbidden())
                    .andExpect(jsonPath("$.code").value(403));
        }

        @Test
        void ownerGetsListWithSourceAndClimateFields() throws Exception {
            when(lessonSessionRepository.findById(LESSON)).thenReturn(Optional.of(session(OWNER)));
            when(versionRepository.findByLessonSessionIdOrderByVersionDesc(LESSON))
                    .thenReturn(List.of(
                            version(2, "published", flatSnapshot("REAL", 80.0), "J2"),
                            version(1, "published", flatSnapshot("DEMO", 55.0), "J1")));

            mockMvc.perform(get("/reports/{id}/versions", LESSON).principal(teacher(OWNER)))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.data.length()").value(2))
                    .andExpect(jsonPath("$.data[0].version").value(2))
                    .andExpect(jsonPath("$.data[0].dataSource").value("REAL"))
                    .andExpect(jsonPath("$.data[0].climateScore").value(80.0))
                    .andExpect(jsonPath("$.data[1].version").value(1));
        }
    }

    // ================================================== 行为证据的版本选择

    /**
     * {@code behavior-evidence?version=N} 的确定性解析：
     * 快照优先 → 版本号等于工作行且无快照才读工作行 → 其余返回空。
     */
    @Nested
    class EvidenceVersionSelection {

        private ReportService reports;
        private BehaviorEvidenceFrameRepository frames;
        private MinioService minio;
        private ReportEvidenceService evidence;
        private ReportDetailDTO working;
        private ReportDetailDTO published;

        @BeforeEach
        void setUpEvidence() {
            reports = mock(ReportService.class);
            frames = mock(BehaviorEvidenceFrameRepository.class);
            minio = mock(MinioService.class);
            evidence = new ReportEvidenceService(reports, frames, minio, objectMapper);
        }

        /** 模拟当前工作行（可能是草稿），并让鉴权入口返回它。 */
        private void workingVersion(int version, String videoJob) {
            working = mock(ReportDetailDTO.class);
            when(working.version()).thenReturn(version);
            when(working.analysisVideoJobId()).thenReturn(videoJob);
            when(reports.getReport(LESSON, OWNER)).thenReturn(working);
            when(minio.generateDownloadUrl(anyString(), anyString(), org.mockito.ArgumentMatchers.anyLong()))
                    .thenReturn("url");
        }

        private void publishedVersion(String videoJob) {
            published = mock(ReportDetailDTO.class);
            when(published.analysisVideoJobId()).thenReturn(videoJob);
        }

        /** 审计主张：version 撞上工作行版本时旧实现会读草稿。快照存在时必须读快照。 */
        @Test
        void requestedVersionPrefersPublishedSnapshotOverDraftWithSameNumber() {
            workingVersion(2, "draft-job");
            publishedVersion("published-job");
            when(reports.getVersionDetail(LESSON, 2, OWNER)).thenReturn(published);
            when(frames.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(LESSON, "published-job"))
                    .thenReturn(List.of(frame()));

            var result = evidence.getFrames(LESSON, 2, OWNER);

            assertThat(result).hasSize(1);
            verify(frames, never())
                    .findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(LESSON, "draft-job");
        }

        /** 该版本号从未发布过、且就是当前工作稿：允许读工作行。 */
        @Test
        void draftVersionWithoutSnapshotReadsTheWorkingReport() {
            workingVersion(3, "draft-job");
            when(reports.getVersionDetail(LESSON, 3, OWNER))
                    .thenThrow(new NoSuchElementException("报告版本不存在"));
            when(frames.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(LESSON, "draft-job"))
                    .thenReturn(List.of(frame()));

            assertThat(evidence.getFrames(LESSON, 3, OWNER)).hasSize(1);
        }

        /** 历史版本号既无快照又非工作行：返回空，绝不拿工作行证据冒充。 */
        @Test
        void unknownVersionReturnsNoFramesInsteadOfMixingInDraftEvidence() {
            workingVersion(4, "draft-job");
            when(reports.getVersionDetail(LESSON, 1, OWNER))
                    .thenThrow(new NoSuchElementException("报告版本不存在"));

            assertThat(evidence.getFrames(LESSON, 1, OWNER)).isEmpty();
            verifyNoInteractions(frames);
        }

        @Test
        void nullVersionReadsTheWorkingReport() {
            workingVersion(2, "draft-job");
            when(frames.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(LESSON, "draft-job"))
                    .thenReturn(List.of(frame()));

            assertThat(evidence.getFrames(LESSON, null, OWNER)).hasSize(1);
            verify(reports, never()).getVersionDetail(any(), any(), any());
        }

        /** 属主校验先于任何帧读取：非属主既读不到快照也读不到图片元数据。 */
        @Test
        void unauthorizedUserIsRejectedBeforeAnySnapshotOrFrameLookup() {
            when(reports.getReport(LESSON, OTHER_TEACHER))
                    .thenThrow(new AccessDeniedException("denied"));

            assertThatThrownBy(() -> evidence.getFrames(LESSON, 2, OTHER_TEACHER))
                    .isInstanceOf(AccessDeniedException.class);
            verify(reports, never()).getVersionDetail(any(), any(), any());
            verifyNoInteractions(frames, minio);
        }

        private com.macteval.model.entity.BehaviorEvidenceFrameEntity frame() {
            var frame = new com.macteval.model.entity.BehaviorEvidenceFrameEntity();
            frame.setId("frame-1");
            frame.setLessonSessionId(LESSON);
            frame.setJobId("published-job");
            frame.setWindowIndex(0);
            frame.setTimestampMs(1_000L);
            frame.setCode("hand_raising");
            frame.setLabel("举手");
            frame.setBucket("analysis-artifacts");
            frame.setObjectKey("frame.jpg");
            return frame;
        }
    }

    // ================================================================ helpers

    private Authentication teacher(String userId) {
        return new UsernamePasswordAuthenticationToken(userId, null,
                List.of(new SimpleGrantedAuthority("ROLE_teacher")));
    }

    private LessonSessionEntity session(String teacherId) {
        LessonSessionEntity session = new LessonSessionEntity();
        session.setId(LESSON);
        session.setCourseId("course-1");
        session.setClassId("class-1");
        session.setTeacherId(teacherId);
        session.setTitle("版本 API 测试课堂");
        return session;
    }

    private EvaluationReportVersionEntity version(int number, String status, String snapshotJson,
                                                  String videoJobId) {
        EvaluationReportVersionEntity entity = new EvaluationReportVersionEntity();
        entity.setId("rv-" + number);
        entity.setReportId("report-1");
        entity.setLessonSessionId(LESSON);
        entity.setVersion(number);
        entity.setStatus(status);
        entity.setSnapshotJson(snapshotJson);
        entity.setContentHash("hash-" + number);
        entity.setAnalysisVideoJobId(videoJobId);
        entity.setPublishedBy(OWNER);
        entity.setPublishedAt(LocalDateTime.of(2026, 10, 28, 9, number));
        return entity;
    }

    /** 整改前的旧快照形状：字段平铺在顶层。 */
    private String flatSnapshot(String dataSource, Double climateScore) {
        Map<String, Object> payload = new java.util.LinkedHashMap<>();
        payload.put("title", "课堂评价报告");
        payload.put("summary", "摘要");
        if (dataSource != null) payload.put("dataSource", dataSource);
        if (climateScore != null) payload.put("climateScore", climateScore);
        try {
            return objectMapper.writeValueAsString(payload);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** 现代快照形状：正文在 {@code detail} 下。 */
    private String detailSnapshot(String dataSource, Double climateScore) {
        Map<String, Object> detail = new java.util.LinkedHashMap<>();
        detail.put("dataSource", dataSource);
        detail.put("climateScore", climateScore);
        try {
            return objectMapper.writeValueAsString(Map.of("detail", detail));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
