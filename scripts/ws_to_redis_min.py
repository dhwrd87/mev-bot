import os, asyncio, json, time, random
import websockets
import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
STREAM    = os.getenv("REDIS_STREAM", "mempool:pending:txs")
WS_EPS    = [e.strip() for e in os.getenv("WS_ENDPOINTS","wss://polygon-bor.publicnode.com").split(",") if e.strip()]

async def run_ep(ep, r):
    while True:
        try:
            async with websockets.connect(ep, ping_interval=20) as ws:
                await ws.send(json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newPendingTransactions"]}))
                while True:
                    msg = json.loads(await ws.recv())
                    txh = msg.get("params",{}).get("result")
                    if isinstance(txh, str) and txh.startswith("0x") and len(txh) == 66:
                        await r.xadd(STREAM, {"hash": txh, "ts": str(int(time.time()))})
        except Exception:
            await asyncio.sleep(random.uniform(1,4))

async def main():
    r = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    await asyncio.gather(*(run_ep(ep, r) for ep in WS_EPS))

if __name__ == "__main__":
    asyncio.run(main())
