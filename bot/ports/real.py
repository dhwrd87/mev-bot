from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from bot.net.rpc_client import RpcClient as _RpcClientImpl
from bot.exec.orderflow import PrivateOrderflowRouter, TxTraits
from bot.ports.interfaces import (
    RpcClient,
    PrivateOrderflowClient,
    ReceiptProvider,
    OpportunityRepo,
    TradeRepo,
    RiskRepo,
    AlertRepo,
    SubmitResult,
)


class RealRpcClient(RpcClient):
    def __init__(self, http_url: Optional[str] = None):
        self._client = _RpcClientImpl(http_url=http_url)

    async def get_tx(self, tx_hash: str) -> Any | None:
        return await self._client.get_tx(tx_hash)

    async def gas_price(self) -> int:
        return await self._client.gas_price()

    async def latest_block(self) -> Any:
        return await self._client.latest_block()

    async def nonce(self, addr: str) -> int:
        return await self._client.nonce(addr)


class RealPrivateOrderflowClient(PrivateOrderflowClient):
    def __init__(self, chain: str):
        self._router = PrivateOrderflowRouter.from_env() if hasattr(PrivateOrderflowRouter, "from_env") else PrivateOrderflowRouter(chain)
        self._chain = chain

    async def submit_tx(
        self,
        tx_hex: str,
        *,
        chain: str,
        traits: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SubmitResult:
        t = TxTraits(
            chain=chain,
            value_wei=int((traits or {}).get("value_wei", 0)),
            size_usd=float((traits or {}).get("size_usd", 0.0)),
            token_is_new=bool((traits or {}).get("token_is_new", False)),
            uses_permit2=bool((traits or {}).get("uses_permit2", False)),
            exact_output=bool((traits or {}).get("exact_output", False)),
            desired_privacy=str((traits or {}).get("desired_privacy", "private")),
            detected_snipers=int((traits or {}).get("detected_snipers", 0)),
        )
        res = await self._router.route_and_submit(tx_hex, t, metadata or {})
        return SubmitResult(ok=res.ok, tx_hash=res.tx_hash, relay=res.relay, error=res.error)


class RealReceiptProvider(ReceiptProvider):
    def __init__(self, http_url: Optional[str] = None):
        from web3 import Web3, HTTPProvider
        self._w3 = Web3(HTTPProvider(http_url))

    async def wait_for_receipt(self, tx_hash: str, *, timeout_s: int = 60) -> Optional[Dict[str, Any]]:
        try:
            r = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_s)
            return dict(r)
        except Exception:
            return None


class RealTradeRepo(TradeRepo):
    def __init__(self):
        from bot.storage import pg as repo
        self._repo = repo
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            self._pool = await self._repo.get_pool()
        return self._pool

    async def insert_trade(self, row: Dict[str, Any]) -> int:
        pool = await self._get_pool()
        return await self._repo.insert_trade(pool, row)

    async def update_trade_outcome(self, **kwargs: Any) -> None:
        pool = await self._get_pool()
        await self._repo.update_trade_outcome(pool, **kwargs)


class RealOpportunityRepo(OpportunityRepo):
    def __init__(self):
        from bot.storage import pg as repo
        self._repo = repo
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            self._pool = await self._repo.get_pool()
        return self._pool

    async def insert_opportunity(self, row: Dict[str, Any]) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO opportunities (chain, pool, type, score, expected_profit, context)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                RETURNING id
                """,
                row.get("chain"),
                row.get("pool"),
                row.get("type"),
                row.get("score"),
                row.get("expected_profit"),
                row.get("context", {}),
            )


class RealRiskRepo(RiskRepo):
    def __init__(self):
        from bot.storage import pg as repo
        self._repo = repo
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            self._pool = await self._repo.get_pool()
        return self._pool

    async def record_state(self, row: Dict[str, Any]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO risk_state (ts, exposure, daily_pnl, consec_losses, settings)
                VALUES (NOW(), $1, $2, $3, $4::jsonb)
                """,
                row.get("exposure"),
                row.get("daily_pnl"),
                row.get("consec_losses"),
                row.get("settings", {}),
            )


class RealAlertRepo(AlertRepo):
    def __init__(self):
        from bot.telemetry.alerts import AlertManager, AlertCfg
        import os
        self._alerts = AlertManager(
            AlertCfg(
                webhook=os.getenv("DISCORD_WEBHOOK", ""),
                service=os.getenv("SERVICE_NAME", "mev-bot"),
                enabled=os.getenv("ALERTS_ENABLED", "true").lower() == "true",
                default_cooldown_s=int(os.getenv("ALERTS_DEFAULT_COOLDOWN", "60")),
            )
        )

    async def send_alert(self, level: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        await self._alerts.send(level, message, payload or {})
