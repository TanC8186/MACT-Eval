package com.macteval.security;

import org.junit.jupiter.api.Test;
import org.springframework.security.access.AccessDeniedException;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;

class ServiceTokenValidatorTest {

    @Test
    void acceptsMatchingToken() {
        var validator = new ServiceTokenValidator("dev-token", "dev");
        assertDoesNotThrow(() -> validator.validate("dev-token"));
    }

    @Test
    void rejectsWrongToken() {
        var validator = new ServiceTokenValidator("dev-token", "dev");
        assertThrows(AccessDeniedException.class, () -> validator.validate("wrong"));
    }

    @Test
    void rejectsNullOrBlankToken() {
        var validator = new ServiceTokenValidator("dev-token", "dev");
        assertThrows(AccessDeniedException.class, () -> validator.validate(null));
        assertThrows(AccessDeniedException.class, () -> validator.validate(""));
    }

    @Test
    void prodRejectsDevelopmentDefaultToken() {
        assertThrows(IllegalStateException.class,
                () -> new ServiceTokenValidator("mact-eval-dev-token", "prod"));
    }
}
