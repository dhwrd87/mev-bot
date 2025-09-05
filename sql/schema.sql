-- sql/schema.sql
CREATE TABLE trades(
  id BIGSERIAL PRIMARY KEY,
  mode TEXT CHECK (mode IN ('stealth','hunter','hybrid')) NOT NULL,
  chain TEXT NOT NULL,
  tx_hash TEXT,
  status TEXT CHECK (status IN ('pending','success','failed','reverted')) NOT NULL,
  reason TEXT, -- e.g. "sandwiched_detected:false", "sim_fail", "risk_block"
  params JSONB NOT NULL,
  expected_profit_usd NUMERIC,
  realized_profit_usd NUMERIC,
  gas_used BIGINT,
  gas_price_gwei NUMERIC,
  slippage NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(),
  executed_at TIMESTAMPTZ
);

CREATE TABLE opportunities(
  id BIGSERIAL PRIMARY KEY,
  detector TEXT,             -- e.g., sandwich, snipe
  chain TEXT NOT NULL,
  source_tx_hash TEXT,
  features JSONB,            -- liquidity, depth, path, est_profit
  score NUMERIC,             -- 0..1
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE pnl_daily(
  day DATE PRIMARY KEY,
  capital_deployed_usd NUMERIC,
  gross_profit_usd NUMERIC,
  gas_usd NUMERIC,
  net_profit_usd NUMERIC,
  win_rate NUMERIC
);

CREATE TABLE alerts(
  id BIGSERIAL PRIMARY KEY,
  level TEXT CHECK(level IN('info','warning','critical','success')),
  message TEXT,
  data JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- indexes
CREATE INDEX idx_trades_created ON trades(created_at);
CREATE INDEX idx_trades_mode ON trades(mode);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_opps_created ON opportunities(created_at);
CREATE INDEX idx_opps_score ON opportunities(score);
