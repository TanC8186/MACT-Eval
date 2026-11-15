package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "users")
@Inheritance(strategy = InheritanceType.SINGLE_TABLE)
@DiscriminatorColumn(name = "role", discriminatorType = DiscriminatorType.STRING)
public class UserEntity {
    @Id
    @Column(length = 36)
    private String id;

    @Column(nullable = false, length = 100)
    private String name;

    @Column(length = 255)
    private String password;

    @Column(length = 50, unique = true)
    private String phone;

    @Column(length = 255)
    private String avatar;

    @Column(name = "wechat_open_id", length = 100)
    private String wechatOpenId;

    @Column(name = "wechat_bound", nullable = false)
    private boolean wechatBound = false;

    /** 管理员导入的初始账号必须先修改密码才能使用业务接口。 */
    @Column(name = "must_change_password", nullable = false)
    private boolean mustChangePassword = false;

    @Column(name = "password_changed_at")
    private java.time.LocalDateTime passwordChangedAt;

    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    @PrePersist
    protected void onCreate() {
        createdAt = LocalDateTime.now();
        updatedAt = LocalDateTime.now();
    }

    @PreUpdate
    protected void onUpdate() {
        updatedAt = LocalDateTime.now();
    }
}
