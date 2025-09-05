# bot/exec/uniswap_v3.py
from web3 import Web3
import json

# Uniswap V3 ISwapRouter address per chain
_UNISWAP_V3_ROUTER = {
    1:   "0xE592427A0AEce92De3Edee1F18E0157C05861564",  # Ethereum mainnet
    137: "0xE592427A0AEce92De3Edee1F18E0157C05861564",  # Polygon mainnet
    11155111: "0xE592427A0AEce92De3Edee1F18E0157C05861564",  # Sepolia (router deployed)
    80002: "0xE592427A0AEce92De3Edee1F18E0157C05861564",     # Polygon Amoy (same addr)
}

# Minimal ABI fragment for exactOutputSingle
_EXACT_OUT_ABI = json.loads("""
[
  {
    "inputs":[
      {
        "components":[
          {"internalType":"address","name":"tokenIn","type":"address"},
          {"internalType":"address","name":"tokenOut","type":"address"},
          {"internalType":"uint24","name":"fee","type":"uint24"},
          {"internalType":"address","name":"recipient","type":"address"},
          {"internalType":"uint256","name":"deadline","type":"uint256"},
          {"internalType":"uint256","name":"amountOut","type":"uint256"},
          {"internalType":"uint256","name":"amountInMaximum","type":"uint256"},
          {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}
        ],
        "internalType":"struct ISwapRouter.ExactOutputSingleParams",
        "name":"params","type":"tuple"
      }
    ],
    "name":"exactOutputSingle",
    "outputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"}],
    "stateMutability":"payable","type":"function"
  }
]
""")

def get_router_address(chain_id: int) -> str:
    if chain_id not in _UNISWAP_V3_ROUTER:
        raise ValueError(f"No Uniswap V3 router for chain {chain_id}")
    return Web3.to_checksum_address(_UNISWAP_V3_ROUTER[chain_id])

def build_exact_output_tx(
    w3: Web3,
    chain_id: int,
    *,
    token_in: str,
    token_out: str,
    fee: int,
    recipient: str,
    deadline: int,
    amount_out: int,
    amount_in_max: int,
    sender: str,
    nonce: int,
    max_fee_per_gas: int | None = None,
    max_priority_fee_per_gas: int | None = None,
    gas: int | None = None,
):
    """Return (router_address, tx_dict) for exactOutputSingle"""
    router = get_router_address(chain_id)
    contract = w3.eth.contract(address=router, abi=_EXACT_OUT_ABI)
    tx = contract.functions.exactOutputSingle({
        "tokenIn": Web3.to_checksum_address(token_in),
        "tokenOut": Web3.to_checksum_address(token_out),
        "fee": fee,
        "recipient": Web3.to_checksum_address(recipient),
        "deadline": deadline,
        "amountOut": amount_out,
        "amountInMaximum": amount_in_max,
        "sqrtPriceLimitX96": 0
    }).build_transaction({
        "from": Web3.to_checksum_address(sender),
        "nonce": nonce,
        "chainId": chain_id
    })
    if gas is not None: tx["gas"] = gas
    if max_fee_per_gas is not None: tx["maxFeePerGas"] = max_fee_per_gas
    if max_priority_fee_per_gas is not None: tx["maxPriorityFeePerGas"] = max_priority_fee_per_gas
    return router, tx

__all__ = ["build_exact_output_tx", "get_router_address"]
