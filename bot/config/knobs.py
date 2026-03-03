# bot/config/knobs.py
import os
from typing import List
from bot.core.chain_config import get_chain_config

def _get_list(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]

def _get_first(*names: str, default: str = "") -> str:
    for name in names:
        val = os.getenv(name)
        if val:
            return val.strip()
    return default

def _get_rpc_urls() -> List[str]:
    urls = _get_list("RPC_URLS")
    if urls:
        return urls
    primary = _get_first("RPC_HTTP", "RPC_ENDPOINT_PRIMARY")
    backup = _get_first("RPC_HTTP_BACKUP", "RPC_ENDPOINT_BACKUP")
    return [u for u in (primary, backup) if u]

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return v.lower() in ("1","true","yes","y","on")

class Knobs:
    _chain_cfg = get_chain_config()
    # WS endpoints (single source of truth)
    WS_ENDPOINTS = _chain_cfg.ws_endpoints

    # Sampling / filtering
    PENDING_SAMPLE_RATE = float(os.getenv("PENDING_SAMPLE_RATE", "0.25"))  # 25% by default
    ALLOW_METHOD_IDS = set(x.lower() for x in _get_list("ALLOW_METHOD_IDS",
        # common DEX: swapExactTokensForTokens, exactInput, exactOutput, etc.
        "0x38ed1739,0x18cbafe5,0x5c11d795,0x414bf389,0x472b43f3,0xb858183f,0xf28c0498,0x3593564c"
    ))
    MIN_VALUE_WEI = int(os.getenv("MIN_VALUE_WEI", "0"))  # drop trivial ETH value if desired

    # HTTP rate limit + caching
    HTTP_MAX_QPS = float(os.getenv("HTTP_MAX_QPS", "5"))   # avg QPS cap
    HTTP_BURST   = int(os.getenv("HTTP_BURST", "10"))      # burst tokens
    GASPRICE_TTL_MS = int(os.getenv("GASPRICE_TTL_MS", "3000"))
    LATEST_BLOCK_TTL_MS = int(os.getenv("LATEST_BLOCK_TTL_MS", "2000"))
    NONCE_TTL_MS = int(os.getenv("NONCE_TTL_MS", "3000"))

    # Receipt polling
    RECEIPT_POLL_MIN_MS = int(os.getenv("RECEIPT_POLL_MIN_MS", "2000"))
    RECEIPT_POLL_MAX_MS = int(os.getenv("RECEIPT_POLL_MAX_MS", "8000"))

    # Misc
    CHAIN_ID = int(os.getenv("CHAIN_ID", "1"))  # 11155111 = sepolia
    RPC_URLS = [_chain_cfg.rpc_http] + _chain_cfg.rpc_http_backups
    RPC_HTTP = _chain_cfg.rpc_http
    RPC_HTTP_BACKUP = _chain_cfg.rpc_http_backups[0] if _chain_cfg.rpc_http_backups else ""
