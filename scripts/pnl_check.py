#!/usr/bin/env python3
"""Validate positive PnL from hunter proof artifact."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    path = Path("artifacts/hunter/hunter_e2e.json")
    if not path.exists():
        print("missing artifact:", path)
        return 1

    data = json.loads(path.read_text())
    pnl = float(data.get("pnl_usd", 0.0))
    backruns = int(data.get("successful_backruns", 0))
    ok = pnl > 0.0 and backruns >= 5
    print(json.dumps({"pnl_usd": pnl, "successful_backruns": backruns, "ok": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
