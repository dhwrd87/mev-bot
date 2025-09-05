# bot/exec/examples/stealth_exact_out.py (usage sketch)
from bot.exec.permit2 import Permit2Handler, PERMIT2_ADDRESS, PermitParams
from bot.exec.exact_output import ExactOutputSwapper, ExactOutputParams
from web3 import Web3

async def build_permit_and_swap(w3: Web3, router_addr: str, owner: str, owner_priv: str,
                                token_in: str, token_out: str, amount_out: int, amount_in_max: int,
                                fee: int, recipient: str, deadline_ts: int):
    # 1) Permit2 signature off-chain
    p2 = Permit2Handler(w3, nonce_store=... )  # inject your persistent store
    permit = PermitParams(
        owner=owner, token=token_in, spender=router_addr,
        amount=amount_in_max, expiration=deadline_ts + 3600, sig_deadline=deadline_ts
    )
    signed = await p2.sign(permit, owner_private_key_hex=owner_priv)
    typed = signed["typed_data"]["message"]   # this is the PermitSingle message the contract expects
    sig   = signed["signature"]

    # 2) Build permit tx (unsigned)
    swapper = ExactOutputSwapper(w3, router_addr)
    permit_tx = swapper.build_permit_tx(PERMIT2_ADDRESS, owner, typed, sig)

    # 3) Build swap tx (unsigned)
    swap_tx = swapper.build_swap_tx(
        ExactOutputParams(
            router=router_addr, token_in=token_in, token_out=token_out,
            fee=fee, recipient=recipient, deadline=deadline_ts,
            amount_out_exact=amount_out, amount_in_max=amount_in_max
        ),
        sender=owner
    )

    # 4) Return the pair for private submission (Flashbots/mev-blocker)
    return permit_tx, swap_tx
