# inside your StealthStrategy.execute_stealth_swap(...)
from web3 import Web3
from bot.exec.exact_output import ExactOutputSwapper, ExactOutputParams
from bot.permit2.handler import Permit2Handler

async def execute_stealth_swap(self, params: dict):
    """
    Expected keys in params:
      token_in, token_out, amount_out_exact, max_amount_in, pool_fee, recipient,
      w3 (Web3), router (optional), from_addr (EOA or smart wallet),
      quote_amount_in (optional), max_slippage_bps (optional)
    """
    w3: Web3 = params["w3"]
    router = params.get("router") or os.getenv("UNISWAP_V3_ROUTER")
    swapper = ExactOutputSwapper(w3, router=router)

    # 1) Off-chain Permit2 signature (we assume your on-chain multicall will include Permit2.permit)
    p2 = Permit2Handler()
    permit = await p2.generate_signature(
        token=params["token_in"],
        amount=params["max_amount_in"],     # allow router to pull up to max
        spender=router,
        expiration_s=3600,
        min_deadline_s=300,
    )

    # 2) Build exactOutputSingle calldata
    eo = ExactOutputParams(
        token_in=params["token_in"],
        token_out=params["token_out"],
        fee=int(params["pool_fee"]),
        recipient=params["recipient"],
        amount_out=int(params["amount_out_exact"]),
        max_amount_in=int(params["max_amount_in"]),
        from_addr=params.get("from_addr"),
        quote_amount_in=params.get("quote_amount_in"),
        max_slippage_bps=params.get("max_slippage_bps"),
    )
    to, data, value = swapper.build_calldata(eo)

    # 3) Simulate (eth_call) before private submit
    ok, err = swapper.simulate(eo)
    if not ok:
        # record metric + raise to caller
        from bot.telemetry.metrics import DETECT_LATENCY_MS  # or a dedicated counter for reverts
        # you can have: exact_output_revert_total.inc()
        raise RuntimeError(f"exactOutputSingle simulation failed: {err}")

    # 4) Submit privately.
    # If you have a multicall/universal router that can combine Permit2.permit + swap, use it.
    # Otherwise do two txs (permit then swap) – less ideal but OK for MVP on private orderflow.
    return await self.private_orderflow.submit_with_permit2_and_swap(
        permit=permit,                        # includes details + signature
        swap={"to": to, "data": data, "value": value},
        max_priority_fee=params.get("max_priority_fee"),
    )
