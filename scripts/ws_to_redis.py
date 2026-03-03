#!/usr/bin/env python3
import os, asyncio, json, time, random
import websockets, aiohttp, redis.asyncio as redis

WS_EPS=[e.strip() for e in os.getenv("WS_ENDPOINTS","").split(",") if e.strip()]
RPC_HTTP=os.getenv("RPC_HTTP")
REDIS_URL=os.getenv("REDIS_URL","redis://redis:6379/0")
STREAM=os.getenv("REDIS_STREAM","mempool:pending:txs")

async def fetch_tx(session,h):
    p={"jsonrpc":"2.0","id":1,"method":"eth_getTransactionByHash","params":[h]}
    async with session.post(RPC_HTTP, json=p, timeout=10) as r:
        j=await r.json()
        return j.get("result")

async def loop_endpoint(ep, r):
    async with aiohttp.ClientSession() as http:
        while True:
            try:
                async with websockets.connect(ep, ping_interval=20) as ws:
                    await ws.send(json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newPendingTransactions"]}))
                    while True:
                        try:
                            msg=await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        try:
                            d=json.loads(msg)
                            h=d.get("params",{}).get("result")
                            if not (isinstance(h,str) and h.startswith("0x")):
                                continue
                            now = int(time.time())
                            fields={"hash": h, "tx": h, "ts": str(now), "ts_ms": str(now * 1000)}
                            tx=await fetch_tx(http,h)
                            if tx:
                                fields.update({k:str(tx.get(k,"")) for k in ("from","to","input","value","nonce","gas","gasPrice","maxFeePerGas","maxPriorityFeePerGas")})
                            await r.xadd(STREAM, fields, maxlen=50000, approximate=True)
                        except Exception:
                            continue
            except Exception:
                await asyncio.sleep(random.uniform(2,5))

async def main():
    assert WS_EPS, "WS_ENDPOINTS empty"
    assert RPC_HTTP, "RPC_HTTP missing"
    r=redis.from_url(REDIS_URL, decode_responses=True)
    await asyncio.gather(*(loop_endpoint(ep,r) for ep in WS_EPS))

if __name__ == "__main__":
    asyncio.run(main())
