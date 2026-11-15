-- 为简答题增加评分标准字段 (rubric)，供后续 AI 阅卷与教师人工评阅使用
ALTER TABLE questions ADD COLUMN rubric TEXT;
