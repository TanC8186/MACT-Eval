package com.macteval.service;

import com.macteval.model.entity.LessonWindowFeatureEntity;
import com.macteval.repository.LessonWindowFeatureRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * R11：窗口版本 fallback 边界。
 *
 * <p>历史 fallback（读取 V30 之前两列为空的行）<b>只允许纯 legacy 报告</b>——
 * 即报告没有任何成功的视频/音频分析任务。只要选定了任一成功任务，精确版本
 * 没有窗口就必须返回空，报告应显示“数据不足/窗口未完成”，不得用旧历史窗口
 * 的分数冒充新任务的结果。</p>
 */
class WindowVersionSelectorTest {

    private LessonWindowFeatureRepository repository;

    @BeforeEach
    void setUp() {
        repository = mock(LessonWindowFeatureRepository.class);
    }

    // ---------------------------------------------------------------- 判定条件

    @Test
    void legacyOnlyRequiresBothJobIdsAbsent() {
        assertTrue(WindowVersionSelector.isLegacyOnly(null, null));
        assertTrue(WindowVersionSelector.isLegacyOnly("", ""));
        assertTrue(WindowVersionSelector.isLegacyOnly("   ", null));
        assertFalse(WindowVersionSelector.isLegacyOnly("J2", null), "有视频任务就不是纯 legacy");
        assertFalse(WindowVersionSelector.isLegacyOnly(null, "A2"), "有音频任务就不是纯 legacy");
        assertFalse(WindowVersionSelector.isLegacyOnly("J2", "A2"));
    }

    // ---------------------------------------------- (1) 无成功任务 + 历史行 → 允许

    @Test
    void noSuccessfulJobAllowsHistoricalFallback() {
        when(repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "", ""))
                .thenReturn(List.of());
        when(repository.findByLessonSessionIdOrderByWindowIndexAsc("ls"))
                .thenReturn(List.of(legacy("w-legacy", 0, 55.0), versioned("w-J2", 0, 70.0, "J2")));

        List<LessonWindowFeatureEntity> read = WindowVersionSelector.read(repository, "ls", null, null);

        assertEquals(1, read.size(), "只返回两列皆空的历史行");
        assertEquals("w-legacy", read.get(0).getId());
    }

    // --------------------------(2) J2 成功 + J2 无窗口 + 历史行存在 → 返回空

    @Test
    void succeededJobWithNoWindowsReturnsEmptyAndNeverReadsHistory() {
        when(repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "J2", ""))
                .thenReturn(List.of());

        List<LessonWindowFeatureEntity> read = WindowVersionSelector.read(repository, "ls", "J2", null);

        assertTrue(read.isEmpty(), "选定任务但没有精确窗口时必须返回空（数据不足）");
        verify(repository, never()).findByLessonSessionIdOrderByWindowIndexAsc(anyString());
    }

    // ------------------------------- (3) J2 有窗口 + J1 有窗口 → 只读 J2

    @Test
    void onlyTheSelectedVersionIsRead() {
        when(repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "J2", ""))
                .thenReturn(List.of(versioned("w-J2", 0, 70.0, "J2")));

        List<LessonWindowFeatureEntity> read = WindowVersionSelector.read(repository, "ls", "J2", null);

        assertEquals(1, read.size());
        assertEquals("w-J2", read.get(0).getId());
        verify(repository, never())
                .findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "J1", "");
    }

    // ---------------- (4) 视频 J2 + 音频 A2 精确组合；缺一模态不退回无任务组合

    @Test
    void videoAndAudioCombinationIsExact() {
        when(repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "J2", "A2"))
                .thenReturn(List.of(versioned("w-both", 0, 88.0, "J2", "A2")));

        List<LessonWindowFeatureEntity> read =
                WindowVersionSelector.read(repository, "ls", "J2", "A2");

        assertEquals(1, read.size());
        assertEquals("w-both", read.get(0).getId());
        verify(repository, never())
                .findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "J2", "");
        verify(repository, never())
                .findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "", "");
    }

    @Test
    void videoOnlyVersionDoesNotDegradeToTheNoJobCombination() {
        when(repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "J2", ""))
                .thenReturn(List.of());
        when(repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "", ""))
                .thenReturn(List.of(legacy("w-history", 0, 33.0)));

        List<LessonWindowFeatureEntity> read = WindowVersionSelector.read(repository, "ls", "J2", null);

        assertTrue(read.isEmpty(), "缺音频任务时组合是 (J2,'')，不得退化为 ('','')");
    }

    // ------------------------------------------------- 显式允许/禁止回退的入口

    @Test
    void explicitFallbackFlagIsHonoured() {
        when(repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "J2", ""))
                .thenReturn(List.of());
        when(repository.findByLessonSessionIdOrderByWindowIndexAsc("ls"))
                .thenReturn(List.of(legacy("w-legacy", 0, 55.0)));

        assertTrue(WindowVersionSelector.read(repository, "ls", "J2", null, true).size() == 1,
                "显式允许时回退历史行");
        assertTrue(WindowVersionSelector.read(repository, "ls", "J2", null, false).isEmpty(),
                "显式禁止时不回退");
    }

    /** 精确版本有行时，无论回退是否被允许，都必须优先用精确版本。 */
    @Test
    void exactVersionWinsOverHistoricalRows() {
        when(repository.findByLessonSessionIdAndJobIdsOrderByWindowIndexAsc("ls", "J2", ""))
                .thenReturn(List.of(versioned("w-J2", 0, 70.0, "J2")));
        when(repository.findByLessonSessionIdOrderByWindowIndexAsc("ls"))
                .thenReturn(List.of(legacy("w-legacy", 0, 55.0)));

        List<LessonWindowFeatureEntity> read =
                WindowVersionSelector.read(repository, "ls", "J2", null, true);

        assertEquals(1, read.size());
        assertEquals("w-J2", read.get(0).getId());
    }

    // ------------------------------------------------------------------ helpers

    private LessonWindowFeatureEntity legacy(String id, int index, double fused) {
        return row(id, index, fused, null, null);
    }

    private LessonWindowFeatureEntity versioned(String id, int index, double fused, String videoJobId) {
        return row(id, index, fused, videoJobId, null);
    }

    private LessonWindowFeatureEntity versioned(String id, int index, double fused,
                                               String videoJobId, String audioJobId) {
        return row(id, index, fused, videoJobId, audioJobId);
    }

    private LessonWindowFeatureEntity row(String id, int index, double fused,
                                          String videoJobId, String audioJobId) {
        LessonWindowFeatureEntity w = new LessonWindowFeatureEntity();
        w.setId(id);
        w.setLessonSessionId("ls");
        w.setWindowIndex(index);
        w.setWindowStart(LocalDateTime.of(2026, 10, 27, 9, 0).plusMinutes(index));
        w.setWindowEnd(w.getWindowStart().plusMinutes(1));
        w.setVideoJobId(videoJobId);
        w.setAudioJobId(audioJobId);
        w.setVideoScore(fused);
        w.setSpeechScore(fused);
        w.setFusedScore(fused);
        w.setVideoFeatures("{\"rule_protocol\":\"v1-system-proxy\"}");
        w.setSpeechFeatures("{\"rule_protocol\":\"v1\"}");
        return w;
    }
}
