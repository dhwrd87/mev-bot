# bot/scripts/smoke_exact_output.py
import os, sys
from web3 import Web3
from bot.exec.exact_output import ExactOutputSwapper, ExactOutputParams

RPC     = (os.getenv("RPC_HTTP") or os.getenv("RPC_ENDPOINT_PRIMARY") or "https://rpc.sepolia.org")
ROUTER  = os.getenv("UNISWAP_V3_ROUTER", "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45")

TOKEN_IN   = os.getenv("SMOKE_TOKEN_IN")
TOKEN_OUT  = os.getenv("SMOKE_TOKEN_OUT")
RECIPIENT  = os.getenv("SMOKE_RECIPIENT")

AMOUNT_OUT   = int(os.getenv("SMOKE_AMOUNT_OUT", "1000000"))
MAX_IN       = int(os.getenv("SMOKE_MAX_IN", "10000000"))
QUOTE_IN     = int(os.getenv("SMOKE_QUOTE_IN", str(AMOUNT_OUT)))
SLIPPAGE_BPS = int(os.getenv("SMOKE_SLIPPAGE_BPS", "100"))  # 1%
FEE          = int(os.getenv("SMOKE_FEE", "3000"))
DEADLINE_S   = int(os.getenv("SMOKE_DEADLINE_S", "600"))
FROM_ADDR    = os.getenv("SMOKE_FROM")

w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
swapper = ExactOutputSwapper(w3, router=ROUTER)

p = ExactOutputParams(
    token_in=Web3.to_checksum_address(TOKEN_IN),
    token_out=Web3.to_checksum_address(TOKEN_OUT),
    fee=FEE,
    recipient=Web3.to_checksum_address(RECIPIENT),
    amount_out=AMOUNT_OUT,
    max_amount_in=MAX_IN,
    deadline_s=DEADLINE_S,
    from_addr=Web3.to_checksum_address(FROM_ADDR) if FROM_ADDR else None,
    quote_amount_in=QUOTE_IN,
    max_slippage_bps=SLIPPAGE_BPS,
)

ok, err = swapper.simulate(p)
print("simulate_ok:", ok, "err:", err)
