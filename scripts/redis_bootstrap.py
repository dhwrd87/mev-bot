#!/usr/bin/env python3
import os, asyncio, sys
import redis.asyncio as redis
from redis.exceptions import ResponseError

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Guess sensible defaults, but honor your env if set
MEMPOOL_STREAM = os.getenv("CONSUMER_STREAM", os.getenv("MEMPOOL_STREAM", "mempool:pending"))
MEMPOOL_GROUP  = os.getenv("CONSUMER_GROUP",  os.getenv("MEMPOOL_GROUP",  "mempool-cg"))

OPP_STREAM     = os.getenv("OPPORTUNITY_STREAM", "opportunities")
OPP_GROUP      = os.getenv("OPPORTUNITY_GROUP",  "orchestrator-cg")

async def ensure_group(r, stream, group):
    try:
        # "$" = read only new messages, MKSTREAM creates stream if missing
        await r.xgroup_create(name=stream, groupname=group, id="$", mkstream=True)
        print(f"XGROUP CREATE {stream} {group} -> OK")
    except ResponseError as e:
        msg = str(e).lower()
        if "busygroup" in msg:
            print(f"XGROUP {stream}/{group} exists")
        else:
            print(f"XGROUP CREATE {stream}/{group} -> {e}")

async def main():
    r = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await ensure_group(r, MEMPOOL_STREAM, MEMPOOL_GROUP)
        await ensure_group(r, OPP_STREAM, OPP_GROUP)
        # Show info if stream now exists
        for s in [MEMPOOL_STREAM, OPP_STREAM]:
            try:
                info = await r.xinfo_groups(s)
                print(f"XINFO GROUPS {s}: {info}")
            except Exception as e:
                print(f"XINFO GROUPS {s} -> {e}")
    finally:
        await r.close()

if __name__ == "__main__":
    asyncio.run(main())
