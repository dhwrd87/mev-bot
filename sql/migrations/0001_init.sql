CREATE TABLE IF NOT EXISTS schema_migrations(
  id SERIAL PRIMARY KEY,
  version TEXT UNIQUE NOT NULL,
  applied_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trades(
  id BIGSERIAL PRIMARY KEY,
  mode TEXT CHECK (mode IN ('stealth','hunter','hybrid')) NOT NULL,
  chain TEXT NOT NULL,
  tx_hash TEXT,
  status TEXT CHECK (status IN ('pending','success','failed','reverted')) NOT NULL,
  reason TEXT,
  params JSONB NOT NULL,
  expected_profit_usd NUMERIC,
  realized_profit_usd NUMERIC,
  gas_used BIGINT,
  gas_price_gwei NUMERIC,
  slippage NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(),
  executed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS opportunities(
  id BIGSERIAL PRIMARY KEY,
  detector TEXT,
  chain TEXT NOT NULL,
  source_tx_hash TEXT,
  features JSONB,
  score NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pnl_daily(
  day DATE PRIMARY KEY,
  capital_deployed_usd NUMERIC,
  gross_profit_usd NUMERIC,
  gas_usd NUMERIC,
  net_profit_usd NUMERIC,
  win_rate NUMERIC
);

CREATE TABLE IF NOT EXISTS alerts(
  id BIGSERIAL PRIMARY KEY,
  level TEXT CHECK(level IN('info','warning','critical','success')),
  message TEXT,
  data JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_opps_created ON opportunities(created_at);
CREATE INDEX IF NOT EXISTS idx_opps_score ON opportunities(score);

INSERT INTO schema_migrations(version) VALUES ('0001_init')
ON CONFLICT (version) DO NOTHING;
