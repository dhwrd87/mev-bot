#!/usr/bin/env python3
import os, time, argparse, math
from web3 import Web3
from eth_account import Account
from bot.utils.keys import load_private_key
from eth_account import Account
from web3 import Web3, HTTPProvider
import sys, os

ROUTER = Web3.to_checksum_address("0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3")  # Uniswap V2 Router02 (Sepolia)
WETH   = Web3.to_checksum_address("0xFfF9976782d46cC05630D1F6eBAb18B2324d6B14")  # WETH (Sepolia)

ABI = [{
  "inputs":[
    {"internalType":"uint256","name":"amountOutMin","type":"uint256"},
    {"internalType":"address[]","name":"path","type":"address[]"},
    {"internalType":"address","name":"to","type":"address"},
    {"internalType":"uint256","name":"deadline","type":"uint256"}],
  "name":"swapExactETHForTokens",
  "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
  "stateMutability":"payable","type":"function"}]

ap = argparse.ArgumentParser()
ap.add_argument("--count", type=int, default=3)
ap.add_argument("--interval-sec", type=float, default=2.0)
ap.add_argument("--eth-value-wei", type=int, default=100000000000000)  # 0.0001 ETH
ap.add_argument("--gas-limit", type=int, default=250000)
args = ap.parse_args()

rpc = os.getenv("RPC_HTTP") or os.getenv("RPC_ENDPOINT_PRIMARY")
w3 = Web3(HTTPProvider(os.environ["RPC_HTTP"]))
pk = load_private_key()
if not pk:
    sys.exit("Missing key: set TRADER_PRIVATE_KEY or mount TRADER_PRIVATE_KEY_FILE")
acct = Account.from_key(pk)

pk = os.environ["PRIVATE_KEY"]
acct = Account.from_key(pk)
sender = acct.address

router = w3.eth.contract(address=ROUTER, abi=ABI)
nonce = w3.eth.get_transaction_count(sender)
chain_id = int(os.getenv("CHAIN_ID","11155111"))

print(f"Using {rpc} | sender {sender} | router {ROUTER} | chain {chain_id} | nonce {nonce}")

for i in range(args.count):
    try:
        base = max(w3.eth.gas_price, 1_000_000_000)   # >=1 gwei
        tip  = max(base // 2,     500_000_000)        # >=0.5 gwei
        max_fee = base * 2

        # silly path WETH->WETH is fine for detectors (methodID + router address)
        call = router.functions.swapExactETHForTokens(
            0, [WETH, WETH], sender, int(time.time()) + 600
        ).build_transaction({
            "from": sender,
            "value": args.eth_value_wei,
            "nonce": nonce + i,
            "chainId": chain_id,
            "gas": args.gas_limit,
            "maxFeePerGas": int(max_fee),
            "maxPriorityFeePerGas": int(tip),
        })

        stx = Account.sign_transaction(call, pk)
        h = w3.eth.send_raw_transaction(stx.rawTransaction).hex()
        print(f"[{i+1}/{args.count}] sent {h} (router call)")
        time.sleep(args.interval_sec)
    except Exception as e:
        print(f"[{i+1}/{args.count}] ERROR:", e)
        time.sleep(1.0)
