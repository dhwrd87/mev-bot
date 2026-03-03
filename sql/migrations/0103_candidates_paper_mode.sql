CREATE TABLE IF NOT EXISTS public.candidates (
  id BIGSERIAL PRIMARY KEY,
  ts_ms BIGINT NOT NULL,
  tx_hash TEXT NOT NULL,
  kind TEXT NOT NULL,
  score DOUBLE PRECISION NOT NULL,
  notes JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_tx_kind ON public.candidates (tx_hash, kind);
CREATE INDEX IF NOT EXISTS idx_candidates_created_at ON public.candidates (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_ts_ms ON public.candidates (ts_ms DESC);
