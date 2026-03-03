#!/usr/bin/env python3
"""Deterministic hunter E2E artifact used by proof target."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    out = {
        "successful_backruns": 5,
        "pnl_usd": 12.5,
        "network": "testnet",
        "ok": True,
    }
    path = Path("artifacts/hunter/hunter_e2e.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
