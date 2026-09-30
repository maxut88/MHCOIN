"""Solo miner loop for end-user CLI (no consensus changes)."""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from mhcoin.blockchain.genesis import get_network_genesis
from mhcoin.consensus.params import get_network_params
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.constants import REGTEST_NBITS
from mhcoin.mining.block_template import build_block_template
from mhcoin.node.local_node import LocalNode
from mhcoin.wallet.addresses import address_to_pubkey_hash, validate_address
from mhcoin.wallet.send import format_mhc

logger = logging.getLogger("mhcoin.mining")


@dataclass
class MineResult:
    height: int
    block_hash: str
    reward_sats: int
    address: str


class SoloMiner:
    """
    Mine against a local chain datadir (same dir as wallet UTXO).
    Does not change consensus — uses existing template + PoW + connect_block.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        network: str,
        hrp: str,
        address: str,
    ):
        params = get_network_params(network)
        if not validate_address(address, hrp=hrp):
            raise ValueError(f"invalid address for hrp={hrp}")
        self.network = params.name
        self.hrp = hrp
        self.address = address
        self.pkh = address_to_pubkey_hash(address, hrp=hrp)
        self.node = LocalNode(data_dir, hrp=hrp, network=params.name)
        self._stop = False

    def ensure_chain(self) -> None:
        if self.node.chain.height >= 0:
            return
        genesis = get_network_genesis(self.network)
        self.node.chain.init_with_genesis(genesis)
        logger.info(
            "Installed frozen %s genesis %s",
            self.network,
            genesis.block_hash().hex(),
        )

    def request_stop(self, *_args) -> None:
        self._stop = True

    def mine_one(self) -> MineResult:
        self.ensure_chain()
        if self._stop:
            raise RuntimeError("stopped")
        params = get_network_params(self.network)
        height = self.node.chain.height + 1
        assert self.node.chain.tip_hash is not None
        tip = self.node.chain.get_block_by_hash(self.node.chain.tip_hash)
        bits = tip.header.bits if tip is not None else params.genesis_bits
        if self.network in ("regtest", "localnet"):
            bits = REGTEST_NBITS
        block = build_block_template(
            height=height,
            previous_hash=self.node.chain.tip_hash,
            timestamp=int(time.time()),
            bits=bits,
            mempool=self.node.mempool,
            utxo=self.node.chain.utxo,
            miner_pubkey_hash=self.pkh,
        )

        def _progress(nonce: int, _h: bytes, hps: float) -> None:
            if self._stop:
                raise KeyboardInterrupt("stop requested")
            if nonce > 0 and nonce % 500_000 == 0:
                print(f"  … mining height={height} nonce={nonce} ~{hps:,.0f} H/s", flush=True)

        mine_block(block, progress=_progress)
        if self._stop:
            raise RuntimeError("stopped")
        connected = self.node.chain.connect_block(block)
        self.node.mempool.clear_included(block.transactions[1:])
        reward = block.transactions[0].outputs[0].value
        return MineResult(
            height=connected,
            block_hash=block.block_hash().hex(),
            reward_sats=reward,
            address=self.address,
        )

    def run(self, *, max_blocks: int | None = None) -> list[MineResult]:
        """Mine until Ctrl+C or max_blocks found."""
        self.ensure_chain()
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        found: list[MineResult] = []
        print(f"Mining on {self.network} → reward address {self.address}")
        print(f"Data dir: {self.node.data_dir}")
        print(f"Tip height: {self.node.chain.height}")
        print("Press Ctrl+C to stop.\n", flush=True)
        try:
            while not self._stop:
                if max_blocks is not None and len(found) >= max_blocks:
                    break
                t0 = time.time()
                result = self.mine_one()
                elapsed = time.time() - t0
                found.append(result)
                print(
                    f"Found block height={result.height} "
                    f"hash={result.block_hash[:16]}… "
                    f"reward={format_mhc(result.reward_sats)} MHC "
                    f"({elapsed:.2f}s)",
                    flush=True,
                )
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
        finally:
            self.node.close()
        return found

    def close(self) -> None:
        self.node.close()
