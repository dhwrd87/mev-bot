#!/usr/bin/env python3
import os, time, argparse
from web3 import Web3
from eth_account import Account
from bot.utils.keys import load_private_key
from eth_account import Account
from web3 import Web3, HTTPProvider
import sys, os

ap = argparse.ArgumentParser()
ap.add_argument("--count", type=int, default=8)
ap.add_argument("--interval-sec", type=float, default=1.5)
ap.add_argument("--value-wei", type=int, default=0)
ap.add_argument("--gas-limit", type=int, default=55000)
args = ap.parse_args()

rpc = os.getenv("RPC_HTTP") or os.getenv("RPC_ENDPOINT_PRIMARY") or "http://localhost:8545"
w3 = Web3(HTTPProvider(os.environ["RPC_HTTP"]))
pk = load_private_key()
if not pk:
    sys.exit("Missing key: set TRADER_PRIVATE_KEY or mount TRADER_PRIVATE_KEY_FILE")
acct = Account.from_key(pk)

pk = os.environ["PRIVATE_KEY"]              # REQUIRED (0x…)
acct = Account.from_key(pk)
sender = os.getenv("FROM") or acct.address
to = os.getenv("TO") or sender              # self-send default

chain_id = int(os.getenv("CHAIN_ID", "11155111"))  # Sepolia default
nonce = w3.eth.get_transaction_count(sender)

print(f"Using {rpc} | sender {sender} -> {to} | chain {chain_id} | starting nonce {nonce}")

for i in range(args.count):
    try:
        base = max(w3.eth.gas_price, 1_000_000_000)   # >= 1 gwei
        tip  = max(base // 2,     500_000_000)        # >= 0.5 gwei
        max_fee = base * 2

        tx = {
            "to": to,
            "value": args.value_wei,
            "gas": args.gas_limit,
            "maxFeePerGas": int(max_fee),
            "maxPriorityFeePerGas": int(tip),
            "nonce": nonce + i,
            "chainId": chain_id,
            "type": 2,
        }

        stx = Account.sign_transaction(tx, pk)
        h = w3.eth.send_raw_transaction(stx.rawTransaction).hex()
        mf = tx["maxFeePerGas"]; mp = tx["maxPriorityFeePerGas"]
        print(f"[{i+1}/{args.count}] sent {h}  fee={mf}  tip={mp}")
        time.sleep(args.interval_sec)
    except Exception as e:
        print(f"[{i+1}/{args.count}] ERROR:", e)
        time.sleep(1.0)
