package com.macteval.controller;

import com.macteval.dto.WeChatBindRequest;
import com.macteval.repository.UserRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoInteractions;

class WeChatControllerTest {

    private UserRepository userRepository;
    private WeChatController controller;

    @BeforeEach
    void setUp() {
        userRepository = mock(UserRepository.class);
        controller = new WeChatController(userRepository);
    }

    @Test
    void qrCodeReturnsNotImplementedUntilOAuthIsImplemented() {
        var response = controller.getQrCode();

        assertEquals(501, response.getStatusCode().value());
        verifyNoInteractions(userRepository);
    }

    @Test
    void bindReturnsNotImplementedWithoutPersistingMockOpenId() {
        var response = controller.bind(
                new WeChatBindRequest("authorization-code", "student-001"),
                null
        );

        assertEquals(501, response.getStatusCode().value());
        verifyNoInteractions(userRepository);
    }
}
