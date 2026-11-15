package com.macteval.service;

import com.macteval.dto.BatchImportRequest;
import com.macteval.dto.StudentImportResult;
import com.macteval.exception.BusinessException;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.UserRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;

/**
 * 管理员批量导入学生。
 *
 * <p>契约：
 * <ul>
 *   <li>去重键是业务学号 {@code student_id}，不是随机主键 UUID；</li>
 *   <li>同一文件内重复学号、库中已存在学号按幂等跳过；</li>
 *   <li>所有行先校验再写入，校验失败整批不落库（全有全无）；</li>
 *   <li>手机号为空也允许导入，学生可用学号登录；</li>
 *   <li>初始密码必须首次登录后修改（must_change_password）。</li>
 * </ul>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class StudentImportService {

    static final String INITIAL_PASSWORD = "123456";
    private static final int MAX_ROWS = 500;

    private final StudentRepository studentRepository;
    private final UserRepository userRepository;
    private final ClassGroupRepository classGroupRepository;
    private final PasswordEncoder passwordEncoder;

    @Transactional
    public StudentImportResult importStudents(BatchImportRequest request) {
        if (request.students() == null || request.students().isEmpty()) {
            throw new BusinessException(400, "学生列表不能为空");
        }
        if (request.students().size() > MAX_ROWS) {
            throw new BusinessException(400, "单次最多导入 " + MAX_ROWS + " 名学生");
        }
        if (classGroupRepository.findById(request.classId()).isEmpty()) {
            throw new BusinessException(404, "班级不存在: " + request.classId());
        }

        List<ValidatedRow> validated = new ArrayList<>();
        List<String> errors = new ArrayList<>();
        Set<String> seen = new HashSet<>();

        for (int i = 0; i < request.students().size(); i++) {
            int rowNumber = i + 1;
            BatchImportRequest.StudentEntry entry = request.students().get(i);
            String studentId = trim(entry.studentId());
            String name = trim(entry.name());
            String phone = trim(entry.phone());

            if (studentId == null) {
                errors.add("第 " + rowNumber + " 行：学号不能为空");
                continue;
            }
            if (studentId.length() > 50) {
                errors.add("第 " + rowNumber + " 行：学号不能超过 50 个字符");
                continue;
            }
            if (name == null) {
                errors.add("第 " + rowNumber + " 行：姓名不能为空");
                continue;
            }
            if (name.length() > 100) {
                errors.add("第 " + rowNumber + " 行：姓名不能超过 100 个字符");
                continue;
            }
            if (phone != null && phone.length() > 50) {
                errors.add("第 " + rowNumber + " 行：手机号/账号不能超过 50 个字符");
                continue;
            }
            if (entry.seatRow() != null && entry.seatRow() < 0
                    || entry.seatCol() != null && entry.seatCol() < 0) {
                errors.add("第 " + rowNumber + " 行：座位行列不能为负数");
                continue;
            }
            if (!seen.add(studentId)) {
                errors.add("第 " + rowNumber + " 行：文件内学号重复: " + studentId);
                continue;
            }
            if (studentRepository.findByStudentId(studentId).isPresent()) {
                validated.add(new ValidatedRow(rowNumber, studentId, name, phone, true, "学号已存在，已跳过"));
                continue;
            }
            if (phone != null) {
                var existingUser = userRepository.findByPhone(phone);
                if (existingUser.isPresent()) {
                    errors.add("第 " + rowNumber + " 行：手机号/账号已被使用: " + phone);
                    continue;
                }
            }
            validated.add(new ValidatedRow(rowNumber, studentId, name, phone, false, null));
        }

        if (!errors.isEmpty()) {
            // 全有全无：先把所有行错误返回，避免部分落库后管理员难以重试。
            String message = "导入校验失败，共 " + errors.size() + " 行错误："
                    + String.join("；", errors.subList(0, Math.min(5, errors.size())));
            if (errors.size() > 5) message += "；其余错误请修正后重试";
            throw new BusinessException(400, message);
        }

        int imported = 0;
        int skipped = 0;
        List<StudentImportResult.Row> rows = new ArrayList<>();
        for (ValidatedRow row : validated) {
            if (row.skip()) {
                skipped++;
                rows.add(new StudentImportResult.Row(
                        row.rowNumber(), row.studentId(), "SKIPPED", row.message()));
                continue;
            }
            StudentEntity student = new StudentEntity();
            student.setId(UUID.randomUUID().toString());
            student.setStudentId(row.studentId());
            student.setName(row.name());
            student.setPhone(row.phone());
            student.setClassId(request.classId());
            student.setPassword(passwordEncoder.encode(INITIAL_PASSWORD));
            student.setMustChangePassword(true);
            try {
                studentRepository.saveAndFlush(student);
            } catch (DataIntegrityViolationException e) {
                // 并发导入时唯一键冲突：不向客户端泄露 SQL，整批回滚。
                log.warn("Student import conflict for studentId={}", row.studentId());
                throw new BusinessException(409, "学号或账号已被并发导入: " + row.studentId());
            }
            imported++;
            rows.add(new StudentImportResult.Row(
                    row.rowNumber(), row.studentId(), "IMPORTED", "初始密码需首次登录后修改"));
        }
        log.info("Student import: class={}, imported={}, skipped={}", request.classId(), imported, skipped);
        return new StudentImportResult(imported, skipped, rows);
    }

    private static String trim(String value) {
        if (value == null) return null;
        String trimmed = value.trim();
        return trimmed.isEmpty() ? null : trimmed;
    }

    private record ValidatedRow(int rowNumber, String studentId, String name,
                                String phone, boolean skip, String message) {
    }
}
