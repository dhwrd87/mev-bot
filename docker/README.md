# Docker Runtime

## How to Switch Chains
Edit `../.env.runtime` and set `CHAIN=<sepolia|amoy|mainnet|polygon>`. That’s the only required change.

## Canonical Env Keys (Chain)
These are the chain-related keys expected by the code:
- CHAIN
- CHAIN_ID (optional)
- INFURA_KEY (optional)
- ALCHEMY_KEY (optional)
- USE_INFURA (optional)
- USE_ALCHEMY (optional)
- WS_ENDPOINTS_EXTRA (optional comma list)
- RPC_HTTP_EXTRA (optional comma list)

`docker-compose.yml` always loads `../.env.runtime`.
