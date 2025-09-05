import os, requests
from eth_account import Account

class PrivateOrderflow:
    def __init__(self):
        eps = os.getenv("PRIVATE_ORDERFLOW_ENDPOINTS","")
        self.endpoints = [e.strip() for e in eps.split(",") if e.strip()]
        self._relay_signer = os.getenv("RELAY_SIGNER_KEY")  # optional (Flashbots Protect header)
        self.session = requests.Session()
        self.session.timeout = 10

    def _fb_header(self, raw_tx_hex: str) -> dict:
        if not self._relay_signer: return {}
        acct = Account.from_key(self._relay_signer)
        # Simplest acceptable header format for many relays:
        # <signer_address>:<signature_over_rawtx>
        sig = acct.sign_message(Account.defunct_hash_message(hexstr=raw_tx_hex.removeprefix("0x"))).signature.hex()
        return {"x-Flashbots-Signature": f"{acct.address}:{sig}"}

    def send(self, raw_tx_hex: str) -> dict:
        if not self.endpoints:
            raise RuntimeError("No PRIVATE_ORDERFLOW_ENDPOINTS configured")
        payload = {"jsonrpc":"2.0","id":1,"method":"eth_sendRawTransaction","params":[raw_tx_hex]}
        last_err = None
        for url in self.endpoints:
            headers = {}
            if "flashbots" in url or "protect" in url:
                headers.update(self._fb_header(raw_tx_hex))
            try:
                r = self.session.post(url, json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
                if "result" in data:
                    return {"ok": True, "relay": url, "txHash": data["result"]}
                last_err = data
            except Exception as e:
                last_err = e
        raise RuntimeError(f"All private relays failed: {last_err}")
