package com.macteval.service;

import com.macteval.dto.PasswordChangeRequest;
import com.macteval.dto.ProfileUpdateRequest;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.UserRepository;
import com.macteval.security.JwtTokenProvider;
import com.macteval.security.RefreshTokenStore;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.crypto.password.PasswordEncoder;

import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class AuthServiceProfileTest {

    private UserRepository userRepository;
    private PasswordEncoder passwordEncoder;
    private AuthService service;

    @BeforeEach
    void setUp() {
        userRepository = mock(UserRepository.class);
        passwordEncoder = mock(PasswordEncoder.class);
        service = new AuthService(
                userRepository,
                mock(StudentRepository.class),
                passwordEncoder,
                mock(JwtTokenProvider.class),
                mock(RefreshTokenStore.class),
                mock(com.macteval.security.LoginThrottleService.class)
        );
    }

    @Test
    void updateProfileRejectsPhoneOwnedByAnotherUser() {
        StudentEntity current = student("student-1", "old-phone");
        StudentEntity existing = student("student-2", "new-phone");
        when(userRepository.findById("student-1")).thenReturn(Optional.of(current));
        when(userRepository.findByPhone("new-phone")).thenReturn(Optional.of(existing));

        var error = assertThrows(
                IllegalArgumentException.class,
                () -> service.updateProfile(
                        "student-1",
                        new ProfileUpdateRequest("新姓名", "new-phone")
                )
        );

        assertEquals("该手机号已被使用", error.getMessage());
    }

    @Test
    void changePasswordRejectsWrongCurrentPassword() {
        StudentEntity current = student("student-1", "student");
        current.setPassword("encoded-old");
        when(userRepository.findById("student-1")).thenReturn(Optional.of(current));
        when(passwordEncoder.matches("wrong", "encoded-old")).thenReturn(false);

        var error = assertThrows(
                IllegalArgumentException.class,
                () -> service.changePassword(
                        "student-1",
                        new PasswordChangeRequest("wrong", "new-password")
                )
        );

        assertEquals("当前密码不正确", error.getMessage());
    }

    @Test
    void changePasswordEncodesAndPersistsNewPassword() {
        StudentEntity current = student("student-1", "student");
        current.setPassword("encoded-old");
        when(userRepository.findById("student-1")).thenReturn(Optional.of(current));
        when(passwordEncoder.matches("old-password", "encoded-old")).thenReturn(true);
        when(passwordEncoder.encode("new-password")).thenReturn("encoded-new");

        service.changePassword(
                "student-1",
                new PasswordChangeRequest("old-password", "new-password")
        );

        assertEquals("encoded-new", current.getPassword());
        verify(userRepository).save(current);
    }

    private StudentEntity student(String id, String phone) {
        StudentEntity student = new StudentEntity();
        student.setId(id);
        student.setName("学生");
        student.setPhone(phone);
        return student;
    }
}
