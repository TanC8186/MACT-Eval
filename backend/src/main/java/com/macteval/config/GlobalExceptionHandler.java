package com.macteval.config;

import com.macteval.dto.ErrorResponse;
import com.macteval.exception.BusinessException;
import com.macteval.exception.InvalidCredentialsException;
import com.macteval.exception.NotFoundException;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.multipart.MaxUploadSizeExceededException;

import java.util.Map;
import java.util.NoSuchElementException;

@RestControllerAdvice
@Slf4j
public class GlobalExceptionHandler {

    @ExceptionHandler(MaxUploadSizeExceededException.class)
    public ResponseEntity<ErrorResponse> handleUploadSizeExceeded(MaxUploadSizeExceededException e) {
        log.warn("Upload size exceeded: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.PAYLOAD_TOO_LARGE)
                .body(ErrorResponse.of(413, "文件大小超出限制，最大支持2GB"));
    }

    @ExceptionHandler(AccessDeniedException.class)
    public ResponseEntity<ErrorResponse> handleAccessDenied(AccessDeniedException e) {
        log.warn("Access denied: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.FORBIDDEN)
                .body(ErrorResponse.of(403, e.getMessage()));
    }

    @ExceptionHandler(InvalidCredentialsException.class)
    public ResponseEntity<ErrorResponse> handleInvalidCredentials(InvalidCredentialsException e) {
        log.warn("Invalid credentials: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.UNAUTHORIZED)
                .body(ErrorResponse.of(e.getCode(), e.getMessage()));
    }

    @ExceptionHandler(BusinessException.class)
    public ResponseEntity<ErrorResponse> handleBusiness(BusinessException e) {
        log.warn("Business error ({}): {}", e.getCode(), e.getMessage());
        // HTTP 状态码必须与响应体里的 `code` 一致。
        //
        // 此前这里只映射到 400，于是 `BusinessException(415, ...)` 与
        // `BusinessException(413, ...)` 会返回 **HTTP 400 + body.code 415**：
        // 前端按 HTTP 状态判断时看到 400，按 body 判断时看到 415，两边都
        // "有依据"。这个不一致在 SEC-03 的绕过测试里被真实抓到过——
        // 测试断言 415，实际拿到 400，看起来像"校验没生效"，其实是映射漏项。
        HttpStatus status = switch (e.getCode()) {
            case 400 -> HttpStatus.BAD_REQUEST;
            case 401 -> HttpStatus.UNAUTHORIZED;
            case 403 -> HttpStatus.FORBIDDEN;
            case 404 -> HttpStatus.NOT_FOUND;
            case 409 -> HttpStatus.CONFLICT;
            case 410 -> HttpStatus.GONE;
            case 413 -> HttpStatus.PAYLOAD_TOO_LARGE;
            case 415 -> HttpStatus.UNSUPPORTED_MEDIA_TYPE;
            case 422 -> HttpStatus.UNPROCESSABLE_ENTITY;
            case 429 -> HttpStatus.TOO_MANY_REQUESTS;
            // 未知业务码一律 400：不猜。若要新增一个 HTTP 语义，
            // 必须在这里显式加一行，而不是让它静默落成 400。
            default -> HttpStatus.BAD_REQUEST;
        };
        return ResponseEntity.status(status)
                .body(ErrorResponse.of(e.getCode(), e.getMessage()));
    }

    @ExceptionHandler(NotFoundException.class)
    public ResponseEntity<ErrorResponse> handleNotFoundCustom(NotFoundException e) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(ErrorResponse.of(404, e.getMessage()));
    }

    /**
     * Bean Validation 失败：返回 400 与逐字段错误，不落入通用 500。
     */
    @ExceptionHandler(org.springframework.web.bind.MethodArgumentNotValidException.class)
    public ResponseEntity<ErrorResponse> handleValidation(
            org.springframework.web.bind.MethodArgumentNotValidException e) {
        Map<String, String> fieldErrors = new java.util.LinkedHashMap<>();
        e.getBindingResult().getFieldErrors().forEach(err ->
                fieldErrors.putIfAbsent(err.getField(), err.getDefaultMessage()));
        e.getBindingResult().getGlobalErrors().forEach(err ->
                fieldErrors.putIfAbsent("_global", err.getDefaultMessage()));
        String message = fieldErrors.isEmpty() ? "请求参数校验失败" : "请求参数校验失败，请检查标红字段";
        log.warn("Validation failed: {}", fieldErrors);
        return ResponseEntity.badRequest()
                .body(ErrorResponse.of(400, message, fieldErrors));
    }

    @ExceptionHandler(jakarta.validation.ConstraintViolationException.class)
    public ResponseEntity<ErrorResponse> handleConstraintViolation(
            jakarta.validation.ConstraintViolationException e) {
        Map<String, String> fieldErrors = new java.util.LinkedHashMap<>();
        e.getConstraintViolations().forEach(v ->
                fieldErrors.putIfAbsent(v.getPropertyPath().toString(), v.getMessage()));
        return ResponseEntity.badRequest()
                .body(ErrorResponse.of(400, "请求参数校验失败", fieldErrors));
    }

    @ExceptionHandler(org.springframework.http.converter.HttpMessageNotReadableException.class)
    public ResponseEntity<ErrorResponse> handleUnreadable(
            org.springframework.http.converter.HttpMessageNotReadableException e) {
        log.warn("Malformed request body: {}", e.getMessage());
        return ResponseEntity.badRequest()
                .body(ErrorResponse.of(400, "请求体格式错误或字段类型不匹配"));
    }

    @ExceptionHandler(org.springframework.web.method.annotation.MethodArgumentTypeMismatchException.class)
    public ResponseEntity<ErrorResponse> handleTypeMismatch(
            org.springframework.web.method.annotation.MethodArgumentTypeMismatchException e) {
        return ResponseEntity.badRequest()
                .body(ErrorResponse.of(400, "参数类型错误: " + e.getName()));
    }

    @ExceptionHandler(org.springframework.web.bind.MissingServletRequestParameterException.class)
    public ResponseEntity<ErrorResponse> handleMissingParameter(
            org.springframework.web.bind.MissingServletRequestParameterException e) {
        return ResponseEntity.badRequest()
                .body(ErrorResponse.of(400, "缺少必填参数: " + e.getParameterName()));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<ErrorResponse> handleBadRequest(IllegalArgumentException e) {
        log.warn("Bad request: {}", e.getMessage());
        return ResponseEntity.badRequest()
                .body(ErrorResponse.of(400, e.getMessage()));
    }

    @ExceptionHandler(NoSuchElementException.class)
    public ResponseEntity<ErrorResponse> handleNotFound(NoSuchElementException e) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(ErrorResponse.of(404, e.getMessage()));
    }

    @ExceptionHandler(org.springframework.dao.OptimisticLockingFailureException.class)
    public ResponseEntity<ErrorResponse> handleOptimisticLock(
            org.springframework.dao.OptimisticLockingFailureException e) {
        log.warn("Concurrent modification rejected: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.CONFLICT)
                .body(ErrorResponse.of(409, "数据已被其他操作修改，请刷新后重试"));
    }

    @ExceptionHandler(org.springframework.dao.DataIntegrityViolationException.class)
    public ResponseEntity<ErrorResponse> handleDataIntegrity(
            org.springframework.dao.DataIntegrityViolationException e) {
        // 不把 SQL/约束名返回给客户端；服务端日志保留细节用于定位。
        log.warn("Data integrity violation", e);
        return ResponseEntity.status(HttpStatus.CONFLICT)
                .body(ErrorResponse.of(409, "数据冲突或违反业务约束，请刷新后重试"));
    }

    @ExceptionHandler(IllegalStateException.class)
    public ResponseEntity<ErrorResponse> handleUnavailable(IllegalStateException e) {
        log.warn("Service unavailable: {}", e.getMessage());
        return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(ErrorResponse.of(503, e.getMessage()));
    }

    /**
     * 已删除的模块或未知路径：返回 404，不要把“没有这个接口”报成 500。
     */
    @ExceptionHandler(org.springframework.web.servlet.resource.NoResourceFoundException.class)
    public ResponseEntity<ErrorResponse> handleNoResource(
            org.springframework.web.servlet.resource.NoResourceFoundException e) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(ErrorResponse.of(404, "接口或资源不存在"));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ErrorResponse> handleGeneral(Exception e) {
        log.error("Unexpected error: ", e);
        return ResponseEntity.internalServerError()
                .body(ErrorResponse.of(500, "服务器内部错误，请稍后重试"));
    }
}
