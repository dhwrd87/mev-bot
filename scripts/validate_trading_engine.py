#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import redis
from psycopg.rows import dict_row


try:
    from rich.console import Console

    _console = Console()

    def _ok(msg: str) -> None:
        _console.print(f"[green]✓ {msg}[/green]")

    def _fail(msg: str) -> None:
        _console.print(f"[red]✗ {msg}[/red]")

    def _info(msg: str) -> None:
        _console.print(f"[cyan]{msg}[/cyan]")

except Exception:

    def _ok(msg: str) -> None:
        print(f"✓ {msg}")

    def _fail(msg: str) -> None:
        print(f"✗ {msg}")

    def _info(msg: str) -> None:
        print(msg)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _dsn() -> str:
    explicit = str(os.getenv("DATABASE_URL", "")).strip()
    if explicit:
        return explicit
    return (
        f"postgresql://{os.getenv('POSTGRES_USER', 'mev_user')}:{os.getenv('POSTGRES_PASSWORD', 'change_me')}"
        f"@{os.getenv('POSTGRES_HOST', 'postgres')}:{os.getenv('POSTGRES_PORT', '5432')}"
        f"/{os.getenv('POSTGRES_DB', 'mev_bot')}"
    )


def _prom_url() -> str:
    return str(os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")).strip()


def _compose_file() -> str:
    return str(Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml")


def _http_get_text(url: str, timeout_s: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"Accept": "text/plain,application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _prom_query(expr: str, timeout_s: float = 10.0) -> float:
    q = urllib.parse.urlencode({"query": expr})
    url = f"{_prom_url().rstrip('/')}/api/v1/query?{q}"
    payload = json.loads(_http_get_text(url, timeout_s=timeout_s))
    if str(payload.get("status")) != "success":
        raise RuntimeError(f"prometheus query failed: {expr}")
    results = payload.get("data", {}).get("result", [])
    if not isinstance(results, list) or not results:
        return 0.0
    total = 0.0
    for item in results:
        try:
            total += float(item["value"][1])
        except Exception:
            continue
    return total


def _with_backoff(check_fn, timeout_s: float, start_sleep_s: float = 1.0, max_sleep_s: float = 8.0):
    deadline = time.time() + timeout_s
    sleep_s = start_sleep_s
    last_err = ""
    while time.time() < deadline:
        try:
            ok, detail = check_fn()
            if ok:
                return True, detail
            last_err = detail
        except Exception as e:
            last_err = str(e)
        time.sleep(sleep_s)
        sleep_s = min(max_sleep_s, sleep_s * 2.0)
    return False, last_err or "timeout"


def check_database_tables_exist() -> CheckResult:
    with psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        tables = {str(r["tablename"]) for r in (cur.fetchall() or [])}
    missing = [t for t in ("trades", "strategy_performance") if t not in tables]
    if missing:
        return CheckResult("db_tables", False, f"missing tables: {','.join(missing)}")
    return CheckResult("db_tables", True, "Database tables exist")


def check_opportunity_processor_running() -> CheckResult:
    cmd = ["docker", "compose", "-f", _compose_file(), "ps", "opportunity-processor"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return CheckResult("processor_running", False, proc.stderr.strip() or proc.stdout.strip())
    out = (proc.stdout or "").strip()
    if "Up" not in out:
        return CheckResult("processor_running", False, f"service not up: {out}")
    return CheckResult("processor_running", True, "Opportunity processor is running")


def check_redis_stream_activity() -> CheckResult:
    redis_url = str(os.getenv("REDIS_URL", "redis://redis:6379/0")).strip()
    stream = str(os.getenv("REDIS_STREAM", "mempool:pending:txs")).strip()
    r = redis.from_url(redis_url)

    def _probe():
        xlen = int(r.xlen(stream))
        if xlen > 0:
            return True, f"stream xlen={xlen}"
        return False, "stream empty"

    ok, detail = _with_backoff(_probe, timeout_s=60.0, start_sleep_s=1.0)
    try:
        r.close()
    except Exception:
        pass
    if not ok:
        return CheckResult("redis_stream_activity", False, detail)
    return CheckResult("redis_stream_activity", True, "Mempool stream has activity")


def check_opportunities_detected() -> CheckResult:
    def _probe():
        v = _prom_query("sum(rate(mevbot_opportunities_detected_total[1m]))")
        if v > 0:
            return True, f"rate={v:.6f}"
        return False, f"rate={v:.6f}"

    ok, detail = _with_backoff(_probe, timeout_s=120.0, start_sleep_s=2.0)
    if not ok:
        return CheckResult("opportunities_detected", False, detail)
    return CheckResult("opportunities_detected", True, "Opportunities being detected")


def check_strategy_decisions() -> CheckResult:
    v = _prom_query("sum(mevbot_strategy_decisions_total)")
    if v <= 0:
        return CheckResult("strategy_decisions", False, f"counter={v}")
    return CheckResult("strategy_decisions", True, "Strategy decisions being made")


def check_metrics_exposed() -> CheckResult:
    metrics = _http_get_text("http://127.0.0.1:9100/metrics", timeout_s=10.0)
    required = [
        "mevbot_opportunities_detected_total",
        "mevbot_strategy_decisions_total",
        "mevbot_executions_attempted_total",
    ]
    missing = [m for m in required if m not in metrics]
    if missing:
        return CheckResult("metrics_exposed", False, f"missing metrics: {','.join(missing)}")
    return CheckResult("metrics_exposed", True, "All trading metrics exposed")


def check_discord_commands_registered() -> CheckResult:
    try:
        import discord
        from discord.ext import commands
        from ops.discord_commands_trading import setup as setup_trading

        async def _run() -> set[str]:
            intents = discord.Intents.none()
            bot = commands.Bot(command_prefix="!", intents=intents)
            try:
                await setup_trading(bot, _dsn())
                return {c.name for c in bot.tree.get_commands()}
            finally:
                await bot.close()

        names = asyncio.run(_run())
        required = {"trades", "strategy", "pnl", "decisions"}
        missing = sorted(required - names)
        if missing:
            return CheckResult("discord_commands", False, f"missing commands: {','.join(missing)}")
        return CheckResult("discord_commands", True, "Discord trading commands registered")
    except Exception as e:
        return CheckResult("discord_commands", False, str(e))


def check_grafana_dashboard_exists() -> CheckResult:
    p = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "trading_overview.json"
    if not p.exists():
        return CheckResult("grafana_dashboard", False, f"missing file: {p}")
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return CheckResult("grafana_dashboard", False, f"invalid JSON: {e}")
    return CheckResult("grafana_dashboard", True, "Trading dashboard available")


def check_trade_recording_works() -> CheckResult:
    marker = f"validate_trade_{uuid4_hex()}"
    with psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades (
                opportunity_id, opportunity_type, detector, family, chain, network,
                mode, strategy, decision_reason, decision_latency_ms,
                executed, execution_reason, execution_latency_ms, tx_hash,
                token_in, token_out, pair, dex,
                requested_size_usd, approved_size_usd, actual_size_usd,
                expected_profit_usd, realized_profit_usd, gas_cost_usd, net_profit_usd,
                slippage_bps, gas_used, gas_price_gwei
            )
            VALUES (
                %s, 'xarb', 'validator', 'evm', 'sepolia', 'testnet',
                'stealth', 'stealth_default', 'validate', 1.0,
                true, 'validate', 1.0, %s,
                '0xWETH', '0xUSDC', '0xWETH-0xUSDC', 'uniswap_v3',
                100, 100, 100, 5, 4, 1, 3,
                10, 21000, 1.0
            )
            RETURNING id
            """,
            (marker, f"0x{marker[:24]}"),
        )
        row = cur.fetchone() or {}
        trade_id = int(row.get("id", -1))
        cur.execute("SELECT opportunity_id, net_profit_usd FROM trades WHERE id = %s", (trade_id,))
        check_row = cur.fetchone() or {}
        cur.execute("DELETE FROM trades WHERE id = %s", (trade_id,))
    if trade_id <= 0 or check_row.get("opportunity_id") != marker:
        return CheckResult("trade_recording", False, "insert/query/delete validation failed")
    return CheckResult("trade_recording", True, "Trade recording functional")


def check_strategy_performance_aggregation() -> CheckResult:
    try:
        from bot.orchestration.trading_orchestrator import ExecutionResult
        from bot.storage.trade_recorder import TradeRecorder

        recorder = TradeRecorder(_dsn())
        strategy = f"validate_strategy_{uuid4_hex()[:8]}"
        base_opp = {
            "id": f"validate_opp_{uuid4_hex()[:8]}",
            "type": "xarb",
            "detector": "validator",
            "family": "evm",
            "chain": "sepolia",
            "network": "testnet",
            "token_in": "0xWETH",
            "token_out": "0xUSDC",
            "dex": "uniswap_v3",
            "size_usd": 100.0,
            "approved_size_usd": 100.0,
        }

        async def _record() -> None:
            for i in range(3):
                opp = dict(base_opp)
                opp["id"] = f"{base_opp['id']}_{i}"
                execution = ExecutionResult(
                    executed=True,
                    mode="stealth",
                    strategy=strategy,
                    reason="ok",
                    trade_id=None,
                    tx_hash=f"0x{uuid4_hex()}",
                    bundle_tag=None,
                    expected_profit_usd=5.0,
                    realized_profit_usd=4.0,
                    gas_cost_usd=1.0,
                    slippage_bps=10.0,
                    latency_ms=5.0,
                    error=None,
                    metadata={},
                )
                await recorder.record_trade(opp, {"reason": "ok", "latency_ms": 1.0}, execution, {})

        asyncio.run(_record())
        with psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT trades_executed, trades_succeeded, trades_failed, gross_profit_usd, gas_cost_usd, net_profit_usd
                FROM strategy_performance
                WHERE date = %s AND family='evm' AND chain='sepolia' AND mode='stealth' AND strategy=%s
                """,
                (date.today(), strategy),
            )
            row = cur.fetchone() or {}
            cur.execute("DELETE FROM trades WHERE strategy = %s", (strategy,))
            cur.execute("DELETE FROM strategy_performance WHERE strategy = %s", (strategy,))
        if int(row.get("trades_executed", 0)) < 3:
            return CheckResult("strategy_aggregation", False, f"unexpected aggregates: {row}")
        return CheckResult("strategy_aggregation", True, "Performance aggregation functional")
    except Exception as e:
        return CheckResult("strategy_aggregation", False, str(e))


def uuid4_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def main() -> int:
    _info("Validating trading engine deployment...")
    checks = [
        check_database_tables_exist,
        check_opportunity_processor_running,
        check_redis_stream_activity,
        check_opportunities_detected,
        check_strategy_decisions,
        check_metrics_exposed,
        check_discord_commands_registered,
        check_grafana_dashboard_exists,
        check_trade_recording_works,
        check_strategy_performance_aggregation,
    ]
    results: list[CheckResult] = []
    for fn in checks:
        try:
            result = fn()
        except Exception as e:
            result = CheckResult(fn.__name__, False, str(e))
        results.append(result)
        if result.ok:
            _ok(result.detail or result.name)
        else:
            _fail(f"{result.name}: {result.detail}")

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    _info(f"{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
