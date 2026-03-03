from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from web3 import HTTPProvider, Web3
from web3.exceptions import ContractLogicError

from adapters.dex_packs.base import DEXPack
from bot.core.types_dex import Quote, SimResult, TradeIntent, TxPlan
from ops import metrics as ops_metrics

_ZERO = "0x0000000000000000000000000000000000000000"

_FACTORY_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getPair",
        "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "address"}],
    }
]
_PAIR_ABI = [
    {
        "type": "function",
        "stateMutability": "view",
        "name": "getReserves",
        "inputs": [],
        "outputs": [
            {"name": "reserve0", "type": "uint112"},
            {"name": "reserve1", "type": "uint112"},
            {"name": "blockTimestampLast", "type": "uint32"},
        ],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "token0",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "stateMutability": "view",
        "name": "token1",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]
_ROUTER_ABI = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "swapExactTokensForTokens",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    }
]


def calc_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    fee_multiplier = max(0, 10_000 - int(fee_bps))
    amount_in_with_fee = int(amount_in) * fee_multiplier
    num = amount_in_with_fee * int(reserve_out)
    den = int(reserve_in) * 10_000 + amount_in_with_fee
    if den <= 0:
        return 0
    return int(num // den)


def min_out_from_slippage(expected_out: int, slippage_bps: int) -> int:
    if expected_out <= 0:
        return 0
    slip = max(0, int(slippage_bps))
    return max(1, int(expected_out * (10_000 - slip) // 10_000))


def parse_revert_reason(exc: Exception) -> tuple[str, str]:
    msg = str(exc or "").strip()
    lower = msg.lower()
    normalized = lower.replace("_", " ")
    if "insufficient output amount" in normalized:
        return "insufficient_output", "INSUFFICIENT_OUTPUT_AMOUNT"
    if "insufficient liquidity" in normalized:
        return "insufficient_liquidity", "INSUFFICIENT_LIQUIDITY"
    if "execution reverted:" in lower:
        reason = msg.split("execution reverted:", 1)[1].strip()
        return "revert", reason or "execution reverted"
    return "rpc_error", msg or "eth_call_failed"


class EVMUniV2Pack(DEXPack):
    def __init__(self, *, config: Optional[Dict[str, Any]] = None, instance_name: Optional[str] = None) -> None:
        super().__init__(config=config, instance_name=instance_name)
        self._factory = str(self.config.get("factory", "")).strip()
        self._router = str(self.config.get("router", "")).strip()
        self._fee_bps = int(self.config.get("fee_bps", 30))
        self._rpc_http = str(self.config.get("rpc_http") or "").strip() or str(os.getenv("RPC_HTTP_PRIMARY", "")).strip()

        self._timeout_s = float(self.config.get("rpc_timeout_s", 5.0))
        provider = HTTPProvider(self._rpc_http, request_kwargs={"timeout": self._timeout_s}) if self._rpc_http else None
        self.w3 = Web3(provider) if provider is not None else Web3()

    def name(self) -> str:
        return self._instance_name or "evm_univ2"

    def family_supported(self) -> str:
        return "evm"

    def _checksum(self, addr: str) -> str:
        return self.w3.to_checksum_address(addr)

    def _factory_contract(self):
        return self.w3.eth.contract(address=self._checksum(self._factory), abi=_FACTORY_ABI)

    def _pair_contract(self, pair_addr: str):
        return self.w3.eth.contract(address=self._checksum(pair_addr), abi=_PAIR_ABI)

    def _router_contract(self):
        return self.w3.eth.contract(address=self._checksum(self._router), abi=_ROUTER_ABI)

    def _resolve_path(self, intent: TradeIntent) -> List[str]:
        direct = [intent.token_in, intent.token_out]
        paths = self.config.get("paths") or {}
        if not isinstance(paths, dict):
            return direct
        key = f"{intent.token_in.lower()}->{intent.token_out.lower()}"
        value = paths.get(key) or paths.get(f"{intent.token_in}->{intent.token_out}")
        if isinstance(value, list) and len(value) >= 2:
            return [str(x) for x in value]
        return direct

    def _quote_hop(self, token_in: str, token_out: str, amount_in: int) -> Tuple[int, float]:
        factory = self._factory_contract()
        pair_addr = str(factory.functions.getPair(self._checksum(token_in), self._checksum(token_out)).call())
        if not pair_addr or pair_addr.lower() == _ZERO:
            raise ValueError(f"pair_not_found:{token_in}->{token_out}")
        pair = self._pair_contract(pair_addr)
        reserve0, reserve1, _ = pair.functions.getReserves().call()
        token0 = str(pair.functions.token0().call())
        reserve_in, reserve_out = (
            (int(reserve0), int(reserve1))
            if token0.lower() == token_in.lower()
            else (int(reserve1), int(reserve0))
        )
        out = calc_amount_out(amount_in, reserve_in, reserve_out, self._fee_bps)
        no_impact = float(amount_in) * float(reserve_out) / float(max(1, reserve_in))
        impact_bps = 0.0 if no_impact <= 0 else max(0.0, (1.0 - (float(out) / no_impact)) * 10_000.0)
        return out, impact_bps

    def quote(self, intent: TradeIntent) -> Quote:
        t0 = time.perf_counter()
        hops = 1
        try:
            if not self._factory:
                raise ValueError("missing_factory")
            path = self._resolve_path(intent)
            hops = max(1, len(path) - 1)
            amount = int(intent.amount_in)
            impacts: List[float] = []
            for i in range(len(path) - 1):
                amount, impact = self._quote_hop(path[i], path[i + 1], amount)
                impacts.append(impact)

            q_latency_ms = (time.perf_counter() - t0) * 1000.0
            quote = Quote(
                dex=self.name(),
                expected_out=int(amount),
                min_out=min_out_from_slippage(int(amount), int(intent.slippage_bps)),
                price_impact_bps=float(sum(impacts) / max(1, len(impacts))),
                fee_estimate=float(intent.amount_in) * float(self._fee_bps) / 10_000.0,
                route_summary="->".join(path),
                quote_latency_ms=q_latency_ms,
            )
            ops_metrics.record_dex_quote(family=intent.family, chain=intent.chain, dex=self.name())
            ops_metrics.record_dex_quote_latency(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                seconds=q_latency_ms / 1000.0,
            )
            ops_metrics.record_dex_route_hops(family=intent.family, chain=intent.chain, dex=self.name(), hops=hops)
            return quote
        except Exception as e:
            ops_metrics.record_dex_quote_fail(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                reason=str(e),
            )
            raise

    def build(self, intent: TradeIntent, quote: Quote) -> TxPlan:
        try:
            if not self._router:
                raise ValueError("missing_router")
            path = quote.route_summary.split("->") if quote.route_summary else self._resolve_path(intent)
            deadline = int(time.time()) + int(max(1, intent.ttl_s))
            recipient = str(self.config.get("recipient") or self.config.get("from") or _ZERO)
            router = self._router_contract()
            calldata = router.encodeABI(
                fn_name="swapExactTokensForTokens",
                args=[int(intent.amount_in), int(quote.min_out), [self._checksum(p) for p in path], self._checksum(recipient), deadline],
            )
            return TxPlan(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                raw_tx=str(calldata),
                value=0,
                metadata={
                    "to": self._checksum(self._router),
                    "path": path,
                    "deadline": deadline,
                    "min_out": int(quote.min_out),
                    "amount_in": int(intent.amount_in),
                    "recipient": recipient,
                },
            )
        except Exception as e:
            ops_metrics.record_dex_build_fail(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                reason=str(e),
            )
            raise

    def simulate(self, plan: TxPlan) -> SimResult:
        try:
            to_addr = str((plan.metadata or {}).get("to") or self._router)
            call = {"to": self._checksum(to_addr), "data": str(plan.raw_tx or "0x")}
            sim_from = str((plan.metadata or {}).get("recipient") or self.config.get("from") or _ZERO)
            if sim_from and sim_from != _ZERO:
                call["from"] = self._checksum(sim_from)

            self.w3.eth.call(call)
            gas_est = None
            try:
                gas_est = int(self.w3.eth.estimate_gas(call))
            except Exception:
                gas_est = None
            return SimResult(ok=True, gas_estimate=gas_est, logs=["eth_call_ok"])
        except (ContractLogicError, ValueError, Exception) as e:
            code, reason = parse_revert_reason(e)
            ops_metrics.record_dex_sim_fail(
                family=plan.family,
                chain=plan.chain,
                dex=self.name(),
                reason=code,
            )
            return SimResult(ok=False, error_code=code, error_message=reason, logs=[str(e)])
