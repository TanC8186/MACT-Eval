package com.macteval.exception;

/**
 * 认证/凭证类业务错误：映射为 HTTP 401。
 */
public class InvalidCredentialsException extends BusinessException {

    public InvalidCredentialsException(String message) {
        super(401, message);
    }
}
