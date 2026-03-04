#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple


BOOL_TRUE = {"1", "true", "yes", "on"}
MODE_ALIASES = {
    "production": "live",
    "staging": "paper",
}
ALLOWED_MODES = {"development", "dryrun", "paper", "live"}
BASE_REQUIRED = {
    "CHAIN",
    "CHAIN_ID",
    "CHAIN_FAMILY",
    "MODE",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "REDIS_URL",
}
LIVE_SECRET_KEYS = (
    "TRADER_PRIVATE_KEY",
    "TRADER_PRIVATE_KEY_FILE",
    "PRIVATE_KEY",
    "PRIVATE_KEY_ENCRYPTED",
    "PRIVATE_KEY_ENCRYPTED_FILE",
)
KNOWN_EVM_CHAINS = {
    "ethereum",
    "mainnet",
    "sepolia",
    "base",
    "amoy",
    "polygon",
    "arbitrum",
    "optimism",
    "bnb",
    "avalanche",
}


def _parse_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        out[key] = val
    return out


def _reference_keys(reference_path: Path) -> set[str]:
    keys = set()
    pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    if not reference_path.exists():
        return keys
    for raw in reference_path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(raw)
        if m:
            keys.add(m.group(1))
    return keys


def _is_true(v: str | None) -> bool:
    return str(v or "").strip().lower() in BOOL_TRUE


def _infer_chain_family(chain: str) -> str:
    c = str(chain or "").strip().lower()
    if not c:
        return "unknown"
    if c.startswith("sol"):
        return "sol"
    if c in KNOWN_EVM_CHAINS:
        return "evm"
    return "unknown"


def _require_non_empty(env: Dict[str, str], keys: Iterable[str], errors: list[str]) -> None:
    for k in keys:
        if not str(env.get(k, "")).strip():
            errors.append(f"missing required variable: {k}")


def validate_env(env: Dict[str, str], reference_keys: set[str]) -> Tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    _require_non_empty(env, BASE_REQUIRED, errors)

    mode_raw = str(env.get("MODE", "")).strip().lower()
    mode = MODE_ALIASES.get(mode_raw, mode_raw)
    if mode_raw in MODE_ALIASES:
        warnings.append(f"MODE={mode_raw} is treated as MODE={mode}")
    if mode not in ALLOWED_MODES:
        errors.append(
            "MODE must be one of development|dryrun|paper|live "
            "(aliases accepted: production->live, staging->paper)"
        )

    family = str(env.get("CHAIN_FAMILY", "")).strip().lower()
    if family and family not in {"evm", "sol"}:
        errors.append("CHAIN_FAMILY must be evm or sol")

    chain = str(env.get("CHAIN", "")).strip().lower()
    inferred = _infer_chain_family(chain)
    if inferred in {"evm", "sol"} and family and family != inferred:
        errors.append(f"CHAIN_FAMILY mismatch: CHAIN={chain} implies {inferred}, got {family}")

    chain_id_raw = str(env.get("CHAIN_ID", "")).strip()
    if chain_id_raw:
        try:
            chain_id = int(chain_id_raw)
            if chain_id <= 0:
                errors.append("CHAIN_ID must be a positive integer")
        except ValueError:
            errors.append("CHAIN_ID must be an integer")

    if _is_true(env.get("USE_ALCHEMY")) and not str(env.get("ALCHEMY_KEY", "")).strip():
        errors.append("USE_ALCHEMY=true requires ALCHEMY_KEY")
    if _is_true(env.get("USE_INFURA")) and not str(env.get("INFURA_KEY", "")).strip():
        errors.append("USE_INFURA=true requires INFURA_KEY")
    if _is_true(env.get("USE_PRIVATE_RPC")):
        rpc_extra = str(env.get("RPC_HTTP_EXTRA", "")).strip()
        ws_extra = str(env.get("WS_ENDPOINTS_EXTRA", "")).strip()
        if not rpc_extra and not ws_extra:
            warnings.append(
                "USE_PRIVATE_RPC=true but RPC_HTTP_EXTRA/WS_ENDPOINTS_EXTRA are empty; relying on chain defaults"
            )

    if mode == "live":
        if not any(str(env.get(k, "")).strip() for k in LIVE_SECRET_KEYS):
            errors.append(
                "MODE=live requires a signing secret: one of "
                + ",".join(LIVE_SECRET_KEYS)
            )

    if str(env.get("PRIVATE_KEY_ENCRYPTED", "")).strip() and not (
        str(env.get("KEY_PASSWORD", "")).strip() or str(env.get("KEY_PASSWORD_FILE", "")).strip()
    ):
        errors.append("PRIVATE_KEY_ENCRYPTED requires KEY_PASSWORD or KEY_PASSWORD_FILE")

    unknown = sorted(k for k in env.keys() if reference_keys and k not in reference_keys)
    if unknown:
        warnings.append(
            "variables not in .env.example (kept as custom overrides): " + ", ".join(unknown[:20])
        )

    return errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate .env.runtime consistency and required variables.")
    ap.add_argument("--env-file", default=".env.runtime", help="Path to runtime env file")
    ap.add_argument("--reference", default=".env.example", help="Path to reference env surface")
    args = ap.parse_args()

    env_path = Path(args.env_file)
    ref_path = Path(args.reference)

    if not env_path.exists():
        print(f"ERROR: env file not found: {env_path}", file=sys.stderr)
        return 2
    if not ref_path.exists():
        print(f"ERROR: reference file not found: {ref_path}", file=sys.stderr)
        return 2

    env = _parse_env_file(env_path)
    ref_keys = _reference_keys(ref_path)
    errors, warnings = validate_env(env, ref_keys)

    print(f"Validated: env={env_path} reference={ref_path} mode={env.get('MODE', '')}")
    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("ENV_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
