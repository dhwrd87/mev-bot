from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from web3 import HTTPProvider, Web3
from web3.exceptions import ContractLogicError

from adapters.dex_packs.base import DEXPack
from adapters.dex_packs.evm_univ2 import min_out_from_slippage, parse_revert_reason
from bot.core.types_dex import Quote, SimResult, TradeIntent, TxPlan
from ops import metrics as ops_metrics

_ZERO = "0x0000000000000000000000000000000000000000"

_QUOTER_ABI = [
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "quoteExactInputSingle",
        "inputs": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "nonpayable",
        "name": "quoteExactInput",
        "inputs": [{"name": "path", "type": "bytes"}, {"name": "amountIn", "type": "uint256"}],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
]

_ROUTER_ABI = [
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "exactInputSingle",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "exactInput",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "path", "type": "bytes"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
]


def _unpack_amount_out(raw: Any) -> int:
    if isinstance(raw, (tuple, list)):
        return int(raw[0])
    return int(raw)


def _fee_tiers_from_cfg(cfg: Dict[str, Any]) -> List[int]:
    default = [500, 3000, 10000]
    raw = cfg.get("fee_tiers", default)
    try:
        if isinstance(raw, str):
            vals = [int(x.strip()) for x in raw.split(",") if x.strip()]
        elif isinstance(raw, (list, tuple)):
            vals = [int(v) for v in raw]
        else:
            vals = []
    except Exception:
        vals = []
    # Keep only valid uint24 positive values and de-duplicate while preserving order.
    out: List[int] = []
    for v in vals:
        if v <= 0 or v >= 2**24:
            continue
        if v not in out:
            out.append(v)
    return out or default


class EVMUniV3Pack(DEXPack):
    def __init__(self, *, config: Optional[Dict[str, Any]] = None, instance_name: Optional[str] = None) -> None:
        super().__init__(config=config, instance_name=instance_name)
        self._factory = str(self.config.get("factory", "")).strip()
        self._quoter = str(self.config.get("quoter", "")).strip()
        self._swap_router = str(self.config.get("swap_router", "")).strip()
        self._fee_tiers = _fee_tiers_from_cfg(self.config)
        self._rpc_http = str(self.config.get("rpc_http") or "").strip() or str(os.getenv("RPC_HTTP_PRIMARY", "")).strip()
        self._timeout_s = float(self.config.get("rpc_timeout_s", 5.0))
        provider = HTTPProvider(self._rpc_http, request_kwargs={"timeout": self._timeout_s}) if self._rpc_http else None
        self.w3 = Web3(provider) if provider is not None else Web3()

    def name(self) -> str:
        return self._instance_name or "evm_univ3"

    def family_supported(self) -> str:
        return "evm"

    def _checksum(self, addr: str) -> str:
        return self.w3.to_checksum_address(addr)

    def _quoter_contract(self):
        return self.w3.eth.contract(address=self._checksum(self._quoter), abi=_QUOTER_ABI)

    def _router_contract(self):
        return self.w3.eth.contract(address=self._checksum(self._swap_router), abi=_ROUTER_ABI)

    def _path_cfg(self, intent: TradeIntent) -> Optional[Tuple[List[str], List[int]]]:
        paths = self.config.get("paths") or {}
        if not isinstance(paths, dict):
            return None
        key = f"{intent.token_in.lower()}->{intent.token_out.lower()}"
        row = paths.get(key) or paths.get(f"{intent.token_in}->{intent.token_out}")
        if not isinstance(row, dict):
            return None
        tokens = row.get("tokens")
        fees = row.get("fees")
        if not isinstance(tokens, list) or not isinstance(fees, list):
            return None
        if len(tokens) < 2 or len(fees) != len(tokens) - 1:
            return None
        return [str(x) for x in tokens], [int(f) for f in fees]

    def _encode_path(self, tokens: List[str], fees: List[int]) -> bytes:
        out = b""
        for i in range(len(fees)):
            out += bytes.fromhex(self._checksum(tokens[i])[2:])
            out += int(fees[i]).to_bytes(3, "big")
        out += bytes.fromhex(self._checksum(tokens[-1])[2:])
        return out

    def _quote_single_hop(self, intent: TradeIntent) -> Tuple[int, int]:
        q = self._quoter_contract()
        best_out = -1
        best_tier = 0
        for fee in self._fee_tiers:
            try:
                out_raw = q.functions.quoteExactInputSingle(
                    self._checksum(intent.token_in),
                    self._checksum(intent.token_out),
                    int(fee),
                    int(intent.amount_in),
                    0,
                ).call()
                out = _unpack_amount_out(out_raw)
                if out > best_out:
                    best_out = out
                    best_tier = int(fee)
            except Exception:
                continue
        if best_out <= 0:
            raise ValueError("quote_failed_all_fee_tiers")
        return int(best_out), int(best_tier)

    def _quote_multi_hop(self, tokens: List[str], fees: List[int], amount_in: int) -> int:
        q = self._quoter_contract()
        enc = self._encode_path(tokens, fees)
        out_raw = q.functions.quoteExactInput(enc, int(amount_in)).call()
        return _unpack_amount_out(out_raw)

    def quote(self, intent: TradeIntent) -> Quote:
        t0 = time.perf_counter()
        try:
            if not self._quoter:
                raise ValueError("missing_quoter")

            path_cfg = self._path_cfg(intent)
            if path_cfg is not None:
                tokens, fees = path_cfg
                amount_out = self._quote_multi_hop(tokens, fees, int(intent.amount_in))
                route_summary = "->".join(
                    [f"{tokens[i]}@{fees[i]}" for i in range(len(fees))] + [tokens[-1]]
                )
                hops = len(tokens) - 1
                fee_est = float(sum(fees)) / float(len(fees))
            else:
                amount_out, fee_tier = self._quote_single_hop(intent)
                route_summary = f"{intent.token_in}->{intent.token_out}@{fee_tier}"
                hops = 1
                fee_est = float(fee_tier)

            latency_ms = (time.perf_counter() - t0) * 1000.0
            quote = Quote(
                dex=self.name(),
                expected_out=int(amount_out),
                min_out=min_out_from_slippage(int(amount_out), int(intent.slippage_bps)),
                price_impact_bps=0.0,
                fee_estimate=fee_est,
                route_summary=route_summary,
                quote_latency_ms=latency_ms,
            )
            ops_metrics.record_dex_quote(family=intent.family, chain=intent.chain, dex=self.name())
            ops_metrics.record_dex_quote_latency(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                seconds=latency_ms / 1000.0,
            )
            ops_metrics.record_dex_route_hops(family=intent.family, chain=intent.chain, dex=self.name(), hops=hops)
            return quote
        except Exception as e:
            ops_metrics.record_dex_quote_fail(family=intent.family, chain=intent.chain, dex=self.name(), reason=str(e))
            raise

    def build(self, intent: TradeIntent, quote: Quote) -> TxPlan:
        try:
            if not self._swap_router:
                raise ValueError("missing_swap_router")
            router = self._router_contract()
            deadline = int(time.time()) + int(max(1, intent.ttl_s))
            recipient = str(self.config.get("recipient") or self.config.get("from") or _ZERO)
            path_cfg = self._path_cfg(intent)

            if path_cfg is not None:
                tokens, fees = path_cfg
                enc_path = self._encode_path(tokens, fees)
                params = (
                    enc_path,
                    self._checksum(recipient),
                    deadline,
                    int(intent.amount_in),
                    int(quote.min_out),
                )
                calldata = router.encodeABI(fn_name="exactInput", args=[params])
                metadata = {
                    "to": self._checksum(self._swap_router),
                    "path_tokens": tokens,
                    "path_fees": fees,
                    "deadline": deadline,
                    "min_out": int(quote.min_out),
                    "amount_in": int(intent.amount_in),
                    "recipient": recipient,
                    "method": "exactInput",
                }
            else:
                fee = int(str(quote.route_summary).rsplit("@", 1)[-1]) if "@" in str(quote.route_summary) else int(self._fee_tiers[0])
                params = (
                    self._checksum(intent.token_in),
                    self._checksum(intent.token_out),
                    fee,
                    self._checksum(recipient),
                    deadline,
                    int(intent.amount_in),
                    int(quote.min_out),
                    0,
                )
                calldata = router.encodeABI(fn_name="exactInputSingle", args=[params])
                metadata = {
                    "to": self._checksum(self._swap_router),
                    "fee_tier": fee,
                    "deadline": deadline,
                    "min_out": int(quote.min_out),
                    "amount_in": int(intent.amount_in),
                    "recipient": recipient,
                    "method": "exactInputSingle",
                }

            return TxPlan(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                raw_tx=str(calldata),
                value=0,
                metadata=metadata,
            )
        except Exception as e:
            ops_metrics.record_dex_build_fail(family=intent.family, chain=intent.chain, dex=self.name(), reason=str(e))
            raise

    def simulate(self, plan: TxPlan) -> SimResult:
        try:
            to_addr = str((plan.metadata or {}).get("to") or self._swap_router)
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
            ops_metrics.record_dex_sim_fail(family=plan.family, chain=plan.chain, dex=self.name(), reason=code)
            return SimResult(ok=False, error_code=code, error_message=reason, logs=[str(e)])
