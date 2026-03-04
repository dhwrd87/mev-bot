from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate_env.py"


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _run(env_file: Path, ref_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--env-file", str(env_file), "--reference", str(ref_file)],
        text=True,
        capture_output=True,
        cwd=str(ROOT),
    )


def _reference_surface() -> str:
    return """\
CHAIN=sepolia
CHAIN_ID=11155111
CHAIN_FAMILY=evm
MODE=development
POSTGRES_DB=mev_bot
POSTGRES_USER=mev_user
POSTGRES_PASSWORD=change_me
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
USE_ALCHEMY=false
ALCHEMY_KEY=
USE_INFURA=false
INFURA_KEY=
USE_PRIVATE_RPC=false
RPC_HTTP_EXTRA=
WS_ENDPOINTS_EXTRA=
PRIVATE_KEY_ENCRYPTED=
KEY_PASSWORD=
KEY_PASSWORD_FILE=
TRADER_PRIVATE_KEY=
TRADER_PRIVATE_KEY_FILE=
PRIVATE_KEY=
PRIVATE_KEY_ENCRYPTED_FILE=
"""


def test_validate_env_ok_development(tmp_path: Path) -> None:
    ref = tmp_path / ".env.example"
    env = tmp_path / ".env.runtime"
    _write(ref, _reference_surface())
    _write(
        env,
        """\
CHAIN=sepolia
CHAIN_ID=11155111
CHAIN_FAMILY=evm
MODE=development
POSTGRES_DB=mev_bot
POSTGRES_USER=mev_user
POSTGRES_PASSWORD=change_me
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
USE_ALCHEMY=false
USE_INFURA=false
USE_PRIVATE_RPC=false
""",
    )
    proc = _run(env, ref)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ENV_OK" in proc.stdout


def test_validate_env_fails_on_chain_family_mismatch(tmp_path: Path) -> None:
    ref = tmp_path / ".env.example"
    env = tmp_path / ".env.runtime"
    _write(ref, _reference_surface())
    _write(
        env,
        """\
CHAIN=sepolia
CHAIN_ID=11155111
CHAIN_FAMILY=sol
MODE=development
POSTGRES_DB=mev_bot
POSTGRES_USER=mev_user
POSTGRES_PASSWORD=change_me
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
""",
    )
    proc = _run(env, ref)
    assert proc.returncode != 0
    assert "CHAIN_FAMILY mismatch" in proc.stdout


def test_validate_env_fails_when_alchemy_enabled_without_key(tmp_path: Path) -> None:
    ref = tmp_path / ".env.example"
    env = tmp_path / ".env.runtime"
    _write(ref, _reference_surface())
    _write(
        env,
        """\
CHAIN=sepolia
CHAIN_ID=11155111
CHAIN_FAMILY=evm
MODE=paper
POSTGRES_DB=mev_bot
POSTGRES_USER=mev_user
POSTGRES_PASSWORD=change_me
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
USE_ALCHEMY=true
ALCHEMY_KEY=
""",
    )
    proc = _run(env, ref)
    assert proc.returncode != 0
    assert "USE_ALCHEMY=true requires ALCHEMY_KEY" in proc.stdout


def test_validate_env_live_requires_secret(tmp_path: Path) -> None:
    ref = tmp_path / ".env.example"
    env = tmp_path / ".env.runtime"
    _write(ref, _reference_surface())
    _write(
        env,
        """\
CHAIN=sepolia
CHAIN_ID=11155111
CHAIN_FAMILY=evm
MODE=live
POSTGRES_DB=mev_bot
POSTGRES_USER=mev_user
POSTGRES_PASSWORD=change_me
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
USE_ALCHEMY=false
USE_INFURA=false
""",
    )
    proc = _run(env, ref)
    assert proc.returncode != 0
    assert "MODE=live requires a signing secret" in proc.stdout

