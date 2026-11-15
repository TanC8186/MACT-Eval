package com.macteval.model.entity;

import jakarta.persistence.*;
import lombok.Data;
import java.time.LocalDateTime;

@Data
@Entity
@Table(name = "suggestions")
public class SuggestionEntity {
    @Id @Column(length = 36) private String id;
    @Column(name = "lesson_session_id", length = 36) private String lessonSessionId;
    @Column(name = "report_id", length = 36) private String reportId;
    @Column(length = 100) private String category;
    @Column(length = 10) private String priority;
    @Column(columnDefinition = "TEXT") private String content;
    @Column(columnDefinition = "TEXT") private String evidence;
    @Column(length = 20) private String status = "pending";
    @Column(name = "accepted_at") private LocalDateTime acceptedAt;
    @Column(name = "ignored_at") private LocalDateTime ignoredAt;
    @Column(name = "adopted_at") private LocalDateTime adoptedAt;
    @Column(name = "rule_id", length = 80) private String ruleId;
    @Column(name = "rule_version", length = 20) private String ruleVersion;
    @Column(name = "evidence_refs", columnDefinition = "TEXT") private String evidenceRefs;
    @Column(name = "report_version") private Integer reportVersion;
    @Column(name = "actor_id", length = 36) private String actorId;
    @Column(name = "status_changed_at") private LocalDateTime statusChangedAt;
    @Column(name = "created_at") private LocalDateTime createdAt;
    @Column(name = "updated_at") private LocalDateTime updatedAt;
    @PrePersist protected void onCreate() { createdAt = LocalDateTime.now(); updatedAt = LocalDateTime.now(); }
    @PreUpdate protected void onUpdate() { updatedAt = LocalDateTime.now(); }
}
