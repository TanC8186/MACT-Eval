-- MACT-Eval 证据面板：evaluation_reports 增加证据溯源列
-- 版本: V9
--
-- 每个报告指标都要能追溯到数据来源、模型版本与覆盖率（审计验收标准）。
-- 新增列与 EvaluationReportEntity 字段保持一致：
--   model_version  — 生成报告所使用的模型/规则引擎版本
--   coverage       — 检测覆盖率 (0-1)
--   window_count   — 分析的时间窗口数
--   feature_count  — 参与计算的模态特征/信号数

ALTER TABLE evaluation_reports
    ADD COLUMN IF NOT EXISTS model_version VARCHAR(50),
    ADD COLUMN IF NOT EXISTS coverage DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS window_count INTEGER,
    ADD COLUMN IF NOT EXISTS feature_count INTEGER;
