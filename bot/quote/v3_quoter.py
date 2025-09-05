# bot/quote/v3_quoter.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from web3 import Web3
from web3.exceptions import ContractLogicError

# Minimal QuoterV2 ABI
QUOTER_V2_ABI = [
    {
        "name": "quoteExactOutputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name":"tokenIn","type":"address"},
            {"name":"tokenOut","type":"address"},
            {"name":"amount","type":"uint256"},
            {"name":"fee","type":"uint24"},
            {"name":"sqrtPriceLimitX96","type":"uint160"}
        ],
        "outputs": [
            {"name":"amountIn","type":"uint256"},
            {"name":"sqrtPriceX96After","type":"uint160"},
            {"name":"initializedTicksCrossed","type":"uint32"},
            {"name":"gasEstimate","type":"uint256"}
        ],
    },
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name":"tokenIn","type":"address"},
            {"name":"tokenOut","type":"address"},
            {"name":"amountIn","type":"uint256"},
            {"name":"fee","type":"uint24"},
            {"name":"sqrtPriceLimitX96","type":"uint160"}
        ],
        "outputs": [
            {"name":"amountOut","type":"uint256"},
            {"name":"sqrtPriceX96After","type":"uint160"},
            {"name":"initializedTicksCrossed","type":"uint32"},
            {"name":"gasEstimate","type":"uint256"}
        ],
    },
]

@dataclass
class V3QuoteOut:
    ok: bool
    amount_in: Optional[int]
    gas_estimate: Optional[int]
    reason: Optional[str] = None

@dataclass
class V3QuoteIn:
    ok: bool
    amount_out: Optional[int]
    gas_estimate: Optional[int]
    reason: Optional[str] = None

class V3Quoter:
    """
    Thin client around QuoterV2. Address must be provided via config per chain.
    """
    def __init__(self, w3: Web3, quoter_address: str):
        self.w3 = w3
        self.quoter = self.w3.eth.contract(address=self.w3.to_checksum_address(quoter_address), abi=QUOTER_V2_ABI)

    def quote_exact_output_single(self, token_in: str, token_out: str, want_out: int, fee: int, sqrt_price_limit_x96: int = 0) -> V3QuoteOut:
        try:
            amt_in, _, _, gas_est = self.quoter.functions.quoteExactOutputSingle(
                self.w3.to_checksum_address(token_in),
                self.w3.to_checksum_address(token_out),
                int(want_out),
                int(fee),
                int(sqrt_price_limit_x96)
            ).call()
            return V3QuoteOut(ok=True, amount_in=int(amt_in), gas_estimate=int(gas_est))
        except ContractLogicError as e:
            return V3QuoteOut(ok=False, amount_in=None, gas_estimate=None, reason=str(e))
        except Exception as e:
            return V3QuoteOut(ok=False, amount_in=None, gas_estimate=None, reason=f"error:{e}")

    def quote_exact_input_single(self, token_in: str, token_out: str, amount_in: int, fee: int, sqrt_price_limit_x96: int = 0) -> V3QuoteIn:
        try:
            amt_out, _, _, gas_est = self.quoter.functions.quoteExactInputSingle(
                self.w3.to_checksum_address(token_in),
                self.w3.to_checksum_address(token_out),
                int(amount_in),
                int(fee),
                int(sqrt_price_limit_x96)
            ).call()
            return V3QuoteIn(ok=True, amount_out=int(amt_out), gas_estimate=int(gas_est))
        except ContractLogicError as e:
            return V3QuoteIn(ok=False, amount_out=None, gas_estimate=None, reason=str(e))
        except Exception as e:
            return V3QuoteIn(ok=False, amount_out=None, gas_estimate=None, reason=f"error:{e}")
