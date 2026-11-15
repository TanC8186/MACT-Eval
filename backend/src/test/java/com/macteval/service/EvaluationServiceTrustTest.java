package com.macteval.service;

import com.macteval.model.entity.EvaluationReportEntity;
import com.macteval.repository.EvaluationReportRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.SuggestionRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

class EvaluationServiceTrustTest {

    private EvaluationReportRepository reportRepository;
    private SuggestionEngine suggestionEngine;
    private EvaluationService service;

    @BeforeEach
    void setUp() {
        reportRepository = mock(EvaluationReportRepository.class);
        suggestionEngine = mock(SuggestionEngine.class);
        service = new EvaluationService(
                reportRepository,
                mock(FusionService.class),
                mock(RuleEngine.class),
                suggestionEngine,
                mock(ModelManager.class),
                mock(SuggestionRepository.class),
                mock(LessonSessionRepository.class)
        );
    }

    @Test
    void dashboardDoesNotConvertUnscoredEvidenceReportToZeroScoreRealReport() {
        EvaluationReportEntity report = report("REAL", null, null, null);
        when(reportRepository.findAll()).thenReturn(List.of(report));

        var summary = service.getDashboardSummary("admin-1", true);

        assertEquals(1, summary.totalLessons());
        assertEquals("EMPTY", summary.dataSource());
        assertEquals(0, summary.avgEngagement());
    }

    @Test
    void dashboardExcludesLegacyDemoReportWhenDemoModeIsDisabled() {
        EvaluationReportEntity report = report("DEMO", 80.0, 81.0, 82.0);
        when(reportRepository.findAll()).thenReturn(List.of(report));

        var summary = service.getDashboardSummary("admin-1", true);

        assertEquals("EMPTY", summary.dataSource());
        assertEquals(0, summary.avgTeaching());
    }

    @Test
    void formalModeDoesNotGenerateDefaultSuggestions() {
        assertTrue(service.getSuggestions("admin-1", true).isEmpty());
        verifyNoInteractions(suggestionEngine);
    }

    @Test
    void formalModeDoesNotConstructClassAndGradeComparison() {
        assertTrue(service.getComparison().isEmpty());
        verifyNoInteractions(reportRepository);
    }

    @Test
    void dashboardIncludesClimateFromPartialReport() {
        // 音频-only 报告没有参与度，但课堂氛围仍应进入工作台，而不是被整份报告过滤掉
        EvaluationReportEntity report = report("REAL", null, 60.0, 70.0);
        when(reportRepository.findAll()).thenReturn(List.of(report));

        var summary = service.getDashboardSummary("admin-1", true);

        assertEquals("REAL", summary.dataSource());
        assertEquals(0, summary.avgEngagement());
        assertEquals(60.0, summary.avgTeaching(), 1e-9);
        assertEquals(70.0, summary.avgClimate(), 1e-9);
    }

    private EvaluationReportEntity report(
            String dataSource,
            Double engagement,
            Double teaching,
            Double climate
    ) {
        EvaluationReportEntity report = new EvaluationReportEntity();
        report.setId("report-001");
        report.setDataSource(dataSource);
        report.setEngagementScore(engagement);
        report.setTeachingScore(teaching);
        report.setClimateScore(climate);
        report.setCreatedAt(LocalDateTime.now());
        return report;
    }
}
