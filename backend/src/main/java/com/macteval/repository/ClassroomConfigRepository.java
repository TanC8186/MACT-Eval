package com.macteval.repository;

import com.macteval.model.entity.ClassroomConfig;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

@Repository
public interface ClassroomConfigRepository extends JpaRepository<ClassroomConfig, String> {
}
