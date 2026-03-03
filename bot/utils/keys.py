# bot/utils/keys.py
import os, pathlib

def load_private_key(
    env_names=("TRADER_PRIVATE_KEY", "PRIVATE_KEY"),
    file_env="TRADER_PRIVATE_KEY_FILE",
) -> str | None:
    """Return hex '0x…' private key from env or a Docker secret file."""
    pk = next((os.getenv(n) for n in env_names if os.getenv(n)), None)
    if not pk:
        pk_file = os.getenv(file_env)
        if pk_file and pathlib.Path(pk_file).exists():
            pk = pathlib.Path(pk_file).read_text().strip()
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk
    return pk
