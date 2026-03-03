from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover
    asyncpg = None

from bot.orchestration.orchestrator import Orchestrator, OrchestratorConfig
from bot.risk.adaptive import AdaptiveRiskManager, RiskConfig
from bot.ports.fakes import FakeTradeRepo


@dataclass
class SmokeResult:
    ok: bool
    repo: str
    trade_id: Optional[int]
    records_written: int
    records: List[Dict[str, Any]]


class DryRunStealthExecutor:
    async def execute_like(self, opp: Dict[str, Any]) -> Dict[str, Any]:
        # Simulate a successful private submission without keys
        return {
            "ok": True,
            "pnl_usd": float(opp.get("expected_profit_usd", 8.0)) - 0.1,
            "tx_hash": "0xDRYRUN",
            "relay": "mev_blocker",
        }


class NoopHunterExecutor:
    async def execute_like(self, opp: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "pnl_usd": 0.0}


async def _pg_available(timeout_s: float = 0.5) -> bool:
    if asyncpg is None:
        return False
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        user = os.getenv("POSTGRES_USER", "mev_user")
        password = os.getenv("POSTGRES_PASSWORD", "")
        host = os.getenv("POSTGRES_HOST", "postgres")
        db = os.getenv("POSTGRES_DB", "mev_bot")
        dsn = f"postgresql://{user}:{password}@{host}:5432/{db}"

    try:
        conn = await asyncpg.connect(dsn, timeout=timeout_s)
    except Exception:
        return False

    try:
        exists = await conn.fetchval("SELECT to_regclass('public.trades')")
        return bool(exists)
    finally:
        await conn.close()


def _opportunity_fixture() -> Dict[str, Any]:
    return {
        "exact_output": True,
        "gas_gwei": 40,
        "size_usd": 500,
        "expected_profit_usd": 8.0,
        "chain": "sepolia",
        "token_in": "USDC",
        "token_out": "TOKENX",
        "amount_in": 1_000_000,
        "desired_output": 100_000,
        "max_input": 1_200_000,
        "router": "0xRouterV3",
        "sender": "0xSender",
        "recipient": "0xRecipient",
    }


async def run_golden_path(force_repo: Optional[str] = None) -> SmokeResult:
    """
    force_repo: None | "pg" | "stub"
    """
    use_pg = False
    if force_repo == "pg":
        use_pg = True
    elif force_repo == "stub":
        use_pg = False
    else:
        use_pg = await _pg_available()

    if use_pg:
        from bot.ports.real import RealTradeRepo
        repo = RealTradeRepo()
        repo_name = "postgres"
    else:
        repo = FakeTradeRepo()
        repo_name = "stub"

    risk = AdaptiveRiskManager(RiskConfig(capital_usd=10_000, max_position_size_pct=5.0, max_daily_loss_usd=1_000, max_consecutive_losses=5))
    orch = Orchestrator(OrchestratorConfig(), risk, DryRunStealthExecutor(), NoopHunterExecutor(), trade_repo=repo)

    opp = _opportunity_fixture()
    result = await orch.handle(opp)

    trade_id = result.get("trade_id")
    if trade_id:
        await repo.update_trade_outcome(id=trade_id, status="included", realized_pnl_usd=result.get("pnl_usd", 0.0))

    records = getattr(repo, "records", [])
    records_written = len(records) if records else (1 if trade_id else 0)

    return SmokeResult(
        ok=bool(result.get("ok")),
        repo=repo_name,
        trade_id=trade_id,
        records_written=records_written,
        records=records,
    )


async def _amain() -> int:
    # Default to stub to avoid requiring live DB credentials/secrets in smoke runs.
    force = os.getenv("GOLDEN_PATH_REPO", "stub")  # "pg" | "stub"
    res = await run_golden_path(force_repo=force)
    if res.ok and res.records_written >= 1:
        print(f"✅ golden path ok | repo={res.repo} | trade_id={res.trade_id} | records={res.records_written}")
        return 0
    print(f"❌ golden path failed | repo={res.repo} | trade_id={res.trade_id} | records={res.records_written}")
    return 1


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
