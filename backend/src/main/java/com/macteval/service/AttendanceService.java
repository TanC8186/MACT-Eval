package com.macteval.service;

import com.macteval.dto.*;
import com.macteval.exception.BusinessException;
import com.macteval.model.entity.AttendanceRecordEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.AttendanceRecordRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.security.ResourceGuard;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

@Slf4j
@Service
@RequiredArgsConstructor
public class AttendanceService {

    private final AttendanceRecordRepository attendanceRepository;
    private final StudentRepository studentRepository;
    private final LessonSessionRepository lessonSessionRepository;
    private final ResourceGuard resourceGuard;

    /** 开始课堂后的迟到宽限期（分钟）。 */
    @Value("${mact.attendance.late-grace-minutes:10}")
    private long lateGraceMinutes;

    private void assertTeacherOwnsSession(String lessonSessionId, String authenticatedTeacherId) {
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("课堂不存在"));
        resourceGuard.assertTeacherOwnsCourse(authenticatedTeacherId, session.getTeacherId());
    }

    /**
     * Student self check-in via mini program.
     */
    @Transactional
    public AttendanceDTO checkin(CheckinRequest request, String authenticatedStudentId) {
        // 属主校验: 学生只能为自己签到, studentId 一律以认证身份为准
        resourceGuard.assertStudentOwns(authenticatedStudentId, request.studentId());

        LessonSessionEntity session = lessonSessionRepository.findById(request.lessonSessionId())
                .orElseThrow(() -> new RuntimeException("课堂不存在"));
        StudentEntity student = studentRepository.findById(authenticatedStudentId)
                .orElseThrow(() -> new RuntimeException("学生不存在"));

        // 课堂时间窗口：只有进行中的课堂接受签到；结束/取消后不得新增证据。
        if (session.getStatus() != LessonSessionEntity.SessionStatus.IN_PROGRESS) {
            throw new BusinessException(409, "课堂当前状态为 " + session.getStatus() + "，暂不能签到");
        }
        // 班级归属必须完整；缺失班级不接受签到，转人工处理，避免跨班误签。
        if (session.getClassId() == null || session.getClassId().isBlank()
                || student.getClassId() == null || student.getClassId().isBlank()) {
            throw new BusinessException(409, "课堂或学生未关联班级，请教师手动处理考勤");
        }
        if (!session.getClassId().equals(student.getClassId())) {
            throw new org.springframework.security.access.AccessDeniedException(
                    "只能为自己班级的课堂签到");
        }

        var existing = attendanceRepository.findByLessonSessionIdAndStudentId(
                request.lessonSessionId(), authenticatedStudentId);
        AttendanceRecordEntity record;
        if (!existing.isEmpty()) {
            record = existing.get(0);
            // 教师最终裁定优先：学生不能把已确认的缺勤改成出勤。
            if (record.isTeacherConfirmed()) {
                throw new BusinessException(409, "教师已确认考勤，无法自行修改；请联系教师复核");
            }
        } else {
            record = new AttendanceRecordEntity();
            record.setId(UUID.randomUUID().toString());
            record.setLessonSessionId(request.lessonSessionId());
            record.setStudentId(authenticatedStudentId);
        }

        LocalDateTime now = LocalDateTime.now();
        LocalDateTime startedAt = session.getActualStartTime() != null
                ? session.getActualStartTime() : session.getStartTime();
        boolean late = startedAt != null
                && now.isAfter(startedAt.plusMinutes(Math.max(0, lateGraceMinutes)));
        record.setStatus(late
                ? AttendanceRecordEntity.AttendanceStatus.LATE
                : AttendanceRecordEntity.AttendanceStatus.PRESENT);
        record.setSource(AttendanceRecordEntity.AttendanceSource.STUDENT_CHECKIN);
        record.setCheckinTime(now);
        // teacherConfirmed / teacherConfirmedAt 不在此处修改。
        attendanceRepository.save(record);
        log.info("Student {} checked in for lesson {} as {}", authenticatedStudentId,
                request.lessonSessionId(), record.getStatus());
        return toDTO(record);
    }

    /**
     * Get attendance records for a lesson session.
     */
    @Transactional(readOnly = true)
    public List<AttendanceDTO> getRecords(String lessonSessionId, String authenticatedUserId) {
        assertTeacherOwnsSession(lessonSessionId, authenticatedUserId);
        List<AttendanceRecordEntity> records = attendanceRepository.findByLessonSessionId(lessonSessionId);
        return mapToDTOs(records);
    }

    /**
     * Get attendance summary for a lesson session.
     */
    @Transactional(readOnly = true)
    public AttendanceSummaryDTO getSummary(String lessonSessionId, String authenticatedUserId) {
        assertTeacherOwnsSession(lessonSessionId, authenticatedUserId);
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("课堂不存在"));
        List<AttendanceRecordEntity> all = attendanceRepository.findByLessonSessionId(lessonSessionId);
        int present = (int) all.stream().filter(r -> r.getStatus() == AttendanceRecordEntity.AttendanceStatus.PRESENT).count();
        int late = (int) all.stream().filter(r -> r.getStatus() == AttendanceRecordEntity.AttendanceStatus.LATE).count();
        int absent = (int) all.stream().filter(r -> r.getStatus() == AttendanceRecordEntity.AttendanceStatus.ABSENT).count();
        int excused = (int) all.stream().filter(r -> r.getStatus() == AttendanceRecordEntity.AttendanceStatus.EXCUSED).count();

        // 分母优先使用班级名册人数：只签到 1 人时不能把统计基数缩成 1。
        long roster = session.getClassId() != null && !session.getClassId().isBlank()
                ? studentRepository.countByClassId(session.getClassId()) : 0;
        int total = (int) Math.max(all.size(), roster);
        int settled = present + late + absent + excused;
        int pending = Math.max(0, total - settled);

        long confirmed = all.stream().filter(r -> r.isTeacherConfirmed()).count();
        double coverage = total > 0 ? (double) confirmed / total : 0;
        String quality = coverage >= 0.8 ? "good" : coverage >= 0.5 ? "limited" : "unavailable";

        return new AttendanceSummaryDTO(lessonSessionId, total, present, late, absent, excused, pending, coverage, quality);
    }

    /**
     * Batch initialize attendance records for a lesson session (prepares records for all students).
     */
    @Transactional
    public void initializeRecords(String lessonSessionId, String authenticatedTeacherId) {
        LessonSessionEntity session = lessonSessionRepository.findById(lessonSessionId)
                .orElseThrow(() -> new RuntimeException("课堂不存在"));
        resourceGuard.assertTeacherOwnsCourse(authenticatedTeacherId, session.getTeacherId());
        String classId = session.getClassId();
        if (classId == null || classId.isBlank()) {
            throw new com.macteval.exception.BusinessException("该课堂未关联班级，无法初始化考勤名册");
        }
        List<StudentEntity> students = studentRepository.findByClassId(classId);
        if (students.isEmpty()) {
            return;
        }
        // 按名册补齐缺失项：已有学生（可能已签到/已确认）保留原记录，
        // 不能因为“存在任意一条记录”就跳过整班初始化。
        int inserted = 0;
        for (StudentEntity student : students) {
            inserted += attendanceRepository.insertPendingIfAbsent(
                    UUID.randomUUID().toString(), lessonSessionId, student.getId());
        }
        log.info("Attendance initialized for lesson {}: roster={}, newlyInserted={}",
                lessonSessionId, students.size(), inserted);
    }

    /**
     * Teacher confirms/corrects attendance records.
     */
    @Transactional
    public List<AttendanceDTO> confirmRecords(AttendanceConfirmRequest request, String authenticatedTeacherId) {
        assertTeacherOwnsSession(request.lessonSessionId(), authenticatedTeacherId);
        if (request.records() == null || request.records().isEmpty()) {
            throw new BusinessException(400, "确认列表不能为空");
        }
        // 先整体校验再写入，避免部分记录被静默跳过造成“确认成功但实际未生效”。
        List<AttendanceRecordEntity> toSave = new java.util.ArrayList<>();
        LocalDateTime now = LocalDateTime.now();
        for (AttendanceConfirmRequest.AttendanceItem item : request.records()) {
            AttendanceRecordEntity record = attendanceRepository.findById(item.recordId())
                    .orElseThrow(() -> new BusinessException(404, "考勤记录不存在: " + item.recordId()));
            if (!record.getLessonSessionId().equals(request.lessonSessionId())) {
                throw new BusinessException(409, "考勤记录不属于该课堂: " + item.recordId());
            }
            AttendanceRecordEntity.AttendanceStatus status;
            try {
                status = AttendanceRecordEntity.AttendanceStatus.valueOf(item.status());
            } catch (IllegalArgumentException | NullPointerException e) {
                throw new BusinessException(400, "无效的考勤状态: " + item.status());
            }
            record.setStatus(status);
            record.setTeacherConfirmed(true);
            record.setTeacherConfirmedAt(now);
            record.setSource(AttendanceRecordEntity.AttendanceSource.TEACHER_CORRECTION);
            if (item.comment() != null) record.setTeacherComment(item.comment());
            toSave.add(record);
        }
        attendanceRepository.saveAll(toSave);
        log.info("Attendance confirmed for lesson {} ({} records)",
                request.lessonSessionId(), toSave.size());
        return getRecords(request.lessonSessionId(), authenticatedTeacherId);
    }

    /**
     * Update camera-based evidence for a student.
     */
    @Transactional
    public void updateCameraEvidence(String lessonSessionId, String studentId, boolean visible, double confidence) {
        var records = attendanceRepository.findByLessonSessionIdAndStudentId(lessonSessionId, studentId);
        AttendanceRecordEntity record;
        if (records.isEmpty()) {
            record = new AttendanceRecordEntity();
            record.setId(UUID.randomUUID().toString());
            record.setLessonSessionId(lessonSessionId);
            record.setStudentId(studentId);
            record.setSource(AttendanceRecordEntity.AttendanceSource.CAMERA_EVIDENCE);
        } else {
            record = records.get(0);
        }
        record.setCameraVisible(visible);
        record.setCameraConfidence(confidence);
        if (!visible && record.getStatus() == AttendanceRecordEntity.AttendanceStatus.PENDING) {
            record.setStatus(AttendanceRecordEntity.AttendanceStatus.PENDING);
        }
        attendanceRepository.save(record);
    }

    private AttendanceDTO toDTO(AttendanceRecordEntity entity) {
        return toDTO(entity, studentRepository.findById(entity.getStudentId())
                .map(StudentEntity::getName).orElse("未知"));
    }

    private AttendanceDTO toDTO(AttendanceRecordEntity entity, String studentName) {
        return new AttendanceDTO(
                entity.getId(), entity.getLessonSessionId(), entity.getStudentId(),
                studentName, entity.getStatus().name(), entity.getSource().name(),
                entity.getCameraConfidence(), entity.getCameraVisible(),
                entity.getCheckinTime(), entity.isTeacherConfirmed(),
                entity.getTeacherComment(), entity.getCreatedAt()
        );
    }

    /** 批量预取学生姓名，避免逐条 findById 的 N+1。 */
    private List<AttendanceDTO> mapToDTOs(List<AttendanceRecordEntity> records) {
        if (records.isEmpty()) {
            return List.of();
        }
        Set<String> studentIds = records.stream()
                .map(AttendanceRecordEntity::getStudentId)
                .filter(Objects::nonNull)
                .collect(Collectors.toSet());
        Map<String, String> names = studentIds.isEmpty()
                ? Map.of()
                : studentRepository.findAllById(studentIds).stream()
                        .collect(Collectors.toMap(StudentEntity::getId, StudentEntity::getName, (a, b) -> a));
        return records.stream()
                .map(r -> toDTO(r, names.getOrDefault(r.getStudentId(), "未知")))
                .toList();
    }
}
