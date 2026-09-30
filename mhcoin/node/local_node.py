"""Local single-node state for regtest (no P2P)."""

from __future__ import annotations

import time
from pathlib import Path

from mhcoin.blockchain.chain import Blockchain
from mhcoin.blockchain.genesis import mine_regtest_genesis
from mhcoin.consensus.params import get_network_params
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.constants import REGTEST_NBITS
from mhcoin.mempool import Mempool, MempoolError
from mhcoin.mining.block_template import build_block_template
from mhcoin.transaction.transaction import Transaction
from mhcoin.wallet.addresses import address_to_pubkey_hash


class LocalNode:
    def __init__(self, data_dir: Path, *, hrp: str = "mhc", network: str = "regtest"):
        self.data_dir = data_dir
        self.hrp = hrp
        self.network = network
        self.chain = Blockchain(data_dir, network=network)
        self.mempool = Mempool(data_dir / "mempool.json")

    def bootstrap_genesis(self, miner_address: str) -> str:
        params = get_network_params(self.network)
        if not params.allow_custom_genesis_bootstrap:
            raise RuntimeError(
                f"{self.network} forbids custom genesis — use frozen genesis via node start"
            )
        pkh = address_to_pubkey_hash(miner_address, hrp=self.hrp)
        genesis = mine_regtest_genesis(pubkey_hash=pkh)
        self.chain.init_with_genesis(genesis)
        return genesis.block_hash().hex()

    def submit_tx(self, tx: Transaction) -> str:
        height = max(self.chain.height, 0)
        return self.mempool.add(tx, self.chain.utxo, height=height)

    def mine_one(self, miner_address: str) -> tuple[int, str]:
        if self.chain.height < 0:
            raise RuntimeError("chain not initialized — run bootstrap first")
        pkh = address_to_pubkey_hash(miner_address, hrp=self.hrp)
        height = self.chain.height + 1
        assert self.chain.tip_hash is not None
        block = build_block_template(
            height=height,
            previous_hash=self.chain.tip_hash,
            timestamp=int(time.time()),
            bits=REGTEST_NBITS,
            mempool=self.mempool,
            utxo=self.chain.utxo,
            miner_pubkey_hash=pkh,
        )
        mine_block(block)
        connected = self.chain.connect_block(block)
        self.mempool.clear_included(block.transactions[1:])
        return connected, block.block_hash().hex()

    def close(self) -> None:
        self.chain.close()
