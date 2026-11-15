package com.macteval.repository;

import com.macteval.model.entity.CourseEntity;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.List;

@Repository
public interface CourseRepository extends JpaRepository<CourseEntity, String> {
    List<CourseEntity> findByTeacherId(String teacherId);
    Page<CourseEntity> findByTeacherId(String teacherId, Pageable pageable);
    List<CourseEntity> findByDepartment(String department);
}
