-- 加宽 analysis_jobs.source_hash：objectName 为 UUID_文件名，长文件名会超出原 varchar(64)
ALTER TABLE analysis_jobs ALTER COLUMN source_hash TYPE VARCHAR(255);
