package com.macteval.service;

import com.macteval.dto.*;
import com.macteval.model.entity.EvaluationReportEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.SuggestionEntity;
import com.macteval.repository.EvaluationReportRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.SuggestionRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.*;
import java.util.stream.Collectors;

/**
 * Legacy evaluation facade.
 *
 * <p>The real multimodal scoring pipeline is not complete. Fixed scores,
 * timelines and suggestions are available only when explicit demo mode is
 * enabled.
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class EvaluationService {

    private final EvaluationReportRepository reportRepository;
    private final FusionService fusionService;
    private final RuleEngine ruleEngine;
    private final SuggestionEngine suggestionEngine;
    private final ModelManager modelManager;
    private final SuggestionRepository suggestionRepository;
    private final LessonSessionRepository lessonSessionRepository;

    @Value("${mact.demo.enabled:false}")
    private boolean demoEnabled;

    /**
     * 获取评价报告。
     */
    @Transactional(readOnly = true)
    public EvaluationReportDTO getEvaluationReport(String lessonId, String userId, boolean isAdmin) {
        var entity = reportRepository.findByLessonSessionId(lessonId).orElse(null);
        if (entity == null && !demoEnabled) {
            throw new NoSuchElementException("评价报告不存在");
        }
        if (entity != null) {
            assertCanReadReport(lessonId, entity, userId, isAdmin);
            return toDTO(entity);
        }
        return createDemoReport(lessonId);
    }

    /** 报告/课堂读取授权：admin 放行；教师须为课堂或报告属主。 */
    private void assertCanReadReport(String lessonId, EvaluationReportEntity report,
                                     String userId, boolean isAdmin) {
        if (isAdmin || userId == null) {
            return;
        }
        String owner = lessonSessionRepository.findById(lessonId)
                .map(LessonSessionEntity::getTeacherId)
                .orElse(report.getTeacherId());
        if (owner == null || !owner.equals(userId)) {
            throw new org.springframework.security.access.AccessDeniedException("无权访问其他教师的评价报告");
        }
    }

    /** 按授权范围读取报告：admin 全局，教师仅自己的课堂。 */
    private List<EvaluationReportEntity> scopedReports(String userId, boolean isAdmin) {
        List<EvaluationReportEntity> reports = reportRepository.findAll();
        if (isAdmin || userId == null || userId.isBlank()) {
            return reports;
        }
        Set<String> ownLessonIds = lessonSessionRepository.findByTeacherId(userId).stream()
                .map(LessonSessionEntity::getId)
                .collect(Collectors.toSet());
        return reports.stream()
                .filter(r -> r.getLessonSessionId() != null && ownLessonIds.contains(r.getLessonSessionId()))
                .toList();
    }

    /**
     * 仪表盘总览（按授权范围聚合）。
     */
    @Transactional(readOnly = true)
    public DashboardSummaryDTO getDashboardSummary(String userId, boolean isAdmin) {
        List<EvaluationReportEntity> reports = scopedReports(userId, isAdmin);
        List<EvaluationReportEntity> scoredReports = reports.stream()
                .filter(this::isUsableReport)
                .toList();

        if (scoredReports.isEmpty()) {
            if (demoEnabled) {
                return new DashboardSummaryDTO(12, 76.3, 82.1, 78.5, "improving", "DEMO");
            }
            return new DashboardSummaryDTO(reports.size(), 0, 0, 0, "unknown", "EMPTY");
        }

        double avgEng = averagePresentScore(scoredReports, ScoreField.ENGAGEMENT);
        double avgTea = averagePresentScore(scoredReports, ScoreField.TEACHING);
        double avgCli = averagePresentScore(scoredReports, ScoreField.CLIMATE);
        String dataSource = scoredReports.stream()
                .anyMatch(report -> "DEMO".equalsIgnoreCase(report.getDataSource()))
                ? "DEMO"
                : "REAL";

        return new DashboardSummaryDTO(
                reports.size(),
                round(avgEng), round(avgTea), round(avgCli), "unknown", dataSource
        );
    }

    /**
     * 班级和年级基准尚未接通，正式模式不构造伪对比。
     */
    public List<ComparisonDataDTO> getComparison() {
        if (!demoEnabled) return List.of();

        log.warn("getComparison: using explicit demo comparison data");
        return List.of(
                new ComparisonDataDTO("学生参与度", 78.5, 72.3, 70.1),
                new ComparisonDataDTO("教学质量", 85.2, 80.5, 78.8),
                new ComparisonDataDTO("课堂氛围", 80.1, 75.6, 74.2),
                new ComparisonDataDTO("互动深度", 76.0, 71.2, 69.5),
                new ComparisonDataDTO("情感支持", 82.3, 77.8, 76.0)
        );
    }

    /**
     * 教学建议：正式模式读取持久化的 pending 建议（与报告/采纳使用同一 ID）。
     * demo 模式仅在没有任何真实建议时返回明确标注的演示建议。
     */
    @Transactional(readOnly = true)
    public List<SuggestionDTO> getSuggestions(String userId, boolean isAdmin) {
        List<SuggestionEntity> pending = suggestionRepository.findByStatus("pending");
        if (!pending.isEmpty()) {
            Set<String> scope = isAdmin || userId == null ? null
                    : lessonSessionRepository.findByTeacherId(userId).stream()
                            .map(LessonSessionEntity::getId)
                            .collect(Collectors.toSet());
            return pending.stream()
                    .filter(s -> scope == null
                            || (s.getLessonSessionId() != null && scope.contains(s.getLessonSessionId())))
                    .sorted(Comparator.comparing(SuggestionEntity::getCreatedAt,
                            Comparator.nullsLast(Comparator.naturalOrder())))
                    .map(s -> new SuggestionDTO(
                            s.getId(), s.getCategory(), s.getPriority(), s.getContent(), s.getEvidence(),
                            s.getStatus(), s.getCreatedAt(), null,
                            s.getRuleId(), s.getRuleVersion(), s.getEvidenceRefs()))
                    .toList();
        }
        if (!demoEnabled) return List.of();
        return suggestionEngine.getDefaultSuggestions();
    }

    /**
     * 处理视频上传。当前仅在显式演示模式下生成带 DEMO 标识的原型报告。
     *
     * @param lessonId 课程 ID（可选）
     * @param filePath 上传文件的本地路径
     */
    @Transactional
    public EvaluationReportDTO processUpload(String lessonId, String filePath, String userId, boolean isAdmin) {
        if (!demoEnabled) {
            throw new IllegalStateException("真实多模态评价管线尚未接通，当前禁止生成模拟评价结果");
        }
        if (!isAdmin && lessonId != null && !lessonId.isBlank()) {
            lessonSessionRepository.findById(lessonId).ifPresent(session -> {
                if (session.getTeacherId() != null && !session.getTeacherId().equals(userId)) {
                    throw new org.springframework.security.access.AccessDeniedException("无权操作其他教师的课堂");
                }
            });
        }

        String id = (lessonId != null && !lessonId.isBlank())
                ? lessonId : "lesson_" + UUID.randomUUID().toString().substring(0, 8);

        log.info("Starting evaluation pipeline for lesson: {}, filePath: {}", id, filePath);

        // 1. 确保模型已加载 —— 注意: 以下均为占位实现，返回 false（未实现），不执行真实加载
        modelManager.loadYolo();
        modelManager.loadWhisper();
        modelManager.loadBert();

        // ===== TODO: 真实多模态分析管线（当前未接通）=====
        // 下面是规划中的真实分析调用，均被 TODO 注释禁用，尚未实现：
        //   - videoAnalysisService.extractFrames(filePath);   // 帧提取未实现
        //   - audioAnalysisService.transcribe(filePath);      // ASR 未接通
        //   - videoAnalysisService.detectFaces(...)           // 视觉分析未接入本流程
        //   - double[] visualFeat = ...; // 视觉特征 (YOLO/OpenCV, 规划中)
        //   - double[] audioFeat  = ...; // 音频特征 (Whisper, 规划中)
        //   - double[] textFeat   = ...; // 文本特征 (BERT, 规划中)
        //   - fusionService.lateFuse(...); // 晚期加权融合（R4 规划中：真实评分管线未接通）
        // 当前 processUpload 走「规则引擎 + 固定演示数据」，真实视觉/ASR 分析未接通。
        // 在接通真实分析之前，本方法禁止生成无标注的「真实」评价结果。
        log.info("File path available for analysis pipeline: {}", filePath);

        // 3. Demo-only deterministic inputs for exercising the report workflow.
        var behaviors = List.of("raising_hand", "reading", "writing", "discussing", "looking_away");
        double behavioralScore = ruleEngine.calcBehavioralEngagement(behaviors);
        double interactionScore = ruleEngine.calcInteractionQuality(22, 2.3, 0.65);
        double gini = ruleEngine.calcParticipationGini(List.of(3, 5, 8, 2, 6, 4, 1, 7));

        // 5. 构建维度分数
        Map<String, Map<String, Double>> dimScores = new LinkedHashMap<>();
        dimScores.put("student_engagement", Map.of(
                "behavioral", behavioralScore, "cognitive", 71.0, "emotional", 83.0));
        dimScores.put("teaching_quality", Map.of(
                "organization", 88.0, "interaction", interactionScore, "affective_support", 83.0));
        dimScores.put("classroom_climate", Map.of(
                "activity", 80.0, "equity", ruleEngine.calcCallEquityScore(Map.of(
                        "front", 85.0, "middle", 78.0, "back", 65.0))));

        // 6. 生成建议
        List<SuggestionDTO> suggestions = suggestionEngine.generate(dimScores);

        // 7. 构建报告（含证据面板）
        EvaluationReportDTO.Evidence evidence = buildDemoEvidence();
        EvaluationReportDTO reportDTO = buildReportDTO(id, dimScores, suggestions, "DEMO", evidence);

        // 8. 持久化
        EvaluationReportEntity reportEntity = new EvaluationReportEntity();
        reportEntity.setId(UUID.randomUUID().toString());
        reportEntity.setLessonSessionId(id);
        reportEntity.setTitle("课堂评价报告 - " + id);
        reportEntity.setEngagementScore(dimScores.get("student_engagement").values().stream()
                .mapToDouble(Double::doubleValue).average().orElse(0));
        reportEntity.setTeachingScore(dimScores.get("teaching_quality").values().stream()
                .mapToDouble(Double::doubleValue).average().orElse(0));
        reportEntity.setClimateScore(dimScores.get("classroom_climate").values().stream()
                .mapToDouble(Double::doubleValue).average().orElse(0));
        reportEntity.setDataSource("DEMO");
        reportEntity.setModelVersion(evidence.modelVersion());
        reportEntity.setCoverage(evidence.coverage());
        reportEntity.setWindowCount(evidence.windowCount());
        reportEntity.setFeatureCount(evidence.featureCount());
        reportRepository.save(reportEntity);
        log.info("Evaluation complete for lesson: {}", id);
        return reportDTO;
    }

    // ========== 私有方法 ==========

    private enum ScoreField { ENGAGEMENT, TEACHING, CLIMATE }

    private EvaluationReportDTO createDemoReport(String lessonId) {
        Map<String, Map<String, Double>> dimScores = buildDefaultDimScores();
        return buildReportDTO(
                lessonId != null ? lessonId : "demo_001",
                dimScores,
                suggestionEngine.getDefaultSuggestions(),
                "DEMO",
                buildDemoEvidence()
        );
    }

    private Map<String, Map<String, Double>> buildDefaultDimScores() {
        Map<String, Map<String, Double>> dims = new LinkedHashMap<>();
        dims.put("student_engagement", Map.of("behavioral", 82.0, "cognitive", 71.0, "emotional", 83.0));
        dims.put("teaching_quality", Map.of("organization", 88.0, "interaction", 84.0, "affective_support", 83.0));
        dims.put("classroom_climate", Map.of("activity", 80.0, "equity", 78.0, "order", 82.0));
        return dims;
    }

    private EvaluationReportDTO buildReportDTO(String lessonId,
                                                Map<String, Map<String, Double>> dimScores,
                                                List<SuggestionDTO> suggestions,
                                                String dataSource,
                                                EvaluationReportDTO.Evidence evidence) {
        Map<String, EvaluationReportDTO.DimensionScore> dims = new LinkedHashMap<>();
        for (var entry : dimScores.entrySet()) {
            double avg = entry.getValue().values().stream()
                    .mapToDouble(Double::doubleValue).average().orElse(0);
            dims.put(entry.getKey(), new EvaluationReportDTO.DimensionScore(
                    round(avg), List.of(), buildEvidenceNote(entry.getKey(), evidence)
            ));
        }

        return new EvaluationReportDTO(
                lessonId, LocalDateTime.now(), dims,
                List.of(
                        new EvaluationReportDTO.TimelineEvent(0, "导入", 72, "课堂导入环节"),
                        new EvaluationReportDTO.TimelineEvent(5, "讲授", 65, "后排注意力下降"),
                        new EvaluationReportDTO.TimelineEvent(15, "互动", 88, "小组讨论活跃"),
                        new EvaluationReportDTO.TimelineEvent(30, "练习", 75, "独立练习阶段"),
                        new EvaluationReportDTO.TimelineEvent(40, "总结", 70, "课堂总结")
                ),
                suggestions.stream().map(SuggestionDTO::content).toList(),
                dataSource,
                evidence
        );
    }

    private EvaluationReportDTO.Evidence buildDemoEvidence() {
        return new EvaluationReportDTO.Evidence("DEMO", "rule-engine-1.0", 0.75, 40, 5);
    }

    private String buildEvidenceNote(String dimensionKey, EvaluationReportDTO.Evidence evidence) {
        int windows = evidence != null ? evidence.windowCount() : 0;
        int features = evidence != null ? evidence.featureCount() : 0;
        return switch (dimensionKey) {
            case "student_engagement" -> "基于 " + windows + " 个时间窗口的平均行为参与度";
            case "teaching_quality" -> "基于 " + features + " 项课堂信号（随堂题/互动）计算";
            case "classroom_climate" -> "基于课堂观察与反馈汇总";
            default -> "基于规则引擎维度分平均";
        };
    }

    private EvaluationReportDTO toDTO(EvaluationReportEntity entity) {
        Map<String, EvaluationReportDTO.DimensionScore> dims = new LinkedHashMap<>();
        int windowCount = entity.getWindowCount() != null ? entity.getWindowCount() : 0;
        int featureCount = entity.getFeatureCount() != null ? entity.getFeatureCount() : 0;
        if (entity.getEngagementScore() != null) {
            dims.put("student_engagement", new EvaluationReportDTO.DimensionScore(
                    round(entity.getEngagementScore()), List.of(),
                    "基于 " + windowCount + " 个时间窗口的平均行为参与度"));
        }
        if (entity.getTeachingScore() != null) {
            dims.put("teaching_quality", new EvaluationReportDTO.DimensionScore(
                    round(entity.getTeachingScore()), List.of(),
                    "基于 " + featureCount + " 项课堂信号（随堂题/互动）计算"));
        }
        if (entity.getClimateScore() != null) {
            dims.put("classroom_climate", new EvaluationReportDTO.DimensionScore(
                    round(entity.getClimateScore()), List.of(),
                    "基于课堂观察与反馈汇总"));
        }

        return new EvaluationReportDTO(
                entity.getLessonSessionId(),
                entity.getCreatedAt() != null ? entity.getCreatedAt() : LocalDateTime.now(),
                dims,
                List.of(),   // timeline data not stored in entity aggregate
                List.of(),   // suggestions not stored in entity aggregate
                entity.getDataSource(),
                buildEntityEvidence(entity)
        );
    }

    /**
     * 从实体读取已存的证据字段；旧记录没有证据列时按数据来源给出合理默认值。
     */
    private EvaluationReportDTO.Evidence buildEntityEvidence(EvaluationReportEntity entity) {
        String dataSource = entity.getDataSource() != null ? entity.getDataSource() : "EMPTY";
        String modelVersion = entity.getModelVersion();
        Double coverage = entity.getCoverage();
        Integer windowCount = entity.getWindowCount();
        Integer featureCount = entity.getFeatureCount();

        if (modelVersion == null && coverage == null && windowCount == null && featureCount == null) {
            return switch (dataSource) {
                case "DEMO" -> buildDemoEvidence();
                case "REAL" -> new EvaluationReportDTO.Evidence("REAL", "evidence-aggregator-1.0", 0.0, 0, 0);
                default -> new EvaluationReportDTO.Evidence("EMPTY", "N/A", 0.0, 0, 0);
            };
        }
        return new EvaluationReportDTO.Evidence(
                dataSource,
                modelVersion != null ? modelVersion : "N/A",
                coverage != null ? coverage : 0.0,
                windowCount != null ? windowCount : 0,
                featureCount != null ? featureCount : 0
        );
    }

    private boolean isUsableReport(EvaluationReportEntity report) {
        boolean hasAnyScore = report.getEngagementScore() != null
                || report.getTeachingScore() != null
                || report.getClimateScore() != null;
        if (!hasAnyScore) return false;
        if (demoEnabled) return true;
        return "REAL".equalsIgnoreCase(report.getDataSource());
    }

    /**
     * 只对存在的维度取平均；缺少某一维度时不能用 0 分稀释其他维度。
     */
    private double averagePresentScore(List<EvaluationReportEntity> reports, ScoreField field) {
        return reports.stream()
                .map(r -> switch (field) {
                    case ENGAGEMENT -> r.getEngagementScore();
                    case TEACHING -> r.getTeachingScore();
                    case CLIMATE -> r.getClimateScore();
                })
                .filter(Objects::nonNull)
                .mapToDouble(Double::doubleValue)
                .average().orElse(0);
    }

    private static double round(double val) {
        return Math.round(val * 10.0) / 10.0;
    }
}
