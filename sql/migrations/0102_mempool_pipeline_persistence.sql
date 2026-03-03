CREATE TABLE IF NOT EXISTS public.mempool_events (
  id BIGSERIAL PRIMARY KEY,
  ts_ms BIGINT NOT NULL,
  tx_hash TEXT NOT NULL,
  source_endpoint TEXT,
  stream_id TEXT NOT NULL,
  raw_json JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT mempool_events_stream_id_key UNIQUE (stream_id)
);

CREATE INDEX IF NOT EXISTS idx_mempool_events_ts_ms ON public.mempool_events (ts_ms);
CREATE INDEX IF NOT EXISTS idx_mempool_events_tx_hash ON public.mempool_events (tx_hash);

CREATE TABLE IF NOT EXISTS public.mempool_tx (
  tx_hash TEXT PRIMARY KEY,
  chain_id BIGINT,
  "from" TEXT,
  "to" TEXT,
  nonce BIGINT,
  gas BIGINT,
  max_fee BIGINT,
  max_priority BIGINT,
  value BIGINT,
  input_len INTEGER,
  first_seen_ts_ms BIGINT NOT NULL,
  last_seen_ts_ms BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mempool_tx_last_seen_ts_ms ON public.mempool_tx (last_seen_ts_ms);

CREATE TABLE IF NOT EXISTS public.mempool_errors (
  id BIGSERIAL PRIMARY KEY,
  ts_ms BIGINT NOT NULL,
  tx_hash TEXT,
  endpoint TEXT,
  error_type TEXT NOT NULL,
  error_msg TEXT,
  http_status INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mempool_errors_ts_ms ON public.mempool_errors (ts_ms);
CREATE INDEX IF NOT EXISTS idx_mempool_errors_tx_hash ON public.mempool_errors (tx_hash);
