-- Add a timestamp column `ts` for older/newer scripts that expect it
ALTER TABLE public.trades ADD COLUMN IF NOT EXISTS ts TIMESTAMPTZ;
UPDATE public.trades SET ts = COALESCE(ts, created_at, now());
ALTER TABLE public.trades ALTER COLUMN ts SET DEFAULT now();

-- Helpful index (matches what many queries expect)
CREATE INDEX IF NOT EXISTS trades_ts_idx ON public.trades (ts DESC);
