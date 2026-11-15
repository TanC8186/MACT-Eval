-- 备课分析新增 AI 语义提取的目标/重难点清单
ALTER TABLE courseware_analysis ADD COLUMN objectives TEXT;
ALTER TABLE courseware_analysis ADD COLUMN key_points TEXT;
