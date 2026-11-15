package com.macteval.repository;

import com.macteval.model.entity.StudentEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
public interface StudentRepository extends JpaRepository<StudentEntity, String> {

    List<StudentEntity> findByClassId(String classId);

    Optional<StudentEntity> findByStudentId(String studentId);

    long countByClassId(String classId);
    boolean existsByClassId(String classId);
}
