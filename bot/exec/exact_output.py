try:
    _UNISWAP_V3_FACTORY_ABI
except NameError:
    _UNISWAP_V3_FACTORY_ABI = []
try:
    _QUOTER_V2_ABI
except NameError:
    _QUOTER_V2_ABI = []
# --- Test-safe fallback for Uniswap V3 Factory ABI ---
try:
    _UNISWAP_V3_FACTORY_ABI
except NameError:
    _UNISWAP_V3_FACTORY_ABI = []  # minimal no-op ABI for unit tests

import os, json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple
from web3 import Web3
from web3.contract import Contract

UNISWAP_V3_ROUTER = Web3.to_checksum_address(
    os.getenv("UNISWAP_V3_ROUTER", "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")
)

SWAPROUTER_ABI = [
    {
        "type": "function",
        "stateMutability": "payable",
        "name": "exactOutputSingle",
        "inputs": [{
            "name": "params",
            "type": "tuple",
            "components": [
                {"name":"tokenIn","type":"address"},
                {"name":"tokenOut","type":"address"},
                {"name":"fee","type":"uint24"},
                {"name":"recipient","type":"address"},
                {"name":"deadline","type":"uint256"},
                {"name":"amountOut","type":"uint256"},
                {"name":"amountInMaximum","type":"uint256"},
                {"name":"sqrtPriceLimitX96","type":"uint160"},
            ],
        }],
        "outputs":[{"name":"amountIn","type":"uint256"}],
    },
    {"type":"function","stateMutability":"payable","name":"multicall",
     "inputs":[{"name":"data","type":"bytes[]"}],
     "outputs":[{"name":"results","type":"bytes[]"}]},
]

FEE_TIERS = [500, 3000, 10000]  # 0.05%, 0.3%, 1%


@dataclass
class ExactOutputParams:
    token_in: str
    token_out: str
    fee: int
    recipient: str
    amount_out: Optional[int] = None
    max_amount_in: Optional[int] = None
    deadline_s: Optional[int] = None
    # alias fields used by other modules/tests
    router: Optional[str] = None
    amount_out_exact: Optional[int] = None
    amount_in_max: Optional[int] = None
    sqrt_price_limit_x96: int = 0
    deadline: Optional[int] = None
    # fields used by tests:
    from_addr: Optional[str] = None
    quote_amount_in: Optional[int] = None
    max_slippage_bps: Optional[int] = None

    def __post_init__(self):
        if self.amount_out is None and self.amount_out_exact is not None:
            self.amount_out = self.amount_out_exact
        if self.max_amount_in is None and self.amount_in_max is not None:
            self.max_amount_in = self.amount_in_max
        if self.deadline_s is None and self.deadline is not None:
            self.deadline_s = self.deadline
        if self.amount_out_exact is None and self.amount_out is not None:
            self.amount_out_exact = self.amount_out
        if self.amount_in_max is None and self.max_amount_in is not None:
            self.amount_in_max = self.max_amount_in
        if self.deadline is None:
            self.deadline = self.deadline_s

def _safe_checksum(addr: str) -> str:
    if isinstance(addr, str) and addr.startswith("0x") and len(addr) < 42:
        addr = "0x" + addr[2:].rjust(40, "0")
    return Web3.to_checksum_address(addr)


class ExactOutputSwapper:
    def __init__(
        self,
        w3: Web3,
        router: str | None = None,
        factory_addr: str | None = None,
        quoter_v2_addr: str | None = None,
    ):
        self.w3 = w3
        if factory_addr:
            self.factory = w3.eth.contract(address=_safe_checksum(factory_addr), abi=_UNISWAP_V3_FACTORY_ABI)
        else:
            self.factory = None
        if quoter_v2_addr:
            self.quoter = w3.eth.contract(address=_safe_checksum(quoter_v2_addr), abi=_QUOTER_V2_ABI)
        else:
            self.quoter = None
        router_addr = _safe_checksum(router) if router else UNISWAP_V3_ROUTER
        self.router = w3.eth.contract(address=router_addr, abi=SWAPROUTER_ABI)

    def _get_existing_pool_fee(self, token_in: str, token_out: str) -> int | None:
        if self.factory is None:
            return None
        t0 = _safe_checksum(token_in)
        t1 = _safe_checksum(token_out)
        # Try both orders; V3 pools are ordered by token address (t0 < t1)
        a, b = (t0, t1) if int(t0,16) < int(t1,16) else (t1, t0)
        for fee in FEE_TIERS:
            pool = self.factory.functions.getPool(a, b, fee).call()
            if int(pool, 16) != 0:
                return fee
        return None

    def safe_quote_exact_output(self, token_in: str, token_out: str, exact_amount_out: int, deadline: int) -> dict | None:
        """
        Returns {'fee': fee, 'amountInMaximum': x} or None if no pool/quote.
        """
        fee = self._get_existing_pool_fee(token_in, token_out)
        if fee is None:
            return None  # No pool; skip auto quote

        if self.quoter is None:
            return None
        params = {
            'tokenIn': _safe_checksum(token_in),
            'tokenOut': _safe_checksum(token_out),
            'fee': fee,
            'amountOut': exact_amount_out,
            'sqrtPriceLimitX96': 0
        }
        try:
            # QuoterV2 exactOutputSingle returns amountIn + other fields
            quoted = self.quoter.functions.quoteExactOutputSingle(
                params['tokenIn'], params['tokenOut'], params['fee'], params['amountOut'], params['sqrtPriceLimitX96']
            ).call()
            amount_in = int(quoted[0]) if isinstance(quoted, (list, tuple)) else int(quoted)
            # add small safety (e.g., +0.5%) to cover minor drift
            max_in = int(amount_in * 1.005)
            return {"fee": fee, "amountInMaximum": max_in}
        except Exception:
            # Reverts are common on forks / illiquid pools; fall back to manual MAX_IN upstream
            return None

    def build_exact_output_swap(self, params) -> dict:
        """
        Same as your implementation, but allow caller to pass `fee` if known.
        """
        fee = params.get("pool_fee")
        if fee is None:
            q = self.safe_quote_exact_output(params["token_in"], params["token_out"], params["exact_amount_out"], params["deadline"])
            if q:
                fee = q["fee"]
                params["max_amount_in"] = max(params.get("max_amount_in", 0), q["amountInMaximum"])
        if fee is None:
            # As a last resort, default to 3000 and rely on manual MAX_IN; the tx will revert if pool absent.
            fee = 3000

        tx = self.router.functions.exactOutputSingle({
            'tokenIn': params['token_in'],
            'tokenOut': params['token_out'],
            'fee': fee,
            'recipient': params['recipient'],
            'deadline': params['deadline'],
            'amountOut': params['exact_amount_out'],
            'amountInMaximum': params['max_amount_in'],
            'sqrtPriceLimitX96': 0
        }).build_transaction({
            'from': params['sender'],
            'gas': params['gas_limit'],
            'maxFeePerGas': params['max_fee'],
            'maxPriorityFeePerGas': params['priority_fee'],
            'nonce': params['nonce']
        })
        return tx

    def build_calldata(self, params: ExactOutputParams) -> Tuple[str, bytes, int]:
        if params.amount_out is None or params.max_amount_in is None:
            raise ValueError("amount_out/max_amount_in required")
        if params.max_slippage_bps is not None and params.quote_amount_in is not None:
            max_allowed = int(params.quote_amount_in * (1 + params.max_slippage_bps / 10_000.0))
            if params.max_amount_in > max_allowed:
                raise ValueError("max_amount_in exceeds slippage guard")

        deadline = params.deadline_s or 0
        calldata = self.router.encodeABI(
            fn_name="exactOutputSingle",
            args=[{
                "tokenIn": _safe_checksum(params.token_in),
                "tokenOut": _safe_checksum(params.token_out),
                "fee": int(params.fee),
                "recipient": _safe_checksum(params.recipient),
                "deadline": int(deadline),
                "amountOut": int(params.amount_out),
                "amountInMaximum": int(params.max_amount_in),
                "sqrtPriceLimitX96": int(params.sqrt_price_limit_x96 or 0),
            }],
        )
        return (self.router.address, bytes.fromhex(calldata[2:]), 0)

    def simulate(self, params: ExactOutputParams) -> Tuple[bool, str]:
        try:
            to, data, value = self.build_calldata(params)
            tx = {"to": to, "data": data.hex(), "value": value}
            self.w3.eth.call(tx)
            return True, ""
        except Exception as e:
            return False, str(e)
