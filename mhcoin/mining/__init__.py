from mhcoin.mining.block_template import build_block_template, create_coinbase

__all__ = ["build_block_template", "create_coinbase", "MineResult", "SoloMiner"]


def __getattr__(name: str):
    if name in ("MineResult", "SoloMiner"):
        from mhcoin.mining import miner as _miner

        return getattr(_miner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
