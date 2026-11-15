-- MACT-Eval 数据库修复：修复 evaluation_reports 表缺陷
-- 版本: V7
--
-- 背景:
--   1. V1 建表时 evaluation_reports.lesson_id 为 VARCHAR(100) NOT NULL UNIQUE，
--      但代码与 JPA 实体从未写入/读取该列（实体映射的是 lesson_session_id），
--      导致生成报告时因 lesson_id 的 NOT NULL 约束直接抛异常。
--   2. lesson_session_id 无唯一约束，并发生成报告会写入重复记录。
--
-- 方案:
--   1. 清理 lesson_session_id 已有的重复数据（每组保留最新一条）。
--   2. 删除完全废弃的 lesson_id 列（实体与代码均未使用，连带移除其 NOT NULL + UNIQUE 约束）。
--   3. 为 lesson_session_id 添加部分唯一索引（WHERE lesson_session_id IS NOT NULL），
--      既防止并发生成重复报告，又允许多条历史 NULL 记录共存。

-- 1. 清理重复的 lesson_session_id（每组保留最新一条；created_at 相同则保留 id 较小的一条）
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY lesson_session_id
               ORDER BY created_at DESC, id
           ) AS rn
    FROM evaluation_reports
    WHERE lesson_session_id IS NOT NULL
)
DELETE FROM evaluation_reports
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- 2. 删除废弃的 lesson_id 列（连同其 NOT NULL + UNIQUE 约束一并移除）
ALTER TABLE evaluation_reports DROP COLUMN IF EXISTS lesson_id;

-- 3. 为 lesson_session_id 添加部分唯一索引，防止并发生成重复报告
CREATE UNIQUE INDEX IF NOT EXISTS uk_evaluation_reports_lesson_session
    ON evaluation_reports (lesson_session_id)
    WHERE lesson_session_id IS NOT NULL;
