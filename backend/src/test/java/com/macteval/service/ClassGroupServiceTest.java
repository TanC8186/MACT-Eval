package com.macteval.service;

import com.macteval.model.entity.ClassGroupEntity;
import com.macteval.repository.ClassGroupRepository;
import org.junit.jupiter.api.Test;

import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class ClassGroupServiceTest {

    @Test
    void updateClassGroupModifiesFields() {
        ClassGroupRepository repository = mock(ClassGroupRepository.class);
        ClassGroupService service = new ClassGroupService(repository, mock(com.macteval.repository.StudentRepository.class), mock(com.macteval.repository.LessonSessionRepository.class));

        ClassGroupEntity entity = new ClassGroupEntity();
        entity.setId("class-1");
        when(repository.findById("class-1")).thenReturn(Optional.of(entity));

        var result = service.updateClassGroup("class-1", "2023级1班", "2023", "教育技术学");

        assertEquals("2023级1班", result.name());
        assertEquals("2023", result.grade());
        assertEquals("教育技术学", result.department());
        verify(repository).save(entity);
    }

    @Test
    void updateClassGroupThrowsWhenNotFound() {
        ClassGroupRepository repository = mock(ClassGroupRepository.class);
        ClassGroupService service = new ClassGroupService(repository, mock(com.macteval.repository.StudentRepository.class), mock(com.macteval.repository.LessonSessionRepository.class));

        when(repository.findById("missing")).thenReturn(Optional.empty());

        assertThrows(RuntimeException.class,
                () -> service.updateClassGroup("missing", "n", "g", "d"));
    }

    @Test
    void deleteClassGroupRemovesById() {
        ClassGroupRepository repository = mock(ClassGroupRepository.class);
        ClassGroupService service = new ClassGroupService(repository, mock(com.macteval.repository.StudentRepository.class), mock(com.macteval.repository.LessonSessionRepository.class));

        when(repository.existsById("class-1")).thenReturn(true);

        service.deleteClassGroup("class-1");

        verify(repository).deleteById("class-1");
    }
}
