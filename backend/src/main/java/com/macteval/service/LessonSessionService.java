package com.macteval.service;

import com.macteval.dto.*;
import com.macteval.model.entity.ClassGroupEntity;
import com.macteval.model.entity.CourseEntity;
import com.macteval.model.entity.LessonSessionEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.AttendanceRecordRepository;
import com.macteval.repository.BehaviorObservationRepository;
import com.macteval.repository.CourseRepository;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.EvaluationReportRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.LessonTranscriptRepository;
import com.macteval.repository.LessonWindowFeatureRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.SuggestionRepository;
import com.macteval.security.ResourceGuard;
import lombok.RequiredArgsConstructor;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Pageable;
import org.springframework.data.domain.Sort;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.stream.Collectors;

@Slf4j
@Service
@RequiredArgsConstructor
public class LessonSessionService {

    private final LessonSessionRepository lessonSessionRepository;
    private final CourseRepository courseRepository;
    private final ClassGroupRepository classGroupRepository;
    private final ResourceGuard resourceGuard;
    private final StudentRepository studentRepository;
    private final AttendanceRecordRepository attendanceRepository;
    private final BehaviorObservationRepository behaviorRepository;
    private final LessonTranscriptRepository transcriptRepository;
    private final LessonWindowFeatureRepository windowFeatureRepository;
    private final EvaluationReportRepository reportRepository;
    private final SuggestionRepository suggestionRepository;

    /**
     * 列出课堂。课程过滤必须先校验课程属主，不能只按 courseId 直接查询，
     * 否则教师 A 传入教师 B 的 courseId 即可读取全部课堂。
     */
    @Transactional(readOnly = true)
    public List<LessonSessionDTO> listSessions(String teacherId, String courseId, boolean isAdmin) {
        List<LessonSessionEntity> sessions;
        if (courseId != null && !courseId.isBlank()) {
            CourseEntity course = courseRepository.findById(courseId)
                    .orElseThrow(() -> new NoSuchElementException("课程不存在"));
            if (!isAdmin && (teacherId == null || !teacherId.equals(course.getTeacherId()))) {
                throw new org.springframework.security.access.AccessDeniedException("无权访问其他教师的课程");
            }
            sessions = lessonSessionRepository.findByCourseId(courseId);
        } else if (teacherId != null && !teacherId.isBlank()) {
            LocalDateTime start = LocalDateTime.now().minusMonths(6);
            LocalDateTime end = LocalDateTime.now().plusMonths(1);
            sessions = lessonSessionRepository.findByTeacherIdAndDateBetween(teacherId, start, end);
        } else if (isAdmin) {
            sessions = lessonSessionRepository.findAll();
        } else {
            return List.of();
        }
        return mapToDTOs(sessions);
    }

    @Transactional(readOnly = true)
    public PageResponse<LessonSessionDTO> listSessionsPage(String teacherId, String courseId,
                                                           boolean isAdmin, int page, int size) {
        int pageSize = Math.max(1, Math.min(size, 100));
        int pageIndex = Math.max(0, page);
        Pageable pageable = PageRequest.of(pageIndex, pageSize,
                Sort.by(Sort.Direction.DESC, "date"));
        if (courseId != null && !courseId.isBlank()) {
            CourseEntity course = courseRepository.findById(courseId)
                    .orElseThrow(() -> new NoSuchElementException("课程不存在"));
            if (!isAdmin && (teacherId == null || !teacherId.equals(course.getTeacherId()))) {
                throw new org.springframework.security.access.AccessDeniedException("无权访问其他教师的课程");
            }
            var result = lessonSessionRepository.findByCourseId(courseId, pageable);
            return new PageResponse<>(mapToDTOs(result.getContent()), pageIndex, pageSize,
                    result.getTotalElements());
        }
        if (isAdmin) {
            var result = lessonSessionRepository.findAll(pageable);
            return new PageResponse<>(mapToDTOs(result.getContent()), pageIndex, pageSize,
                    result.getTotalElements());
        }
        if (teacherId == null || teacherId.isBlank()) {
            return new PageResponse<>(List.of(), pageIndex, pageSize, 0);
        }
        var result = lessonSessionRepository.findByTeacherId(teacherId, pageable);
        return new PageResponse<>(mapToDTOs(result.getContent()), pageIndex, pageSize,
                result.getTotalElements());
    }

    @Transactional(readOnly = true)
    public List<LessonSessionDTO> listSessionsForStudentClass(String studentUserId) {
        StudentEntity student = studentRepository.findById(studentUserId)
                .orElseThrow(() -> new RuntimeException("学生账号不存在"));
        if (student.getClassId() == null || student.getClassId().isBlank()) {
            return List.of();
        }
        return mapToDTOs(lessonSessionRepository.findByClassId(student.getClassId()));
    }

    @Transactional(readOnly = true)
    public LessonSessionDTO getSession(String id) {
        LessonSessionEntity entity = lessonSessionRepository.findById(id)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        return toDTO(entity);
    }

    /**
     * 读取课堂详情：管理员放行；教师须为属主；学生须属于课堂班级。
     */
    @Transactional(readOnly = true)
    public LessonSessionDTO getSessionForUser(String id, String userId, String role) {
        LessonSessionEntity entity = lessonSessionRepository.findById(id)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        if ("admin".equalsIgnoreCase(role)) {
            return toDTO(entity);
        }
        if ("teacher".equalsIgnoreCase(role)) {
            if (userId == null || !userId.equals(entity.getTeacherId())) {
                throw new org.springframework.security.access.AccessDeniedException("无权访问其他教师的课堂");
            }
            return toDTO(entity);
        }
        if ("student".equalsIgnoreCase(role)) {
            StudentEntity student = studentRepository.findById(userId)
                    .orElseThrow(() -> new NoSuchElementException("学生账号不存在"));
            if (student.getClassId() == null || !student.getClassId().equals(entity.getClassId())) {
                throw new org.springframework.security.access.AccessDeniedException("只能访问自己班级的课堂");
            }
            return toDTO(entity);
        }
        throw new org.springframework.security.access.AccessDeniedException("无权访问该课堂");
    }

    @Transactional
    public LessonSessionDTO createSession(LessonSessionCreateRequest request, String teacherId, boolean isAdmin) {
        CourseEntity course = courseRepository.findById(request.courseId())
                .orElseThrow(() -> new NoSuchElementException("课程不存在"));
        // 先校验课程属主，再检查班级是否存在，避免向非属主泄露其他教师课程的关联信息。
        if (!isAdmin && (teacherId == null || !teacherId.equals(course.getTeacherId()))) {
            throw new org.springframework.security.access.AccessDeniedException("只能为自己的课程创建课堂");
        }
        classGroupRepository.findById(request.classId())
                .orElseThrow(() -> new NoSuchElementException("班级不存在"));
        // 管理员代建课堂时，课堂仍归属课程教师，避免出现无法被任何教师管理的课堂。
        String ownerTeacherId = isAdmin ? course.getTeacherId() : teacherId;
        LessonSessionEntity entity = new LessonSessionEntity();
        entity.setId(UUID.randomUUID().toString());
        entity.setCourseId(request.courseId());
        entity.setClassId(request.classId());
        entity.setTeacherId(ownerTeacherId);
        entity.setTitle(request.title());
        entity.setDate(request.date() != null ? request.date() : LocalDateTime.now());
        entity.setStartTime(request.startTime() != null ? request.startTime() : LocalDateTime.now());
        entity.setEndTime(request.endTime());
        entity.setTeachingObjectives(request.teachingObjectives());
        entity.setStatus(LessonSessionEntity.SessionStatus.SCHEDULED);
        lessonSessionRepository.save(entity);
        log.info("Lesson session created: {} for course {}", entity.getId(), request.courseId());
        return toDTO(entity);
    }

    @Transactional
    public LessonSessionDTO updateStatus(String id, String status, String authenticatedTeacherId) {
        return updateStatus(id, status, authenticatedTeacherId, false);
    }

    /**
     * 课堂状态机：SCHEDULED → IN_PROGRESS → COMPLETED，异常路径只允许取消。
     * 已结束/已取消课堂不能直接重开；管理员通过显式 reopen 操作处理异常。
     * 重复提交同一状态是幂等的，不改变实际时间。
     */
    @Transactional
    public LessonSessionDTO updateStatus(String id, String status, String authenticatedTeacherId, boolean isAdmin) {
        LessonSessionEntity entity = lessonSessionRepository.findByIdForUpdate(id)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        if (!isAdmin) {
            resourceGuard.assertTeacherOwnsCourse(authenticatedTeacherId, entity.getTeacherId());
        }
        LessonSessionEntity.SessionStatus target;
        try {
            target = LessonSessionEntity.SessionStatus.valueOf(status);
        } catch (IllegalArgumentException | NullPointerException e) {
            throw new com.macteval.exception.BusinessException(400, "无效的课堂状态: " + status);
        }
        LessonSessionEntity.SessionStatus current = entity.getStatus();
        if (current == target) {
            return toDTO(entity);
        }
        if (!isTransitionAllowed(current, target)) {
            throw new com.macteval.exception.BusinessException(
                    409, "课堂状态不能从 " + current + " 变更为 " + target);
        }
        entity.setStatus(target);
        LocalDateTime now = LocalDateTime.now();
        if (target == LessonSessionEntity.SessionStatus.IN_PROGRESS) {
            if (entity.getActualStartTime() == null) {
                entity.setActualStartTime(now);
            }
            if (entity.getStartTime() == null) {
                // 兼容历史数据：计划开始时间缺失时补一次，但不覆盖已有计划时间。
                entity.setStartTime(now);
            }
        }
        if (target == LessonSessionEntity.SessionStatus.COMPLETED) {
            if (entity.getActualStartTime() == null) {
                entity.setActualStartTime(entity.getStartTime() != null ? entity.getStartTime() : now);
            }
            if (entity.getActualEndTime() == null) {
                entity.setActualEndTime(now);
            }
            entity.setEndTime(now);
        }
        lessonSessionRepository.save(entity);
        return toDTO(entity);
    }

    private boolean isTransitionAllowed(LessonSessionEntity.SessionStatus from,
                                        LessonSessionEntity.SessionStatus to) {
        return switch (from) {
            case SCHEDULED -> to == LessonSessionEntity.SessionStatus.IN_PROGRESS
                    || to == LessonSessionEntity.SessionStatus.CANCELLED;
            case IN_PROGRESS -> to == LessonSessionEntity.SessionStatus.COMPLETED
                    || to == LessonSessionEntity.SessionStatus.CANCELLED;
            case COMPLETED, CANCELLED -> false;
        };
    }

    /** 管理员异常重开：COMPLETED → IN_PROGRESS，清空实际结束时间并保留审计日志。 */
    @Transactional
    public LessonSessionDTO reopenSession(String id, String adminId) {
        LessonSessionEntity entity = lessonSessionRepository.findByIdForUpdate(id)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        if (entity.getStatus() != LessonSessionEntity.SessionStatus.COMPLETED) {
            throw new com.macteval.exception.BusinessException(409, "只有已结束课堂可以重开");
        }
        entity.setStatus(LessonSessionEntity.SessionStatus.IN_PROGRESS);
        entity.setActualEndTime(null);
        entity.setEndTime(null);
        if (entity.getActualStartTime() == null) {
            entity.setActualStartTime(LocalDateTime.now());
        }
        lessonSessionRepository.save(entity);
        log.warn("AUDIT_SESSION_REOPEN lesson={} admin={}", id, adminId);
        return toDTO(entity);
    }

    @Transactional
    public LessonSessionDTO updateSession(String id, UpdateSessionRequest request, String authenticatedTeacherId) {
        LessonSessionEntity entity = lessonSessionRepository.findById(id)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        resourceGuard.assertTeacherOwnsCourse(authenticatedTeacherId, entity.getTeacherId());
        if (request.title() != null && !request.title().isBlank()) {
            entity.setTitle(request.title());
        }
        if (request.date() != null) {
            entity.setDate(request.date());
        }
        if (request.startTime() != null) {
            entity.setStartTime(request.startTime());
        }
        if (request.endTime() != null) {
            entity.setEndTime(request.endTime());
        }
        if (request.teachingObjectives() != null) {
            entity.setTeachingObjectives(request.teachingObjectives());
        }
        lessonSessionRepository.save(entity);
        return toDTO(entity);
    }

    @Transactional
    public void deleteSession(String id, String userId, boolean isAdmin) {
        LessonSessionEntity entity = lessonSessionRepository.findById(id)
                .orElseThrow(() -> new NoSuchElementException("课堂不存在"));
        if (!isAdmin && (userId == null || !userId.equals(entity.getTeacherId()))) {
            throw new org.springframework.security.access.AccessDeniedException("无权删除其他教师的课堂");
        }
        // 有业务记录的课堂不直接物理删除：报告/考勤/分析结果需要可追溯。
        List<String> blockers = new java.util.ArrayList<>();
        if (reportRepository.existsByLessonSessionId(id)) blockers.add("评价报告");
        if (attendanceRepository.existsByLessonSessionId(id)) blockers.add("考勤记录");
        if (behaviorRepository.existsByLessonSessionId(id)) blockers.add("行为观测");
        if (transcriptRepository.existsByLessonSessionId(id)) blockers.add("转写");
        if (windowFeatureRepository.existsByLessonSessionId(id)) blockers.add("窗口特征");
        if (suggestionRepository.existsByLessonSessionId(id)) blockers.add("教学建议");
        if (!blockers.isEmpty()) {
            throw new com.macteval.exception.BusinessException(409,
                    "该课堂仍有业务记录（" + String.join("、", blockers)
                            + "），请先删除报告并归档后再删除课堂");
        }
        lessonSessionRepository.deleteById(id);
    }

    private LessonSessionDTO toDTO(LessonSessionEntity entity) {
        String courseName = courseRepository.findById(entity.getCourseId())
                .map(c -> c.getName()).orElse("");
        String className = classGroupRepository.findById(entity.getClassId())
                .map(c -> c.getName()).orElse("");
        return toDTO(entity, courseName, className);
    }

    private LessonSessionDTO toDTO(LessonSessionEntity entity, String courseName, String className) {
        List<String> kpIds = entity.getKnowledgePointIds() != null && !entity.getKnowledgePointIds().isBlank()
                ? List.of(entity.getKnowledgePointIds().split(","))
                : List.of();

        return new LessonSessionDTO(
                entity.getId(), entity.getCourseId(), courseName,
                entity.getClassId(), className,
                entity.getTeacherId(), entity.getTitle(),
                entity.getDate(), entity.getStartTime(), entity.getEndTime(),
                entity.getStatus().name(), kpIds,
                entity.getTeachingObjectives(), entity.getCreatedAt()
        );
    }

    /** 批量预取课程/班级名，避免列表映射时的 N+1。 */
    private List<LessonSessionDTO> mapToDTOs(List<LessonSessionEntity> sessions) {
        if (sessions.isEmpty()) {
            return List.of();
        }
        Set<String> courseIds = sessions.stream()
                .map(LessonSessionEntity::getCourseId)
                .filter(Objects::nonNull)
                .collect(Collectors.toSet());
        Set<String> classIds = sessions.stream()
                .map(LessonSessionEntity::getClassId)
                .filter(Objects::nonNull)
                .collect(Collectors.toSet());
        Map<String, String> courseNames = courseIds.isEmpty() ? Map.of()
                : courseRepository.findAllById(courseIds).stream()
                        .collect(Collectors.toMap(CourseEntity::getId, CourseEntity::getName, (a, b) -> a));
        Map<String, String> classNames = classIds.isEmpty() ? Map.of()
                : classGroupRepository.findAllById(classIds).stream()
                        .collect(Collectors.toMap(ClassGroupEntity::getId, ClassGroupEntity::getName, (a, b) -> a));
        return sessions.stream()
                .map(s -> toDTO(s,
                        courseNames.getOrDefault(s.getCourseId(), ""),
                        classNames.getOrDefault(s.getClassId(), "")))
                .toList();
    }
}
