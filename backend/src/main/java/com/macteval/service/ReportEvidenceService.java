package com.macteval.service;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.macteval.dto.EvidenceFrameDTO;
import com.macteval.dto.ReportDetailDTO;
import com.macteval.model.entity.BehaviorEvidenceFrameEntity;
import com.macteval.repository.BehaviorEvidenceFrameRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.Objects;

/** Teacher-only evidence, scoped to the exact video job of a report version. */
@Slf4j
@Service
@RequiredArgsConstructor
public class ReportEvidenceService {
    private final ReportService reportService;
    private final BehaviorEvidenceFrameRepository frameRepository;
    private final MinioService minioService;
    private final ObjectMapper objectMapper;

    /**
     * 读取某课堂的证据帧。
     *
     * <p><b>版本解析规则（确定性）</b>：证据帧按报告版本记录的 {@code analysisVideoJobId}
     * 精确取，绝不跨版本混用。</p>
     * <ol>
     *   <li>{@code version == null}：取当前工作行的证据（前端未指定版本时的默认视图）。</li>
     *   <li>{@code version != null} 且存在该版本号的<b>已发布快照</b>：一律取快照记录的任务。
     *       快照优先，因此即使请求的版本号恰好等于当前工作行的版本号，也不会读到
     *       尚未发布的草稿证据。</li>
     *   <li>该版本号没有快照，且等于当前工作行版本：说明这一版本从未发布过，请求的就是
     *       当前工作稿，取工作行的证据。</li>
     *   <li>其余情况（历史版本号既无快照又非工作行版本）：返回空列表，绝不退回到
     *       当前工作行的证据冒充历史版本。</li>
     * </ol>
     *
     * <p>鉴权始终先执行：进入任何分支之前都用 {@code getReport} 对课堂做属主校验；
     * 快照分支另有一次属主校验，保持 fail-closed。</p>
     */
    @Transactional(readOnly = true)
    public List<EvidenceFrameDTO> getFrames(String lessonId, Integer version, String userId) {
        // Always authorize against the lesson before loading any image metadata.
        ReportDetailDTO current = reportService.getReport(lessonId, userId);
        if (version == null) {
            return framesOf(lessonId, current);
        }
        ReportDetailDTO snapshot = publishedSnapshot(lessonId, version, userId);
        if (snapshot != null) {
            return framesOf(lessonId, snapshot);
        }
        if (Objects.equals(version, current.version())) {
            return framesOf(lessonId, current);
        }
        log.debug("No published snapshot for lesson={} version={}; returning no evidence frames",
                lessonId, version);
        return List.of();
    }

    /**
     * 该版本号的已发布快照；不存在时返回 {@code null}。
     *
     * <p>{@link ReportService#getVersionDetail} 用 {@code NoSuchElementException} 同时表达
     * “版本不存在”和“课堂不存在”（映射为 404）；课堂存在性已由先行的
     * {@code getReport} 校验，因此这里只可能是版本缺失。</p>
     */
    private ReportDetailDTO publishedSnapshot(String lessonId, Integer version, String userId) {
        try {
            return reportService.getVersionDetail(lessonId, version, userId);
        } catch (NoSuchElementException e) {
            return null;
        }
    }

    private List<EvidenceFrameDTO> framesOf(String lessonId, ReportDetailDTO report) {
        String videoJobId = report == null ? null : report.analysisVideoJobId();
        if (videoJobId == null || videoJobId.isBlank()) return List.of();
        return frameRepository.findByLessonSessionIdAndJobIdOrderByWindowIndexAsc(lessonId, videoJobId)
                .stream()
                // 帧时间缺失时排到末尾，绝不当作课堂零点；主键仍优先用窗口序号，
                // 使顺序与报告中的 60s 窗口顺序一致，而不是由秒级抓帧偏移决定。
                .sorted(Comparator
                        .comparingInt((BehaviorEvidenceFrameEntity f) ->
                                f.getWindowIndex() == null ? Integer.MAX_VALUE : f.getWindowIndex())
                        .thenComparingLong(f ->
                                f.getTimestampMs() == null ? Long.MAX_VALUE : f.getTimestampMs()))
                .map(frame -> {
                    String url = null;
                    try {
                        url = minioService.generateDownloadUrl(frame.getBucket(), frame.getObjectKey(), 30);
                    } catch (Exception e) {
                        log.warn("Could not sign report evidence frame {}", frame.getId());
                    }
                    return new EvidenceFrameDTO(frame.getId(), frame.getWindowIndex(), frame.getTimestampMs(),
                            frame.getCode(), frame.getLabel(), url, parseMetadata(frame.getMetadata()));
                }).toList();
    }

    private Map<String, Object> parseMetadata(String value) {
        if (value == null || value.isBlank()) return Map.of();
        try {
            return objectMapper.readValue(value, new TypeReference<Map<String, Object>>() {});
        } catch (Exception e) {
            log.warn("Invalid evidence metadata; returning original frame without annotations");
            return Map.of();
        }
    }
}
