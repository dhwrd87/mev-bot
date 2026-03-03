import asyncio

from bot.ops.status_card import StatusCardManager, StatusCardSnapshot, fmt_num


def test_fmt_num_handles_none_and_float():
    assert fmt_num(None) == "n/a"
    assert fmt_num(1.2345, 2) == "1.23"


def test_status_card_embed_contains_required_fields():
    mgr = StatusCardManager(
        bot=None,  # type: ignore[arg-type]
        status_channel_id=1,
        refresh_s=45,
        snapshot_fetcher=lambda: None,  # type: ignore[arg-type]
        kv_read=lambda *_: "",
        kv_write=lambda *_: None,
        audit_fn=lambda *_: asyncio.sleep(0),
    )
    snap = StatusCardSnapshot(
        state="PAUSED",
        mode="paper",
        chain_family="evm",
        chain="sepolia",
        rpc_health="w3_connected=False circuit_open=0 429_ratio=0.000",
        last_trade_time="n/a",
        today_pnl="$0.00",
        error_rate="0.00%",
        updated_at="2026-02-28 00:00:00 UTC",
    )
    embed = mgr._build_embed(snap)
    names = [f.name for f in embed.fields]
    assert "State" in names
    assert "Mode" in names
    assert "Chain" in names
    assert "RPC Health" in names
    assert "Last Trade" in names
    assert "Today PnL" in names
    assert "Error Rate" in names
