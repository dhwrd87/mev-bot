import asyncio, json, time, pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.exec.orderflow import PrivateOrderflowManager, Endpoint, TxMeta
from ops.metrics import blocked_by_operator_total as blocked_by_operator_total_v2

pytestmark = pytest.mark.asyncio

def mk_manager(monkeypatch, responses, operator_state: dict | None = None):
    """
    responses: list of (status_code, json_payload_dict) returned per POST call
    """
    class FakeResp:
        def __init__(self, status, data):
            self.status_code = status
            self._data = data
            self.text = json.dumps(data)
        def json(self): return self._data

    async def fake_post(url, headers=None, content=None):
        try:
            status, data = responses.pop(0)
        except IndexError:
            status, data = 200, {"jsonrpc":"2.0","result":"0xok"}
        return FakeResp(status, data)

    ep = [
        Endpoint(name="A", url="https://a", kind="rpc"),
        Endpoint(name="B", url="https://b", kind="builder", method_send_bundle="eth_sendBundle"),
    ]
    state_payload = operator_state or {"state": "TRADING", "mode": "live", "kill_switch": False, "last_actor": "test"}
    st = Path(f"/tmp/operator_state_test_{state_payload.get('state','X').lower()}_{time.time_ns()}.json")
    st.write_text(
        json.dumps(state_payload),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPERATOR_STATE_PATH", str(st))
    monkeypatch.setenv("BOT_RUNTIME_STATE", "TRADING")
    mgr = PrivateOrderflowManager(ep)
    mgr._client.post = AsyncMock(side_effect=fake_post)
    return mgr

async def test_submit_private_tx_success_first_try(monkeypatch):
    mgr = mk_manager(monkeypatch, [(200, {"jsonrpc":"2.0","result":"0xHASH"})])
    res = await mgr.submit_private_tx("0xsigned", TxMeta(chain="polygon"))
    assert res["result"] == "0xHASH"

async def test_submit_private_tx_retry_then_success(monkeypatch):
    # first endpoint returns RPC error; second succeeds
    mgr = mk_manager(monkeypatch, [
        (200, {"jsonrpc":"2.0","error":{"code":-32000,"message":"temporarily underpriced"}}),
        (200, {"jsonrpc":"2.0","result":"0xHASH2"})
    ])
    res = await mgr.submit_private_tx("0xsigned", TxMeta(chain="polygon"))
    assert res["result"] == "0xHASH2"

async def test_submit_bundle_then_fallback_sequential(monkeypatch):
    # builder fails, then sequential path sends both ok
    mgr = mk_manager(monkeypatch, [
        (200, {"jsonrpc":"2.0","error":{"code":-32000,"message":"bundle rejected"}}),  # builder
        (200, {"jsonrpc":"2.0","result":"0xOK1"}),                                     # seq tx1
        (200, {"jsonrpc":"2.0","result":"0xOK2"})                                      # seq tx2
    ])
    res = await mgr.submit_private_bundle(["0x1","0x2"], TxMeta(chain="polygon"))
    assert res["bundle"] is False
    assert res["result"] == "ok"

async def test_submit_all_fail(monkeypatch):
    mgr = mk_manager(monkeypatch, [
        (429, {"jsonrpc":"2.0","error":{"code":429,"message":"limited"}}),
        (500, {"jsonrpc":"2.0","error":{"code":-32000,"message":"upstream 500"}}),
        (200, {"jsonrpc":"2.0","error":{"code":-32000,"message":"bad"}}),
    ])
    with pytest.raises(RuntimeError):
        await mgr.submit_private_tx("0xsigned", TxMeta(chain="polygon"), retries_per_endpoint=0)


async def test_submit_private_tx_blocked_by_operator_when_paused(monkeypatch, tmp_path):
    mgr = mk_manager(
        monkeypatch,
        [(200, {"jsonrpc": "2.0", "result": "0xHASH"})],
        operator_state={
            "state": "PAUSED",
            "mode": "live",
            "kill_switch": False,
            "last_actor": "1:test",
        },
    )

    res = await mgr.submit_private_tx("0xsigned", TxMeta(chain="polygon"))
    assert res["ok"] is False
    assert res["error"] == "blocked_by_operator"
    assert mgr._client.post.await_count == 0


async def test_submit_private_tx_proceeds_when_operator_trading(monkeypatch, tmp_path):
    mgr = mk_manager(
        monkeypatch,
        [(200, {"jsonrpc": "2.0", "result": "0xHASH"})],
        operator_state={
            "state": "TRADING",
            "mode": "live",
            "kill_switch": False,
            "last_actor": "1:test",
        },
    )

    res = await mgr.submit_private_tx("0xsigned", TxMeta(chain="polygon"))
    assert res["ok"] is True
    assert mgr._client.post.await_count >= 1


async def test_submit_private_tx_dryrun_skips_network(monkeypatch):
    mgr = mk_manager(
        monkeypatch,
        [(200, {"jsonrpc": "2.0", "result": "0xHASH"})],
        operator_state={
            "state": "TRADING",
            "mode": "dryrun",
            "kill_switch": False,
            "last_actor": "1:test",
        },
    )
    res = await mgr.submit_private_tx("0xsigned", TxMeta(chain="polygon"))
    assert res["ok"] is True
    assert res["endpoint"] == "dryrun"
    assert mgr._client.post.await_count == 0


async def test_submit_private_tx_paper_virtual_fill(monkeypatch):
    mgr = mk_manager(
        monkeypatch,
        [(200, {"jsonrpc": "2.0", "result": "0xHASH"})],
        operator_state={
            "state": "TRADING",
            "mode": "paper",
            "kill_switch": False,
            "last_actor": "1:test",
        },
    )
    res = await mgr.submit_private_tx("0xsigned", TxMeta(chain="polygon"))
    assert res["ok"] is True
    assert res["endpoint"] == "paper"
    assert str(res.get("result", "")).startswith("paper:")
    assert mgr._client.post.await_count == 0


async def test_submit_private_tx_blocked_by_operator_when_kill_switch_on(monkeypatch):
    before = blocked_by_operator_total_v2.labels(
        family="evm",
        scope="submit_private_tx",
        chain="polygon",
        network="mainnet",
        reason="operator_kill_switch",
    )._value.get()
    mgr = mk_manager(
        monkeypatch,
        [(200, {"jsonrpc": "2.0", "result": "0xHASH"})],
        operator_state={
            "state": "TRADING",
            "mode": "live",
            "kill_switch": True,
            "last_actor": "1:test",
        },
    )

    res = await mgr.submit_private_tx("0xsigned", TxMeta(chain="polygon"))
    assert res["ok"] is False
    assert res["error"] == "blocked_by_operator"
    assert mgr._client.post.await_count == 0
    after = blocked_by_operator_total_v2.labels(
        family="evm",
        scope="submit_private_tx",
        chain="polygon",
        network="mainnet",
        reason="operator_kill_switch",
    )._value.get()
    assert after >= before + 1
