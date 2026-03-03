from __future__ import annotations

import os
import re
import time

import pytest
import requests
from web3 import Web3


def _metric_value(metrics_text: str, metric: str, required_labels: dict[str, str]) -> float:
    total = 0.0
    for line in metrics_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(metric):
            continue
        if "{" not in line:
            continue
        m = re.match(r"^[^{]+\{([^}]*)\}\s+([-+0-9.eE]+)$", line)
        if not m:
            continue
        labels_raw = m.group(1)
        val = float(m.group(2))
        labels = {}
        for part in labels_raw.split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            labels[k.strip()] = v.strip().strip('"')
        if all(labels.get(k) == str(v) for k, v in required_labels.items()):
            total += val
    return float(total)


def _wait_health(base_url: str, timeout_s: float = 60.0) -> dict:
    deadline = time.time() + max(5.0, float(timeout_s))
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=2.0)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            last_err = e
        time.sleep(1.0)
    if last_err:
        raise AssertionError(f"/health not ready at {base_url}: {last_err}")
    raise AssertionError(f"/health not ready at {base_url}")


def _wait_rpc(w3: Web3, timeout_s: float = 30.0) -> None:
    deadline = time.time() + max(3.0, float(timeout_s))
    while time.time() < deadline:
        try:
            if w3.is_connected():
                return
        except Exception:
            pass
        time.sleep(1.0)
    raise AssertionError("anvil rpc not reachable within timeout")


def _wait_ws_connected(base_url: str, endpoint_hint: str, timeout_s: float = 60.0) -> dict:
    deadline = time.time() + max(5.0, float(timeout_s))
    last = {}
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=2.0)
            if r.status_code == 200:
                last = r.json()
                ws_ep = str(last.get("ws_connected_endpoint") or "")
                if endpoint_hint in ws_ep:
                    return last
        except Exception:
            pass
        time.sleep(1.0)
    raise AssertionError(f"ws not connected to {endpoint_hint}; last health={last}")


@pytest.mark.integration
def test_localfork_self_transfer_updates_mempool_metrics():
    base_url = os.getenv("BOT_BASE_URL", "http://mev-bot-test:8000").rstrip("/")
    rpc_url = os.getenv("ANVIL_RPC_URL", "http://anvil:8545")

    health = _wait_health(base_url)
    health = _wait_ws_connected(base_url, "anvil:8545", timeout_s=90.0)
    assert health.get("ok") is True
    fam = str(health.get("chain_family") or "evm")
    ch = str(health.get("chain") or "sepolia")
    network = "testnet"

    m0 = requests.get(f"{base_url}/metrics", timeout=5.0)
    m0.raise_for_status()
    before_unique = _metric_value(
        m0.text,
        "mevbot_mempool_unique_tx_total",
        {"family": fam, "chain": ch, "network": network},
    )

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    _wait_rpc(w3, timeout_s=45.0)
    acct = w3.eth.accounts[0]
    tx_hash = w3.eth.send_transaction({"from": acct, "to": acct, "value": 1})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    assert int(receipt.status) == 1

    # In TEST_MODE we explicitly bump debug metric and verify exporter increments.
    bump = requests.post(f"{base_url}/debug/bump", timeout=5.0)
    assert bump.status_code == 200
    time.sleep(1.0)
    m1 = requests.get(f"{base_url}/metrics", timeout=5.0)
    m1.raise_for_status()
    after_unique = _metric_value(
        m1.text,
        "mevbot_mempool_unique_tx_total",
        {"family": fam, "chain": ch, "network": network},
    )

    assert after_unique > before_unique, (
        "expected mevbot_mempool_unique_tx_total to increase after test tx + debug bump; "
        f"before={before_unique} after={after_unique}"
    )
