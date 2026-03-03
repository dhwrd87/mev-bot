#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.core.config_loader import load_chain_profile_dict


def main() -> int:
    ap = argparse.ArgumentParser(description="Print resolved chain profile JSON")
    ap.add_argument("profile", nargs="?", default=None, help="Profile name (defaults to CHAIN_PROFILE)")
    args = ap.parse_args()

    resolved = load_chain_profile_dict(args.profile)
    print(json.dumps(resolved, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
