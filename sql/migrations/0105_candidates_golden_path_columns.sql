CREATE TABLE IF NOT EXISTS public.candidates (
  id BIGSERIAL PRIMARY KEY,
  ts_ms BIGINT NOT NULL,
  tx_hash TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'golden_path',
  score DOUBLE PRECISION NOT NULL DEFAULT 0,
  notes JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS chain TEXT;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS seen_ts BIGINT;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS to_addr TEXT;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS decoded_method TEXT;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS venue_tag TEXT;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS estimated_gas BIGINT;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS estimated_edge_bps DOUBLE PRECISION;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS sim_ok BOOLEAN;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS pnl_est DOUBLE PRECISION;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS decision TEXT;
ALTER TABLE public.candidates ADD COLUMN IF NOT EXISTS reject_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_candidates_decision_created ON public.candidates (decision, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_seen_ts ON public.candidates (seen_ts DESC);
