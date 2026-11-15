package com.macteval.service;

import com.macteval.dto.RefreshTokenRequest;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.UserRepository;
import com.macteval.security.JwtTokenProvider;
import com.macteval.security.RefreshTokenStore;
import org.junit.jupiter.api.Test;
import org.springframework.security.crypto.password.PasswordEncoder;

import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class AuthServiceLogoutTest {

    private final UserRepository userRepository = mock(UserRepository.class);
    private final PasswordEncoder passwordEncoder = mock(PasswordEncoder.class);
    private final JwtTokenProvider tokenProvider = mock(JwtTokenProvider.class);
    private final RefreshTokenStore refreshTokenStore = mock(RefreshTokenStore.class);
    private final AuthService service =
            new AuthService(userRepository, mock(StudentRepository.class), passwordEncoder, tokenProvider, refreshTokenStore, mock(com.macteval.security.LoginThrottleService.class));

    @Test
    void logoutConsumesRefreshTokenJti() {
        when(tokenProvider.isRefreshToken("refresh-token")).thenReturn(true);
        when(tokenProvider.getJtiFromToken("refresh-token")).thenReturn("jti-001");

        service.logout(new RefreshTokenRequest("refresh-token"));

        verify(refreshTokenStore).consume("jti-001");
    }

    @Test
    void logoutWithBlankTokenIsNoop() {
        service.logout(new RefreshTokenRequest(""));

        verify(refreshTokenStore, never()).consume(anyString());
    }

    @Test
    void logoutWithInvalidTokenIsNoop() {
        when(tokenProvider.isRefreshToken("bad-token")).thenReturn(false);

        service.logout(new RefreshTokenRequest("bad-token"));

        verify(refreshTokenStore, never()).consume(anyString());
    }
}
