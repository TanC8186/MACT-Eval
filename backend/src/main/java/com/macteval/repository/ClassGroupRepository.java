package com.macteval.repository;

import com.macteval.model.entity.ClassGroupEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

@Repository
public interface ClassGroupRepository extends JpaRepository<ClassGroupEntity, String> {
}
