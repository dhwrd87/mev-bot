#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import psycopg
import redis
import requests

from bot.core.chain_config import get_chain_config, _reset_chain_config_cache_for_tests
from bot.core.config_loader import load_chain_profile


@dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool = True
    details: dict[str, Any] | None = None
    error: str | None = None


def _dsn() -> str:
    dsn = str(os.getenv("DATABASE_URL", "")).strip()
    if dsn:
        return dsn
    user = os.getenv("POSTGRES_USER", "mev_user")
    pwd = os.getenv("POSTGRES_PASSWORD", "change_me")
    db = os.getenv("POSTGRES_DB", "mev_bot")
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


def _add(results: list[CheckResult], name: str, ok: bool, *, required: bool = True, details=None, error: str | None = None):
    results.append(CheckResult(name=name, ok=bool(ok), required=required, details=details or {}, error=error))


def _check_env(results: list[CheckResult], root: Path) -> None:
    import subprocess

    cmd = [
        sys.executable,
        str(root / "scripts" / "validate_env.py"),
        "--env-file",
        str(root / ".env.runtime"),
        "--reference",
        str(root / ".env.example"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    ok = proc.returncode == 0
    details = {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip().splitlines()[-8:],
        "stderr": proc.stderr.strip().splitlines()[-8:],
    }
    _add(results, "env_validity", ok, details=details, error=None if ok else "validate_env_failed")


def _check_chain_resolution(results: list[CheckResult]) -> tuple[str, str, str, list[str]] | None:
    try:
        _reset_chain_config_cache_for_tests()
        cfg = get_chain_config()
        chain = str(cfg.chain)
        chain_id = int(cfg.chain_id)
        family = str(os.getenv("CHAIN_FAMILY", "evm")).strip().lower() or "evm"
        network = "devnet" if family == "sol" and "devnet" in chain else ("testnet" if chain in {"sepolia", "amoy"} else "mainnet")

        profile = str(os.getenv("CHAIN_PROFILE", "")).strip()
        profile_ok = True
        profile_err = None
        profile_resolved = None
        if profile:
            try:
                p = load_chain_profile(profile)
                profile_resolved = {
                    "family": p.family,
                    "chain": p.chain,
                    "network": p.network,
                    "dexes_enabled": list(p.dexes_enabled),
                }
            except Exception as e:
                profile_ok = False
                profile_err = str(e)
        ok = bool(chain and chain_id > 0 and profile_ok)
        rpc_candidates = [cfg.rpc_http_selected, *list(cfg.rpc_http_backups)]
        _add(
            results,
            "chain_profile_resolution",
            ok,
            details={
                "chain": chain,
                "chain_id": chain_id,
                "family": family,
                "network": network,
                "rpc_http_selected": cfg.rpc_http_selected,
                "rpc_http_backups": cfg.rpc_http_backups,
                "rpc_http_candidates": rpc_candidates,
                "ws_endpoints_selected": cfg.ws_endpoints_selected,
                "chain_profile": profile or None,
                "chain_profile_resolved": profile_resolved,
            },
            error=profile_err,
        )
        return family, chain, network, rpc_candidates
    except Exception as e:
        _add(results, "chain_profile_resolution", False, error=str(e))
        return None


def _rpc_read_head(endpoint: str, family: str, timeout_s: float) -> int:
    if family == "sol":
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
        resp = requests.post(endpoint, json=payload, timeout=timeout_s)
        resp.raise_for_status()
        body = resp.json() or {}
        return int(body.get("result") or 0)

    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    resp = requests.post(endpoint, json=payload, timeout=timeout_s)
    resp.raise_for_status()
    body = resp.json() or {}
    return int(body.get("result") or "0x0", 16)


def _check_rpc_advancing(results: list[CheckResult], family: str, rpc_endpoints: list[str]) -> None:
    wait_s = max(5, int(os.getenv("READINESS_HEAD_WAIT_S", "25")))
    timeout_s = float(os.getenv("READINESS_RPC_TIMEOUT_S", "8"))
    retries = max(1, int(os.getenv("READINESS_RPC_RETRIES", "3")))
    endpoints = [str(x).strip() for x in rpc_endpoints if str(x).strip()]
    if not endpoints:
        _add(results, "rpc_reachability_advancing_head", False, error="no_rpc_endpoints")
        return
    try:
        head_1 = None
        selected = None
        first_errors: list[str] = []
        for endpoint in endpoints:
            for attempt in range(1, retries + 1):
                try:
                    head_1 = _rpc_read_head(endpoint, family, timeout_s)
                    selected = endpoint
                    break
                except Exception as e:
                    first_errors.append(f"{endpoint} attempt={attempt}: {e}")
                    time.sleep(min(1.0, 0.2 * attempt))
            if selected:
                break

        if selected is None or head_1 is None:
            _add(
                results,
                "rpc_reachability_advancing_head",
                False,
                details={"rpc_endpoints": endpoints, "errors": first_errors[-8:]},
                error="rpc_unreachable",
            )
            return

        time.sleep(wait_s)
        head_2 = None
        second_errors: list[str] = []
        for endpoint in [selected, *[e for e in endpoints if e != selected]]:
            for attempt in range(1, retries + 1):
                try:
                    head_2 = _rpc_read_head(endpoint, family, timeout_s)
                    selected = endpoint
                    break
                except Exception as e:
                    second_errors.append(f"{endpoint} attempt={attempt}: {e}")
                    time.sleep(min(1.0, 0.2 * attempt))
            if head_2 is not None:
                break

        if head_2 is None:
            _add(
                results,
                "rpc_reachability_advancing_head",
                False,
                details={"rpc_endpoints": endpoints, "head_1": head_1, "errors": second_errors[-8:]},
                error="rpc_second_probe_failed",
            )
            return

        ok = head_2 > head_1 > 0
        details_key_1 = "slot_1" if family == "sol" else "head_1"
        details_key_2 = "slot_2" if family == "sol" else "head_2"
        _add(
            results,
            "rpc_reachability_advancing_head",
            ok,
            details={
                details_key_1: head_1,
                details_key_2: head_2,
                "wait_s": wait_s,
                "selected_endpoint": selected,
                "rpc_endpoints": endpoints,
            },
            error=None if ok else "head_not_advancing",
        )
    except Exception as e:
        _add(results, "rpc_reachability_advancing_head", False, error=str(e))


def _check_redis_stream_growth(results: list[CheckResult], probe_tx: str) -> tuple[int, int] | None:
    stream = str(os.getenv("REDIS_STREAM", "mempool:pending:txs")).strip()
    url = str(os.getenv("REDIS_URL", "redis://redis:6379/0")).strip()
    try:
        r = redis.from_url(url)
        xlen_before = int(r.xlen(stream))
        probe_id = r.xadd(stream, {"tx": probe_tx, "selector": "0x", "ts_ms": str(int(time.time() * 1000))})
        xlen_after = int(r.xlen(stream))
        ok = xlen_after > xlen_before
        _add(
            results,
            "redis_stream_growth",
            ok,
            details={
                "stream": stream,
                "xlen_before": xlen_before,
                "xlen_after": xlen_after,
                "probe_id": probe_id.decode() if isinstance(probe_id, bytes) else str(probe_id),
            },
            error=None if ok else "xlen_not_increasing",
        )
        return xlen_before, xlen_after
    except Exception as e:
        _add(results, "redis_stream_growth", False, error=str(e))
        return None


def _check_migrations_up_to_date(results: list[CheckResult], root: Path) -> None:
    try:
        files = sorted([Path(p).name for p in glob.glob(str(root / "migrations" / "*.sql")) if Path(p).is_file()])
        with psycopg.connect(_dsn(), autocommit=True) as conn:
            rows = conn.execute("SELECT filename FROM app_schema_migrations").fetchall()
        applied = {str(r[0]) for r in rows}
        pending = [f for f in files if f not in applied]
        ok = len(pending) == 0
        _add(results, "db_migrations_up_to_date", ok, details={"applied": len(applied), "files": len(files), "pending": pending[:20]}, error=None if ok else "pending_migrations")
    except Exception as e:
        _add(results, "db_migrations_up_to_date", False, error=str(e))


def _check_candidates_produced(results: list[CheckResult], wait_s: int = 20) -> None:
    try:
        with psycopg.connect(_dsn(), autocommit=True) as conn:
            before = int(conn.execute("SELECT count(*) FROM candidates").fetchone()[0] or 0)
        deadline = time.time() + max(5, wait_s)
        after = before
        while time.time() < deadline:
            with psycopg.connect(_dsn(), autocommit=True) as conn:
                after = int(conn.execute("SELECT count(*) FROM candidates").fetchone()[0] or 0)
            if after > before:
                break
            time.sleep(1.0)
        ok = after > before
        _add(results, "candidate_pipeline_producing_candidates", ok, details={"before": before, "after": after, "delta": after - before, "wait_s": wait_s}, error=None if ok else "candidates_not_increasing")
    except Exception as e:
        _add(results, "candidate_pipeline_producing_candidates", False, error=str(e))


def _check_sim_pass_rate(results: list[CheckResult]) -> None:
    window = max(1, int(os.getenv("READINESS_SIM_WINDOW", "100")))
    threshold = float(os.getenv("READINESS_SIM_PASS_THRESHOLD", "0.20"))
    min_count = max(1, int(os.getenv("READINESS_SIM_MIN_COUNT", "5")))
    try:
        with psycopg.connect(_dsn(), autocommit=True) as conn:
            rows = conn.execute(
                "SELECT sim_ok FROM opportunity_simulations ORDER BY created_at DESC LIMIT %s",
                (window,),
            ).fetchall()
        total = len(rows)
        passed = sum(1 for r in rows if bool(r[0]))
        rate = (passed / total) if total else 0.0
        ok = total >= min_count and rate >= threshold
        err = None
        if total < min_count:
            err = "insufficient_sim_samples"
        elif rate < threshold:
            err = "sim_pass_rate_below_threshold"
        _add(
            results,
            "sim_pass_rate_threshold",
            ok,
            details={"window": window, "min_count": min_count, "threshold": threshold, "total": total, "passed": passed, "pass_rate": rate},
            error=err,
        )
    except Exception as e:
        _add(results, "sim_pass_rate_threshold", False, error=str(e))


def _check_discord_optional(results: list[CheckResult]) -> None:
    token = str(os.getenv("DISCORD_OPERATOR_TOKEN", "")).strip()
    if not token:
        _add(results, "discord_operator_connectivity", True, required=False, details={"status": "skipped", "reason": "DISCORD_OPERATOR_TOKEN not set"})
        return
    try:
        resp = requests.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=10,
        )
        ok = resp.status_code == 200
        details = {"status_code": resp.status_code}
        if ok:
            body = resp.json() or {}
            details["bot_user"] = body.get("username")
            details["bot_id"] = body.get("id")
        _add(results, "discord_operator_connectivity", ok, required=False, details=details, error=None if ok else "discord_auth_failed")
    except Exception as e:
        _add(results, "discord_operator_connectivity", False, required=False, error=str(e))


def _print_human(results: list[CheckResult], overall_ok: bool) -> None:
    print("Readiness Summary")
    for r in results:
        icon = "PASS" if r.ok else ("SKIP" if not r.required else "FAIL")
        suffix = ""
        if r.error:
            suffix = f" error={r.error}"
        print(f"- {icon} {r.name}{suffix}")
    print(f"OVERALL={'PASS' if overall_ok else 'FAIL'}")


def _report_json(results: list[CheckResult], overall_ok: bool) -> dict[str, Any]:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall_ok": overall_ok,
        "checks": [asdict(r) for r in results],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Testnet readiness checks for MEV stack")
    ap.add_argument("--json-out", default="", help="Optional file path to write JSON report")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    results: list[CheckResult] = []

    probe_tx = "0x" + (hex(int(time.time() * 1_000_000))[2:]).rjust(64, "0")[:64]

    _check_env(results, root)

    resolved = _check_chain_resolution(results)
    if resolved:
        family, _chain, _network, rpc_endpoints = resolved
        _check_rpc_advancing(results, family, rpc_endpoints)

    _check_redis_stream_growth(results, probe_tx)
    _check_migrations_up_to_date(results, root)
    _check_candidates_produced(results, wait_s=max(5, int(os.getenv("READINESS_CANDIDATE_WAIT_S", "20"))))
    _check_sim_pass_rate(results)
    _check_discord_optional(results)

    overall_ok = all((r.ok or not r.required) for r in results)

    report = _report_json(results, overall_ok)
    _print_human(results, overall_ok)
    print(json.dumps(report, indent=2, sort_keys=True))

    if args.json_out:
        json_out = Path(args.json_out)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
