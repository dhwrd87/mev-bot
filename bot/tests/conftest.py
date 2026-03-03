import sys, types
from web3.eth import Eth

class AttrDict(dict):
    def __getattr__(self, k):
        try: return self[k]
        except KeyError: raise AttributeError(k)
    def __setattr__(self, k, v): self[k] = v
    @staticmethod
    def wrap(v):
        if isinstance(v, dict): return AttrDict({k: AttrDict.wrap(x) for k, x in v.items()})
        if isinstance(v, list): return [AttrDict.wrap(x) for x in v]
        return v

settings_dict = {
  "detectors": {
    "sniper": {
      "threshold": 0.35,  # relaxed for our tiny fixtures
      "priority_ratio_min": 1.8, "slippage_min": 0.05, "token_age_hours_max": 2,
      "size_mid_usd": [300, 7000],
      "weights": {"token_age":0.35,"priority_ratio":0.20,"slippage":0.15,"trending":0.10,"path_simple":0.10,"size_mid":0.10}
    },
    "sandwich_victim": {
      "threshold": 0.60, "victim_slippage_min":0.01, "size_pool_ratio_min":0.015,
      "deadline_max_s":90, "priority_ratio_max":1.5,
      "weights": {"exact_input":0.20,"slippage":0.25,"size_vs_pool":0.25,"deadline":0.10,"low_priority":0.10,"path_simple":0.10}
    }
  },
  "hunter_strategy": {
    "min_profit_usd": 5.0, "min_profit_wei": "100000000000000", "max_gas_ratio": 0.30,
    "safety": {"min_pool_liquidity_usd":200000, "max_trade_share_of_pool":0.10},
    "model": {"pool_fee_bps_v2":30, "supported_v3_fees_bps":[5,30,100]},
    "gas_estimates": {"polygon": {"swap_v2":140000, "swap_v3":180000, "bundle_overhead":40000}}
  },
  "stealth_strategy": {
    "triggers": {
      "min_flags": 2,
      "flags": {
        "high_slippage": 0.005,
        "new_token_age_hours": 24,
        "low_liquidity_usd": 100000,
        "trending": True,
        "active_snipers_min": 1,
        "large_trade_usd": 20000,
        "gas_spike_gwei": 120
      }
    }
  },
  "chains": {
    "polygon": {
      # relays for router tests
      "relays": {
        "flashbots_protect": {"type":"flashbots","url":"https://relay.flashbots.net"},
        "mev_blocker": {"type":"mevblocker","url":"https://rpc.mevblocker.io"},
        "cow_protocol": {"type":"cow","url":"https://api.cow.fi/polygon/api/v1"}
      },
      "default_order": ["flashbots_protect","mev_blocker","cow_protocol"],
      "max_retries_per_relay": 2,
      "backoff": {"base":0.3,"factor":2.0,"max":3.0,"jitter":0.25},
      # builders for bundle tests
      "builders": {
        "flashbots_builder":{"url":"https://relay.flashbots.net","type":"flashbots"},
        "beaver_builder":{"url":"https://builder.beaverbuild.org","type":"flashbots"},
        "titan_builder":{"url":"https://rpc.titanbuilder.xyz","type":"flashbots"}
      },
      "builder_order":["flashbots_builder","beaver_builder","titan_builder"],
      "bundle":{
        "max_block_skew":2,"sim_timeout_ms":600,"submit_timeout_ms":1200,"max_retries_per_builder":1,
        "backoff":{"base":0.2,"factor":2.0,"max":1.5,"jitter":0.25}
      }
    }
  }
}

# routing rules the router tests look for
routing_rules = {
  "rules": {
    "prefer_flashbots_if": [
      "traits.value_wei >= 10 * 10**18",
      "traits.exact_output",
      "traits.uses_permit2"
    ],
    "prefer_cow_if": [
      "traits.token_is_new",
      "traits.desired_privacy == \"offchain_solver\""
    ],
    "avoid_relay_if": {
      "mev_blocker": ["traits.detected_snipers > 0 and traits.size_usd > 20000"]
    }
  }
}

settings = AttrDict.wrap(settings_dict)
settings["routing"] = AttrDict.wrap(routing_rules)

# Expose as bot.core.config.settings
try:
    import bot.core.config as cfg
except ImportError:
    bot = types.ModuleType("bot"); core = types.ModuleType("bot.core"); cfg = types.ModuleType("bot.core.config")
    sys.modules.setdefault("bot", bot); sys.modules.setdefault("bot.core", core); sys.modules.setdefault("bot.core.config", cfg)
cfg.settings = settings

# pytest fixture for settings when tests request it
import pytest

@pytest.fixture(name="settings")
def settings_fixture():
    return settings

# Allow tests to set w3.eth.chain_id
if isinstance(Eth.chain_id, property) and Eth.chain_id.fset is None:
    _orig_get = Eth.chain_id.fget
    def _get(self):
        override = getattr(self, "_chain_id_override", None)
        return override if override is not None else (_orig_get(self) if _orig_get else None)
    def _set(self, value):
        setattr(self, "_chain_id_override", value)
    Eth.chain_id = property(_get, _set)
