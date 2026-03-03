# bot/security/key_manager.py
from bot.utils.keys import load_private_key

class SecureKeyManager:
    ...
    async def sign_transaction(self, tx: dict, chain: str) -> str:
        pk = load_private_key()  # falls back to the secret file
        if not pk:
            raise SecurityError("No private key provided")
        signed = Account.sign_transaction(tx, pk)
        return signed.rawTransaction.hex()
