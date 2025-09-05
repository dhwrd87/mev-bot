-- sql/permit2_nonces.sql
CREATE TABLE IF NOT EXISTS permit2_nonces (
  owner   TEXT NOT NULL,
  token   TEXT NOT NULL,
  spender TEXT NOT NULL,
  nonce   BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner, token, spender)
);
