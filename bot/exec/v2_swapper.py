# bot/exec/v2_swapper.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from web3 import Web3

ERC20_ABI = [
    {"name":"approve","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"outputs":[{"name":"","type":"bool"}]},
    {"name":"allowance","type":"function","stateMutability":"view",
     "inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"decimals","type":"function","stateMutability":"view","inputs":[],"outputs":[{"name":"","type":"uint8"}]},
]

V2_ROUTER_ABI = [
    # swapTokensForExactTokens(uint amountOut, uint amountInMax, address[] calldata path, address to, uint deadline)
    {"name":"swapTokensForExactTokens","type":"function","stateMutability":"nonpayable",
     "inputs":[
        {"name":"amountOut","type":"uint256"},
        {"name":"amountInMax","type":"uint256"},
        {"name":"path","type":"address[]"},
        {"name":"to","type":"address"},
        {"name":"deadline","type":"uint256"}],
     "outputs":[{"name":"amounts","type":"uint256[]"}]},
]

@dataclass
class V2SwapParams:
    router: str
    token_in: str
    token_out: str
    amount_out_exact: int
    amount_in_max: int
    recipient: str
    deadline: int
    path: Optional[List[str]] = None  # default [token_in, token_out]
    gas_limit: Optional[int] = None
    max_fee_per_gas: Optional[int] = None
    max_priority_fee_per_gas: Optional[int] = None
    nonce: Optional[int] = None

class V2ExactOutputSwapper:
    def __init__(self, w3: Web3, router: str):
        self.w3 = w3
        self.router = self.w3.to_checksum_address(router)
        self.contract = self.w3.eth.contract(address=self.router, abi=V2_ROUTER_ABI)

    def build_swap_tx(self, p: V2SwapParams, sender: str) -> Dict[str, Any]:
        path = p.path or [self.w3.to_checksum_address(p.token_in), self.w3.to_checksum_address(p.token_out)]
        fn = self.contract.functions.swapTokensForExactTokens(
            int(p.amount_out_exact),
            int(p.amount_in_max),
            path,
            self.w3.to_checksum_address(p.recipient),
            int(p.deadline),
        )
        tx = fn.build_transaction({
            "from": self.w3.to_checksum_address(sender),
            **({"gas": p.gas_limit} if p.gas_limit else {}),
            **({"maxFeePerGas": p.max_fee_per_gas} if p.max_fee_per_gas else {}),
            **({"maxPriorityFeePerGas": p.max_priority_fee_per_gas} if p.max_priority_fee_per_gas else {}),
            **({"nonce": p.nonce} if p.nonce is not None else {}),
        })
        return tx

    def allowance(self, token: str, owner: str) -> int:
        t = self.w3.eth.contract(address=self.w3.to_checksum_address(token), abi=ERC20_ABI)
        return int(t.functions.allowance(self.w3.to_checksum_address(owner), self.router).call())

    def build_approve_tx(self, token: str, owner: str, amount: int, gas_limit: Optional[int] = None,
                         max_fee_per_gas: Optional[int] = None, max_priority_fee_per_gas: Optional[int] = None,
                         nonce: Optional[int] = None) -> Dict[str, Any]:
        t = self.w3.eth.contract(address=self.w3.to_checksum_address(token), abi=ERC20_ABI)
        fn = t.functions.approve(self.router, int(amount))
        tx = fn.build_transaction({
            "from": self.w3.to_checksum_address(owner),
            **({"gas": gas_limit} if gas_limit else {}),
            **({"maxFeePerGas": max_fee_per_gas} if max_fee_per_gas else {}),
            **({"maxPriorityFeePerGas": max_priority_fee_per_gas} if max_priority_fee_per_gas else {}),
            **({"nonce": nonce} if nonce is not None else {}),
        })
        return tx
