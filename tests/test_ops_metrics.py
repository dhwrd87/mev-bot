from ops import metrics as m


def test_metric_helpers_increment_and_set():
    m.record_tx_sent(family="evm", chain="sepolia", strategy="s1")
    assert m.tx_sent_total.labels(family="evm", chain="sepolia", network="testnet", strategy="s1")._value.get() >= 1

    m.record_tx_confirmed(family="evm", chain="sepolia", strategy="s1")
    assert (
        m.tx_confirmed_total.labels(family="evm", chain="sepolia", network="testnet", strategy="s1")._value.get()
        >= 1
    )

    m.record_tx_failed(family="evm", chain="sepolia", strategy="s1", reason="rpc")
    assert (
        m.tx_failed_total.labels(family="evm", chain="sepolia", network="testnet", strategy="s1", reason="rpc")._value.get()
        >= 1
    )

    m.record_rpc_latency(family="evm", chain="sepolia", provider="publicnode", seconds=0.12)
    assert (
        m.rpc_latency_seconds.labels(family="evm", chain="sepolia", network="testnet", provider="publicnode")._sum.get()
        > 0
    )
    before = m.tx_confirm_latency_seconds.labels(
        family="evm", chain="sepolia", network="testnet", strategy="s1"
    )._sum.get()
    m.record_tx_confirm_latency(family="evm", chain="sepolia", strategy="s1", seconds=1.25)
    after = m.tx_confirm_latency_seconds.labels(
        family="evm", chain="sepolia", network="testnet", strategy="s1"
    )._sum.get()
    assert after > before

    m.set_runtime_bot_state(family="evm", chain="sepolia", state="TRADING")
    assert m.bot_state_value.labels(family="evm", chain="sepolia", network="testnet")._value.get() == 4.0
    assert m.state.labels(family="evm", chain="sepolia", network="testnet")._value.get() == 3.0

    m.set_head_lag(family="evm", chain="sepolia", provider="p", blocks=2)
    m.set_slot_lag(family="sol", chain="solana", provider="p", lag=3)
    m.set_chain_head(family="evm", chain="sepolia", provider="p", height=123)
    m.set_chain_slot(family="sol", chain="solana", provider="p", slot=999)
    m.set_heartbeat(family="evm", chain="sepolia", unix_ts=1_700_000_000)
    before_stream = m.stream_events_observed_total.labels(
        stream="mempool:pending:txs", source="api_probe"
    )._value.get()
    m.record_stream_events_observed(stream="mempool:pending:txs", count=3, source="api_probe")
    assert m.head_lag_blocks.labels(family="evm", chain="sepolia", network="testnet", provider="p")._value.get() == 2.0
    assert m.slot_lag.labels(family="sol", chain="solana", network="mainnet", provider="p")._value.get() == 3.0
    assert m.chain_head.labels(family="evm", chain="sepolia", network="testnet", provider="p")._value.get() == 123.0
    assert m.chain_slot.labels(family="sol", chain="solana", network="mainnet", provider="p")._value.get() == 999.0
    assert m.heartbeat_ts.labels(family="evm", chain="sepolia", network="testnet")._value.get() == 1_700_000_000.0
    assert (
        m.stream_events_observed_total.labels(stream="mempool:pending:txs", source="api_probe")._value.get()
        == before_stream + 3
    )
    assert m.chain_info.labels(family="evm", chain="sepolia", network="testnet")._value.get() == 1.0


def test_metric_helpers_canonicalize_chain_aliases():
    m.record_tx_sent(family="evm", chain="ethereum", strategy="s1")
    assert m.tx_sent_total.labels(family="evm", chain="ethereum", network="mainnet", strategy="s1")._value.get() >= 1

    m.record_blocked_by_operator(scope="runtime", chain="eth", reason="operator_not_trading")
    assert (
        m.blocked_by_operator_total.labels(
            family="evm",
            scope="runtime",
            chain="ethereum",
            network="mainnet",
            reason="operator_not_trading",
        )._value.get()
        >= 1
    )


def test_start_metrics_server_idempotent(monkeypatch):
    calls = {"n": 0}

    def _fake_start_http_server(_port):
        calls["n"] += 1

    monkeypatch.setattr(m, "start_http_server", _fake_start_http_server)
    monkeypatch.setattr(m, "_SERVER_STARTED", False)
    m.start_metrics_http_server(port=9900)
    m.start_metrics_http_server(port=9900)
    assert calls["n"] == 1


def test_revert_reason_bucket_mapping():
    assert m.map_revert_reason("nonce too low") == "nonce_too_low"
    assert m.map_revert_reason("UNDERPRICED gas") == "fee_underpriced"
    assert m.map_revert_reason("execution reverted") == "reverted"
    assert m.map_revert_reason("random_unknown_reason_foo") == "other"


def test_rpc_error_bucket_and_tx_result_helpers():
    m.record_rpc_error(provider="publicnode", code_bucket="429", family="evm", chain="sepolia")
    assert (
        m.rpc_errors_total.labels(
            family="evm", chain="sepolia", network="testnet", provider="publicnode", code_bucket="429"
        )._value.get()
        >= 1
    )

    before_ok = m.tx_confirmed_total.labels(
        family="evm", chain="sepolia", network="testnet", strategy="s2"
    )._value.get()
    before_fail = m.tx_failed_total.labels(
        family="evm", chain="sepolia", network="testnet", strategy="s2", reason="reverted"
    )._value.get()

    m.record_tx_result(family="evm", chain="sepolia", strategy="s2", ok=True, confirm_latency_s=1.0)
    m.record_tx_result(
        family="evm", chain="sepolia", strategy="s2", ok=False, reason="reverted", confirm_latency_s=2.0
    )

    assert (
        m.tx_confirmed_total.labels(family="evm", chain="sepolia", network="testnet", strategy="s2")._value.get()
        > before_ok
    )
    assert (
        m.tx_failed_total.labels(
            family="evm", chain="sepolia", network="testnet", strategy="s2", reason="reverted"
        )._value.get()
        > before_fail
    )


def test_dex_pack_metric_helpers():
    m.record_dex_quote(family="evm", chain="sepolia", dex="univ3")
    assert m.dex_quote_total.labels(dex="univ3", family="evm", chain="sepolia", network="testnet")._value.get() >= 1

    m.record_dex_quote_fail(family="evm", chain="sepolia", dex="univ3", reason="timeout")
    assert (
        m.dex_quote_fail_total.labels(
            dex="univ3", reason="timeout", family="evm", chain="sepolia", network="testnet"
        )._value.get()
        >= 1
    )

    before_lat = m.dex_quote_latency_seconds.labels(
        dex="univ3", family="evm", chain="sepolia", network="testnet"
    )._sum.get()
    m.record_dex_quote_latency(family="evm", chain="sepolia", dex="univ3", seconds=0.03)
    after_lat = m.dex_quote_latency_seconds.labels(
        dex="univ3", family="evm", chain="sepolia", network="testnet"
    )._sum.get()
    assert after_lat > before_lat

    m.record_dex_build_fail(family="evm", chain="sepolia", dex="univ3", reason="rpc_error")
    assert (
        m.dex_build_fail_total.labels(
            dex="univ3", reason="rpc_error", family="evm", chain="sepolia", network="testnet"
        )._value.get()
        >= 1
    )

    m.record_dex_sim_fail(family="evm", chain="sepolia", dex="univ3", reason="simulation_fail")
    assert (
        m.dex_sim_fail_total.labels(
            dex="univ3",
            reason="simulation_fail",
            family="evm",
            chain="sepolia",
            network="testnet",
        )._value.get()
        >= 1
    )

    before_hops = m.dex_route_hops.labels(dex="univ3", family="evm", chain="sepolia", network="testnet")._sum.get()
    m.record_dex_route_hops(family="evm", chain="sepolia", dex="univ3", hops=3)
    after_hops = m.dex_route_hops.labels(dex="univ3", family="evm", chain="sepolia", network="testnet")._sum.get()
    assert after_hops > before_hops


def test_router_metric_helpers():
    m.record_router_quote(family="evm", chain="sepolia", dex="univ3", ok=True)
    m.record_router_quote(family="evm", chain="sepolia", dex="univ3", ok=False)
    assert m.router_quotes_total.labels(
        family="evm", chain="sepolia", network="testnet", dex="univ3", result="ok"
    )._value.get() >= 1
    assert m.router_quotes_total.labels(
        family="evm", chain="sepolia", network="testnet", dex="univ3", result="fail"
    )._value.get() >= 1

    before_best = m.router_best_dex_selected_total.labels(
        family="evm", chain="sepolia", network="testnet", dex="univ3"
    )._value.get()
    m.record_router_best_dex_selected(family="evm", chain="sepolia", dex="univ3")
    after_best = m.router_best_dex_selected_total.labels(
        family="evm", chain="sepolia", network="testnet", dex="univ3"
    )._value.get()
    assert after_best > before_best
    assert (
        m.router_best_selected_total.labels(
            family="evm", chain="sepolia", network="testnet", dex="univ3"
        )._value.get()
        >= 1
    )

    before_fanout = m.router_quote_fanout.labels(family="evm", chain="sepolia", network="testnet")._sum.get()
    before_fanout_new = m.router_fanout.labels(family="evm", chain="sepolia", network="testnet")._sum.get()
    m.record_router_quote_fanout(family="evm", chain="sepolia", fanout=3)
    after_fanout = m.router_quote_fanout.labels(family="evm", chain="sepolia", network="testnet")._sum.get()
    after_fanout_new = m.router_fanout.labels(family="evm", chain="sepolia", network="testnet")._sum.get()
    assert after_fanout > before_fanout
    assert after_fanout_new > before_fanout_new


def test_mode_outcome_metric_helper():
    before = m.mode_outcomes_total.labels(
        family="evm",
        chain="sepolia",
        network="testnet",
        mode="paper",
        outcome="virtual_fill",
    )._value.get()
    m.record_mode_outcome(family="evm", chain="sepolia", mode="paper", outcome="virtual_fill")
    after = m.mode_outcomes_total.labels(
        family="evm",
        chain="sepolia",
        network="testnet",
        mode="paper",
        outcome="virtual_fill",
    )._value.get()
    assert after > before


def test_risk_metric_helpers():
    before_allow = m.risk_allow_total.labels(chain="sepolia")._value.get()
    before_watch = m.risk_watch_total.labels(chain="sepolia")._value.get()
    before_deny = m.risk_deny_total.labels(chain="sepolia")._value.get()
    before_sell_fail = m.sell_sim_fail_total.labels(chain="sepolia", reason="reverted")._value.get()

    m.record_risk_allow(chain="sepolia")
    m.record_risk_watch(chain="sepolia")
    m.record_risk_deny(chain="sepolia")
    m.record_sell_sim_fail(chain="sepolia", reason="reverted")

    assert m.risk_allow_total.labels(chain="sepolia")._value.get() > before_allow
    assert m.risk_watch_total.labels(chain="sepolia")._value.get() > before_watch
    assert m.risk_deny_total.labels(chain="sepolia")._value.get() > before_deny
    assert m.sell_sim_fail_total.labels(chain="sepolia", reason="reverted")._value.get() > before_sell_fail
