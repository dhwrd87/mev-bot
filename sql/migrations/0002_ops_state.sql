CREATE TABLE IF NOT EXISTS ops_state(
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);
INSERT INTO ops_state(k,v) VALUES ('paused','false')
ON CONFLICT (k) DO NOTHING;
