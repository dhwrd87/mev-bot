from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from adapters.dex_packs.base import DEXPack
from adapters.dex_packs.evm_univ2 import min_out_from_slippage
from bot.core.types_dex import Quote, SimResult, TradeIntent, TxPlan
from ops import metrics as ops_metrics


class SolJupiterPack(DEXPack):
    def __init__(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        instance_name: Optional[str] = None,
        sol_client: Any = None,
    ) -> None:
        super().__init__(config=config, instance_name=instance_name)
        self.base_url = str(self.config.get("base_url") or "https://quote-api.jup.ag/v6").rstrip("/")
        self.api_key = str(self.config.get("api_key") or "").strip()
        self.rpc_http = str(self.config.get("rpc_http") or os.getenv("RPC_HTTP_PRIMARY", "")).strip()
        if not self.rpc_http:
            self.rpc_http = "https://api.mainnet-beta.solana.com"
        self.user_pubkey = str(self.config.get("user_public_key") or "").strip()
        self.wrap_unwrap_sol = bool(self.config.get("wrap_unwrap_sol", True))
        self.commitment = str(self.config.get("commitment") or "processed").strip()
        self.timeout_s = float(self.config.get("http_timeout_s", 6.0))
        self._http = requests.Session()
        # Optional externally managed Solana RPC client for simulateTransaction.
        self._sol_client = sol_client
        if self.api_key:
            self._http.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def name(self) -> str:
        return self._instance_name or "jupiter"

    def family_supported(self) -> str:
        return "sol"

    def _http_get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._http.get(f"{self.base_url}{path}", params=params, timeout=self.timeout_s)
        resp.raise_for_status()
        out = resp.json()
        if not isinstance(out, dict):
            raise ValueError("invalid_jupiter_get_json")
        return out

    def _http_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._http.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout_s)
        resp.raise_for_status()
        out = resp.json()
        if not isinstance(out, dict):
            raise ValueError("invalid_jupiter_post_json")
        return out

    def _rpc_call(self, method: str, params: List[Any]) -> Dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = self._http.post(self.rpc_http, json=body, timeout=self.timeout_s)
        resp.raise_for_status()
        out = resp.json()
        if not isinstance(out, dict):
            raise ValueError("invalid_solana_rpc_json")
        return out

    @staticmethod
    def _extract_route_stats(quote_resp: Dict[str, Any]) -> Tuple[int, str]:
        route_plan = quote_resp.get("routePlan")
        market_infos = quote_resp.get("marketInfos")

        venues: List[str] = []
        hops = 0

        if isinstance(route_plan, list):
            hops = len(route_plan)
            for item in route_plan:
                if not isinstance(item, dict):
                    continue
                swap_info = item.get("swapInfo")
                if isinstance(swap_info, dict):
                    label = str(swap_info.get("label") or swap_info.get("ammKey") or "").strip()
                    if label:
                        venues.append(label)
        elif isinstance(market_infos, list):
            hops = len(market_infos)
            for item in market_infos:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or item.get("id") or "").strip()
                if label:
                    venues.append(label)

        unique = []
        seen = set()
        for v in venues:
            k = v.lower()
            if k in seen:
                continue
            seen.add(k)
            unique.append(v)
        top = ",".join(unique[:3]) if unique else "unknown"
        return max(1, hops), top

    def quote(self, intent: TradeIntent) -> Quote:
        t0 = time.perf_counter()
        try:
            params = {
                "inputMint": intent.token_in,
                "outputMint": intent.token_out,
                "amount": int(intent.amount_in),
                "slippageBps": int(intent.slippage_bps),
            }
            data = self._http_get("/quote", params=params)
            out_amount = int(data.get("outAmount") or 0)
            if out_amount <= 0:
                raise ValueError("invalid_out_amount")

            hops, venues = self._extract_route_stats(data)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            quote = Quote(
                dex=self.name(),
                expected_out=out_amount,
                min_out=min_out_from_slippage(out_amount, int(intent.slippage_bps)),
                price_impact_bps=float(data.get("priceImpactPct") or 0.0) * 10_000.0,
                fee_estimate=float(data.get("platformFee", {}).get("amount") or 0),
                route_summary=f"hops={hops};venues={venues}",
                quote_latency_ms=latency_ms,
            )
            ops_metrics.record_dex_quote(family=intent.family, chain=intent.chain, dex=self.name())
            ops_metrics.record_dex_quote_latency(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                seconds=max(0.0, latency_ms / 1000.0),
            )
            ops_metrics.record_dex_route_hops(family=intent.family, chain=intent.chain, dex=self.name(), hops=hops)
            return quote
        except Exception as e:
            ops_metrics.record_dex_quote_fail(family=intent.family, chain=intent.chain, dex=self.name(), reason=str(e))
            raise

    def build(self, intent: TradeIntent, quote: Quote) -> TxPlan:
        try:
            user_pk = str(self.config.get("user_public_key") or self.user_pubkey).strip()
            if not user_pk:
                raise ValueError("missing_user_public_key")
            quote_resp = self._http_get(
                "/quote",
                {
                    "inputMint": intent.token_in,
                    "outputMint": intent.token_out,
                    "amount": int(intent.amount_in),
                    "slippageBps": int(intent.slippage_bps),
                },
            )
            swap_payload = {
                "quoteResponse": quote_resp,
                "userPublicKey": user_pk,
                "wrapAndUnwrapSol": bool(self.wrap_unwrap_sol),
            }
            swap_resp = self._http_post("/swap", swap_payload)
            tx_b64 = str(swap_resp.get("swapTransaction") or "").strip()
            if not tx_b64:
                raise ValueError("missing_swap_transaction")
            return TxPlan(
                family=intent.family,
                chain=intent.chain,
                dex=self.name(),
                raw_tx=tx_b64,
                value=0,
                instruction_bundle={
                    "swap_transaction_b64": tx_b64,
                    "last_valid_block_height": swap_resp.get("lastValidBlockHeight"),
                    "prioritization_fee_lamports": swap_resp.get("prioritizationFeeLamports"),
                    "wrap_unwrap_sol": bool(self.wrap_unwrap_sol),
                },
                metadata={
                    "user_public_key": user_pk,
                    "route_summary": quote.route_summary,
                },
            )
        except Exception as e:
            ops_metrics.record_dex_build_fail(family=intent.family, chain=intent.chain, dex=self.name(), reason=str(e))
            raise

    def simulate(self, plan: TxPlan) -> SimResult:
        try:
            tx_b64 = str(plan.raw_tx or "")
            if not tx_b64:
                tx_b64 = str((plan.instruction_bundle or {}).get("swap_transaction_b64") or "")
            if not tx_b64:
                raise ValueError("missing_serialized_transaction")
            result: Dict[str, Any]
            if self._sol_client is not None and hasattr(self._sol_client, "simulate_transaction"):
                # Keep call shape generic so tests and optional clients can be plugged in.
                sim_resp = self._sol_client.simulate_transaction(
                    tx_b64,
                    sig_verify=False,
                    replace_recent_blockhash=True,
                    commitment=self.commitment,
                    encoding="base64",
                )
                if isinstance(sim_resp, dict):
                    value = sim_resp.get("value")
                else:
                    value = getattr(sim_resp, "value", None)
                result = {"value": value if isinstance(value, dict) else {}}
            else:
                rpc_out = self._rpc_call(
                    "simulateTransaction",
                    [
                        tx_b64,
                        {
                            "sigVerify": False,
                            "replaceRecentBlockhash": True,
                            "encoding": "base64",
                            "commitment": self.commitment,
                        },
                    ],
                )
                result = rpc_out.get("result") if isinstance(rpc_out.get("result"), dict) else {}
            value = result.get("value") if isinstance(result.get("value"), dict) else {}
            err = value.get("err")
            logs = value.get("logs") if isinstance(value.get("logs"), list) else None
            units = value.get("unitsConsumed")
            if err:
                ops_metrics.record_dex_sim_fail(family=plan.family, chain=plan.chain, dex=self.name(), reason="simulation_error")
                return SimResult(
                    ok=False,
                    error_code="simulation_error",
                    error_message=str(err),
                    compute_units=int(units) if isinstance(units, int) else None,
                    logs=[str(x) for x in logs] if logs else None,
                )
            return SimResult(
                ok=True,
                compute_units=int(units) if isinstance(units, int) else None,
                logs=[str(x) for x in logs] if logs else None,
            )
        except Exception as e:
            ops_metrics.record_dex_sim_fail(family=plan.family, chain=plan.chain, dex=self.name(), reason="rpc_error")
            return SimResult(ok=False, error_code="rpc_error", error_message=str(e), logs=[str(e)])
