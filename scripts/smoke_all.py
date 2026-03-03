#!/usr/bin/env python3
import subprocess, sys

def run(cmd):
    print(f"\n$ {cmd}")
    r = subprocess.run(cmd, shell=True)
    if r.returncode != 0:
        sys.exit(r.returncode)

run("python3 scripts/smoke_stealth.py --network sepolia --stub-relays")
run("python3 scripts/smoke_orchestrator.py")
print("\n🎉 ALL SMOKES PASS")
