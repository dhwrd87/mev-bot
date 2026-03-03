# bot/mempool/monitor_handler.py (new or in your aggregator)
from bot.mempool.detectors import TxFeatures, is_sniper, is_sandwich_victim
from bot.mempool.monitor_handler import on_sniper_candidate
from bot.mempool.detectors import TxFeatures  # your decoded features
from bot.hunter.pipeline import process_candidate, PoolReserves, SignerStub

features: TxFeatures = decode_pending_tx(tx_hash)
res_in, res_out = pool_cache.get_reserves(features.pair_id)  # your pool cache/indexer
await on_sniper_candidate(features, res_in, res_out)

async def handle_sniper(features: TxFeatures, reserves_in: float, reserves_out: float, current_block: int):
    signer = SignerStub()  # replace with real signer
    res = await process_candidate(features, PoolReserves(reserves_in, reserves_out), current_block, signer)
    # Persist outcome to DB / metrics if you like

async def on_pending_decoded(features: TxFeatures):
    sniper_pred, sniper_score, _ = is_sniper(features)
    victim_pred, victim_score, _ = is_sandwich_victim(features)

    if sniper_pred:
        # raise an opportunity for Hunter mode (backrun candidate)
        await opp_sink.emit({
            "detector": "sniper",
            "pair_id": features.pair_id,
            "score": sniper_score,
            "context": features.__dict__,
        })

    if victim_pred:
        # mark flows likely to be sandwiched (stealth bias for our own trades)
        await opp_sink.emit({
            "detector": "sandwich_victim",
            "pair_id": features.pair_id,
            "score": victim_score,
            "context": features.__dict__,
        })