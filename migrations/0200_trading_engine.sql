-- Migration: Trading Engine Tables
-- Date: 2026-03-05
-- Description: Tables for trade recording and strategy performance tracking

-- Main trades table with complete trade lifecycle
CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- Opportunity info
    opportunity_id TEXT,
    opportunity_type TEXT,
    detector TEXT,
    
    -- Chain info
    family TEXT NOT NULL,
    chain TEXT NOT NULL,
    network TEXT,
    
    -- Decision
    mode TEXT NOT NULL CHECK (mode IN ('stealth', 'hunter', 'none', 'paper', 'live')),
    strategy TEXT NOT NULL,
    decision_reason TEXT,
    decision_latency_ms FLOAT,
    
    -- Execution
    executed BOOLEAN NOT NULL,
    execution_reason TEXT,
    execution_latency_ms FLOAT,
    tx_hash TEXT,
    bundle_tag TEXT,
    relay TEXT,
    
    -- Trading pair
    token_in TEXT,
    token_out TEXT,
    pair TEXT,
    dex TEXT,
    
    -- Sizing
    requested_size_usd FLOAT CHECK (requested_size_usd >= 0),
    approved_size_usd FLOAT CHECK (approved_size_usd >= 0),
    actual_size_usd FLOAT,
    
    -- P&L
    expected_profit_usd FLOAT,
    realized_profit_usd FLOAT,
    gas_cost_usd FLOAT CHECK (gas_cost_usd >= 0),
    net_profit_usd FLOAT,
    profit_margin_pct FLOAT,
    
    -- Execution details
    slippage_bps FLOAT CHECK (slippage_bps >= 0),
    gas_used BIGINT CHECK (gas_used >= 0),
    gas_price_gwei FLOAT CHECK (gas_price_gwei >= 0),
    sandwiched BOOLEAN DEFAULT FALSE,
    
    -- Context (JSONB for flexibility)
    risk_state JSONB,
    opportunity_data JSONB,
    decision_context JSONB,
    execution_metadata JSONB,
    
    -- Errors
    error TEXT,
    
    -- Constraints
    CONSTRAINT unique_tx_hash UNIQUE (tx_hash)
);

-- Backward-compatible column additions for pre-existing trades table.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS opportunity_id TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS opportunity_type TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS detector TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS family TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS chain TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS network TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS mode TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS decision_reason TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS decision_latency_ms FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS executed BOOLEAN;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS execution_reason TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS execution_latency_ms FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS tx_hash TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS bundle_tag TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS relay TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS token_in TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS token_out TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS pair TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS dex TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS requested_size_usd FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS approved_size_usd FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS actual_size_usd FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS expected_profit_usd FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS realized_profit_usd FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS gas_cost_usd FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS net_profit_usd FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS profit_margin_pct FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS slippage_bps FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS gas_used BIGINT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS gas_price_gwei FLOAT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS sandwiched BOOLEAN DEFAULT FALSE;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS risk_state JSONB;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS opportunity_data JSONB;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS decision_context JSONB;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS execution_metadata JSONB;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS error TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'unique_tx_hash'
    ) THEN
        ALTER TABLE trades ADD CONSTRAINT unique_tx_hash UNIQUE (tx_hash);
    END IF;
END
$$;

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_chain_mode ON trades(chain, mode, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_executed ON trades(strategy, executed, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_executed_profit ON trades(executed, net_profit_usd DESC) WHERE executed = true;

-- Strategy performance aggregates
CREATE TABLE IF NOT EXISTS strategy_performance (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    family TEXT NOT NULL,
    chain TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    
    -- Counts
    opportunities_total INT DEFAULT 0 CHECK (opportunities_total >= 0),
    trades_attempted INT DEFAULT 0 CHECK (trades_attempted >= 0),
    trades_executed INT DEFAULT 0 CHECK (trades_executed >= 0),
    trades_succeeded INT DEFAULT 0 CHECK (trades_succeeded >= 0),
    trades_failed INT DEFAULT 0 CHECK (trades_failed >= 0),
    
    -- P&L
    gross_profit_usd FLOAT DEFAULT 0,
    gas_cost_usd FLOAT DEFAULT 0 CHECK (gas_cost_usd >= 0),
    net_profit_usd FLOAT DEFAULT 0,
    
    -- Performance
    win_rate FLOAT DEFAULT 0 CHECK (win_rate >= 0 AND win_rate <= 100),
    avg_profit_per_trade FLOAT DEFAULT 0,
    largest_win_usd FLOAT DEFAULT 0,
    largest_loss_usd FLOAT DEFAULT 0,
    
    -- Latency
    avg_decision_latency_ms FLOAT DEFAULT 0 CHECK (avg_decision_latency_ms >= 0),
    avg_execution_latency_ms FLOAT DEFAULT 0 CHECK (avg_execution_latency_ms >= 0),
    
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- One record per day per strategy
    UNIQUE(date, family, chain, mode, strategy)
);

-- Backward-compatible column additions for pre-existing strategy_performance table.
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS date DATE;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS family TEXT;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS chain TEXT;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS mode TEXT;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS strategy TEXT;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS opportunities_total INT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS trades_attempted INT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS trades_executed INT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS trades_succeeded INT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS trades_failed INT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS gross_profit_usd FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS gas_cost_usd FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS net_profit_usd FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS win_rate FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS avg_profit_per_trade FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS largest_win_usd FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS largest_loss_usd FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS avg_decision_latency_ms FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS avg_execution_latency_ms FLOAT DEFAULT 0;
ALTER TABLE strategy_performance ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'strategy_performance_date_family_chain_mode_strategy_key'
    ) THEN
        ALTER TABLE strategy_performance
            ADD CONSTRAINT strategy_performance_date_family_chain_mode_strategy_key
            UNIQUE (date, family, chain, mode, strategy);
    END IF;
END
$$;

-- Index for querying recent performance
CREATE INDEX IF NOT EXISTS idx_strategy_performance_date ON strategy_performance(date DESC, family, chain);
CREATE INDEX IF NOT EXISTS idx_strategy_performance_strategy ON strategy_performance(strategy, date DESC);

-- Comments for documentation
COMMENT ON TABLE trades IS 'Complete record of all trading decisions and executions';
COMMENT ON TABLE strategy_performance IS 'Daily aggregated performance metrics per strategy';

COMMENT ON COLUMN trades.decision_reason IS 'Why this mode/strategy was chosen (gas_spike, high_slippage_risk, etc)';
COMMENT ON COLUMN trades.execution_reason IS 'Outcome reason (executed, risk_blocked, sim_failed, etc)';
COMMENT ON COLUMN trades.profit_margin_pct IS 'Net profit as percentage of position size';
COMMENT ON COLUMN trades.risk_state IS 'Risk manager state at time of trade';
COMMENT ON COLUMN trades.opportunity_data IS 'Full opportunity data for debugging';
COMMENT ON COLUMN trades.decision_context IS 'Full decision context for analysis';
COMMENT ON COLUMN trades.execution_metadata IS 'Detailed execution results';
