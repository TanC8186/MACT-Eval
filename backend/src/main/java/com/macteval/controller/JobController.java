package com.macteval.controller;

import com.macteval.dto.ApiResponse;
import com.macteval.dto.JobStatusDTO;
import com.macteval.model.entity.AnalysisJobEntity;
import com.macteval.security.ServiceTokenValidator;
import com.macteval.service.AnalysisJobService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/jobs")
@RequiredArgsConstructor
public class JobController {

    private final AnalysisJobService analysisJobService;
    private final ServiceTokenValidator serviceTokenValidator;

    /**
     * List recent analysis jobs, optionally filtered by status.
     */
    @GetMapping
    @org.springframework.security.access.prepost.PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<List<JobStatusDTO>>> listJobs(
            @RequestParam(defaultValue = "50") int limit,
            @RequestParam(required = false) String status,
            org.springframework.security.core.Authentication auth) {
        AnalysisJobEntity.JobStatus jobStatus = null;
        if (status != null && !status.isBlank()) {
            try {
                jobStatus = AnalysisJobEntity.JobStatus.valueOf(status.toUpperCase());
            } catch (IllegalArgumentException e) {
                return ApiResponse.error(400, "无效的任务状态: " + status);
            }
        }
        boolean isAdmin = auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
        return ApiResponse.ok(analysisJobService.listJobs(auth.getName(), isAdmin, limit, jobStatus));
    }

    @GetMapping("/{jobId}")
    @org.springframework.security.access.prepost.PreAuthorize("hasAnyRole('teacher','admin')")
    public ResponseEntity<ApiResponse<JobStatusDTO>> getJobStatus(
            @PathVariable String jobId,
            org.springframework.security.core.Authentication auth) {
        boolean isAdmin = auth.getAuthorities().stream()
                .anyMatch(a -> "ROLE_admin".equals(a.getAuthority()));
        return ApiResponse.ok(analysisJobService.getJobStatus(jobId, auth.getName(), isAdmin));
    }

    /** Worker 执行前领取任务（租约）。 */
    @PostMapping("/{jobId}/claim")
    public ResponseEntity<ApiResponse<Map<String, Object>>> claim(
            @PathVariable String jobId,
            @RequestBody(required = false) Map<String, Object> body,
            @RequestHeader(value = "X-Service-Token", required = false) String serviceToken) {
        serviceTokenValidator.validate(serviceToken);
        int leaseSeconds = body != null && body.get("leaseSeconds") instanceof Number n
                ? n.intValue() : 120;
        return ApiResponse.ok(analysisJobService.claimJob(jobId, leaseSeconds));
    }

    /** 长任务心跳续租。 */
    @PostMapping("/{jobId}/heartbeat")
    public ResponseEntity<ApiResponse<Map<String, Object>>> heartbeat(
            @PathVariable String jobId,
            @RequestBody(required = false) Map<String, Object> body,
            @RequestHeader(value = "X-Service-Token", required = false) String serviceToken) {
        serviceTokenValidator.validate(serviceToken);
        int leaseSeconds = body != null && body.get("leaseSeconds") instanceof Number n
                ? n.intValue() : 120;
        AnalysisJobService.ParsedAttempt parsed = AnalysisJobService.parseAttemptField(body);
        if (parsed.present() && !parsed.valid()) {
            Map<String, Object> invalid = new LinkedHashMap<>();
            invalid.put("extended", false);
            invalid.put("reason", "invalid_attempt");
            return ApiResponse.ok(invalid);
        }
        return ApiResponse.ok(analysisJobService.heartbeatWithReason(
                jobId, leaseSeconds, parsed.present() ? parsed.value() : null));
    }

    /**
     * 内网任务状态查询（供 ai-service 判断“该 attempt 是否已终态”）。
     *
     * <p>结果对象损坏时，worker 不能据此重新推理，只能先确认后端是否已把任务判为终态；
     * 非终态或状态不可查询时同样不能冒然 ACK。契约（AI 组按此实现，勿改路径）：</p>
     * <pre>
     * 200 {"code":200,"message":"success","data":{"status":"FAILED","attempt":1,"terminal":true}}
     * 404 {"code":404,"message":"任务不存在","data":null}
     * 403 服务令牌无效
     * </pre>
     */
    @GetMapping("/{jobId}/internal-status")
    public ResponseEntity<ApiResponse<Map<String, Object>>> internalStatus(
            @PathVariable String jobId,
            @RequestHeader(value = "X-Service-Token", required = false) String serviceToken) {
        serviceTokenValidator.validate(serviceToken);
        AnalysisJobEntity job;
        try {
            job = analysisJobService.findJob(jobId);
        } catch (java.util.NoSuchElementException e) {
            return ApiResponse.error(404, "任务不存在");
        }
        if (job == null) {
            return ApiResponse.error(404, "任务不存在");
        }
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("status", job.getStatus() == null ? null : job.getStatus().name());
        data.put("attempt", job.getAttempt() == null ? 0 : job.getAttempt());
        data.put("terminal", AnalysisJobService.isTerminalStatus(job.getStatus()));
        return ApiResponse.ok(data);
    }

    /**
     * Worker completion callback: marks the job SUCCEEDED/PARTIAL/FAILED
     * and stores the result metadata returned by the AI worker.
     */
    @PostMapping("/{jobId}/complete")
    public ResponseEntity<ApiResponse<Map<String, Object>>> completeJob(
            @PathVariable String jobId,
            @RequestBody Map<String, Object> result,
            @RequestHeader(value = "X-Service-Token", required = false) String serviceToken) {
        serviceTokenValidator.validate(serviceToken);
        return ApiResponse.ok(analysisJobService.completeJob(jobId, result));
    }
}
