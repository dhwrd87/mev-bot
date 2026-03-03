from __future__ import annotations

import asyncio

from bot.exec.engine import (
    EvmNonceManager,
    FeePolicy,
    RpcCaller,
    RpcCircuitBreaker,
    SolBlockhashManager,
    choose_fee_gwei,
    reconcile_trade,
    ReconcileInput,
)


def test_fee_policy_respects_cap():
    policy = FeePolicy(max_fee_gwei=50, escalation_factor=2.0, max_escalations=3, profit_safety_margin_usd=0.0)
    decision = choose_fee_gwei(
        policy=policy,
        initial_fee_gwei=60,
        gas_limit=200000,
        expected_profit_usd=1000.0,
        native_usd=3000.0,
    )
    assert decision.allowed is False
    assert decision.reason == "fee_cap_exceeded"


def test_fee_policy_escalates_only_when_profitable():
    policy = FeePolicy(max_fee_gwei=200, escalation_factor=2.0, max_escalations=3, profit_safety_margin_usd=0.0)
    # 200k gas, 3k ETH, 10 gwei => 6 USD fee, profitable with 8 USD expected.
    decision = choose_fee_gwei(
        policy=policy,
        initial_fee_gwei=10,
        gas_limit=200000,
        expected_profit_usd=8.0,
        native_usd=3000.0,
    )
    assert decision.allowed is True
    assert decision.reason == "ok"
    assert decision.fee_gwei == 10


def test_fee_policy_rejects_not_profitable_after_escalation():
    policy = FeePolicy(max_fee_gwei=200, escalation_factor=2.0, max_escalations=2, profit_safety_margin_usd=0.0)
    decision = choose_fee_gwei(
        policy=policy,
        initial_fee_gwei=30,
        gas_limit=300000,
        expected_profit_usd=2.0,
        native_usd=3000.0,
    )
    assert decision.allowed is False
    assert decision.reason in {"not_profitable_after_escalation", "fee_cap_exceeded"}


async def test_rpc_caller_retries_then_succeeds():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    caller = RpcCaller(chain="sepolia", provider="test", retries=3, timeout_s=1.0, backoff_base_s=0.01, backoff_max_s=0.02)
    out = await caller.call(flaky)
    assert out == "ok"
    assert calls["n"] == 3


async def test_rpc_caller_circuit_opens_on_failures():
    async def fail():
        raise RuntimeError("boom")

    circuit = RpcCircuitBreaker(window_size=10, error_ratio_threshold=0.5, open_seconds=60.0)
    caller = RpcCaller(
        chain="sepolia",
        provider="test",
        retries=0,
        timeout_s=0.1,
        circuit=circuit,
    )

    for _ in range(10):
        try:
            await caller.call(fail)
        except RuntimeError:
            pass
    assert circuit.is_open() is True

    # when circuit is open, call fails fast with circuit marker
    try:
        await caller.call(fail)
    except RuntimeError as e:
        assert "rpc_circuit_open" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected circuit open failure")


async def test_nonce_manager_cache_and_refresh():
    nm = EvmNonceManager(ttl_s=0.05)
    source = {"n": 7}

    async def fetch(_addr: str) -> int:
        return source["n"]

    a = "0xabc"
    first = await nm.next_nonce(address=a, fetch_nonce=fetch)
    second = await nm.next_nonce(address=a, fetch_nonce=fetch)
    assert first == 7
    assert second == 8

    await asyncio.sleep(0.06)
    source["n"] = 20
    third = await nm.next_nonce(address=a, fetch_nonce=fetch)
    assert third == 20


async def test_blockhash_manager_refreshes_after_ttl():
    bm = SolBlockhashManager(ttl_s=0.05)
    source = {"v": "h1"}

    async def fetch() -> str:
        return source["v"]

    a = await bm.get_recent_blockhash(fetch)
    b = await bm.get_recent_blockhash(fetch)
    assert a == "h1"
    assert b == "h1"

    await asyncio.sleep(0.06)
    source["v"] = "h2"
    c = await bm.get_recent_blockhash(fetch)
    assert c == "h2"


def test_reconcile_trade_outputs():
    out = reconcile_trade(
        ReconcileInput(
            chain="sepolia",
            strategy="default",
            tx_hash="0x1",
            sent_ts=10.0,
            confirmed_ts=13.5,
            gross_pnl_usd=12.0,
            fees_usd=2.5,
            expected_out=1000,
            actual_out=990,
            success=False,
            revert_reason="execution reverted",
        )
    )
    assert out.success is False
    assert out.realized_pnl_usd == 9.5
    assert out.confirm_latency_s == 3.5
    assert out.slippage_bps is not None and out.slippage_bps > 0
    assert out.revert_bucket in {"reverted", "other"}

