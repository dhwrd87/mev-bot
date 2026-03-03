# bot/scripts/run_stealth_trade.py
from __future__ import annotations
import os, time, sys, asyncio
from typing import Optional, Dict, Any

from web3 import Web3, HTTPProvider
from eth_account import Account

from bot.exec.permit2 import (
    Permit2Handler, PermitParams, PERMIT2_ADDRESS, InMemoryNonceStore
)
from bot.exec.orderflow import PrivateOrderflowManager, Endpoint, TxMeta

# Minimal ABI for Uniswap V3 SwapRouter exactOutputSingle
SWAPROUTER_ABI = [
    {
        "inputs": [
            {"components": [
                {"internalType":"address","name":"tokenIn","type":"address"},
                {"internalType":"address","name":"tokenOut","type":"address"},
                {"internalType":"uint24","name":"fee","type":"uint24"},
                {"internalType":"address","name":"recipient","type":"address"},
                {"internalType":"uint256","name":"deadline","type":"uint256"},
                {"internalType":"uint256","name":"amountOut","type":"uint256"},
                {"internalType":"uint256","name":"amountInMaximum","type":"uint256"},
                {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}
            ], "internalType":"struct ISwapRouter.ExactOutputSingleParams", "name":"params", "type":"tuple"}
        ],
        "name": "exactOutputSingle",
        "outputs": [{"internalType":"uint256","name":"amountIn","type":"uint256"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

# add near imports (top)
QUOTER_V2 = os.getenv("QUOTER_V2", "0x61fFE014bA17989E743c5F6cB21bF9697530B21e")
QUOTER_ABI = [{
  "inputs":[{"components":[
    {"internalType":"address","name":"tokenIn","type":"address"},
    {"internalType":"address","name":"tokenOut","type":"address"},
    {"internalType":"uint24","name":"fee","type":"uint24"},
    {"internalType":"address","name":"recipient","type":"address"},
    {"internalType":"uint256","name":"deadline","type":"uint256"},
    {"internalType":"uint256","name":"amountOut","type":"uint256"},
    {"internalType":"uint256","name":"amountInMaximum","type":"uint256"},
    {"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],
    "internalType":"struct ISwapRouter.ExactOutputSingleParams","name":"params","type":"tuple"}],
  "name":"quoteExactOutputSingle",
  "outputs":[
    {"internalType":"uint256","name":"amountIn","type":"uint256"},
    {"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},
    {"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},
    {"internalType":"uint256","name":"gasEstimate","type":"uint256"}],
  "stateMutability":"nonpayable","type":"function"
}]

def _amount_in_used(rcpt, token_addr, owner_addr):
    TRANSFER_SIG = Web3.keccak(text="Transfer(address,address,uint256)").hex()
    spent = 0
    for lg in rcpt.logs:
        if Web3.to_checksum_address(lg.address) != Web3.to_checksum_address(token_addr):
            continue
        if not lg.topics or lg.topics[0].hex() != TRANSFER_SIG:
            continue
        from_addr = Web3.to_checksum_address("0x" + lg.topics[1].hex()[-40:])
        if from_addr == Web3.to_checksum_address(owner_addr):
            spent += int(lg.data.hex(), 16)  # HexBytes -> int
    return spent

def _amount_out_received(rcpt, token_addr, recipient_addr):
    TRANSFER_SIG = Web3.keccak(text="Transfer(address,address,uint256)").hex()
    got = 0
    for lg in rcpt.logs:
        if Web3.to_checksum_address(lg.address) != Web3.to_checksum_address(token_addr):
            continue
        if not lg.topics or lg.topics[0].hex() != TRANSFER_SIG:
            continue
        to_addr = Web3.to_checksum_address("0x" + lg.topics[2].hex()[-40:])
        if to_addr == Web3.to_checksum_address(recipient_addr):
            got += int(lg.data.hex(), 16)
    return got


def _require(env: str) -> str:
    v = os.getenv(env)
    if not v:
        print(f"missing required env: {env}", file=sys.stderr)
        sys.exit(2)
    return v

def _require_any(*envs: str) -> str:
    for env in envs:
        v = os.getenv(env)
        if v:
            return v
    print(f"missing required env: {'|'.join(envs)}", file=sys.stderr)
    sys.exit(2)

async def main():
    # --- Inputs ---
    rpc         = _require_any("RPC_HTTP", "RPC_ENDPOINT_PRIMARY")
    router      = Web3.to_checksum_address(_require("UNISWAP_V3_ROUTER"))
    token_in    = Web3.to_checksum_address(_require("TOKEN_IN"))
    token_out   = Web3.to_checksum_address(_require("TOKEN_OUT"))
    recipient   = Web3.to_checksum_address(os.getenv("RECIPIENT") or "0x" + "0"*40)
    amount_out  = int(_require("AMOUNT_OUT"))     # exact output
    max_in_env  = os.getenv("MAX_IN")
    max_in      = int(max_in_env) if max_in_env else None    
    priv_hex    = _require("PRIVATE_KEY")         # TEST KEY ONLY!
    pool_fee    = int(os.getenv("POOL_FEE", "3000"))  # 500/3000/10000
    deadline_s  = int(os.getenv("DEADLINE_SEC", "600"))
    priority_gw = int(os.getenv("PRIORITY_GWEI", "2"))
    maxfee_gw   = int(os.getenv("MAXFEE_GWEI", "30"))
    chain_label = os.getenv("CHAIN_LABEL", "sepolia")

    w3 = Web3(HTTPProvider(rpc))
    assert w3.is_connected(), "RPC not reachable"
    chain_id = int(w3.eth.chain_id)

    acct = Account.from_key(priv_hex)
    owner = Web3.to_checksum_address(acct.address)
    if recipient == Web3.to_checksum_address("0x" + "0"*40):
        recipient = owner  # default to self

    now = int(time.time())
    deadline = now + deadline_s
    base_nonce = w3.eth.get_transaction_count(owner)

    print("---- stealth trade cfg ----")
    print(f"RPC        : {rpc}")
    print(f"CHAIN_ID   : {chain_id} ({chain_label})")
    print(f"ROUTER     : {router}")
    print(f"TOKEN_IN   : {token_in}")
    print(f"TOKEN_OUT  : {token_out}")
    print(f"RECIPIENT  : {recipient}")
    print(f"OWNER      : {owner}")
    print(f"AMOUNT_OUT : {amount_out}")
    print(f"FEE(bps)   : {pool_fee}")
    print(f"DEADLINE   : {deadline} (in {deadline_s}s)")
    print("---------------------------")

    slip_bps = int(os.getenv("SLIPPAGE_BPS", "100"))
    fees_to_try = [int(os.getenv("POOL_FEE"))] if os.getenv("POOL_FEE") else [500, 3000, 10000]

    if max_in is None:
        last_err = None
        for fee_try in fees_to_try:
            try:
                quoter = w3.eth.contract(address=Web3.to_checksum_address(QUOTER_V2), abi=QUOTER_ABI)
                quoted_in, *_ = quoter.functions.quoteExactOutputSingle({
                    "tokenIn": token_in,
                    "tokenOut": token_out,
                    "fee": int(fee_try),
                    "recipient": owner,
                    "deadline": deadline,
                    "amountOut": int(amount_out),
                    "amountInMaximum": 2**256-1,
                    "sqrtPriceLimitX96": 0
                }).call()
                max_in = quoted_in * (10_000 + slip_bps) // 10_000
                pool_fee = int(fee_try)
                print(f"quoted amountIn: {quoted_in}  -> max_in: {max_in}  (fee={pool_fee})")
                break
            except Exception as e:
                last_err = e
        if max_in is None:
            print("❌ Quoter failed for all fee tiers. Set MAX_IN manually.", file=sys.stderr)
            print(f"   last quoter error: {last_err}", file=sys.stderr)
            sys.exit(12)
    else:
        print(f"MAX_IN     : {max_in}  (env)")


    # --- 1) Permit2 signature off-chain (persist nonces) ---
    nonce_store = InMemoryNonceStore()  # swap to PgNonceStore in prod
    p2 = Permit2Handler(w3, nonce_store)

    permit = PermitParams(
        owner=owner,
        token=token_in,
        spender=router,
        amount=max_in,                 # ceiling allowance for this swap
        expiration=deadline + 3600,    # allow some headroom
        sig_deadline=deadline          # tight signer deadline
    )

    try:
        signed = await p2.sign(permit, owner_private_key_hex=priv_hex)
    except Exception as e:
        print(f"❌ Permit2 signing failed: {e}", file=sys.stderr)
        sys.exit(10)

    sig_hex = signed.get("signature")
    if not isinstance(sig_hex, str) or len(sig_hex) < 10:
        print("❌ Permit2 signing returned no signature.", file=sys.stderr)
        sys.exit(11)
    permit_single_msg = signed["typed_data"]["message"]

    # --- 2) Build permit tx (correct arg order: owner, permitSingle, signature) ---
    PERMIT2_PERMIT_ABI = [{
        "inputs": [
          {"internalType":"address","name":"owner","type":"address"},
          {"components": [
            {"components": [
              {"internalType":"address","name":"token","type":"address"},
              {"internalType":"uint160","name":"amount","type":"uint160"},
              {"internalType":"uint48","name":"expiration","type":"uint48"},
              {"internalType":"uint48","name":"nonce","type":"uint48"}],
             "internalType":"struct IAllowanceTransfer.PermitDetails","name":"details","type":"tuple"},
            {"internalType":"address","name":"spender","type":"address"},
            {"internalType":"uint256","name":"sigDeadline","type":"uint256"}],
           "internalType":"struct IAllowanceTransfer.PermitSingle","name":"permitSingle","type":"tuple"},
          {"internalType":"bytes","name":"signature","type":"bytes"}
        ],
        "name":"permit","outputs":[],"stateMutability":"nonpayable","type":"function"
    }]
    permit2 = w3.eth.contract(address=PERMIT2_ADDRESS, abi=PERMIT2_PERMIT_ABI)
    hx = sig_hex[2:] if sig_hex.startswith("0x") else sig_hex
    if len(hx) % 2 != 0: hx = "0"+hx
    sig_bytes = bytes.fromhex(hx)

    permit_tx = permit2.functions.permit(
        owner, permit_single_msg, sig_bytes
    ).build_transaction({
        "from": owner,
        "nonce": base_nonce,
    })
    permit_tx["chainId"] = chain_id
    permit_tx["maxPriorityFeePerGas"] = w3.to_wei(priority_gw, "gwei")
    permit_tx["maxFeePerGas"] = w3.to_wei(maxfee_gw, "gwei")
    try:
        permit_tx["gas"] = w3.eth.estimate_gas({**permit_tx, "from": owner})
    except Exception:
        permit_tx["gas"] = 200_000

    # --- 3) Build swap tx directly with router ABI (no ExactOutput* helpers) ---
    router_c = w3.eth.contract(address=router, abi=SWAPROUTER_ABI)
    params_tuple = {
        "tokenIn":   token_in,
        "tokenOut":  token_out,
        "fee":       int(pool_fee),
        "recipient": recipient,
        "deadline":  int(deadline),
        "amountOut": int(amount_out),
        "amountInMaximum": int(max_in),
        "sqrtPriceLimitX96": 0,
    }
    swap_tx = router_c.functions.exactOutputSingle(params_tuple).build_transaction({
        "from": owner,
        "nonce": base_nonce + 1,
        "value": 0,
        "gas": 600_000,
    })
    swap_tx["chainId"] = chain_id
    swap_tx["maxPriorityFeePerGas"] = w3.to_wei(priority_gw, "gwei")
    swap_tx["maxFeePerGas"] = w3.to_wei(maxfee_gw, "gwei")
    try:
        swap_tx["gas"] = w3.eth.estimate_gas({**swap_tx, "from": owner, "value": 0})
    except Exception:
        swap_tx["gas"] = 600_000  # conservative

    # --- 4) Sanity on amounts ---
    if amount_out < 1 or max_in < 1:
        print("bad amounts; refusing to submit", file=sys.stderr)
        sys.exit(3)

    # --- 5) Sign both ---
    signed_permit = Account.sign_transaction(permit_tx, priv_hex).rawTransaction.hex()
    signed_swap   = Account.sign_transaction(swap_tx,   priv_hex).rawTransaction.hex()

    # --- 6) Submit privately (sequential on Sepolia) ---
    def _send_raw(label: str, raw_hex: str):
        raw = bytes.fromhex(raw_hex[2:] if raw_hex.startswith("0x") else raw_hex)
        tx_hash = w3.eth.send_raw_transaction(raw)
        h = tx_hash.hex()
        print(f"✅ {label} sent: {h}")
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash)
        print(f"   {label} mined: status={rcpt.status} block={rcpt.blockNumber}")
        if rcpt.status != 1:
            print(f"❌ {label} reverted; aborting.", file=sys.stderr)
            sys.exit(6 if label == "permit" else 7)
        return h

    permit_hash = _send_raw("permit", signed_permit)
    swap_hash   = _send_raw("swap",   signed_swap)

    swap_rcpt = w3.eth.get_transaction_receipt(swap_hash)
    print("amountIn used:", _amount_in_used(swap_rcpt, token_in, owner))
    print("amountOut received:", _amount_out_received(swap_rcpt, token_out, recipient))


if __name__ == "__main__":
    asyncio.run(main())
