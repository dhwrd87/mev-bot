from adapters.dex_packs.base import DEXPack, JupiterPack, UniV2Pack, UniV3Pack
from adapters.dex_packs.evm_univ2 import EVMUniV2Pack
from adapters.dex_packs.evm_univ3 import EVMUniV3Pack
from adapters.dex_packs.sol_jupiter import SolJupiterPack


def __getattr__(name: str):
    if name == "DEXPackRegistry":
        from adapters.dex_packs.registry import DEXPackRegistry

        return DEXPackRegistry
    raise AttributeError(name)

__all__ = [
    "DEXPack",
    "UniV2Pack",
    "UniV3Pack",
    "EVMUniV2Pack",
    "EVMUniV3Pack",
    "JupiterPack",
    "SolJupiterPack",
    "DEXPackRegistry",
]
