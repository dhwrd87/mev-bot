# Disable auto-loading of 3rd-party pytest plugins (e.g. web3.tools.pytest_ethereum)
import os
os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

# Allow tests to set Web3.eth.chain_id
try:
    from web3.eth import Eth
    if isinstance(Eth.chain_id, property) and Eth.chain_id.fset is None:
        _orig_get = Eth.chain_id.fget
        def _get(self):
            override = getattr(self, "_chain_id_override", None)
            return override if override is not None else (_orig_get(self) if _orig_get else None)
        def _set(self, value):
            setattr(self, "_chain_id_override", value)
        Eth.chain_id = property(_get, _set)
except Exception:
    pass
