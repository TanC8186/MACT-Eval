-- Preserve model predictions and normalized boxes alongside the original frame.
-- Signed download URLs are generated on reads, never stored in report snapshots.
ALTER TABLE behavior_evidence_frames ADD COLUMN IF NOT EXISTS metadata TEXT;
