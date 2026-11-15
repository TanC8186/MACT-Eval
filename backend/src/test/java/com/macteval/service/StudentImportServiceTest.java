package com.macteval.service;

import com.macteval.dto.BatchImportRequest;
import com.macteval.model.entity.ClassGroupEntity;
import com.macteval.model.entity.StudentEntity;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.StudentRepository;
import com.macteval.repository.UserRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.crypto.password.PasswordEncoder;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.*;

/**
 * BE02：学号去重、重复导入幂等、无效行全有全无、初始密码强制修改。
 */
class StudentImportServiceTest {

    private StudentRepository students;
    private UserRepository users;
    private ClassGroupRepository classes;
    private StudentImportService service;

    @BeforeEach
    void setUp() {
        students = mock(StudentRepository.class);
        users = mock(UserRepository.class);
        classes = mock(ClassGroupRepository.class);
        PasswordEncoder encoder = mock(PasswordEncoder.class);
        when(encoder.encode("123456")).thenReturn("hashed");
        when(classes.findById("class-1")).thenReturn(Optional.of(new ClassGroupEntity()));
        service = new StudentImportService(students, users, classes, encoder);
    }

    @Test
    void duplicateImportIsIdempotentByStudentId() {
        StudentEntity existing = new StudentEntity();
        existing.setId("uuid-1");
        existing.setStudentId("S001");
        when(students.findByStudentId("S001")).thenReturn(Optional.of(existing));

        var result = service.importStudents(new BatchImportRequest("class-1",
                List.of(new BatchImportRequest.StudentEntry("S001", "张三", null, 1, 1))));

        assertEquals(0, result.imported());
        assertEquals(1, result.skipped());
        assertEquals("SKIPPED", result.rows().getFirst().status());
        verify(students, never()).saveAndFlush(any());
    }

    @Test
    void invalidRowRejectsWholeBatchBeforeWriting() {
        when(students.findByStudentId("S002")).thenReturn(Optional.empty());

        var request = new BatchImportRequest("class-1", List.of(
                new BatchImportRequest.StudentEntry("S002", "李四", null, 1, 1),
                new BatchImportRequest.StudentEntry("", "缺学号", null, 1, 2)));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.importStudents(request));
        verify(students, never()).saveAndFlush(any());
    }

    @Test
    void duplicateStudentIdInsideFileIsRejected() {
        var request = new BatchImportRequest("class-1", List.of(
                new BatchImportRequest.StudentEntry("S003", "王五", null, 1, 1),
                new BatchImportRequest.StudentEntry("S003", "王五重复", null, 1, 2)));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.importStudents(request));
    }

    @Test
    void importedStudentCanLoginByStudentIdAndMustChangePassword() {
        when(students.findByStudentId("S004")).thenReturn(Optional.empty());
        when(users.findByPhone("S004")).thenReturn(Optional.empty());
        StudentEntity[] saved = new StudentEntity[1];
        when(students.saveAndFlush(any(StudentEntity.class))).thenAnswer(inv -> {
            saved[0] = inv.getArgument(0);
            return saved[0];
        });

        var result = service.importStudents(new BatchImportRequest("class-1",
                List.of(new BatchImportRequest.StudentEntry("S004", "赵六", "S004", 1, 1))));

        assertEquals(1, result.imported());
        assertNotNull(saved[0]);
        assertEquals("S004", saved[0].getStudentId());
        assertEquals("S004", saved[0].getPhone());
        assertTrue(saved[0].isMustChangePassword());
    }

    @Test
    void phoneOwnedByAnotherAccountRejectsBatch() {
        when(students.findByStudentId("S005")).thenReturn(Optional.empty());
        when(users.findByPhone("13800000000")).thenReturn(Optional.of(new StudentEntity()));

        var request = new BatchImportRequest("class-1",
                List.of(new BatchImportRequest.StudentEntry("S005", "钱七", "13800000000", 1, 1)));

        assertThrows(com.macteval.exception.BusinessException.class,
                () -> service.importStudents(request));
        verify(students, never()).saveAndFlush(any());
    }
}
