-- MACT-Eval 历史任务归属回填
-- 版本: V26
--
-- V22 新增 owner_id/lesson_session_id 后，历史任务保持 NULL，教师列表看不到
-- 自己过去提交的任务。这里只对 params 是合法 JSON 且包含 lessonSessionId 的
-- 任务回填；无法解析/找不到课堂的任务保持 NULL（仅管理员可见），不猜测归属。
UPDATE analysis_jobs j
SET lesson_session_id = s.id,
    owner_id = s.teacher_id
FROM lesson_sessions s
WHERE j.lesson_session_id IS NULL
  AND j.params IS NOT NULL
  AND j.params <> ''
  AND pg_input_is_valid(j.params, 'jsonb')
  AND (j.params::jsonb ->> 'lessonSessionId') = s.id;
