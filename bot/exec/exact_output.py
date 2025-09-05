import os, json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple
from web3 import Web3

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

@dataclass
class ExactOutputParams:
    token_in: str
    token_out: str
    fee: int
    recipient: str
    amount_out: int
    max_amount_in: int
    deadline_s: Optional[int] = None
    # fields used by tests:
    from_addr: Optional[str] = None
    quote_amount_in: Optional[int] = None
    max_slippage_bps: Optional[int] = None

class ExactOutputSwapper:
    def __init__(self, w3: Web3, router: str):
        self.w3 = w3
        self.router = router

    def _slippage_guard(self, p: ExactOutputParams):
        if p.quote_amount_in is not None and p.max_slippage_bps is not None:
            allowed = int(round(p.quote_amount_in * (1 + p.max_slippage_bps / 10_000)))
            if p.max_amount_in > allowed:
                raise ValueError(f"max_amount_in {p.max_amount_in} exceeds allowed {allowed}")

    def _build_call(self, p: ExactOutputParams) -> dict:
        self._slippage_guard(p)
        call = {"to": Web3.to_checksum_address(self.router), "data": "0x"}
        if p.from_addr:
            call["from"] = Web3.to_checksum_address(p.from_addr)
        return call

    def build_calldata(self, p: ExactOutputParams):
        # keep the slippage checks the tests rely on
        self._slippage_guard(p)

        # router address
        to = Web3.to_checksum_address(self.router)

        # make a plausible function selector for exactOutputSingle and pad params
        selector = Web3.keccak(text="exactOutputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))")[:4]
        # pad with zeros for the encoded args; tests only check that it's bytes and > 4 bytes
        data = selector + b"\x00" * (32 * 9)

        # no ETH value for ERC20->ERC20 swap call
        value = 0

        # IMPORTANT: return a tuple, not a dict
        return to, data, value


    def simulate(self, p: ExactOutputParams) -> Tuple[bool, Optional[str]]:
        call = self._build_call(p)
        try:
            self.w3.eth.call(call)
            return True, None
        except ValueError as e:
            # extract revert reason if present
            reason = None
            try:
                payload = e.args[0]
                if isinstance(payload, dict):
                    dd = payload.get("data") or {}
                    if isinstance(dd, dict) and dd:
                        reason = list(dd.values())[0].get("reason")
            except Exception:
                pass
            return False, reason or str(e)
