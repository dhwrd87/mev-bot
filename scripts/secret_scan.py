#!/usr/bin/env python3
"""Simple deterministic secret scan for obvious key material."""

from __future__ import annotations

import json
import re
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}
PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret[_-]?key|private[_-]?key)\s*[:=]\s*[^\s\"']+"),
    re.compile(r"0x[a-fA-F0-9]{64}"),
]


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.parts)
        if parts & SKIP_DIRS:
            continue
        yield path


def main() -> int:
    findings = []
    for path in iter_files(Path(".")):
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        for pat in PATTERNS:
            for match in pat.finditer(text):
                findings.append({"path": str(path), "match": match.group(0)[:80]})

    out = {"findings": findings, "count": len(findings)}
    report = Path("artifacts/proof/secret_scan.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({"count": len(findings), "report": str(report)}))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
