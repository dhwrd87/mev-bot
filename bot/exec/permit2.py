# bot/exec/permit2.py
from __future__ import annotations
import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, Protocol, Tuple

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

# ---- Canonical Permit2 address (same on all chains) ----
# Ref: 0x docs: "Permit2 is deployed to 0x0000...c78BA3 across all chains."
PERMIT2_ADDRESS = Web3.to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3")

# Minimal ABI surface we need to read nonces (public mapping getter)
# mapping(address owner => mapping(address token => mapping(address spender => PackedAllowance))) public allowance;
# returns (uint160 amount, uint48 expiration, uint48 nonce)
PERMIT2_ABI = [{
    "constant": True, "inputs": [
        {"internalType": "address", "name": "owner", "type": "address"},
        {"internalType": "address", "name": "token", "type": "address"},
        {"internalType": "address", "name": "spender", "type": "address"},
    ],
    "name": "allowance", "outputs": [
        {"internalType": "uint160", "name": "amount", "type": "uint160"},
        {"internalType": "uint48", "name": "expiration", "type": "uint48"},
        {"internalType": "uint48", "name": "nonce", "type": "uint48"},
    ], "payable": False, "stateMutability": "view", "type": "function"
}]

# ---------------- Nonce persistence ----------------
class NonceStore(Protocol):
    async def get(self, owner: str, token: str, spender: str) -> Optional[int]: ...
    async def put(self, owner: str, token: str, spender: str, nonce: int) -> None: ...

class InMemoryNonceStore:
    def __init__(self):
        self._d: Dict[Tuple[str, str, str], int] = {}
    async def get(self, owner, token, spender): return self._d.get((owner, token, spender))
    async def put(self, owner, token, spender, nonce): self._d[(owner, token, spender)] = nonce

# (Optional) Postgres implementation stub — wire into your DAL
class PgNonceStore:
    def __init__(self, pool):  # asyncpg pool or sqlalchemy async session
        self.pool = pool
    async def get(self, owner, token, spender):
        q = """SELECT nonce FROM permit2_nonces WHERE owner=$1 AND token=$2 AND spender=$3"""
        async with self.pool.acquire() as con:
            row = await con.fetchrow(q, owner, token, spender)
            return None if row is None else int(row["nonce"])
    async def put(self, owner, token, spender, nonce):
        q = """INSERT INTO permit2_nonces(owner,token,spender,nonce,updated_at)
               VALUES($1,$2,$3,$4,now())
               ON CONFLICT(owner,token,spender) DO UPDATE SET nonce=EXCLUDED.nonce, updated_at=now()"""
        async with self.pool.acquire() as con:
            await con.execute(q, owner, token, spender, int(nonce))

# ---------------- Types & Handler ----------------
@dataclass
class PermitParams:
    owner: str
    token: str
    spender: str
    amount: int                # uint160
    expiration: int            # unix ts (uint48)
    sig_deadline: int          # unix ts

class Permit2Handler:
    """
    Builds and signs EIP-712 Permit2 'PermitSingle' (AllowanceTransfer.permit),
    manages nonces (persisted), and fetches on-chain nonce for safety.
    """
    def __init__(self, w3: Web3, nonce_store: NonceStore, verifying_contract: Optional[str] = None):
        self.w3 = w3
        self.nonce_store = nonce_store
        self.verifying_contract = Web3.to_checksum_address(verifying_contract or PERMIT2_ADDRESS)
        self.contract = self.w3.eth.contract(address=self.verifying_contract, abi=PERMIT2_ABI)
    
    def _chain_id(w3) -> int:
        # Prefer env if present (your .env.runtime already has CHAIN_ID=137)
        if os.getenv("CHAIN_ID"):
            try: return int(os.getenv("CHAIN_ID"))
            except ValueError: pass
        try: return int(w3.eth.chain_id)
        except Exception: return 137  # sensible default for tests

    async def get_effective_nonce(self, owner: str, token: str, spender: str) -> int:
        """
        Use max(persisted, on-chain) to be robust to restarts and external usage.
        (Permit2 nonces are incrementing per (owner, token, spender) tuple.)
        """
        persisted = await self.nonce_store.get(owner, token, spender)
        _amount, _exp, onchain = self.contract.functions.allowance(owner, token, spender).call()
        onchain = int(onchain)
        if persisted is None:
            return onchain
        return max(int(persisted), onchain)

    def _domain(self) -> Dict[str, Any]:
        # Domain per Permit2 spec: name 'Permit2', chainId, verifyingContract
        return {
            "name": "Permit2",
            "chainId": int(self.w3.eth.chain_id),
            "verifyingContract": self.verifying_contract,
        }

    @staticmethod
    def _types() -> Dict[str, Any]:
        # Typed data per Uniswap docs: PermitSingle + PermitDetails
        # (uint160, uint48 sizes are important)
        return {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "PermitSingle": [
                {"name": "details", "type": "PermitDetails"},
                {"name": "spender", "type": "address"},
                {"name": "sigDeadline", "type": "uint256"},
            ],
            "PermitDetails": [
                {"name": "token", "type": "address"},
                {"name": "amount", "type": "uint160"},
                {"name": "expiration", "type": "uint48"},
                {"name": "nonce", "type": "uint48"},
            ],
        }

    def _message(self, p: PermitParams, nonce: int) -> Dict[str, Any]:
        # clamp to types' max values just in case callers pass big ints
        amount = min(int(p.amount), (1 << 160) - 1)
        expiration = min(int(p.expiration), (1 << 48) - 1)
        nonce = min(int(nonce), (1 << 48) - 1)

        return {
            "details": {
                "token": Web3.to_checksum_address(p.token),
                "amount": amount,
                "expiration": expiration,
                "nonce": nonce,
            },
            "spender": Web3.to_checksum_address(p.spender),
            "sigDeadline": int(p.sig_deadline),
        }

    async def build_typed_data(self, p: PermitParams) -> Dict[str, Any]:
        nonce = await self.get_effective_nonce(p.owner, p.token, p.spender)
        return {
            "types": self._types(),
            "domain": self._domain(),
            "primaryType": "PermitSingle",
            "message": self._message(p, nonce),
        }

    async def sign(self, p: PermitParams, owner_private_key_hex: str) -> Dict[str, Any]:
        """
        Returns dict with {typed_data, signature, nonce_used}.
        Caller should include (permitSingle, signature) when calling Permit2.permit().
        """
        td = await self.build_typed_data(p)
        msg = encode_typed_data(full_message=td)
        signed = Account.sign_message(msg, private_key=owner_private_key_hex)

        # Persist the nonce we used (increment for the next call).
        # Permit2 will consume the exact nonce we signed.
        current = td["message"]["details"]["nonce"]
        await self.nonce_store.put(p.owner, p.token, p.spender, int(current + 1))

        return {
            "typed_data": td,
            "signature": signed.signature.hex(),
            "nonce_used": int(current),
        }
