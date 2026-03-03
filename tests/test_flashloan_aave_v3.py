from __future__ import annotations

import json

import pytest

from adapters.flashloans.aave_v3 import AaveV3FlashloanProvider
from bot.core.types_dex import TxPlan


def test_aave_v3_loads_chain_config_and_fee(tmp_path):
    cfg = tmp_path / "aave_v3.json"
    cfg.write_text(
        json.dumps(
            {
                "default_fee_bps": 9.0,
                "chains": {
                    "sepolia": {
                        "network": "testnet",
                        "pool": "0x0000000000000000000000000000000000001001",
                        "assets": [
                            "0x0000000000000000000000000000000000000001",
                            "0x0000000000000000000000000000000000000002",
                        ],
                        "fee_bps": 7.5,
                        "executor_mode": "predeployed",
                        "executor_address": "0x0000000000000000000000000000000000002001",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    p = AaveV3FlashloanProvider(chain="sepolia", config_path=str(cfg))
    assert p.name() == "aave_v3"
    assert p.fee_bps() == 7.5
    assert list(p.supported_assets()) == [
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
    ]


def test_aave_v3_fee_calc_and_wrapper_payload(tmp_path):
    cfg = tmp_path / "aave_v3.json"
    cfg.write_text(
        json.dumps(
            {
                "chains": {
                    "base": {
                        "network": "mainnet",
                        "pool": "0x0000000000000000000000000000000000001002",
                        "assets": ["0x0000000000000000000000000000000000000001"],
                        "fee_bps": 9.0,
                        "executor_mode": "predeployed",
                        "executor_address": "0x0000000000000000000000000000000000002002",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    p = AaveV3FlashloanProvider(chain="base", config_path=str(cfg))
    assert round(p.estimate_fee_usd(amount_in_usd=10_000.0), 6) == 9.0

    plan = TxPlan(family="evm", chain="base", dex="univ3", value=0, raw_tx="0xabc", metadata={"k": "v"})
    wrapped = p.build_flashloan_wrapper(plan)
    assert wrapped.raw_tx == "0xabc"
    assert wrapped.metadata["k"] == "v"
    assert wrapped.metadata["flashloan"]["provider"] == "aave_v3"
    assert wrapped.metadata["flashloan"]["pool"] == "0x0000000000000000000000000000000000001002"
    assert wrapped.metadata["flashloan"]["executor_mode"] == "predeployed"


def test_aave_v3_requires_executor_fields(tmp_path):
    cfg = tmp_path / "aave_v3.json"
    cfg.write_text(
        json.dumps(
            {
                "chains": {
                    "base": {
                        "pool": "0x0000000000000000000000000000000000001002",
                        "executor_mode": "predeployed",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing_executor_address"):
        AaveV3FlashloanProvider(chain="base", config_path=str(cfg))
