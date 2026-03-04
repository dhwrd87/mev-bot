-- Split internal attempt identifiers from on-chain tx hash.

ALTER TABLE IF EXISTS opportunity_attempts
  ADD COLUMN IF NOT EXISTS payload_hash TEXT;

ALTER TABLE IF EXISTS opportunity_attempts
  ADD COLUMN IF NOT EXISTS broadcasted_at TIMESTAMPTZ;

ALTER TABLE IF EXISTS opportunity_attempts
  ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;

ALTER TABLE IF EXISTS opportunity_attempts
  ADD COLUMN IF NOT EXISTS receipt_block_number BIGINT;

CREATE INDEX IF NOT EXISTS idx_opportunity_attempts_payload_hash
  ON opportunity_attempts (payload_hash);

-- Legacy cleanup: rows that were never broadcast should not surface tx_hash.
UPDATE opportunity_attempts
SET
  payload_hash = COALESCE(payload_hash, tx_hash),
  tx_hash = NULL
WHERE lifecycle_state NOT IN ('SENT', 'CONFIRMED', 'REVERTED', 'DROPPED')
  AND tx_hash IS NOT NULL;

