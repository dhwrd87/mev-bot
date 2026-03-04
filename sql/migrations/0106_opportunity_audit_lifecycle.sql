-- Auditable, replayable opportunity lifecycle persistence.

DO $$
BEGIN
    CREATE TYPE attempt_lifecycle_status AS ENUM (
        'CREATED',
        'SIM_OK',
        'SIM_FAIL',
        'SENT',
        'BLOCKED',
        'CONFIRMED',
        'REVERTED',
        'DROPPED'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS reject_reason_codes (
    code TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS opportunity_inputs (
    input_id TEXT PRIMARY KEY,
    ts_ms BIGINT NOT NULL,
    family TEXT NOT NULL,
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    tx_hash TEXT,
    stream_id TEXT,
    raw_json JSONB,
    normalized_tx JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_inputs_stream_id ON opportunity_inputs (stream_id) WHERE stream_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opportunity_inputs_ts_ms ON opportunity_inputs (ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_opportunity_inputs_chain ON opportunity_inputs (family, chain, network);

CREATE TABLE IF NOT EXISTS opportunities_audit (
    opportunity_id TEXT PRIMARY KEY,
    input_id TEXT REFERENCES opportunity_inputs(input_id) ON DELETE SET NULL,
    ts_ms BIGINT NOT NULL,
    family TEXT NOT NULL,
    chain TEXT NOT NULL,
    network TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunities_audit_dedupe
    ON opportunities_audit (family, chain, network, tx_hash, kind);
CREATE INDEX IF NOT EXISTS idx_opportunities_audit_ts_ms ON opportunities_audit (ts_ms DESC);

CREATE TABLE IF NOT EXISTS opportunity_decisions (
    decision_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities_audit(opportunity_id) ON DELETE CASCADE,
    ts_ms BIGINT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ACCEPT', 'REJECT')),
    reason_code TEXT REFERENCES reject_reason_codes(code),
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_decisions_dedupe
    ON opportunity_decisions (opportunity_id, decision, COALESCE(reason_code, ''));
CREATE INDEX IF NOT EXISTS idx_opportunity_decisions_ts_ms ON opportunity_decisions (ts_ms DESC);

CREATE TABLE IF NOT EXISTS opportunity_simulations (
    sim_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities_audit(opportunity_id) ON DELETE CASCADE,
    ts_ms BIGINT NOT NULL,
    simulator TEXT NOT NULL,
    sim_ok BOOLEAN NOT NULL,
    pnl_est DOUBLE PRECISION,
    error_code TEXT,
    error_message TEXT,
    latency_ms DOUBLE PRECISION,
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_opportunity_simulations_ts_ms ON opportunity_simulations (ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_opportunity_simulations_opp ON opportunity_simulations (opportunity_id);

CREATE TABLE IF NOT EXISTS opportunity_attempts (
    attempt_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities_audit(opportunity_id) ON DELETE CASCADE,
    ts_ms BIGINT NOT NULL,
    mode TEXT NOT NULL,
    lifecycle_state attempt_lifecycle_status NOT NULL DEFAULT 'CREATED',
    reason_code TEXT REFERENCES reject_reason_codes(code),
    tx_hash TEXT,
    meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_opportunity_attempts_created_at ON opportunity_attempts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunity_attempts_state ON opportunity_attempts (lifecycle_state, updated_at DESC);

CREATE TABLE IF NOT EXISTS opportunity_attempt_events (
    event_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES opportunity_attempts(attempt_id) ON DELETE CASCADE,
    ts_ms BIGINT NOT NULL,
    from_state attempt_lifecycle_status,
    to_state attempt_lifecycle_status NOT NULL,
    reason_code TEXT REFERENCES reject_reason_codes(code),
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_opportunity_attempt_events_attempt ON opportunity_attempt_events (attempt_id, created_at DESC);

INSERT INTO reject_reason_codes(code, category, description)
VALUES
    ('none', 'system', 'No reject reason'),
    ('detector_miss', 'detector', 'Detector did not match candidate rules'),
    ('low_edge_bps', 'risk', 'Expected edge below threshold'),
    ('high_gas_gwei', 'risk', 'Gas price above configured threshold'),
    ('max_position_size', 'risk', 'Position size above configured threshold'),
    ('max_daily_loss', 'risk', 'Daily loss threshold exceeded'),
    ('sim_negative', 'simulation', 'Simulation expected pnl is negative'),
    ('sim_failed', 'simulation', 'Simulation failed'),
    ('operator_not_trading', 'operator', 'Operator state is not TRADING'),
    ('operator_kill_switch', 'operator', 'Operator kill switch is enabled'),
    ('runtime_state_not_ready', 'runtime', 'Runtime state does not allow broadcast'),
    ('paper_mode_no_send', 'mode', 'Paper mode does not broadcast transactions'),
    ('invalid_mode', 'mode', 'Execution mode is invalid'),
    ('sim_missing', 'simulation', 'Simulation result missing while required'),
    ('decision_reject', 'decision', 'Candidate rejected during evaluation'),
    ('forwarded_to_orchestrator', 'execution', 'Candidate accepted and forwarded to orchestrator'),
    ('execute_cb_false', 'execution', 'Execution callback returned false'),
    ('send_error', 'execution', 'Execution callback raised an exception'),
    ('confirm_failed', 'execution', 'Transaction did not confirm successfully'),
    ('reverted', 'execution', 'Transaction reverted'),
    ('dropped', 'execution', 'Transaction dropped from mempool'),
    ('fork_sim_failed', 'simulation', 'Fork simulator validation failed'),
    ('risk_max_fee_gwei', 'risk', 'Fee above configured maximum'),
    ('risk_slippage_bps', 'risk', 'Slippage above configured maximum'),
    ('risk_min_edge_bps', 'risk', 'Edge below configured minimum'),
    ('risk_max_daily_loss', 'risk', 'Daily loss above configured maximum'),
    ('risk_gate_failed', 'risk', 'Risk gate returned failure')
ON CONFLICT (code) DO NOTHING;
