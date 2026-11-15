package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import lombok.EqualsAndHashCode;

@Data
@EqualsAndHashCode(callSuper = true)
@Entity
@DiscriminatorValue("admin")
public class AdminEntity extends UserEntity {
    @Column(name = "admin_id", length = 50, unique = true)
    private String adminId;

    @Column(name = "mfa_enabled")
    private boolean mfaEnabled = false;
}
