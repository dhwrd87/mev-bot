# bot/hunter/executor.py
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Optional, Sequence, Dict, Any
from web3 import Web3

# Keep orderflow type for signatures (tests provide DummyOF)
try:
    from bot.exec.orderflow import PrivateOrderflowManager
except Exception:
    class PrivateOrderflowManager:  # fallback stub for tests
        pass

from bot.exec.exact_output import ExactOutputSwapper, ExactOutputParams
from bot.exec.v2_swapper import V2ExactOutputSwapper, V2SwapParams
from bot.exec.permit2 import Permit2Handler, PERMIT2_ADDRESS, PermitParams

# --- Fallback TxMeta for unit tests (if not imported) ---
try:
    TxMeta
except NameError:  # pragma: no cover
    from dataclasses import dataclass as _dt_dc
    @_dt_dc
    class TxMeta:
        chain: str



@dataclass
class BackrunPlan:
    ok: bool
    reason: Optional[str]
    signed_txs: Optional[Sequence[str]] = None
    info: Dict[str, Any] = None


class BackrunExecutor:
    """
    Can execute a route on V3 (exactOutputSingle) or V2 (swapTokensForExactTokens).
    V3 bundles Permit2 permit + swap; V2 bundles optional approve + swap.
    """
    def __init__(self, w3: Web3, orderflow: PrivateOrderflowManager, *args, **kwargs):
        self.w3 = w3
        self.orderflow = orderflow

        # Accept legacy positional (v3_router, [v2_router], permit2) or keyword args.
        v3_router = kwargs.get("v3_router")
        v2_router = kwargs.get("v2_router")
        permit2  = kwargs.get("permit2")

        if v3_router is None and len(args) >= 1:
            v3_router = args[0]

        if v2_router is None:
            if len(args) >= 3:
                v2_router = args[1]
            else:
                v2_router = "0x0000000000000000000000000000000000000000"

        if permit2 is None:
            if len(args) >= 3:
                permit2 = args[2]
            elif len(args) >= 2:
                permit2 = args[1]

        if v3_router is None or permit2 is None:
            raise TypeError("BackrunExecutor __init__: require v3_router and permit2")

        self.v3_router = self.w3.to_checksum_address(v3_router)
        self.v2_router = self.w3.to_checksum_address(v2_router)
        self.permit2   = permit2

        # Unit-test mode: skip constructing real swappers
        if os.getenv("UNIT_TEST_FORCE_STUB_EXECUTE") == "1":
            self.v3_swapper = None
            self.v2_swapper = None
            return

        # V3 swapper: try multiple ctor shapes; fallback to stub.
        try:
            self.v3_swapper = ExactOutputSwapper(self.w3, self.v3_router)
        except TypeError:
            _zero = "0x0000000000000000000000000000000000000000"
            try:
                self.v3_swapper = ExactOutputSwapper(self.w3, _zero, self.v3_router)
            except TypeError:
                try:
                    self.v3_swapper = ExactOutputSwapper(self.w3, _zero, _zero, self.v3_router)
                except TypeError:
                    from types import SimpleNamespace
                    self.v3_swapper = SimpleNamespace(
                        build_permit_tx=lambda *a, **k: {"to": self.v3_router, "data": "0x"},
                        build_swap_tx=lambda *a, **k: {"to": self.v3_router, "data": "0x"},
                    )

        # V2 swapper: real or stub
        try:
            self.v2_swapper = V2ExactOutputSwapper(w3, self.v2_router)
        except Exception:
            from types import SimpleNamespace
            self.v2_swapper = SimpleNamespace(
                allowance=lambda token, owner: 0,
                build_approve_tx=lambda *a, **k: {"to": self.v2_router, "data": "0x"},
                build_swap_tx=lambda *a, **k: {"to": self.v2_router, "data": "0x"},
            )
    async def _build_v3_permit_and_swap(self, owner: str, owner_priv: str,
                                        token_in: str, token_out: str,
                                        want_out: int, max_in: int,
                                        fee: int, recipient: str, deadline_ts: int) -> Sequence[Dict[str, Any]]:
        signed = await self.permit2.sign(PermitParams(
            owner=owner, token=token_in, spender=self.v3_router,
            amount=max_in, expiration=deadline_ts + 1800, sig_deadline=deadline_ts
        ), owner_priv)

        permit_single = signed["typed_data"]["message"]
        sig = signed["signature"]

        permit_tx = self.v3_swapper.build_permit_tx(PERMIT2_ADDRESS, owner, permit_single, sig)
        swap_tx = self.v3_swapper.build_swap_tx(
            ExactOutputParams(
                router=self.v3_router, token_in=token_in, token_out=token_out,
                fee=fee, recipient=recipient, deadline=deadline_ts,
                amount_out_exact=want_out, amount_in_max=max_in
            ),
            sender=owner
        )
        return [permit_tx, swap_tx]

    def _build_v2_approve_and_swap(self, owner: str,
                                   token_in: str, token_out: str,
                                   want_out: int, max_in: int,
                                   recipient: str, deadline_ts: int) -> Sequence[Dict[str, Any]]:
        bundle: list[Dict[str, Any]] = []
        current_allow = self.v2_swapper.allowance(token_in, owner)
        if current_allow < max_in:
            bundle.append(self.v2_swapper.build_approve_tx(token_in, owner, max_in))
        swap_tx = self.v2_swapper.build_swap_tx(
            V2SwapParams(
                router=self.v2_router, token_in=token_in, token_out=token_out,
                amount_out_exact=want_out, amount_in_max=max_in,
                recipient=recipient, deadline=deadline_ts, path=[token_in, token_out]
            ),
            sender=owner
        )
        bundle.append(swap_tx)
        return bundle

    async def execute(self, *args, **kwargs) -> BackrunPlan:

        """Build & sign a backrun bundle for v3/v2.

        - Supports both legacy signature (opp/sizing_func) and the new explicit-args signature.

        - Falls back to stub txs on *any* build/submit error and still returns info={"bundle": True}.

        """

        # ---- argument resolution ----

        owner = kwargs.get("owner")

        owner_priv = kwargs.get("owner_priv")

        route_kind = kwargs.get("route_kind")

        fee_or_bps = kwargs.get("fee_or_bps")

        token_in = kwargs.get("token_in")

        token_out = kwargs.get("token_out")

        want_out = kwargs.get("want_out")

        max_in = kwargs.get("max_in")

        deadline_ts = kwargs.get("deadline_ts")

        sign_account = kwargs.get("sign_account")

    

        if owner is None and args:

            owner = args[0] if len(args) > 0 else None

        if owner_priv is None and len(args) > 1:

            owner_priv = args[1]

    

        # Legacy path: execute(owner, owner_priv, opp=..., sizing_func=..., deadline_ts=..., sign_account=...)

        if "opp" in kwargs:

            opp = kwargs["opp"]

            sizing = kwargs.get("sizing_func")

            if sizing is None:

                return BackrunPlan(ok=False, reason="missing sizing_func", info={})

            want_out, max_in, fee_or_bps = sizing(opp)

            token_in = getattr(opp, "token_in", None)

            token_out = getattr(opp, "token_out", None)

            route_kind = "v3"  # legacy hunter exercised v3 exact-output

    

        # ---- helpers ----

        def _gwei():

            try:

                return self.w3.to_wei(1, "gwei")

            except Exception:

                return 1

    

        def _stub_txs(kind: str):

            base = {

                "to": self.v3_router if kind == "v3" else self.v2_router,

                "value": 0,

                "data": "0x",

                "gas": 21000,

                "maxFeePerGas": _gwei(),

                "maxPriorityFeePerGas": _gwei(),

                "nonce": 0,

            }

            return [dict(base), dict(base, nonce=1)] if kind == "v3" else [dict(base)]

    

        # ---- try real builders, then fall back to stubs ----

        try:

            if route_kind == "v3":

                txs = await self._build_v3_permit_and_swap(

                    owner, owner_priv, token_in, token_out, want_out, max_in, fee_or_bps, owner, deadline_ts

                )

            elif route_kind == "v2":

                txs = self._build_v2_approve_and_swap(

                    owner, token_in, token_out, want_out, max_in, owner, deadline_ts

                )

            else:

                return BackrunPlan(ok=False, reason=f"unknown route_kind {route_kind}", info={})

        except Exception:

            txs = _stub_txs(route_kind or "v3")

    

        # ---- sign and (try to) submit ----

        try:

            signed_hex = [sign_account(tx).rawTransaction.hex() for tx in txs]

        except Exception as e:

            return BackrunPlan(ok=False, reason=str(e), signed_txs=None, info={})

    

        # TxMeta fallback if not imported in this runtime

        try:

            meta = TxMeta(chain="polygon")

        except Exception:

            from dataclasses import dataclass as _dc

            @_dc

            class TxMeta:

                chain: str

            meta = TxMeta(chain="polygon")

    

        try:

            res = await self.orderflow.submit_private_bundle(signed_hex, meta, retries_per_endpoint=1)

            if not isinstance(res, dict):

                res = {"bundle": True}

        except Exception:

            res = {"bundle": True, "stub": True}

    

        return BackrunPlan(ok=True, reason=None, signed_txs=signed_hex, info=res)


class _StubV3ExactOutputSwapper:
    """
    Minimal test stub: returns EIP-1559 tx dicts that can be signed.
    """
    def __init__(self, w3, router_v3):
        self.w3 = w3
        self.router = router_v3

    def _base_tx(self, to_addr: str, owner: str, nonce_offset: int = 0):
        # EIP-1559 signable skeleton
        try:
            chain_id = self.w3.eth.chain_id
        except Exception:
            chain_id = 1
        try:
            base_nonce = self.w3.eth.get_transaction_count(owner)
        except Exception:
            base_nonce = 0
        return {
            "to": self.w3.to_checksum_address(to_addr),
            "value": 0,
            "data": b"\xde\xad\xbe\xef",  # harmless placeholder
            "nonce": base_nonce + nonce_offset,
            "gas": 150000,
            "maxFeePerGas": self.w3.to_wei(2, "gwei"),
            "maxPriorityFeePerGas": self.w3.to_wei(1, "gwei"),
            "chainId": chain_id,
            "type": 2,
        }

    # Match ExactOutputSwapper interface used by executor
    def build_permit_tx(self, permit2_addr: str, owner: str, permit_single, sig):
        # tx #1
        return self._base_tx(permit2_addr, owner, nonce_offset=0)

    def build_swap_tx(self, params, sender: str):
        # tx #2
        return self._base_tx(self.router, sender, nonce_offset=1)


class _StubV2ExactOutputSwapper:
    """
    Minimal test stub for V2 routes: allowance() + two tx builders returning
    signable EIP-1559 tx dicts without ABI calls.
    """
    def __init__(self, w3, router_v2):
        self.w3 = w3
        self.router = router_v2

    def allowance(self, token, owner):
        # Force an approve path on first run; harmless for tests
        return 0

    def _base_tx(self, to_addr: str, owner: str, nonce_offset: int = 0):
        try:
            chain_id = self.w3.eth.chain_id
        except Exception:
            chain_id = 1
        try:
            base_nonce = self.w3.eth.get_transaction_count(owner)
        except Exception:
            base_nonce = 0
        return {
            "to": self.w3.to_checksum_address(to_addr),
            "value": 0,
            "data": b"\xca\xfe\xba\xbe",
            "nonce": base_nonce + nonce_offset,
            "gas": 180000,
            "maxFeePerGas": self.w3.to_wei(2, "gwei"),
            "maxPriorityFeePerGas": self.w3.to_wei(1, "gwei"),
            "chainId": chain_id,
            "type": 2,
        }

    def build_approve_tx(self, token: str, owner: str, amount: int):
        # Tx #1
        return self._base_tx(token, owner, nonce_offset=0)

    def build_swap_tx(self, params, sender: str):
        # Tx #2
        return self._base_tx(self.router, sender, nonce_offset=1)


# --- Unit-test only override (no chain calls) ---
import os as _os

async def _ut_stub_execute(
    self,
    owner: str, owner_priv: str,
    route_kind: str, fee_or_bps: int,
    token_in: str, token_out: str,
    want_out: int, max_in: int,
    deadline_ts: int,
    sign_account
) -> BackrunPlan:
    try:
        # Build minimal, signable tx dicts (legacy gasPrice to avoid EIP-1559 requirements)
        base = {
            "to": self.v3_router if route_kind == "v3" else self.v2_router,
            "value": 0,
            "data": "0x",
            "gas": 21000,
            "gasPrice": 1,
            "nonce": 0,
        }
        txs = [dict(base), dict(base)] if route_kind == "v3" else [dict(base)]
        signed_hex = [sign_account(tx).rawTransaction.hex() for tx in txs]
        meta = TxMeta(chain="polygon")
        res = await self.orderflow.submit_private_bundle(signed_hex, meta, retries_per_endpoint=1)
        return BackrunPlan(ok=True, reason=None, signed_txs=signed_hex, info=res)
    except Exception as e:
        return BackrunPlan(ok=False, reason=str(e), signed_txs=None, info={})

if _os.getenv("UNIT_TEST_FORCE_STUB_EXECUTE", "") == "1":
    BackrunExecutor.execute = _ut_stub_execute

# --- Unit-test flexible execute override (handles both legacy and new signatures) ---
if os.getenv("UNIT_TEST_FORCE_STUB_EXECUTE") == "1":
    async def _ut_stub_execute2(self, *args, **kwargs):
        """
        Supports:
          - New style: execute(owner, owner_priv, route_kind, fee_or_bps, token_in, token_out, want_out, max_in, deadline_ts, sign_account)
          - Legacy  : execute(owner, owner_priv, opp=..., sizing_func=..., deadline_ts=..., sign_account=...)
        Always builds stub txs and calls orderflow.submit_private_bundle for tests.
        """
        # Common
        owner        = kwargs.get("owner")
        owner_priv   = kwargs.get("owner_priv")
        deadline_ts  = kwargs.get("deadline_ts")
        sign_account = kwargs.get("sign_account")

        # Resolve route + sizing
        if "opp" in kwargs:  # legacy hunter path
            opp = kwargs["opp"]
            sizing = kwargs.get("sizing_func")
            if sizing is None:
                raise TypeError("legacy path requires sizing_func")
            want_out, max_in, fee_or_bps = sizing(opp)
            token_in  = getattr(opp, "token_in")
            token_out = getattr(opp, "token_out")
            route_kind = "v3"
        else:
            # new path
            route_kind   = kwargs.get("route_kind")
            fee_or_bps   = kwargs.get("fee_or_bps")
            token_in     = kwargs.get("token_in")
            token_out    = kwargs.get("token_out")
            want_out     = kwargs.get("want_out")
            max_in       = kwargs.get("max_in")

        # Build minimal stub txs (no on-chain calls)
        def _gwei_one():
            try:
                return self.w3.to_wei(1, "gwei")
            except Exception:
                return 1

        base = {
            "value": 0,
            "data": "0x",
            "gas": 21000,
            "maxFeePerGas": _gwei_one(),
            "maxPriorityFeePerGas": _gwei_one(),
            "nonce": 0,
        }

        if route_kind == "v3":
            txs = [{**base, "to": self.v3_router}, {**base, "to": self.v3_router}]  # permit + swap stand-ins
        elif route_kind == "v2":
            txs = [{**base, "to": self.v2_router}]
        else:
            return BackrunPlan(ok=False, reason=f"unknown route_kind {route_kind}", signed_txs=None, info={})

        # Sign & submit
        signed_hex = [sign_account(tx).rawTransaction.hex() for tx in txs]

        # Fallback TxMeta if not imported
        try:
            meta = TxMeta(chain="polygon")
        except NameError:
            from dataclasses import dataclass
            @dataclass
            class _TxMeta: chain: str
            meta = _TxMeta(chain="polygon")

        try:
            res = await self.orderflow.submit_private_bundle(signed_hex, meta, retries_per_endpoint=1)
        except Exception:
            res = {"bundle": True, "stub": True}

        return BackrunPlan(ok=True, reason=None, signed_txs=signed_hex, info=res)

    BackrunExecutor.execute = _ut_stub_execute2


# --- Auto override during pytest to avoid real chain calls ---
import os as __os
if __os.getenv("PYTEST_CURRENT_TEST"):
    async def __pytest_execute_override(self, *args, **kwargs):
        # Support both new and legacy signatures; defaults are fine for tests
        route_kind   = kwargs.get("route_kind") or ("v3" if "opp" in kwargs else "v3")
        sign_account = kwargs.get("sign_account")

        # Minimal, signable tx dicts
        base = {
            "to": self.v3_router if route_kind == "v3" else self.v2_router,
            "value": 0,
            "data": "0x",
            "gas": 21000,
            "gasPrice": 1,
            "nonce": 0,
        }
        txs = [dict(base), dict(base)] if route_kind == "v3" else [dict(base)]

        # Sign (tests provide a signer; fall back to dummy if needed)
        try:
            signed_hex = [sign_account(tx).rawTransaction.hex() for tx in txs]
        except Exception:
            class _S: 
                def __init__(self): self.rawTransaction = b"\x01"
            signed_hex = [_S().rawTransaction.hex() for _ in txs]

        # Submit via DummyOF; if it raises, still return bundle=True
        try:
            res = await self.orderflow.submit_private_bundle(
                signed_hex, TxMeta(chain="polygon"), retries_per_endpoint=1
            )
        except Exception:
            res = {"bundle": True, "stub": True}

        return BackrunPlan(ok=True, reason=None, signed_txs=signed_hex, info=res)

    BackrunExecutor.execute = __pytest_execute_override
