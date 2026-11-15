package com.macteval.service;

import com.macteval.dto.ClassGroupDTO;
import com.macteval.model.entity.ClassGroupEntity;
import com.macteval.repository.ClassGroupRepository;
import com.macteval.repository.LessonSessionRepository;
import com.macteval.repository.StudentRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.NoSuchElementException;
import java.util.UUID;

@Service
@RequiredArgsConstructor
public class ClassGroupService {

    private final ClassGroupRepository classGroupRepository;
    private final StudentRepository studentRepository;
    private final LessonSessionRepository lessonSessionRepository;

    @Transactional(readOnly = true)
    public List<ClassGroupDTO> listClassGroups() {
        return classGroupRepository.findAll().stream().map(this::toDTO).toList();
    }

    @Transactional(readOnly = true)
    public ClassGroupDTO getClassGroup(String id) {
        ClassGroupEntity entity = classGroupRepository.findById(id)
                .orElseThrow(() -> new NoSuchElementException("班级不存在"));
        return toDTO(entity);
    }

    @Transactional
    public ClassGroupDTO createClassGroup(String name, String grade, String department) {
        ClassGroupEntity entity = new ClassGroupEntity();
        entity.setId(UUID.randomUUID().toString());
        entity.setName(name);
        entity.setGrade(grade);
        entity.setDepartment(department);
        classGroupRepository.save(entity);
        return toDTO(entity);
    }

    @Transactional
    public ClassGroupDTO updateClassGroup(String id, String name, String grade, String department) {
        ClassGroupEntity entity = classGroupRepository.findById(id)
                .orElseThrow(() -> new NoSuchElementException("班级不存在"));
        entity.setName(name);
        entity.setGrade(grade);
        entity.setDepartment(department);
        classGroupRepository.save(entity);
        return toDTO(entity);
    }

    @Transactional
    public void deleteClassGroup(String id) {
        if (!classGroupRepository.existsById(id)) {
            throw new NoSuchElementException("班级不存在");
        }
        if (studentRepository.existsByClassId(id) || lessonSessionRepository.existsByClassId(id)) {
            throw new com.macteval.exception.BusinessException(
                    "该班级仍有关联学生或课堂，请先转班/归档后再删除");
        }
        classGroupRepository.deleteById(id);
    }

    private ClassGroupDTO toDTO(ClassGroupEntity entity) {
        return new ClassGroupDTO(
                entity.getId(), entity.getName(), entity.getGrade(),
                entity.getDepartment(),
                entity.getStudentCount() != null ? entity.getStudentCount() : 0,
                entity.getCreatedAt()
        );
    }
}
