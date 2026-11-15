package com.macteval.service;

import com.macteval.dto.*;
import com.macteval.exception.BusinessException;
import com.macteval.exception.InvalidCredentialsException;
import com.macteval.model.entity.AdminEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.model.entity.TeacherEntity;
import com.macteval.model.entity.UserEntity;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.UserRepository;
import com.macteval.security.JwtTokenProvider;
import com.macteval.security.LoginThrottleService;
import com.macteval.security.RefreshTokenStore;
import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.util.Locale;
import java.util.UUID;

@Slf4j
@Service
@RequiredArgsConstructor
public class AuthService {

    private final UserRepository userRepository;
    private final StudentRepository studentRepository;
    private final PasswordEncoder passwordEncoder;
    private final JwtTokenProvider tokenProvider;
    private final RefreshTokenStore refreshTokenStore;
    private final LoginThrottleService loginThrottleService;

    @Value("${mact.demo.enabled:false}")
    private boolean demoEnabled;

    @Value("${mact.jwt.refresh-expiration}")
    private long refreshExpirationMs;

    @PostConstruct
    public void seedDemoAccounts() {
        if (!demoEnabled) {
            return;
        }

        if (!userRepository.findByPhone("teacher").isPresent()) {
            TeacherEntity teacher = new TeacherEntity();
            teacher.setId(UUID.randomUUID().toString());
            teacher.setName("张老师");
            teacher.setPhone("teacher");
            teacher.setPassword(passwordEncoder.encode("123456"));
            teacher.setTeacherId("TCH_DEMO_" + UUID.randomUUID().toString().substring(0, 6));
            userRepository.save(teacher);
            log.info("Demo teacher account created: teacher / 123456");
        }

        if (!userRepository.findByPhone("admin").isPresent()) {
            AdminEntity admin = new AdminEntity();
            admin.setId(UUID.randomUUID().toString());
            admin.setName("管理员");
            admin.setPhone("admin");
            admin.setPassword(passwordEncoder.encode("admin123"));
            admin.setAdminId("ADM_DEMO_" + UUID.randomUUID().toString().substring(0, 6));
            admin.setMfaEnabled(false);
            userRepository.save(admin);
            log.info("Demo admin account created: admin / admin123");
        }

        if (!userRepository.findByPhone("student").isPresent()) {
            StudentEntity student = new StudentEntity();
            student.setId(UUID.randomUUID().toString());
            student.setName("李明");
            student.setPhone("student");
            student.setPassword(passwordEncoder.encode("123456"));
            student.setStudentId("STU_DEMO_" + UUID.randomUUID().toString().substring(0, 6));
            student.setClassId("class-001");
            student.setSeatRow(3);
            student.setSeatCol(2);
            userRepository.save(student);
            log.info("Demo student account created: student / 123456");
        }
    }

    @Transactional
    public AuthResponse login(LoginRequest request) {
        String username = request.username().trim();
        loginThrottleService.assertAllowed(username);
        // 登录名契约：手机号/账号优先；学生可用管理员导入的学号登录。
        UserEntity user = userRepository.findByPhone(username)
                .or(() -> studentRepository.findByStudentId(username).map(s -> (UserEntity) s))
                .orElseThrow(() -> {
                    loginThrottleService.recordFailure(username);
                    return new InvalidCredentialsException("用户名或密码错误");
                });

        if (!passwordEncoder.matches(request.password(), user.getPassword())) {
            loginThrottleService.recordFailure(username);
            throw new InvalidCredentialsException("用户名或密码错误");
        }
        loginThrottleService.recordSuccess(username);

        AuthResponse response = issueTokens(user);
        log.info("User {} logged in as {}", user.getId(), response.role());
        return response;
    }

    @Transactional
    public AuthResponse register(RegisterRequest request) {
        if (userRepository.findByPhone(request.username()).isPresent()) {
            throw new BusinessException("该手机号已注册");
        }

        // 仅开放教师自助注册；学生账号由管理员批量导入（含初始密码），不提供自助注册。
        String role = request.role() != null ? request.role().toLowerCase(Locale.ROOT) : "teacher";
        if (!"teacher".equals(role)) {
            throw new IllegalArgumentException("仅支持注册教师账号");
        }
        String uuid = UUID.randomUUID().toString();

        TeacherEntity teacher = new TeacherEntity();
        teacher.setId(uuid);
        teacher.setTeacherId("TCH-" + uuid.substring(0, 8));
        UserEntity user = teacher;

        user.setName(request.name() != null ? request.name() : request.username());
        user.setPhone(request.username());
        user.setPassword(passwordEncoder.encode(request.password()));
        userRepository.save(user);

        return issueTokens(user);
    }

    @Transactional(readOnly = true)
    public AuthResponse refreshToken(RefreshTokenRequest request) {
        if (!tokenProvider.isRefreshToken(request.refreshToken())) {
            throw new InvalidCredentialsException("无效的刷新令牌");
        }

        String userId = tokenProvider.getUserIdFromToken(request.refreshToken());
        UserEntity user = userRepository.findById(userId)
                .orElseThrow(() -> new InvalidCredentialsException("用户不存在"));

        // Rotation + reuse detection: the presented token is consumed atomically.
        // A replayed or expired token is no longer present in Redis and is rejected.
        String jti = tokenProvider.getJtiFromToken(request.refreshToken());
        if (!refreshTokenStore.consume(jti)) {
            throw new InvalidCredentialsException("刷新令牌已失效，请重新登录");
        }

        return issueTokens(user);
    }

    private AuthResponse issueTokens(UserEntity user) {
        String role = extractRole(user);
        boolean mustChange = user.isMustChangePassword();
        String accessToken = tokenProvider.generateAccessToken(user.getId(), role, mustChange);
        String refreshToken = tokenProvider.generateRefreshToken(user.getId());
        refreshTokenStore.store(
                tokenProvider.getJtiFromToken(refreshToken),
                user.getId(),
                Duration.ofMillis(refreshExpirationMs));
        return new AuthResponse(accessToken, refreshToken, user.getId(), role, user.getName(), mustChange);
    }

    @Transactional(readOnly = true)
    public UserProfileDTO getProfile(String userId) {
        UserEntity user = userRepository.findById(userId)
                .orElseThrow(() -> new RuntimeException("用户不存在"));
        return toProfileDTO(user);
    }

    @Transactional
    public UserProfileDTO updateProfile(String userId, ProfileUpdateRequest request) {
        UserEntity user = userRepository.findById(userId)
                .orElseThrow(() -> new RuntimeException("用户不存在"));

        String phone = request.phone() != null ? request.phone().trim() : "";
        if (!phone.isBlank() && !phone.equals(user.getPhone())) {
            userRepository.findByPhone(phone).ifPresent(existing -> {
                if (!existing.getId().equals(userId)) {
                    throw new IllegalArgumentException("该手机号已被使用");
                }
            });
            user.setPhone(phone);
        }

        user.setName(request.name().trim());
        userRepository.save(user);
        return toProfileDTO(user);
    }

    /**
     * 修改密码后立即签发不含 mustChangePassword 的新令牌，旧令牌到期前仍受
     * PasswordChangeRequiredFilter 限制，不能继续调用业务接口。
     */
    @Transactional
    public AuthResponse changePassword(String userId, PasswordChangeRequest request) {
        UserEntity user = userRepository.findById(userId)
                .orElseThrow(() -> new RuntimeException("用户不存在"));
        if (!passwordEncoder.matches(request.oldPassword(), user.getPassword())) {
            throw new IllegalArgumentException("当前密码不正确");
        }
        user.setPassword(passwordEncoder.encode(request.newPassword()));
        user.setMustChangePassword(false);
        user.setPasswordChangedAt(java.time.LocalDateTime.now());
        userRepository.save(user);
        // 改密后撤销该用户全部旧 refresh token，旧设备无法继续续期。
        refreshTokenStore.revokeAllForUser(userId);
        return issueTokens(user);
    }

    /**
     * 服务端登出：撤销 refresh token（删除 Redis 中的 jti），使其无法再被刷新。
     * 无效/空 token 静默 no-op。
     */
    @Transactional
    public void logout(RefreshTokenRequest request) {
        String refreshToken = request.refreshToken();
        if (refreshToken == null || refreshToken.isBlank()) {
            return;
        }
        if (!tokenProvider.isRefreshToken(refreshToken)) {
            return;
        }
        refreshTokenStore.consume(tokenProvider.getJtiFromToken(refreshToken));
    }

    private String extractRole(UserEntity user) {
        if (user instanceof AdminEntity) return "admin";
        if (user instanceof StudentEntity) return "student";
        if (user instanceof TeacherEntity) return "teacher";
        return "teacher";
    }

    private UserProfileDTO toProfileDTO(UserEntity user) {
        String teacherId = user instanceof TeacherEntity t ? t.getTeacherId() : null;
        String studentId = user instanceof StudentEntity s ? s.getStudentId() : null;

        return new UserProfileDTO(
                user.getId(), user.getName(), extractRole(user),
                user.getPhone(), user.getAvatar(), user.isWechatBound(),
                teacherId, studentId, user.isMustChangePassword()
        );
    }
}
