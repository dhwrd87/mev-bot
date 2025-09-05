# bot/hunter/decoder.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from web3 import Web3

# Minimal ABIs for decoding the most common swap functions
UNIV2_ROUTER_ABI = [
    # swapExactTokensForTokens(uint amountIn,uint amountOutMin,address[] path,address to,uint deadline)
    {"name":"swapExactTokensForTokens","type":"function","stateMutability":"nonpayable",
     "inputs":[
        {"name":"amountIn","type":"uint256"},
        {"name":"amountOutMin","type":"uint256"},
        {"name":"path","type":"address[]"},
        {"name":"to","type":"address"},
        {"name":"deadline","type":"uint256"}],
     "outputs":[{"name":"amounts","type":"uint256[]"}]},
    # swapExactETHForTokens(uint amountOutMin,address[] path,address to,uint deadline)
    {"name":"swapExactETHForTokens","type":"function","stateMutability":"payable",
     "inputs":[
        {"name":"amountOutMin","type":"uint256"},
        {"name":"path","type":"address[]"},
        {"name":"to","type":"address"},
        {"name":"deadline","type":"uint256"}],
     "outputs":[{"name":"amounts","type":"uint256[]"}]},
    # swapExactTokensForETH(uint amountIn,uint amountOutMin,address[] path,address to,uint deadline)
    {"name":"swapExactTokensForETH","type":"function","stateMutability":"nonpayable",
     "inputs":[
        {"name":"amountIn","type":"uint256"},
        {"name":"amountOutMin","type":"uint256"},
        {"name":"path","type":"address[]"},
        {"name":"to","type":"address"},
        {"name":"deadline","type":"uint256"}],
     "outputs":[{"name":"amounts","type":"uint256[]"}]},
]
UNIV3_ROUTER_ABI = [
    # exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))
    {"name":"exactInputSingle","type":"function","stateMutability":"payable",
     "inputs":[{"name":"params","type":"tuple","components":[
        {"name":"tokenIn","type":"address"},
        {"name":"tokenOut","type":"address"},
        {"name":"fee","type":"uint24"},
        {"name":"recipient","type":"address"},
        {"name":"deadline","type":"uint256"},
        {"name":"amountIn","type":"uint256"},
        {"name":"amountOutMinimum","type":"uint256"},
        {"name":"sqrtPriceLimitX96","type":"uint160"}]}],
     "outputs":[{"name":"amountOut","type":"uint256"}]},
    # exactInput(bytes path,uint256 amountIn,uint256 amountOutMinimum)
    {"name":"exactInput","type":"function","stateMutability":"payable",
     "inputs":[
        {"name":"path","type":"bytes"},
        {"name":"amountIn","type":"uint256"},
        {"name":"amountOutMinimum","type":"uint256"}],
     "outputs":[{"name":"amountOut","type":"uint256"}]},
]

@dataclass
class PendingTxView:
    hash: str
    to: Optional[str]
    from_: Optional[str]
    max_fee_per_gas: Optional[int]
    max_priority_fee_per_gas: Optional[int]
    gas_price_legacy: Optional[int]
    input: str

@dataclass
class SwapIntent:
    router: str
    kind: str  # "v2" | "v3"
    token_in: str
    token_out: str
    amount_in: int
    min_out: int
    fee_tier: Optional[int] = None
    path: Optional[List[str]] = None  # v2 multihop, first->last define in/out

class TxDecoder:
    def __init__(self, w3: Web3):
        self.w3 = w3
        self.v2_contract = None
        self.v3_contract = None

    def _v2(self, router: str):
        if not self.v2_contract or self.v2_contract.address != self.w3.to_checksum_address(router):
            self.v2_contract = self.w3.eth.contract(address=self.w3.to_checksum_address(router), abi=UNIV2_ROUTER_ABI)
        return self.v2_contract

    def _v3(self, router: str):
        if not self.v3_contract or self.v3_contract.address != self.w3.to_checksum_address(router):
            self.v3_contract = self.w3.eth.contract(address=self.w3.to_checksum_address(router), abi=UNIV3_ROUTER_ABI)
        return self.v3_contract

    def decode_swap(self, tx: PendingTxView) -> Optional[SwapIntent]:
        if not tx.to or not tx.input or len(tx.input) < 10:
            return None
        to = self.w3.to_checksum_address(tx.to)
        selector = tx.input[:10].lower()

        # Try V3 first (most common on majors)
        try:
            v3 = self._v3(to)
            fn, args = v3.decode_function_input(tx.input)
            if fn.fn_name == "exactInputSingle":
                p = args["params"]
                return SwapIntent(
                    router=to, kind="v3",
                    token_in=self.w3.to_checksum_address(p["tokenIn"]),
                    token_out=self.w3.to_checksum_address(p["tokenOut"]),
                    fee_tier=int(p["fee"]),
                    amount_in=int(p["amountIn"]),
                    min_out=int(p["amountOutMinimum"]),
                    path=None
                )
            if fn.fn_name == "exactInput":
                # V3 path is bytes: [tokenIn][fee][tokenMid][fee][tokenOut]...
                # For detection we only need first and last token.
                path_bytes = bytes.fromhex(args["path"][2:]) if isinstance(args["path"], str) else args["path"]
                if len(path_bytes) < 20*2+3:  # token/fee/token
                    return None
                token_in = self.w3.to_checksum_address("0x" + path_bytes[:20].hex())
                token_out = self.w3.to_checksum_address("0x" + path_bytes[-20:].hex())
                return SwapIntent(
                    router=to, kind="v3",
                    token_in=token_in, token_out=token_out,
                    fee_tier=None,
                    amount_in=int(args["amountIn"]),
                    min_out=int(args["amountOutMinimum"]),
                    path=None
                )
        except Exception:
            pass

        # Fallback: V2 common exact swaps
        try:
            v2 = self._v2(to)
            fn, args = v2.decode_function_input(tx.input)
            if fn.fn_name in ("swapExactTokensForTokens", "swapExactETHForTokens", "swapExactTokensForETH"):
                path = [self.w3.to_checksum_address(x) for x in args["path"]]
                amount_in = int(args.get("amountIn", 0))  # for ETH-in, amountIn from msg.value (not visible here)
                min_out = int(args.get("amountOutMin", 0))
                return SwapIntent(
                    router=to, kind="v2",
                    token_in=path[0], token_out=path[-1],
                    amount_in=amount_in, min_out=min_out,
                    path=path
                )
        except Exception:
            pass

        return None
