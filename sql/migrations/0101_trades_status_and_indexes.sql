-- widen status set to what the app emits
ALTER TABLE public.trades DROP CONSTRAINT IF EXISTS trades_status_chk;
ALTER TABLE public.trades DROP CONSTRAINT IF EXISTS trades_status_check;
ALTER TABLE public.trades
  ADD CONSTRAINT trades_status_check
  CHECK (status IN ('submitted','pending','included','success','failed','reverted'));

-- indexes used by queries/dashboards
CREATE INDEX IF NOT EXISTS idx_trades_tx_hash     ON public.trades (tx_hash);
CREATE INDEX IF NOT EXISTS idx_trades_bundle_tag  ON public.trades (bundle_tag);

-- ensure ops_state has a paused flag
INSERT INTO public.ops_state(k,v) VALUES ('paused','false')
ON CONFLICT (k) DO NOTHING;
