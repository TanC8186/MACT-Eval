package com.macteval.repository;

import com.macteval.model.entity.UserEntity;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.stereotype.Repository;

import java.util.Optional;

@Repository
public interface UserRepository extends JpaRepository<UserEntity, String> {
    Optional<UserEntity> findByPhone(String phone);
    Optional<UserEntity> findByWechatOpenId(String wechatOpenId);
}
