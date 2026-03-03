#!/usr/bin/env python3
import asyncio, json, os, websockets
EPS=[e.strip() for e in os.getenv("WS_ENDPOINTS","").split(",") if e.strip()]
SUB={"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newPendingTransactions"]}

async def check(ep):
    try:
        async with websockets.connect(ep, ping_interval=20) as ws:
            await ws.send(json.dumps(SUB))
            got=0
            for _ in range(50):
                try:
                    msg=await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                if '"result":"' in msg:
                    got+=1
            print(f"[OK ] {ep} -> pending {got}")
    except Exception as e:
        print(f"[ERR] {ep} -> {e}")

async def main():
    if not EPS:
        print("No WS_ENDPOINTS set"); return
    await asyncio.gather(*(check(ep) for ep in EPS))

if __name__ == "__main__":
    asyncio.run(main())
