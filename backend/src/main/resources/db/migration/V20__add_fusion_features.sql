-- Persist the fusion weight source and per-modality contributions so a stored
-- climate score can be traced back to EQUAL / PRIOR / CONFIG / LEARNED.
ALTER TABLE lesson_window_features
    ADD COLUMN fusion_features TEXT;
