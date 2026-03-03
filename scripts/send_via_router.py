import os, asyncio, time
from web3 import Web3
from eth_account import Account
from bot.exec.orderflow import PrivateOrderflowRouter

async def main():
    pk = os.getenv("TEST_PK")  # put an Anvil funded key here
    if not pk: raise SystemExit("Set TEST_PK to an Anvil private key")

    w3 = Web3(Web3.HTTPProvider(os.getenv("RPC_ENDPOINT_PRIMARY_ETH")))
    acct = Account.from_key(pk)

    # Build a trivial 0-value tx to self (min gas)
    nonce = w3.eth.get_transaction_count(acct.address)
    tx = {
        "to": acct.address,
        "value": 0,
        "nonce": nonce,
        "gas": 21000,
        "maxFeePerGas": w3.to_wei(2, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
        "chainId": 1,  # matches Anvil chain-id in this test
        "type": 2,
    }
    signed = Account.sign_transaction(tx, pk).rawTransaction.hex()

    router = PrivateOrderflowRouter.from_env()
    traits = {"value_usd": 10_000, "high_slippage": True, "token_new": True, "detected_snipers": 1}

    res = await router.submit(signed_raw_tx=signed, chain="ethereum", traits=traits, timeout=1.5)
    print("ROUTER_RES:", res)

asyncio.run(main())
