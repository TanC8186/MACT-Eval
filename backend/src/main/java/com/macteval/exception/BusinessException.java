package com.macteval.exception;

/**
 * 业务可预期错误：映射为 HTTP 400 + 业务文案，避免落到 500。
 * 仅用于「用户可理解、非系统故障」的失败路径。
 */
public class BusinessException extends RuntimeException {

    private final int code;

    public BusinessException(String message) {
        this(400, message);
    }

    public BusinessException(int code, String message) {
        super(message);
        this.code = code;
    }

    public int getCode() {
        return code;
    }
}
