from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import requests
from eth_account import Account
from web3 import HTTPProvider, Web3


@dataclass(frozen=True)
class ChainSelection:
    family: str
    chain: str


def parse_chain_selection(name: str) -> ChainSelection:
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("chain selection is required")
    if ":" in raw:
        fam, ch = raw.split(":", 1)
        fam = fam.strip().upper()
        ch = ch.strip().lower()
        if fam not in {"EVM", "SOL"}:
            raise ValueError("family must be EVM or SOL")
        if not ch:
            raise ValueError("chain is required")
        return ChainSelection(family=fam, chain=ch)
    ch = raw.lower()
    if ch in {"sol", "solana"}:
        return ChainSelection(family="SOL", chain="solana")
    return ChainSelection(family="EVM", chain=ch)


def _chains_file() -> Path:
    env_path = os.getenv("CHAINS_CONFIG_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parents[2] / "config" / "chains.yaml"


def _load_registry() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    p = _chains_file()
    if p.exists():
        raw = json.loads(p.read_text())
        chains = raw.get("chains", {})
        if isinstance(chains, dict):
            for k, v in chains.items():
                if isinstance(v, dict):
                    out[str(k).lower()] = dict(v)
    profiles = Path(os.getenv("CHAIN_PROFILES_DIR", str(p.parent / "chains")))
    if profiles.exists():
        for fp in sorted(profiles.rglob("*.yaml")):
            try:
                prof = json.loads(fp.read_text())
            except Exception:
                continue
            if not isinstance(prof, dict):
                continue
            chain = str(prof.get("chain", "")).strip().lower()
            if not chain:
                continue
            base = out.get(chain, {})
            if not isinstance(base, dict):
                base = {}
            for key in (
                "chain_id",
                "default_rpc_http",
                "default_rpc_http_backups",
                "default_ws_endpoints",
                "explorer_base_url",
                "native_symbol",
            ):
                val = prof.get(key)
                if val not in (None, "", []):
                    base[key] = val
            out[chain] = base
    if not out:
        raise ValueError("invalid chains registry")
    return out


def _rpc_candidates_evm(chain: str) -> List[str]:
    reg = _load_registry()
    c = reg.get(chain)
    if not c:
        raise ValueError(f"unsupported EVM chain: {chain}")
    vals = [str(c.get("default_rpc_http", "")).strip()]
    vals.extend([str(x).strip() for x in c.get("default_rpc_http_backups", []) if str(x).strip()])
    vals.extend([x.strip() for x in str(os.getenv("RPC_HTTP_EXTRA", "")).split(",") if x.strip()])
    out: List[str] = []
    for v in vals:
        if not v or v in out:
            continue
        out.append(v)
    if not out:
        raise ValueError(f"no EVM RPC endpoints configured for {chain}")
    return out


def _rpc_candidate_sol(chain: str) -> str:
    explicit = str(os.getenv("SOL_RPC_HTTP", "")).strip()
    if explicit:
        return explicit
    reg = _load_registry()
    c = reg.get(chain)
    if c:
        v = str(c.get("default_rpc_http", "")).strip()
        if v:
            return v
    return "https://api.mainnet-beta.solana.com"


def _derive_evm_wallet() -> str:
    pk = str(os.getenv("TRADER_PK", "")).strip()
    if pk:
        try:
            return Account.from_key(pk).address
        except Exception as e:
            raise ValueError(f"failed to derive EVM wallet from TRADER_PK: {e}") from e
    addr = str(os.getenv("WALLET_ADDRESS", "")).strip()
    if addr:
        return Web3.to_checksum_address(addr)
    raise ValueError("TRADER_PK or WALLET_ADDRESS required for EVM validation")


def _derive_sol_wallet() -> str:
    addr = str(os.getenv("SOL_WALLET_ADDRESS", "")).strip()
    if addr:
        return addr
    raise ValueError("SOL_WALLET_ADDRESS required for SOL validation")


def _wait_for_evm_block_advance(w3: Web3, start_h: int, timeout_s: int = 30) -> int:
    deadline = time.time() + max(1, timeout_s)
    last = start_h
    while time.time() < deadline:
        h = int(w3.eth.block_number)
        if h > start_h:
            return h
        last = h
        time.sleep(1.5)
    raise RuntimeError(f"EVM block height did not advance (start={start_h}, last={last})")


def _wait_for_sol_slot_advance(endpoint: str, start_slot: int, timeout_s: int = 15) -> int:
    deadline = time.time() + max(1, timeout_s)
    last = start_slot
    while time.time() < deadline:
        slot = int(_sol_rpc(endpoint, "getSlot"))
        if slot > start_slot:
            return slot
        last = slot
        time.sleep(0.8)
    raise RuntimeError(f"SOL slot did not advance (start={start_slot}, last={last})")


def _sol_rpc(endpoint: str, method: str, params: list[Any] | None = None) -> Any:
    resp = requests.post(
        endpoint,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
        timeout=8,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"SOL RPC {method} error: {body['error']}")
    return body.get("result")


def validate_chain_selection(sel: ChainSelection) -> Dict[str, Any]:
    if sel.family == "EVM":
        wallet = _derive_evm_wallet()
        errors: List[str] = []
        for endpoint in _rpc_candidates_evm(sel.chain):
            try:
                w3 = Web3(HTTPProvider(endpoint, request_kwargs={"timeout": 8}))
                if not w3.is_connected():
                    raise RuntimeError("rpc not connected")
                h0 = int(w3.eth.block_number)
                h1 = _wait_for_evm_block_advance(w3, h0)
                bal = int(w3.eth.get_balance(wallet))
                return {
                    "family": sel.family,
                    "chain": sel.chain,
                    "endpoint": endpoint,
                    "height_start": h0,
                    "height_end": h1,
                    "wallet": wallet,
                    "balance": bal,
                    "balance_unit": "wei",
                }
            except Exception as e:
                errors.append(f"{endpoint}: {e}")
        raise RuntimeError(f"EVM validation failed for {sel.chain}: {' | '.join(errors)}")

    if sel.family == "SOL":
        endpoint = _rpc_candidate_sol(sel.chain)
        wallet = _derive_sol_wallet()
        _ = _sol_rpc(endpoint, "getHealth")
        s0 = int(_sol_rpc(endpoint, "getSlot"))
        s1 = _wait_for_sol_slot_advance(endpoint, s0)
        bal_obj = _sol_rpc(endpoint, "getBalance", [wallet])
        bal = int((bal_obj or {}).get("value", 0))
        return {
            "family": sel.family,
            "chain": sel.chain,
            "endpoint": endpoint,
            "slot_start": s0,
            "slot_end": s1,
            "wallet": wallet,
            "balance": bal,
            "balance_unit": "lamports",
        }

    raise ValueError(f"unsupported family: {sel.family}")
