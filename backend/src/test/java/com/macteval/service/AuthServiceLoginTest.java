package com.macteval.service;

import com.macteval.dto.LoginRequest;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.UserRepository;
import com.macteval.security.JwtTokenProvider;
import com.macteval.security.RefreshTokenStore;
import org.junit.jupiter.api.Test;
import org.springframework.security.crypto.password.PasswordEncoder;

import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class AuthServiceLoginTest {

    @Test
    void loginTrimsUsernameBeforeLookup() {
        UserRepository userRepository = mock(UserRepository.class);
        PasswordEncoder passwordEncoder = mock(PasswordEncoder.class);
        JwtTokenProvider tokenProvider = mock(JwtTokenProvider.class);
        RefreshTokenStore refreshTokenStore = mock(RefreshTokenStore.class);
        AuthService service = new AuthService(userRepository, mock(StudentRepository.class), passwordEncoder, tokenProvider, refreshTokenStore, mock(com.macteval.security.LoginThrottleService.class));

        StudentEntity student = new StudentEntity();
        student.setId("student-001");
        student.setPhone("student");
        student.setPassword("encoded-password");
        student.setName("李明");

        when(userRepository.findByPhone("student")).thenReturn(Optional.of(student));
        when(passwordEncoder.matches("123456", "encoded-password")).thenReturn(true);
        when(tokenProvider.generateAccessToken("student-001", "student", false)).thenReturn("access-token");
        when(tokenProvider.generateRefreshToken("student-001")).thenReturn("refresh-token");
        when(tokenProvider.getJtiFromToken("refresh-token")).thenReturn("jti-001");

        var response = service.login(new LoginRequest(" student ", "123456"));

        assertEquals("student", response.role());
        verify(userRepository).findByPhone("student");
    }

    @Test
    void loginFallsBackToStudentIdForImportedAccounts() {
        UserRepository userRepository = mock(UserRepository.class);
        com.macteval.repository.StudentRepository studentRepository =
                mock(com.macteval.repository.StudentRepository.class);
        PasswordEncoder passwordEncoder = mock(PasswordEncoder.class);
        JwtTokenProvider tokenProvider = mock(JwtTokenProvider.class);
        RefreshTokenStore refreshTokenStore = mock(RefreshTokenStore.class);
        AuthService service = new AuthService(
                userRepository, studentRepository, passwordEncoder, tokenProvider, refreshTokenStore,
                mock(com.macteval.security.LoginThrottleService.class));

        StudentEntity student = new StudentEntity();
        student.setId("student-002");
        student.setStudentId("S2023001");
        student.setPassword("encoded-password");
        student.setName("导入学生");
        student.setMustChangePassword(true);

        when(userRepository.findByPhone("S2023001")).thenReturn(Optional.empty());
        when(studentRepository.findByStudentId("S2023001")).thenReturn(Optional.of(student));
        when(passwordEncoder.matches("123456", "encoded-password")).thenReturn(true);
        when(tokenProvider.generateAccessToken("student-002", "student", true)).thenReturn("access-token");
        when(tokenProvider.generateRefreshToken("student-002")).thenReturn("refresh-token");
        when(tokenProvider.getJtiFromToken("refresh-token")).thenReturn("jti-002");

        var response = service.login(new LoginRequest("S2023001", "123456"));

        assertEquals("student", response.role());
        assertTrue(response.mustChangePassword(),
                "管理员导入的初始账号必须被标记为需修改密码");
        verify(studentRepository).findByStudentId("S2023001");
    }
}
