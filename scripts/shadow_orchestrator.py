#!/usr/bin/env python3
import asyncio, aiohttp, time, argparse, re, os, json, sys

METRICS_URL = os.getenv("METRICS_URL", "http://localhost:8000/metrics")
WANTED = {
    "mevbot_orchestrator_decisions_total": "decisions",
    "mevbot_risk_blocks_total": "risk_blocks",
    "mevbot_backrun_candidates_total": "candidates",
    "mevbot_stealth_decisions_total": "stealth_decisions",
    "mevbot_relay_attempts_total": "relay_attempts",
    "mevbot_relay_success_total": "relay_success",
    "mevbot_sim_bundle_total": "sim_bundles",
    "mevbot_sim_bundle_success_total": "sim_success",
}
LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$")

async def fetch_raw(session):
    async with session.get(METRICS_URL) as r:
        return await r.text()

def parse(text):
    vals = {}
    for raw in text.splitlines():
        if not raw or raw.startswith("#"):
            continue
        m = LINE.match(raw.strip())
        if not m:
            continue
        n, v = m.group(1), float(m.group(2))
        if n in WANTED:
            vals[n] = vals.get(n, 0.0) + v
    return vals

def delta(a, b, key):
    return max(0.0, b.get(key, 0.0) - a.get(key, 0.0))

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-min", type=float, default=2)
    ap.add_argument("--interval-sec", type=float, default=10)
    ap.add_argument("--min-opps", type=int, default=0)
    args = ap.parse_args()

    async with aiohttp.ClientSession() as s:
        start = parse(await fetch_raw(s))
        print(f"⏱  Shadow window: {args.duration_min} min | poll {args.interval_sec}s | source {METRICS_URL}")
        end_time = time.time() + args.duration_min * 60
        while time.time() < end_time:
            await asyncio.sleep(args.interval_sec)
            mid = parse(await fetch_raw(s))
            print("… decisions+{d}  blocks+{b}  candidates+{c}  sim+{sim}".format(
                d=int(delta(start, mid, "mevbot_orchestrator_decisions_total")),
                b=int(delta(start, mid, "mevbot_risk_blocks_total")),
                c=int(delta(start, mid, "mevbot_backrun_candidates_total")),
                sim=int(delta(start, mid, "mevbot_sim_bundle_total")),
            ))
        endv = parse(await fetch_raw(s))

    summary = {
        "duration_min": args.duration_min,
        "decisions": delta(start, endv, "mevbot_orchestrator_decisions_total"),
        "risk_blocks": delta(start, endv, "mevbot_risk_blocks_total"),
        "candidates": delta(start, endv, "mevbot_backrun_candidates_total"),
        "stealth_decisions": delta(start, endv, "mevbot_stealth_decisions_total"),
        "relay_attempts": delta(start, endv, "mevbot_relay_attempts_total"),
        "relay_success": delta(start, endv, "mevbot_relay_success_total"),
        "sim_bundles": delta(start, endv, "mevbot_sim_bundle_total"),
        "sim_success": delta(start, endv, "mevbot_sim_bundle_success_total"),
    }
    print("\\n== Shadow summary ==")
    print(json.dumps(summary, indent=2))

    ok = any(summary[k] > 0 for k in ("decisions", "candidates", "sim_bundles"))
    if ok:
        print("🎉 Shadow orchestrator PASS"); sys.exit(0)
    else:
        print("❌ No orchestrator activity observed."); sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
