-- 2026-10-21: 论文范围收窄为视频 + 语音两个模态。
-- 移除文本语义相关模块（课件解析/题库/随堂题/作业/反馈/知识点/复核），
-- 这些表不再由后端实体或服务使用；应用已删除对应 controller/service/repository/entity。
--
-- 说明：
-- * 不修改任何已应用的迁移（V1–V20），只新增本迁移。
-- * 这些表之间没有外键，可安全删除。
-- * evaluation_reports 中的 quiz_statistics / assignment_summary / feedback_summary
--   历史文本列保留（可为空），不参与任何新报告评分。

DROP TABLE IF EXISTS review_requests;
DROP TABLE IF EXISTS quiz_publishes;
DROP TABLE IF EXISTS in_class_answers;
DROP TABLE IF EXISTS questions;
DROP TABLE IF EXISTS knowledge_points;
DROP TABLE IF EXISTS submissions;
DROP TABLE IF EXISTS assignments;
DROP TABLE IF EXISTS feedback_entries;
DROP TABLE IF EXISTS courseware_analysis;
DROP TABLE IF EXISTS courseware;
