# bot/exec/permit2_nonce_store.py
import json, threading
from pathlib import Path
from bot.exec.permit2 import Permit2Handler
from bot.exec.permit2_nonce_store import FileNonceStore
permit2 = Permit2Handler(w3, FileNonceStore())

class FileNonceStore:
    def __init__(self, path=".data/permit2_nonces.json"):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _key(self, owner, token, spender):  # or whatever granularity you use
        return f"{owner.lower()}|{token.lower()}|{spender.lower()}"

    def get(self, owner, token, spender):
        with self._lock:
            return int(self._data.get(self._key(owner, token, spender), 0))

    def bump(self, owner, token, spender):
        with self._lock:
            k = self._key(owner, token, spender)
            self._data[k] = int(self._data.get(k, 0)) + 1
            self.path.write_text(json.dumps(self._data))
            return int(self._data[k])
