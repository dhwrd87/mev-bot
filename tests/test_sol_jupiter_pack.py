from __future__ import annotations

from adapters.dex_packs.evm_univ2 import min_out_from_slippage
from adapters.dex_packs.sol_jupiter import SolJupiterPack
from bot.core.types_dex import TradeIntent


def _intent() -> TradeIntent:
    return TradeIntent(
        family="sol",
        chain="solana",
        network="devnet",
        dex_preference="jupiter",
        token_in="So11111111111111111111111111111111111111112",
        token_out="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        amount_in=1_000_000,
        slippage_bps=50,
        ttl_s=30,
        strategy="default",
    )


def test_quote_parses_out_amount_and_route(monkeypatch):
    pack = SolJupiterPack(config={"base_url": "https://quote-api.jup.ag/v6"}, instance_name="jupiter_main")

    def _fake_get(path, params):
        assert path == "/quote"
        assert params["amount"] == 1_000_000
        return {
            "outAmount": "1234567",
            "priceImpactPct": "0.0012",
            "routePlan": [
                {"swapInfo": {"label": "Raydium"}},
                {"swapInfo": {"label": "Orca"}},
            ],
        }

    monkeypatch.setattr(pack, "_http_get", _fake_get)
    q = pack.quote(_intent())
    assert q.expected_out == 1_234_567
    assert q.min_out == min_out_from_slippage(1_234_567, 50)
    assert "hops=2" in q.route_summary
    assert "Raydium" in q.route_summary


def test_build_calls_swap_api_and_returns_txplan(monkeypatch):
    pack = SolJupiterPack(
        config={
            "base_url": "https://quote-api.jup.ag/v6",
            "user_public_key": "9xQeWvG816bUx9EP5HmaT23yvVMH5Q5VYqVVX5xVJ8Q",
            "wrap_unwrap_sol": True,
        },
        instance_name="jupiter_main",
    )
    calls = {"quote": 0, "swap": 0}

    def _fake_get(path, params):
        calls["quote"] += 1
        assert path == "/quote"
        return {"outAmount": "100", "routePlan": [{"swapInfo": {"label": "Meteora"}}]}

    def _fake_post(path, payload):
        calls["swap"] += 1
        assert path == "/swap"
        assert payload["userPublicKey"]
        assert payload["wrapAndUnwrapSol"] is True
        return {"swapTransaction": "BASE64_TX", "lastValidBlockHeight": 12345}

    monkeypatch.setattr(pack, "_http_get", _fake_get)
    monkeypatch.setattr(pack, "_http_post", _fake_post)

    quote = type(
        "Quote",
        (),
        {
            "route_summary": "hops=1;venues=Meteora",
            "min_out": 99,
        },
    )()
    plan = pack.build(_intent(), quote)
    assert calls["quote"] == 1 and calls["swap"] == 1
    assert plan.raw_tx == "BASE64_TX"
    assert plan.instruction_bundle["swap_transaction_b64"] == "BASE64_TX"


def test_simulate_transaction_ok_and_error(monkeypatch):
    pack = SolJupiterPack(config={"commitment": "processed"}, instance_name="jupiter_main")
    plan = type("Plan", (), {"family": "sol", "chain": "solana", "raw_tx": "BASE64_TX", "instruction_bundle": {}})()

    monkeypatch.setattr(
        pack,
        "_rpc_call",
        lambda method, params: {
            "result": {"value": {"err": None, "logs": ["ok"], "unitsConsumed": 190000}},
        },
    )
    ok = pack.simulate(plan)
    assert ok.ok is True
    assert ok.compute_units == 190000

    monkeypatch.setattr(
        pack,
        "_rpc_call",
        lambda method, params: {
            "result": {"value": {"err": {"InstructionError": [0, "Custom"]}, "logs": ["bad"], "unitsConsumed": 111}},
        },
    )
    bad = pack.simulate(plan)
    assert bad.ok is False
    assert bad.error_code == "simulation_error"


def test_quote_and_build_via_mocked_http_session(monkeypatch):
    pack = SolJupiterPack(
        config={
            "base_url": "https://quote-api.jup.ag/v6",
            "user_public_key": "9xQeWvG816bUx9EP5HmaT23yvVMH5Q5VYqVVX5xVJ8Q",
        },
        instance_name="jupiter_main",
    )

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def _fake_get(url, params, timeout):
        assert url.endswith("/quote")
        assert int(params["amount"]) == 1_000_000
        return _Resp(
            {
                "outAmount": "2000000",
                "priceImpactPct": "0.002",
                "routePlan": [{"swapInfo": {"label": "Orca"}}],
            }
        )

    def _fake_post(url, json, timeout):
        assert url.endswith("/swap")
        assert json["userPublicKey"]
        return _Resp({"swapTransaction": "BASE64_SWAP_TX"})

    monkeypatch.setattr(pack._http, "get", _fake_get)
    monkeypatch.setattr(pack._http, "post", _fake_post)

    quote = pack.quote(_intent())
    assert quote.expected_out == 2_000_000
    assert quote.min_out == min_out_from_slippage(2_000_000, 50)
    plan = pack.build(_intent(), quote)
    assert plan.raw_tx == "BASE64_SWAP_TX"


def test_simulate_uses_sol_client_when_available():
    class _FakeSolClient:
        def simulate_transaction(self, tx_b64, **kwargs):
            assert tx_b64 == "BASE64_TX"
            assert kwargs["encoding"] == "base64"
            return {"value": {"err": None, "logs": ["ok"], "unitsConsumed": 321000}}

    pack = SolJupiterPack(
        config={"commitment": "processed"},
        instance_name="jupiter_main",
        sol_client=_FakeSolClient(),
    )
    plan = type("Plan", (), {"family": "sol", "chain": "solana", "raw_tx": "BASE64_TX", "instruction_bundle": {}})()
    out = pack.simulate(plan)
    assert out.ok is True
    assert out.compute_units == 321000
