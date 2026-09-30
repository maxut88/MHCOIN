from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.merkle import merkle_root

__all__ = ["Block", "BlockHeader", "Blockchain", "merkle_root"]


def __getattr__(name: str):
    if name == "Blockchain":
        from mhcoin.blockchain.chain import Blockchain

        return Blockchain
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
