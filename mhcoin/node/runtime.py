"""Long-running MHCOIN node (Stage 4: P2P + TX/BLOCK relay; Stage 9: genesis verify)."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

from mhcoin.blockchain.block import Block
from mhcoin.blockchain.chain import Blockchain
from mhcoin.blockchain.genesis import get_network_genesis
from mhcoin.consensus.params import PROTOCOL_VERSION, SOFTWARE_VERSION, get_network_params
from mhcoin.mempool import Mempool
from mhcoin.mining.abortable_pow import MiningAborted, mine_block_cancellable
from mhcoin.mining.block_template import build_block_template
from mhcoin.network.p2p import P2PConfig, P2PManager
from mhcoin.network.relay import TxRelay
from mhcoin.network.sync import SyncManager
from mhcoin.transaction.transaction import Transaction
from mhcoin.wallet.addresses import address_to_pubkey_hash

logger = logging.getLogger("mhcoin.node")


class NodeRuntime:
    def __init__(
        self,
        *,
        data_dir: Path,
        network: str,
        host: str,
        port: int,
        max_peers: int = 32,
        connect: list[str] | None = None,
        enable_listen: bool = True,
    ):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        params = get_network_params(network)
        self.network = params.name
        self.host = host
        self.port = port
        self.connect_targets = connect or []
        self.enable_listen = enable_listen
        # Blockchain verifies disk genesis against `network` on load.
        self.chain = Blockchain(data_dir, network=self.network)
        if self.chain.height < 0:
            # Fresh node: install frozen canonical genesis (never mine at runtime).
            # Custom wallet genesis remains CLI `blockchain init` on regtest/localnet only.
            genesis = get_network_genesis(self.network)
            self.chain.init_with_genesis(genesis)
            logger.info(
                "Initialized %s with frozen genesis %s",
                self.network,
                genesis.block_hash().hex(),
            )
        height = max(self.chain.height, 0)
        self.mempool = Mempool(data_dir / "mempool.json")
        self.p2p = P2PManager(
            P2PConfig(
                network=self.network,
                host=host,
                port=port,
                max_peers=max_peers,
                start_height=height,
                data_dir=data_dir,
                connect_seeds=list(self.connect_targets),
                enable_listen=enable_listen,
            )
        )
        self.relay = TxRelay(
            mempool=self.mempool,
            get_utxo=lambda: self.chain.utxo,
            get_height=lambda: max(self.chain.height, 0),
            get_peers=self.p2p.handshaked_peers,
            chain=self.chain,
        )
        self.sync = SyncManager(
            chain=self.chain,
            relay=self.relay,
            get_our_height=lambda: max(self.chain.height, 0),
        )
        self.relay.attach_sync(self.sync)
        self.p2p.attach_relay(self.relay)
        self._running = False
        self._stopped = False
        self._stop_lock = threading.Lock()
        self.pid_path = data_dir / "node.pid"
        self.status_path = data_dir / "node_status.json"

    def start(self, *, blocking: bool = True) -> None:
        self._write_pid()
        self.p2p.start()
        self._running = True
        for target in self.connect_targets:
            if not self._running:
                break
            host, _, port_s = target.rpartition(":")
            try:
                self.p2p.connect_to_peer(host, int(port_s))
            except Exception as e:
                logger.warning("connect %s failed: %s", target, e)
        if self._running:
            self._write_status()
        if not blocking:
            return
        # signal.signal only works in the main thread (Desktop starts node in a worker).
        try:
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGINT, self._signal_stop)
                signal.signal(signal.SIGTERM, self._signal_stop)
        except (ValueError, RuntimeError):
            pass
        try:
            while self._running:
                self._write_status()
                time.sleep(1.0)
        finally:
            self.stop()

    def stop(self) -> None:
        """Idempotent stop — safe if Desktop and the run-loop finally both call it."""
        self._running = False
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            try:
                self.p2p.stop()
            except Exception:
                logger.exception("p2p stop failed")
            try:
                self.chain.close()
            except Exception:
                logger.exception("chain close failed")
            if self.pid_path.exists():
                try:
                    self.pid_path.unlink()
                except OSError:
                    pass
            logger.info("Node stopped")

    def submit_tx(self, tx: Transaction) -> str:
        return self.p2p.broadcast_transaction(tx)

    def accept_block(self, block: Block) -> int:
        return self.relay.accept_block(block)

    def prepare_block_template(
        self, miner_address: str, *, hrp: str | None = None
    ) -> tuple[Block, int, int]:
        """Build a block template from the live node tip/mempool (Bitcoin-style).

        Callers mine outside locks, then submit via accept_block().
        Returns (block, height, bits).
        """
        # Chain is open from __init__; _running flips true after p2p.start() in a
        # worker thread — only refuse once stop() has been called.
        if getattr(self, "_stopped", False):
            raise RuntimeError("node stopped")
        if self.chain.height < 0 or self.chain.tip_hash is None:
            raise RuntimeError("chain not initialized")
        params = get_network_params(self.network)
        use_hrp = hrp or params.address_hrp
        pkh = address_to_pubkey_hash(miner_address, hrp=use_hrp)
        with self.chain._lock:
            tip_hash = self.chain.tip_hash
            if tip_hash is None:
                raise RuntimeError("chain not initialized")
            height = self.chain.height + 1
            bits = self.chain.get_next_work_for_parent(tip_hash)
            # Template timestamp must exceed MTP; wall-clock is policy only for mining.
            mtp = self.chain.median_time_past_for_parent(tip_hash)
            ts = int(time.time())
            if ts <= mtp:
                ts = mtp + 1
            block = build_block_template(
                height=height,
                previous_hash=tip_hash,
                timestamp=ts,
                bits=bits,
                mempool=self.mempool,
                utxo=self.chain.utxo,
                miner_pubkey_hash=pkh,
            )
        return block, height, bits

    def mine_one(self, miner_address: str, *, hrp: str | None = None) -> tuple[int, str]:
        """Mine one block on the live node and announce BLOCK INV to peers."""
        block, _height, _bits = self.prepare_block_template(miner_address, hrp=hrp)
        parent = block.header.previous_block_hash
        epoch0 = int(self.chain.tip_epoch)

        def _abort() -> bool:
            if getattr(self, "_stopped", False) or not getattr(self, "_running", True):
                return True
            if int(self.chain.tip_epoch) != epoch0:
                return True
            tip = self.chain.tip_hash
            return tip is not None and tip != parent

        try:
            mine_block_cancellable(block, abort_check=_abort)
        except MiningAborted as e:
            raise RuntimeError("mining aborted: tip moved or node stopped") from e
        if self.chain.tip_hash != parent:
            raise RuntimeError("stale template after PoW")
        new_height = self.relay.accept_block(block)
        self._write_status()
        return new_height, block.block_hash().hex()

    def _signal_stop(self, *_args) -> None:
        logger.info("Shutdown signal received")
        self._running = False

    def _write_pid(self) -> None:
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def _write_status(self) -> None:
        if not self._running or getattr(self, "_stopped", False):
            return
        if getattr(self.chain, "_closed", False) or getattr(self.chain, "_db", None) is None:
            return
        try:
            tip = self.chain.tip_hash.hex() if self.chain.tip_hash else None
            info = self.chain.info()
            params = get_network_params(self.network)
            status = {
                "network": self.network,
                "listen": f"{self.host}:{self.port}",
                "protocol": PROTOCOL_VERSION,
                "software_version": SOFTWARE_VERSION,
                "magic": params.magic.hex(),
                "genesis_hash": params.genesis_hash_hex,
                "peers": self.p2p.get_peers(),
                "peer_count": self.p2p.peer_count(),
                "height": self.chain.height,
                "tip": tip,
                "chain_work": self.chain.get_chain_work() if self.chain.height >= 0 else 0,
                "known_blocks": info.get("known_blocks"),
                "side_chains": info.get("side_chains"),
                "utxo_fingerprint": info.get("utxo_fingerprint"),
                "mempool_size": len(self.mempool),
                "sync": self.sync.status(),
                "p2p": self.p2p.status(),
                "known_addrs": self.p2p.addrdb.count(),
                "bans": len(self.p2p.bans.list_bans()),
                "pid": os.getpid(),
            }
            self.status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("status write skipped (node shutting down)", exc_info=True)

    @staticmethod
    def read_status(data_dir: Path) -> dict | None:
        path = data_dir / "node_status.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
