package com.macteval.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.*;
import com.macteval.model.entity.*;
import com.macteval.repository.*;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.LocalDateTime;
import java.util.*;
import java.util.stream.Collectors;

@Slf4j
@Service
@RequiredArgsConstructor
public class ReportService {

    /** 证据覆盖率分母中的固定证据来源：行为/视频/语音。 */
    private static final int EVIDENCE_SOURCE_TOTAL = 3;

    /**
     * 模型版本标识，表示报告分数确实来自所选版本组合的窗口行。
     * <p>作为 {@link #windowState} 的版本精确判据：{@code windowCount} 在无窗口
     * 特征时会退回版本盲的行为观测行数，不能用来判断“窗口是否就绪”。</p>
     */
    private static final String MODEL_WINDOW_AGGREGATOR = "window-aggregator-1.0";

    @Value("${mact.demo.enabled:false}")
    private boolean demoEnabled;

    private final EvaluationReportRepository reportRepository;
    private final EvaluationReportVersionRepository reportVersionRepository;
    private final LessonSessionRepository lessonSessionRepository;
    private final CourseRepository courseRepository;
    private final ClassGroupRepository classGroupRepository;
    private final BehaviorObservationRepository behaviorRepository;
    private final SuggestionRepository suggestionRepository;
    private final AnalysisJobRepository analysisJobRepository;
    private final SuggestionEngine suggestionEngine;
    private final StudentRepository studentRepository;
    private final LessonWindowFeatureRepository windowFeatureRepository;
    private final ObjectMapper objectMapper;

    /**
     * 识别证据帧仓库与对象存储客户端，仅用于「删除报告 = 删除课堂影像」这条链路（SEC-11）。
     *
     * <p><b>为什么是 setter 注入而不是构造参数</b>：本类由 {@link RequiredArgsConstructor}
     * 生成构造器，而多个既有测试按位置直接调用该构造器（13 个参数）；追加构造参数会让
     * 那些测试文件编译失败，而它们不在本次改动范围内。生产环境里 Spring 仍必然注入
     * （缺 bean 启动即失败），所以这只是注入形式的差异，不是能力降级。</p>
     */
    private BehaviorEvidenceFrameRepository evidenceFrameRepository;
    private MinioService minioService;

    @Autowired
    void setEvidenceDeletionDependencies(BehaviorEvidenceFrameRepository evidenceFrameRepository,
                                         MinioService minioService) {
        this.evidenceFrameRepository = evidenceFrameRepository;
        this.minioService = minioService;
    }

    // =====================================================================
    // 列表
    // =====================================================================

    @Transactional(readOnly = true)
    public List<ReportSummaryDTO> listReports(String userId, String role) {
        List<LessonSessionEntity> sessions;
        boolean studentView = "student".equalsIgnoreCase(role);
        if ("admin".equalsIgnoreCase(role)) {
            sessions = lessonSessionRepository.findAll();
        } else if (studentView) {
            StudentEntity student = studentRepository.findById(userId).orElse(null);
            if (student == null || student.getClassId() == null || student.getClassId().isBlank()) {
                return List.of();
            }
            sessions = lessonSessionRepository.findByClassId(student.getClassId());
        } else if (userId != null && !userId.isBlank()) {
            sessions = lessonSessionRepository.findByTeacherId(userId);
        } else {
            return List.of();
        }

        return mapReportSummaries(sessions, studentView);
    }

    /**
     * 服务端分页：教师/管理员按数据库页查询；学生按班级已发布过滤后分页。
     */
    @Transactional(readOnly = true)
    public PageResponse<ReportSummaryDTO> listReportsPage(String userId, String role, int page, int size) {
        int pageSize = Math.max(1, Math.min(size, 100));
        int pageIndex = Math.max(0, page);
        boolean studentView = "student".equalsIgnoreCase(role);
        org.springframework.data.domain.Pageable pageable = org.springframework.data.domain.PageRequest.of(
                pageIndex, pageSize,
                org.springframework.data.domain.Sort.by(
                        org.springframework.data.domain.Sort.Direction.DESC, "createdAt"));
        if ("admin".equalsIgnoreCase(role)) {
            var result = lessonSessionRepository.findAll(pageable);
            return new PageResponse<>(mapReportSummaries(result.getContent(), false),
                    pageIndex, pageSize, result.getTotalElements());
        }
        if (studentView) {
            StudentEntity student = studentRepository.findById(userId).orElse(null);
            if (student == null || student.getClassId() == null || student.getClassId().isBlank()) {
                return new PageResponse<>(List.of(), pageIndex, pageSize, 0);
            }
            List<ReportSummaryDTO> all = mapReportSummaries(
                    lessonSessionRepository.findByClassId(student.getClassId()), true);
            int from = Math.min(pageIndex * pageSize, all.size());
            int to = Math.min(from + pageSize, all.size());
            return new PageResponse<>(all.subList(from, to), pageIndex, pageSize, all.size());
        }
        if (userId == null || userId.isBlank()) {
            return new PageResponse<>(List.of(), pageIndex, pageSize, 0);
        }
        var result = lessonSessionRepository.findByTeacherId(userId, pageable);
        return new PageResponse<>(mapReportSummaries(result.getContent(), false),
                pageIndex, pageSize, result.getTotalElements());
    }

    private List<ReportSummaryDTO> mapReportSummaries(List<LessonSessionEntity> sessions, boolean studentView) {
        if (sessions.isEmpty()) {
            return List.of();
        }
        Set<String> sessionIds = sessions.stream().map(LessonSessionEntity::getId).collect(Collectors.toSet());
        Map<String, EvaluationReportEntity> reportBySession = reportRepository
                .findByLessonSessionIdIn(sessionIds).stream()
                .collect(Collectors.toMap(EvaluationReportEntity::getLessonSessionId, r -> r, (a, b) -> a));
        Map<String, EvaluationReportVersionEntity> publishedVersionByLesson = studentView
                ? reportVersionRepository.findByLessonSessionIdInAndStatus(sessionIds, "published")
                        .stream()
                        .collect(Collectors.toMap(
                                EvaluationReportVersionEntity::getLessonSessionId,
                                v -> v,
                                (a, b) -> a.getVersion() >= b.getVersion() ? a : b))
                : Map.of();

        Set<String> courseIds = sessions.stream().map(LessonSessionEntity::getCourseId)
                .filter(Objects::nonNull).collect(Collectors.toSet());
        Set<String> classIds = sessions.stream().map(LessonSessionEntity::getClassId)
                .filter(Objects::nonNull).collect(Collectors.toSet());
        Map<String, String> courseNames = courseIds.isEmpty() ? Map.of()
                : courseRepository.findAllById(courseIds).stream()
                        .collect(Collectors.toMap(CourseEntity::getId, CourseEntity::getName, (a, b) -> a));
        Map<String, String> classNames = classIds.isEmpty() ? Map.of()
                : classGroupRepository.findAllById(classIds).stream()
                        .collect(Collectors.toMap(ClassGroupEntity::getId, ClassGroupEntity::getName, (a, b) -> a));

        return sessions.stream()
                // 学生可见“当前已发布版本”；工作行被重新生成成草稿时，仍从
                // 历史发布快照展示内容，不能整份报告消失。
                .filter(s -> !studentView
                        || (reportBySession.get(s.getId()) != null
                            && "published".equalsIgnoreCase(reportBySession.get(s.getId()).getStatus()))
                        || publishedVersionByLesson.containsKey(s.getId()))
                .map(s -> {
                    var report = reportBySession.get(s.getId());
                    var publishedVersion = studentView ? publishedVersionByLesson.get(s.getId()) : null;
                    Map<String, Object> snapshot = publishedVersion == null
                            ? Map.of() : parseMap(publishedVersion.getSnapshotJson());
                    String status = publishedVersion != null ? "published"
                            : (report != null ? report.getStatus() : "draft");
                    Double climate = publishedVersion != null
                            ? snapshotDouble(snapshot, "climateScore")
                            : (report != null ? report.getClimateScore() : null);
                    String dataSource = publishedVersion != null
                            ? snapshotString(snapshot, "dataSource")
                            : (report != null ? report.getDataSource() : "EMPTY");
                    String courseName = courseNames.getOrDefault(s.getCourseId(), "");
                    String className = classNames.getOrDefault(s.getClassId(), "");
                    return new ReportSummaryDTO(
                            s.getId(), s.getTitle(), courseName, className, "",
                            s.getDate(), status,
                            // 主评价目标只有一个：课堂氛围融合分；不再用三维平均重复使用同一信息。
                            climate, climate,
                            dataSource == null ? "EMPTY" : dataSource,
                            s.getCreatedAt(),
                            report != null || publishedVersion != null
                    );
                })
                .sorted((a, b) -> b.createdAt().compareTo(a.createdAt()))
                .toList();
    }

    // =====================================================================
    // 生成 / 读取 / 发布 / 版本
    // =====================================================================

    @Transactional
    public ReportDetailDTO generateReport(String lessonSessionId) {
        // 免校验不是默认行为：内部调用必须显式声明，且不再靠传 null 触发。
        LessonSessionEntity session = lessonSessionRepository.findByIdForUpdate(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("课堂不存在"));
        assertInternalCallerBypassesOwnerCheck("generateReport", session);
        return generateReportOn(session);
    }

    @Transactional
    public ReportDetailDTO generateReport(String lessonSessionId, String teacherId) {
        // 行锁串行化同一课堂的并发生成；唯一约束仍是最后防线。
        LessonSessionEntity session = lessonSessionRepository.findByIdForUpdate(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("课堂不存在"));
        assertTeacherOwnsSession(teacherId, session);
        return generateReportOn(session);
    }

    private ReportDetailDTO generateReportOn(LessonSessionEntity session) {
        String lessonSessionId = session.getId();
        var existing = reportRepository.findByLessonSessionIdForUpdate(lessonSessionId);
        EvaluationReportEntity report = existing.orElse(new EvaluationReportEntity());

        if (existing.isPresent() && "published".equalsIgnoreCase(report.getStatus())) {
            // 已发布内容不可原地改写：先固化快照，再开启新草稿版本。
            snapshotVersion(report, session, "published", report.getPublishedBy());
            report.setStatus("draft");
            report.setPublishedAt(null);
            report.setPublishedBy(null);
            report.setContentHash(null);
            report.setVersion((report.getVersion() == null ? 1 : report.getVersion()) + 1);
        }

        if (existing.isEmpty()) {
            report.setId(UUID.randomUUID().toString());
            report.setLessonSessionId(lessonSessionId);
            report.setCourseId(session.getCourseId());
            report.setClassId(session.getClassId());
            report.setTeacherId(session.getTeacherId());
            report.setTitle("课堂评价报告 - " + session.getTitle());
            report.setVersion(1);
        }

        var videoJob = latestSuccessfulJob(lessonSessionId, AnalysisJobEntity.JobType.VIDEO_ANALYSIS);
        var audioJob = latestSuccessfulJob(lessonSessionId, AnalysisJobEntity.JobType.AUDIO_ANALYSIS);
        String behaviorSummary = collectBehaviorSummary(lessonSessionId,
                videoJob.map(AnalysisJobEntity::getId).orElse(null));
        String dataCoverage = buildDataCoverage(lessonSessionId,
                videoJob.map(AnalysisJobEntity::getId).orElse(null),
                audioJob.map(AnalysisJobEntity::getId).orElse(null));

        // 只使用 v1/v1-system-proxy 新口径窗口，旧口径分数不得混入新报告。
        // 并且只读“报告选定版本组合”的窗口（R04）：不能读课堂全局行，否则旧任务的
        // 补偿对齐会让分数来自 J1、截图/任务号来自 J2。
        List<LessonWindowFeatureEntity> windowFeatures = currentProtocolWindows(
                lessonSessionId,
                videoJob.map(AnalysisJobEntity::getId).orElse(null),
                audioJob.map(AnalysisJobEntity::getId).orElse(null));
        boolean hasWindowFeatures = !windowFeatures.isEmpty();

        Double engagementScore;
        Double teachingScore;
        Double climateScore;
        if (demoEnabled) {
            engagementScore = calcDemoEngagementScore(lessonSessionId);
            teachingScore = calcDemoTeachingScore(lessonSessionId);
            climateScore = calcDemoClimateScore(lessonSessionId);
        } else if (hasWindowFeatures) {
            engagementScore = averageScore(windowFeatures, LessonWindowFeatureEntity::getVideoScore);
            teachingScore = averageScore(windowFeatures, LessonWindowFeatureEntity::getSpeechScore);
            climateScore = averageScore(windowFeatures, LessonWindowFeatureEntity::getFusedScore);
        } else {
            engagementScore = null;
            teachingScore = null;
            climateScore = null;
        }

        SuggestionEngine.ReportEvidence evidence =
                buildReportEvidence(windowFeatures, engagementScore, teachingScore, climateScore);

        report.setEngagementScore(engagementScore);
        report.setTeachingScore(teachingScore);
        report.setClimateScore(climateScore);
        report.setAttendanceSummary(null);
        report.setBehaviorSummary(behaviorSummary);
        report.setQuizStatistics("");
        report.setAssignmentSummary("");
        report.setFeedbackSummary("");
        report.setDataCoverage(dataCoverage);
        boolean hasRealEvidence = hasWindowFeatures || dataCoverage.contains("true");
        report.setDataSource(demoEnabled ? "DEMO" : hasRealEvidence ? "REAL" : "EMPTY");

        // R11：窗口数必须只来自**所选版本组合**的窗口行。
        // 旧实现在没有窗口特征时退回"课堂内全部行为观测行数"——那条查询不按
        // jobId 过滤，是版本盲的：报告会显示"窗口 40"，而实际一个对齐窗口都没有。
        // 行为观测数改由 traceability 单独暴露，不冒充窗口数。
        int windowCount = windowFeatures.size();
        int featureCount = windowCount + countPresentSources(dataCoverage);
        int evidenceDenominator = windowCount + EVIDENCE_SOURCE_TOTAL;
        double coverage = evidenceDenominator > 0
                ? Math.min(1.0, (double) featureCount / evidenceDenominator) : 0.0;
        report.setModelVersion(demoEnabled ? "rule-engine-1.0"
                : hasWindowFeatures ? MODEL_WINDOW_AGGREGATOR
                : hasRealEvidence ? "evidence-aggregator-1.0" : null);
        report.setCoverage(coverage);
        report.setWindowCount(windowCount);
        report.setFeatureCount(featureCount);

        // 分析版本可追溯：记录生成该报告时实际成功完成的媒体任务。
        report.setAnalysisVideoJobId(videoJob.map(AnalysisJobEntity::getId).orElse(null));
        report.setAnalysisAudioJobId(audioJob.map(AnalysisJobEntity::getId).orElse(null));
        report.setTraceabilityJson(writeJson(buildTraceability(videoJob.orElse(null), audioJob.orElse(null))));

        List<SuggestionDTO> suggestions;
        if (demoEnabled) {
            var dimScores = new LinkedHashMap<String, Map<String, Double>>();
            dimScores.put("student_engagement", Map.of("behavioral", engagementScore));
            dimScores.put("teaching_quality", Map.of("organization", teachingScore));
            dimScores.put("classroom_climate", Map.of("activity", climateScore));
            suggestions = suggestionEngine.generate(dimScores);
            if (suggestions.isEmpty()) suggestions = suggestionEngine.getDefaultSuggestions();
        } else {
            suggestions = suggestionEngine.generateForReport(evidence);
        }
        persistSuggestions(report, suggestions);
        report.setSuggestions(writeJson(suggestions));

        report.setSummary(generateSummary(climateScore, evidence, hasWindowFeatures,
                videoJob.isPresent() || audioJob.isPresent()));
        report.setStatus("draft");
        reportRepository.save(report);
        log.info("Report generated for lesson {} version {} (videoJob={}, audioJob={})",
                lessonSessionId, report.getVersion(),
                report.getAnalysisVideoJobId(), report.getAnalysisAudioJobId());

        return toDetailDTO(report, session);
    }

    private Double averageScore(List<LessonWindowFeatureEntity> windows,
                                java.util.function.Function<LessonWindowFeatureEntity, Double> getter) {
        List<Double> scores = windows.stream().map(getter).filter(Objects::nonNull).toList();
        return scores.isEmpty() ? null
                : clamp(scores.stream().mapToDouble(Double::doubleValue).average().orElse(0));
    }

    private Optional<AnalysisJobEntity> latestSuccessfulJob(
            String lessonSessionId, AnalysisJobEntity.JobType type) {
        return analysisJobRepository
                .findFirstByTypeAndLessonSessionIdAndStatusOrderByCompletedAtDesc(
                        type, lessonSessionId, AnalysisJobEntity.JobStatus.SUCCEEDED);
    }

    /** 把任务级媒体摘要、模型摘要与运行配置固化到报告。 */
    private Map<String, Object> buildTraceability(AnalysisJobEntity videoJob,
                                                   AnalysisJobEntity audioJob) {
        Map<String, Object> trace = new LinkedHashMap<>();
        trace.put("video", jobTrace(videoJob));
        trace.put("audio", jobTrace(audioJob));
        return trace;
    }

    private Map<String, Object> jobTrace(AnalysisJobEntity job) {
        if (job == null) return Map.of("available", false);
        Map<String, Object> trace = new LinkedHashMap<>();
        trace.put("available", true);
        trace.put("jobId", job.getId());
        trace.put("mediaSha256", job.getSourceHash());
        trace.put("modelDigest", job.getModelDigest());
        trace.put("attempt", job.getAttempt());
        trace.put("completedAt", job.getCompletedAt() == null ? null : job.getCompletedAt().toString());
        Map<String, Object> config = parseMap(job.getConfigJson());
        if (!config.isEmpty()) trace.put("config", config);
        return trace;
    }

    @Transactional(readOnly = true)
    public ReportDetailDTO getReport(String lessonSessionId) {
        // 免校验不是默认行为：内部调用必须显式声明，且不再靠传 null 触发。
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId).orElse(null);
        assertInternalCallerBypassesOwnerCheck("getReport", session);
        return getReportOn(lessonSessionId, session);
    }

    @Transactional(readOnly = true)
    public ReportDetailDTO getReport(String lessonSessionId, String teacherId) {
        EvaluationReportEntity report = reportRepository.findByLessonSessionId(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("报告不存在，请先生成"));
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId).orElse(null);
        assertCanAccessReport(teacherId, report, session);
        return toDetailDTO(report, session);
    }

    private ReportDetailDTO getReportOn(String lessonSessionId, LessonSessionEntity session) {
        EvaluationReportEntity report = reportRepository.findByLessonSessionId(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("报告不存在，请先生成"));
        return toDetailDTO(report, session);
    }

    /**
     * 学生读取报告：仅限已发布且属于自己班级的课堂。
     */
    @Transactional(readOnly = true)
    public ReportDetailDTO getReportForStudent(String lessonSessionId, String studentUserId) {
        EvaluationReportEntity report = reportRepository.findByLessonSessionId(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("报告不存在"));
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new java.util.NoSuchElementException("课堂不存在"));
        StudentEntity student = studentRepository.findById(studentUserId)
                .orElseThrow(() -> new java.util.NoSuchElementException("学生账号不存在"));
        if (student.getClassId() == null || !student.getClassId().equals(session.getClassId())) {
            throw new AccessDeniedException("只能查看自己班级的报告");
        }
        if (!"published".equalsIgnoreCase(report.getStatus())) {
            // 重新生成会把工作行改为 draft；学生仍应看到最近一次已发布版本。
            return reportVersionRepository
                    .findFirstByLessonSessionIdAndStatusOrderByVersionDesc(lessonSessionId, "published")
                    .map(version -> snapshotDetail(version, session))
                    .orElseThrow(() -> new AccessDeniedException("报告尚未发布"));
        }
        return toDetailDTO(report, session);
    }

    /** 教师查看某个已发布版本的内容（快照可追溯）。 */
    @Transactional(readOnly = true)
    public ReportDetailDTO getVersionDetail(String lessonSessionId, Integer version, String teacherId) {
        return findVersionDetail(lessonSessionId, version, teacherId)
                .orElseThrow(() -> new java.util.NoSuchElementException("报告版本不存在"));
    }

    /**
     * 按版本号读取已发布快照，缺失时返回 {@link Optional#empty()} 而不是抛异常。
     *
     * <p>供证据接口使用：请求的版本必须精确对应<b>已发布快照</b>，快照缺失时上层
     * 返回空证据列表，绝不退回到当前工作行（草稿）的证据。</p>
     *
     * <p>属主校验与 {@link #getVersionDetail} 完全一致，保持 fail-closed。</p>
     */
    @Transactional(readOnly = true)
    public Optional<ReportDetailDTO> findVersionDetail(String lessonSessionId, Integer version,
                                                       String teacherId) {
        // 课堂不存在是 404 语义，不能被通用异常处理器映射成 500。
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new java.util.NoSuchElementException("课堂不存在"));
        assertTeacherOwnsSession(teacherId, session);
        if (version == null) {
            return Optional.empty();
        }
        return reportVersionRepository
                .findByLessonSessionIdAndVersion(lessonSessionId, version)
                .map(entity -> snapshotDetail(entity, session));
    }

    private ReportDetailDTO snapshotDetail(EvaluationReportVersionEntity version,
                                           LessonSessionEntity session) {
        Map<String, Object> payload = parseMap(version.getSnapshotJson());
        Object detail = payload.get("detail");
        if (detail instanceof Map<?, ?>) {
            try {
                return objectMapper.convertValue(detail, ReportDetailDTO.class);
            } catch (Exception e) {
                log.warn("Could not convert report snapshot {} detail: {}", version.getId(), e.getMessage());
            }
        }
        // 兼容整改前生成的旧快照（只有平铺字段，没有完整 detail）。
        String courseName = session == null ? "" : courseRepository.findById(session.getCourseId())
                .map(CourseEntity::getName).orElse("");
        String className = session == null ? "" : classGroupRepository.findById(session.getClassId())
                .map(ClassGroupEntity::getName).orElse("");
        Double climate = snapshotDouble(payload, "climateScore");
        Double engagement = snapshotDouble(payload, "engagementScore");
        Double teaching = snapshotDouble(payload, "teachingScore");
        String dataSource = snapshotString(payload, "dataSource");
        List<SuggestionDTO> suggestions = List.of();
        Object rawSuggestions = payload.get("suggestions");
        if (rawSuggestions != null) {
            try {
                suggestions = objectMapper.convertValue(rawSuggestions,
                        new com.fasterxml.jackson.core.type.TypeReference<List<SuggestionDTO>>() {});
            } catch (Exception e) {
                log.debug("Could not parse snapshot suggestions {}", version.getId());
            }
        }
        Map<String, String> validation = new LinkedHashMap<>();
        validation.put("climate", climate == null ? "unavailable" : "pending_validation");
        validation.put("engagement", engagement == null ? "unavailable" : "engineering_mapping_not_validated");
        validation.put("teaching", teaching == null ? "unavailable" : "engineering_mapping_not_validated");
        return new ReportDetailDTO(
                (String) payload.getOrDefault("reportId", version.getReportId()),
                version.getLessonSessionId(), courseName, className, "",
                snapshotString(payload, "title"), snapshotString(payload, "summary"),
                engagement, teaching, climate, climate,
                snapshotString(payload, "dataCoverage"),
                null,
                snapshotString(payload, "behaviorSummary"), "", "", "",
                suggestions, dataSource == null ? "EMPTY" : dataSource,
                new ReportDetailDTO.Evidence(dataSource == null ? "EMPTY" : dataSource,
                        snapshotString(payload, "modelVersion"),
                        snapshotDouble(payload, "coverage"),
                        snapshotInt(payload, "windowCount"),
                        snapshotInt(payload, "featureCount"),
                        Map.of()),
                "published", version.getPublishedAt(), version.getCreatedAt(), validation,
                version.getVersion(), version.getAnalysisVideoJobId(),
                version.getAnalysisAudioJobId(), version.getPublishedBy());
    }

    private Double snapshotDouble(Map<String, Object> snapshot, String key) {
        Object detail = snapshot.get("detail");
        Object value = detail instanceof Map<?, ?> map ? map.get(key) : snapshot.get(key);
        return doubleValue(value);
    }

    private Integer snapshotInt(Map<String, Object> snapshot, String key) {
        Object detail = snapshot.get("detail");
        Object value = detail instanceof Map<?, ?> map ? map.get(key) : snapshot.get(key);
        return intValue(value);
    }

    private String snapshotString(Map<String, Object> snapshot, String key) {
        Object detail = snapshot.get("detail");
        Object value = detail instanceof Map<?, ?> map ? map.get(key) : snapshot.get(key);
        return value == null ? null : String.valueOf(value);
    }

    @Transactional
    public ReportDetailDTO publishReport(String lessonSessionId) {
        // 免校验不是默认行为：内部调用必须显式声明，且不再靠传 null 触发。
        LessonSessionEntity session = lessonSessionRepository.findByIdForUpdate(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("课堂不存在"));
        assertInternalCallerBypassesOwnerCheck("publishReport", session);
        return publishReportOn(lessonSessionId, session, null);
    }

    @Transactional
    public ReportDetailDTO publishReport(String lessonSessionId, String teacherId) {
        LessonSessionEntity session = lessonSessionRepository.findByIdForUpdate(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("课堂不存在"));
        assertTeacherOwnsSession(teacherId, session);
        return publishReportOn(lessonSessionId, session, teacherId);
    }

    private ReportDetailDTO publishReportOn(String lessonSessionId, LessonSessionEntity session,
                                            String teacherId) {
        EvaluationReportEntity report = reportRepository.findByLessonSessionIdForUpdate(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("报告不存在"));

        if ("published".equalsIgnoreCase(report.getStatus())) {
            throw new com.macteval.exception.BusinessException(409, "报告已发布；如需更新请先生成新草稿");
        }
        if (report.getDataSource() == null || "EMPTY".equalsIgnoreCase(report.getDataSource())) {
            throw new com.macteval.exception.BusinessException(409, "报告无有效数据，不能发布");
        }
        if (report.getClimateScore() == null) {
            throw new com.macteval.exception.BusinessException(409, "课堂氛围分不可用，不能发布");
        }

        // 发布即固化内容快照；之后重新生成只改工作行，不改变历史发布内容。
        // 先写入发布状态，快照 detail 才能正确标记 published/publishedAt。
        report.setStatus("published");
        report.setPublishedAt(LocalDateTime.now());
        report.setPublishedBy(teacherId);
        String hash = snapshotVersion(report, session, "published", teacherId);
        report.setContentHash(hash);
        reportRepository.save(report);
        log.info("Report published: lesson={} version={} by={}",
                lessonSessionId, report.getVersion(), teacherId);
        return toDetailDTO(report, session);
    }

    @Transactional(readOnly = true)
    public List<ReportVersionSummaryDTO> getVersionHistory(String lessonSessionId, String teacherId) {
        // 课堂不存在是 404 语义，不能被通用异常处理器映射成 500。
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new java.util.NoSuchElementException("课堂不存在"));
        assertTeacherOwnsSession(teacherId, session);
        return reportVersionRepository.findByLessonSessionIdOrderByVersionDesc(lessonSessionId).stream()
                .map(v -> {
                    Map<String, Object> payload = parseMap(v.getSnapshotJson());
                    // 版本选择器需要按版本显示“数据来源/氛围分”；快照缺失时保持 null，
                    // 不伪造默认值（否则旧快照会显示成 EMPTY 或 0 分）。
                    return new ReportVersionSummaryDTO(
                            v.getVersion(), v.getStatus(), v.getPublishedAt(), v.getPublishedBy(),
                            v.getContentHash(), v.getAnalysisVideoJobId(), v.getAnalysisAudioJobId(),
                            snapshotString(payload, "dataSource"),
                            snapshotDouble(payload, "climateScore"));
                })
                .toList();
    }

    /**
     * 属主校验。{@code teacherId} 缺失一律拒绝：传 null 不再等同于「跳过校验」。
     * 内部免校验路径必须显式调用 {@link #assertInternalCallerBypassesOwnerCheck}。
     */
    private void assertTeacherOwnsSession(String teacherId, LessonSessionEntity session) {
        if (teacherId == null || teacherId.isBlank()) {
            throw new IllegalArgumentException("teacherId is required for owner checks");
        }
        if (isAdminCaller()) {
            return;
        }
        // 课堂无属主时不再短路放行：普通教师同样无权操作。
        if (!teacherId.equals(session.getTeacherId())) {
            throw new AccessDeniedException("无权操作其他教师的课堂报告");
        }
    }

    /**
     * 内部调用（无外部主体）的显式免校验入口：仅记录审计日志，不做属主判断。
     *
     * <p>只允许同一 JVM 内的服务/测试使用；HTTP 层必须走带 teacherId 的重载。</p>
     */
    void assertInternalCallerBypassesOwnerCheck(String operation, LessonSessionEntity session) {
        log.debug("Owner check explicitly bypassed for internal call: operation={}, lesson={}, owner={}",
                operation, session == null ? null : session.getId(),
                session == null ? null : session.getTeacherId());
    }

    private boolean isAdminCaller() {
        var authentication = org.springframework.security.core.context.SecurityContextHolder
                .getContext().getAuthentication();
        return authentication != null && authentication.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
    }

    /**
     * 删除评价报告（数据删除 / 授权撤回）。
     * 同时删除该课堂的报告版本快照、待处理建议与识别证据帧；
     * 已采纳建议保留以维持效果追踪审计。
     * 原始课堂媒体文件由 DataRetentionService / 媒体资产生命周期统一清理。
     */
    @Transactional
    public void deleteReport(String lessonSessionId) {
        // 免校验不是默认行为：内部调用必须显式声明，且不再靠传 null 触发。
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId).orElse(null);
        assertInternalCallerBypassesOwnerCheck("deleteReport", session);
        EvaluationReportEntity report = reportRepository.findByLessonSessionId(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("报告不存在"));
        deleteReportOn(report);
    }

    @Transactional
    public void deleteReport(String lessonSessionId, String teacherId) {
        EvaluationReportEntity report = reportRepository.findByLessonSessionId(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("报告不存在"));
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId).orElse(null);
        assertCanAccessReport(teacherId, report, session);
        deleteReportOn(report);
    }

    private void deleteReportOn(EvaluationReportEntity report) {
        String lessonSessionId = report.getLessonSessionId();
        reportVersionRepository.deleteAll(
                reportVersionRepository.findByLessonSessionIdOrderByVersionDesc(lessonSessionId));
        var pending = suggestionRepository.findByLessonSessionIdAndStatus(lessonSessionId, "pending");
        if (!pending.isEmpty()) suggestionRepository.deleteAll(pending);
        // 证据帧放在删除报告行之前：帧行与报告行没有外键，顺序不影响正确性；
        // 但先把影像删掉，能让事务即便在后面失败回滚时也不再残留可见的课堂截图为先、
        // 元数据为后的观感最差情形——回滚后残留的帧行会在下一次删除时按 NOT_FOUND 清掉。
        deleteEvidenceFrames(lessonSessionId);
        reportRepository.delete(report);
        log.info("PRIVACY_DELETE reportId={}, lessonSessionId={}, deletedAt={}",
                report.getId(), lessonSessionId, LocalDateTime.now());
    }

    /**
     * 删除某课堂的全部识别证据帧：先删 MinIO 对象，再删数据库行（SEC-11）。
     *
     * <p><b>为什么失败时不删行</b>：这与 {@code MediaAssetService.cleanupExpiredAssets}
     * 的既有约定一致——「删除失败（MinIO 抖动/权限）不能标记已过期，否则下次查询
     * 不会再选中它，含学生影像的对象会永久滞留」。证据帧的到期扫描按 {@code created_at}
     * 选行，行一旦删掉，90 天清理任务就再也找不到这个对象，课堂截图会永久留在桶里。
     * 因此：对象没删掉 → 保留行 → 记 {@code PRIVACY_DELETE_EVIDENCE_FRAME_FAILED}
     * WARN → 夜间保留期清理继续重试。</p>
     *
     * <p><b>为什么删除失败后还要 probe</b>：{@link MinioService#removeObject} 把任何异常
     * （含 403/存储不可达）折叠成 {@code false}，无法区分「对象本来就不存在」和
     * 「我们没权限/连不上」。前一种情况行必须删掉（否则永远留一条指向空气的行），
     * 后一种情况行必须保留（否则对象仍在、行已失联）。{@link MinioService#probeObject}
     * 的 {@code NOT_FOUND} 只来自明确的 NoSuchKey/NoSuchObject/NoSuchBucket，
     * {@code ERROR} 覆盖 403/超时/5xx——正是这里需要的区分。probe 只在删除失败后调用，
     * 而不是每帧先 probe 一次：正常路径只付一次请求的代价。</p>
     */
    private void deleteEvidenceFrames(String lessonSessionId) {
        // 课堂 ID 缺失时不能执行删除：按 null 查会命中「lesson_session_id is null」的
        // 孤行，把不属于任何课堂的帧也删掉。宁可少删，交给运维排查。
        if (lessonSessionId == null || lessonSessionId.isBlank()) {
            log.warn("PRIVACY_DELETE_EVIDENCE_FRAME_SKIPPED lessonSessionId=null; "
                    + "cannot scope evidence frames to a lesson");
            return;
        }
        if (evidenceFrameRepository == null || minioService == null) {
            // 只有测试里手工 new 出来的实例会走到这里；生产由 Spring 注入。
            log.warn("PRIVACY_DELETE_EVIDENCE_FRAME_SKIPPED lessonSessionId={}; "
                    + "evidence frame repository or object store not wired", lessonSessionId);
            return;
        }
        List<BehaviorEvidenceFrameEntity> frames =
                evidenceFrameRepository.findByLessonSessionIdOrderByWindowIndexAsc(lessonSessionId);
        for (BehaviorEvidenceFrameEntity frame : frames) {
            boolean alreadyMissing = false;
            if (!minioService.removeObject(frame.getBucket(), frame.getObjectKey())) {
                // removeObject 只回 false，不说明原因。这里才 probe 一次，把「对象早已不存在」
                // 与「删不掉」分开：前者必须删行，后者必须留行。顺序刻意如此——正常路径只发
                // 一次删除请求，不为每一帧多付一次 stat。
                MinioService.ObjectProbe probe =
                        minioService.probeObject(frame.getBucket(), frame.getObjectKey());
                if (probe.isNotFound()) {
                    alreadyMissing = true;
                } else if (probe.isError()) {
                    // ERROR（403/超时/5xx）绝不能当作「不存在」：对象可能还在桶里。
                    log.warn("PRIVACY_DELETE_EVIDENCE_FRAME_FAILED frameId={}, bucket={}, objectKey={}, "
                                    + "errorCode={}; object state unknown, keeping row for retention retry",
                            frame.getId(), frame.getBucket(), frame.getObjectKey(), probe.errorCode());
                    continue;
                } else {
                    // FOUND：对象确实还在，只是这次没删掉（权限/抖动）。
                    log.warn("PRIVACY_DELETE_EVIDENCE_FRAME_FAILED frameId={}, bucket={}, objectKey={}; "
                                    + "object still present, keeping row for retention retry",
                            frame.getId(), frame.getBucket(), frame.getObjectKey());
                    continue;
                }
            }
            evidenceFrameRepository.deleteById(frame.getId());
            log.info("PRIVACY_DELETE_EVIDENCE_FRAME frameId={}, lessonSessionId={}, bucket={}, "
                            + "objectKey={}, alreadyMissing={}, deletedAt={}",
                    frame.getId(), lessonSessionId, frame.getBucket(), frame.getObjectKey(),
                    alreadyMissing, LocalDateTime.now());
        }
    }

    private void assertCanAccessReport(String teacherId, EvaluationReportEntity report, LessonSessionEntity session) {
        if (teacherId == null || teacherId.isBlank()) {
            throw new IllegalArgumentException("teacherId is required for owner checks");
        }
        if (isAdminCaller()) {
            return;
        }
        // 课堂/报告无属主时不再短路放行：普通教师同样无权访问。
        if (session != null) {
            if (teacherId.equals(session.getTeacherId())) {
                return;
            }
        } else if (teacherId.equals(report.getTeacherId())) {
            return;
        }
        throw new AccessDeniedException("无权访问其他教师的课堂报告");
    }

    // =====================================================================
    // 建议持久化
    // =====================================================================

    private void persistSuggestions(EvaluationReportEntity report, List<SuggestionDTO> suggestions) {
        if (report.getLessonSessionId() == null) return;
        // 重新生成时只替换未处理建议；已采纳/忽略记录保留，保证效果追踪可审计。
        var oldPending = suggestionRepository.findByLessonSessionIdAndStatus(
                report.getLessonSessionId(), "pending");
        if (!oldPending.isEmpty()) {
            suggestionRepository.deleteAll(oldPending);
        }
        if (suggestions.isEmpty()) return;

        List<SuggestionEntity> rows = suggestions.stream().map(dto -> {
            SuggestionEntity entity = new SuggestionEntity();
            entity.setId(dto.id());
            entity.setLessonSessionId(report.getLessonSessionId());
            entity.setReportId(report.getId());
            entity.setCategory(dto.category());
            entity.setPriority(dto.priority());
            entity.setContent(dto.content());
            entity.setEvidence(dto.evidence());
            entity.setRuleId(dto.ruleId());
            entity.setRuleVersion(dto.ruleVersion());
            entity.setEvidenceRefs(dto.evidenceRefs());
            entity.setReportVersion(report.getVersion());
            entity.setStatus("pending");
            return entity;
        }).toList();
        suggestionRepository.saveAll(rows);
    }

    private List<SuggestionDTO> suggestionsFromDatabase(String lessonSessionId) {
        List<SuggestionEntity> rows = suggestionRepository.findByLessonSessionId(lessonSessionId);
        if (rows.isEmpty()) return List.of();
        return rows.stream()
                .sorted(Comparator.comparing(SuggestionEntity::getCreatedAt,
                        Comparator.nullsLast(Comparator.naturalOrder())))
                .map(e -> new SuggestionDTO(
                        e.getId(), e.getCategory(), e.getPriority(), e.getContent(), e.getEvidence(),
                        e.getStatus(), e.getCreatedAt(), resolvedAt(e),
                        e.getRuleId(), e.getRuleVersion(), e.getEvidenceRefs()))
                .toList();
    }

    private LocalDateTime resolvedAt(SuggestionEntity entity) {
        LocalDateTime resolved = null;
        for (LocalDateTime candidate : Arrays.asList(
                entity.getAdoptedAt(), entity.getAcceptedAt(), entity.getIgnoredAt())) {
            if (candidate != null && (resolved == null || candidate.isAfter(resolved))) {
                resolved = candidate;
            }
        }
        return resolved;
    }

    // =====================================================================
    // 证据与摘要
    // =====================================================================

    private SuggestionEngine.ReportEvidence buildReportEvidence(
            List<LessonWindowFeatureEntity> windows,
            Double videoScore, Double speechScore, Double climateScore) {
        int usable = 0, limited = 0, unavailable = 0;
        int lowVideo = 0, lowSpeech = 0;
        int videoCount = 0, speechCount = 0;
        double uprightSum = 0;
        int uprightCount = 0;
        double coverageSum = 0;
        int coverageCount = 0;
        double rateSum = 0;
        int rateCount = 0;
        int questionSum = 0;

        for (LessonWindowFeatureEntity w : windows) {
            Map<String, Object> vf = parseMap(w.getVideoFeatures());
            Map<String, Object> sf = parseMap(w.getSpeechFeatures());
            String quality = stringValue(vf.get("data_quality"));
            if ("usable".equals(quality)) usable++;
            else if ("limited".equals(quality)) limited++;
            else if ("unavailable".equals(quality)) unavailable++;

            Double upright = doubleValue(vf.get("upright_proportion"));
            if (upright != null) {
                uprightSum += upright;
                uprightCount++;
            }
            if (w.getVideoScore() != null) {
                videoCount++;
                if (w.getVideoScore() < 60) lowVideo++;
            }
            Double coverage = doubleValue(sf.get("speech_coverage"));
            if (coverage != null) {
                coverageSum += coverage;
                coverageCount++;
            }
            Double rate = doubleValue(sf.get("speech_rate"));
            if (rate != null) {
                rateSum += rate;
                rateCount++;
            }
            Integer questions = intValue(sf.get("question_count"));
            if (questions != null) questionSum += questions;
            if (w.getSpeechScore() != null) {
                speechCount++;
                if (w.getSpeechScore() < 60) lowSpeech++;
            }
        }

        int n = windows.size();
        return new SuggestionEngine.ReportEvidence(
                videoScore, speechScore, climateScore,
                n, usable, limited, unavailable, lowVideo, lowSpeech,
                uprightCount == 0 ? null : uprightSum / uprightCount,
                coverageCount == 0 ? null : coverageSum / coverageCount,
                n == 0 ? null : (double) questionSum / n,
                rateCount == 0 ? null : rateSum / rateCount,
                videoCount > 0, speechCount > 0
        );
    }

    private String generateSummary(Double climate, SuggestionEngine.ReportEvidence e, boolean hasWindows) {
        return generateSummary(climate, e, hasWindows, false);
    }

    /**
     * @param jobSelected 报告是否已选定至少一个成功分析任务。已选定任务但没有窗口
     *                    意味着对齐未完成/结果为空，必须与"从未有过分析任务"区分开，
     *                    不能笼统写成同一句话（R11）。
     */
    private String generateSummary(Double climate, SuggestionEngine.ReportEvidence e,
                                   boolean hasWindows, boolean jobSelected) {
        if (climate == null) {
            if (hasWindows) {
                return "已汇总课堂证据，但课堂氛围融合分不可用；请检查视频/语音覆盖与数据质量。";
            }
            if (jobSelected) {
                return "已选定分析任务，但该任务版本的统一时间窗尚未生成或对齐仍在进行，"
                        + "因此本次不生成课堂氛围分（数据不足）。请等待对齐完成后重新生成报告；"
                        + "不会用历史版本的窗口分数代替。";
            }
            return "已汇总课堂证据；当前没有可用的统一时间窗特征，因此不生成课堂氛围分。";
        }
        String base = climate >= 85 ? "课堂氛围融合分为优秀水平。"
                : climate >= 75 ? "课堂氛围融合分良好，仍有提升空间。"
                : climate >= 60 ? "课堂氛围融合分一般，建议关注低分窗口。"
                : "课堂氛围融合分偏低，建议结合教学建议调整。";
        String availability = String.format(Locale.ROOT,
                " 有效窗口 %d 个%s%s。", e.windowCount(),
                e.videoAvailable() ? "" : "（视频缺失）",
                e.speechAvailable() ? "" : "（语音缺失）");
        return base + availability + " 该分数为规则基线 + 晚期融合的工程结果，效度待公开课堂标注验证。";
    }

    private String collectBehaviorSummary(String lessonId, String videoJobId) {
        List<BehaviorObservationEntity> obs = demoEnabled
                ? behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc(lessonId)
                : videoJobId == null ? List.of()
                : behaviorRepository.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(lessonId, videoJobId);
        if (obs.isEmpty()) return "暂无行为数据";
        double uprightSum = 0;
        int uprightCount = 0;
        for (BehaviorObservationEntity o : obs) {
            Double proportion = doubleValue(o.getAggregatedUprightProportion());
            if (proportion != null) {
                uprightSum += proportion;
                uprightCount++;
            }
        }
        String uprightText = uprightCount == 0 ? "不可用"
                : String.format(Locale.ROOT, "%.0f%%", uprightSum / uprightCount * 100);
        return "端正坐姿比例均值: " + uprightText + "；举手次数暂未统计，请结合行为识别画面复核";
    }

    /**
     * 数据覆盖证据。视频/语音两项按报告选定版本组合统计（R04）：
     * 否则旧版本的窗口会把“有视频分”写进新报告，与其他字段来源不一致。
     */
    private String buildDataCoverage(String lessonId, String videoJobId, String audioJobId) {
        // R11：behavior 也必须按所选视频任务统计。课堂级查询是版本盲的，会把旧任务的行为观测算成"本报告有行为证据"，与 video/speech 两项的口径不一致。
        boolean hasBehavior = videoJobId == null
                ? !behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc(lessonId).isEmpty()
                : !behaviorRepository
                        .findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(lessonId, videoJobId)
                        .isEmpty();
        List<LessonWindowFeatureEntity> windows = WindowVersionSelector.read(
                windowFeatureRepository, lessonId, videoJobId, audioJobId);
        boolean hasVideo = windows.stream().anyMatch(w -> w.getVideoScore() != null);
        boolean hasSpeech = windows.stream().anyMatch(w -> w.getSpeechScore() != null);
        try {
            return objectMapper.writeValueAsString(Map.of(
                    "behavior", hasBehavior,
                    "video", hasVideo,
                    "speech", hasSpeech
            ));
        } catch (Exception e) {
            log.warn("Failed to serialize data coverage for lesson {}", lessonId, e);
            return "{\"behavior\":false,\"video\":false,\"speech\":false}";
        }
    }

    /** 只接受 v1 / v1-system-proxy 新口径窗口，避免旧口径分数混入。 */
    private boolean isCurrentProtocol(LessonWindowFeatureEntity entity) {
        return hasV1Protocol(entity.getVideoFeatures()) || hasV1Protocol(entity.getSpeechFeatures());
    }

    private boolean hasV1Protocol(String json) {
        if (json == null || json.isBlank()) return false;
        try {
            var node = objectMapper.readTree(json);
            var protocol = node.get("rule_protocol");
            return protocol != null && protocol.asText().startsWith("v1");
        } catch (Exception e) {
            return false;
        }
    }

    private List<LessonWindowFeatureEntity> currentProtocolWindows(String lessonId) {
        return currentProtocolWindows(lessonId, null, null);
    }

    /**
     * 指定分析版本组合的 v1 窗口（R04）。
     *
     * <p>版本组合没有行时回退到“两列皆空”的历史行；仍没有则视为无窗口特征。</p>
     */
    private List<LessonWindowFeatureEntity> currentProtocolWindows(String lessonId,
                                                                   String videoJobId,
                                                                   String audioJobId) {
        return WindowVersionSelector.read(windowFeatureRepository, lessonId, videoJobId, audioJobId)
                .stream().filter(this::isCurrentProtocol).toList();
    }

    private double calcDemoEngagementScore(String lessonId) {
        List<BehaviorObservationEntity> obs = behaviorRepository.findByLessonSessionIdOrderByWindowStartAsc(lessonId);
        if (obs.isEmpty()) return 75.0;
        double upright = obs.stream().filter(o -> o.getCoverage() != null)
                .mapToDouble(BehaviorObservationEntity::getCoverage).average().orElse(0.7);
        return Math.min(100, Math.max(0, upright * 100));
    }

    private double calcDemoTeachingScore(String lessonId) {
        List<Double> speechScores = currentProtocolWindows(lessonId).stream()
                .map(LessonWindowFeatureEntity::getSpeechScore)
                .filter(Objects::nonNull)
                .toList();
        if (speechScores.isEmpty()) return 78.0;
        return clamp(speechScores.stream().mapToDouble(Double::doubleValue).average().orElse(78.0));
    }

    private double calcDemoClimateScore(String lessonId) {
        List<Double> fusedScores = currentProtocolWindows(lessonId).stream()
                .map(LessonWindowFeatureEntity::getFusedScore)
                .filter(Objects::nonNull)
                .toList();
        if (fusedScores.isEmpty()) return 76.0;
        return clamp(fusedScores.stream().mapToDouble(Double::doubleValue).average().orElse(76.0));
    }

    // =====================================================================
    // 版本快照
    // =====================================================================

    private String snapshotVersion(EvaluationReportEntity report, LessonSessionEntity session,
                                   String status, String publishedBy) {
        int version = report.getVersion() == null ? 1 : report.getVersion();
        var existing = reportVersionRepository.findByLessonSessionIdAndVersion(
                report.getLessonSessionId(), version);
        if (existing.isPresent()) {
            return existing.get().getContentHash();
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("reportId", report.getId());
        payload.put("lessonSessionId", report.getLessonSessionId());
        payload.put("title", report.getTitle());
        payload.put("summary", report.getSummary());
        payload.put("engagementScore", report.getEngagementScore());
        payload.put("teachingScore", report.getTeachingScore());
        payload.put("climateScore", report.getClimateScore());
        payload.put("dataSource", report.getDataSource());
        payload.put("dataCoverage", report.getDataCoverage());
        payload.put("behaviorSummary", report.getBehaviorSummary());
        payload.put("suggestions", report.getSuggestions());
        payload.put("modelVersion", report.getModelVersion());
        payload.put("coverage", report.getCoverage());
        payload.put("windowCount", report.getWindowCount());
        payload.put("featureCount", report.getFeatureCount());
        payload.put("version", version);
        payload.put("status", status);
        payload.put("analysisVideoJobId", report.getAnalysisVideoJobId());
        payload.put("analysisAudioJobId", report.getAnalysisAudioJobId());
        payload.put("courseName", courseRepository.findById(session.getCourseId())
                .map(CourseEntity::getName).orElse(""));
        payload.put("className", classGroupRepository.findById(session.getClassId())
                .map(ClassGroupEntity::getName).orElse(""));
        payload.put("publishedBy", publishedBy);
        LocalDateTime publishedAt = report.getPublishedAt() != null
                ? report.getPublishedAt() : LocalDateTime.now();
        payload.put("publishedAt", publishedAt.toString());
        // 完整 DTO 快照：学生/教师可在重新生成后直接读取历史发布内容。
        try {
            payload.put("detail", objectMapper.convertValue(
                    toDetailDTO(report, session), Map.class));
        } catch (Exception e) {
            log.warn("Could not serialize report detail snapshot for {}", report.getId(), e);
        }

        String json = writeJson(payload);
        String hash = sha256(json);
        EvaluationReportVersionEntity entity = new EvaluationReportVersionEntity();
        entity.setId(UUID.randomUUID().toString());
        entity.setReportId(report.getId());
        entity.setLessonSessionId(report.getLessonSessionId());
        entity.setVersion(version);
        entity.setStatus(status);
        entity.setSnapshotJson(json);
        entity.setContentHash(hash);
        entity.setAnalysisVideoJobId(report.getAnalysisVideoJobId());
        entity.setAnalysisAudioJobId(report.getAnalysisAudioJobId());
        entity.setPublishedBy(publishedBy);
        entity.setPublishedAt(publishedAt);
        reportVersionRepository.save(entity);
        return hash;
    }

    private static String sha256(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (Exception e) {
            return null;
        }
    }

    // =====================================================================
    // DTO 映射
    // =====================================================================

    private ReportDetailDTO toDetailDTO(EvaluationReportEntity report, LessonSessionEntity session) {
        String courseName = "", className = "";
        if (session != null) {
            courseName = courseRepository.findById(session.getCourseId()).map(CourseEntity::getName).orElse("");
            className = classGroupRepository.findById(session.getClassId()).map(ClassGroupEntity::getName).orElse("");
        }

        // 主评价目标只有一个：课堂氛围融合分。参与度/教学质量是辅助工程映射。
        Double overall = report.getClimateScore();

        List<SuggestionDTO> suggestions = suggestionsFromDatabase(report.getLessonSessionId());
        if (suggestions.isEmpty() && report.getSuggestions() != null && !report.getSuggestions().isBlank()) {
            try {
                var list = objectMapper.readValue(report.getSuggestions(), List.class);
                suggestions = objectMapper.convertValue(list,
                        new com.fasterxml.jackson.core.type.TypeReference<List<SuggestionDTO>>() {});
            } catch (Exception e) {
                log.debug("Could not parse legacy suggestions for report {}", report.getId());
            }
        }

        java.util.Map<String, String> scoreValidation = new java.util.LinkedHashMap<>();
        scoreValidation.put("climate", report.getClimateScore() == null
                ? "unavailable" : ("DEMO".equalsIgnoreCase(report.getDataSource())
                        ? "demo_only" : "pending_validation"));
        scoreValidation.put("engagement", report.getEngagementScore() == null
                ? "unavailable" : "engineering_mapping_not_validated");
        scoreValidation.put("teaching", report.getTeachingScore() == null
                ? "unavailable" : "engineering_mapping_not_validated");
        // R11：把"数据不足"的三种成因做成机器可读状态，而不是只靠摘要自由文本。
        // 前端与验收脚本据此区分"从未有分析任务""任务已成功但窗口未生成""窗口已就绪"。
        scoreValidation.put("windowState", windowState(report));

        return new ReportDetailDTO(
                report.getId(), report.getLessonSessionId(), courseName, className, "",
                report.getTitle(), report.getSummary(),
                report.getEngagementScore(), report.getTeachingScore(), report.getClimateScore(),
                overall, report.getDataCoverage(), null,
                report.getBehaviorSummary(), report.getQuizStatistics(),
                report.getAssignmentSummary(), report.getFeedbackSummary(),
                suggestions, report.getDataSource(), buildEvidence(report), report.getStatus(),
                report.getPublishedAt(), report.getCreatedAt(), scoreValidation,
                report.getVersion(), report.getAnalysisVideoJobId(),
                report.getAnalysisAudioJobId(), report.getPublishedBy()
        );
    }

    /**
     * R11：窗口状态的机器可读判定。
     *
     * <p>判定基准必须是<b>版本精确</b>且已持久化的信号，不能用 {@code windowCount}：
     * 该字段在没有窗口特征时会退回“课堂内全部行为观测行”，那是版本盲的，会让
     * “J2 已成功但窗口未生成”的报告错误地显示为 {@code windows_ready}。这里改用
     * 生成报告时写入的 {@code modelVersion}（与所选版本组合一一对应）：
     * {@code window-aggregator-1.0} 表示确有所选版本的窗口行，否则没有。</p>
     *
     * <ul>
     *   <li>{@code legacy_no_job} —— 从未选定成功的分析任务（纯历史）：允许读取
     *       两列皆空的历史行，分数可能来自历史窗口。</li>
     *   <li>{@code windows_pending} —— 已选定成功任务，但该版本组合没有窗口行：
     *       对齐尚未完成或结果为空，属“数据不足”，不得回退历史行。</li>
     *   <li>{@code windows_ready} —— 存在所选版本的窗口行。</li>
     * </ul>
     */
    private String windowState(EvaluationReportEntity report) {
        boolean hasAnyJob = report.getAnalysisVideoJobId() != null
                || report.getAnalysisAudioJobId() != null;
        boolean versionedWindows = MODEL_WINDOW_AGGREGATOR.equals(report.getModelVersion());
        if (versionedWindows) {
            return "windows_ready";
        }
        return hasAnyJob ? "windows_pending" : "legacy_no_job";
    }

    private ReportDetailDTO.Evidence buildEvidence(EvaluationReportEntity report) {
        String dataSource = report.getDataSource() != null ? report.getDataSource() : "EMPTY";
        String modelVersion = report.getModelVersion();
        Double coverage = report.getCoverage();
        Integer windowCount = report.getWindowCount();
        Integer featureCount = report.getFeatureCount();

        Map<String, Object> traceability = parseMap(report.getTraceabilityJson());
        if ("DEMO".equalsIgnoreCase(dataSource)) {
            return new ReportDetailDTO.Evidence("DEMO",
                    modelVersion != null ? modelVersion : "rule-engine-1.0",
                    coverage != null ? coverage : 0.75,
                    windowCount != null ? windowCount : 40,
                    featureCount != null ? featureCount : 5,
                    traceability);
        }
        if ("REAL".equalsIgnoreCase(dataSource)) {
            return new ReportDetailDTO.Evidence("REAL",
                    modelVersion != null ? modelVersion : "evidence-aggregator-1.0",
                    coverage != null ? coverage : 0.0,
                    windowCount != null ? windowCount : 0,
                    featureCount != null ? featureCount : 0,
                    traceability);
        }
        return new ReportDetailDTO.Evidence("EMPTY", "N/A", 0.0, 0, 0, traceability);
    }

    private int countPresentSources(String dataCoverage) {
        if (dataCoverage == null || dataCoverage.isBlank()) return 0;
        try {
            var node = objectMapper.readTree(dataCoverage);
            int count = 0;
            for (String source : List.of("behavior", "video", "speech")) {
                if (node.path(source).asBoolean(false)) count++;
            }
            return count;
        } catch (Exception e) {
            log.warn("Failed to parse dataCoverage for evidence: {}", dataCoverage, e);
            return 0;
        }
    }

    private Map<String, Object> parseMap(String json) {
        if (json == null || json.isBlank()) return Map.of();
        try {
            return objectMapper.readValue(json,
                    new com.fasterxml.jackson.core.type.TypeReference<Map<String, Object>>() {});
        } catch (Exception e) {
            return Map.of();
        }
    }

    private String writeJson(Object value) {
        if (value == null) return null;
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            log.warn("Failed to serialize report payload", e);
            return null;
        }
    }

    private static String stringValue(Object value) {
        return value == null ? null : String.valueOf(value);
    }

    private static Double doubleValue(Object value) {
        if (value == null) return null;
        try {
            return value instanceof Number n ? n.doubleValue() : Double.parseDouble(String.valueOf(value));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static Integer intValue(Object value) {
        if (value == null) return null;
        try {
            return value instanceof Number n ? n.intValue() : Integer.parseInt(String.valueOf(value));
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static double clamp(Double v) {
        return Math.max(0, Math.min(100, v != null ? v : 0));
    }
}
