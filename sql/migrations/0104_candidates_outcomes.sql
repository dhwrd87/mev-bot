CREATE TABLE IF NOT EXISTS public.candidates_outcomes (
  id BIGSERIAL PRIMARY KEY,
  candidate_id BIGINT NOT NULL REFERENCES public.candidates(id) ON DELETE CASCADE,
  mined_block BIGINT,
  success BOOLEAN,
  gas_used BIGINT,
  effective_gas_price BIGINT,
  observed_after_sec DOUBLE PRECISION,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT candidates_outcomes_candidate_id_key UNIQUE (candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_outcomes_created_at ON public.candidates_outcomes (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_outcomes_candidate_id ON public.candidates_outcomes (candidate_id);
