"""Stage 3–4 inventory relay: INV → GETDATA → TX/BLOCK."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Callable

from mhcoin.blockchain.block import Block
from mhcoin.blockchain.chain import Blockchain, ChainError
from mhcoin.blockchain.validation import ValidationError
from mhcoin.mempool import Mempool, MempoolError
from mhcoin.network.constants import (
    GETDATA_RATE_WINDOW,
    INV_RATE_WINDOW,
    INV_TYPE_BLOCK,
    INV_TYPE_TX,
    MAX_GETDATA_RATE_PER_PEER,
    MAX_INV_RATE_PER_PEER,
    MAX_REQUESTED_BLOCKS,
    MAX_REQUESTED_TX,
    MISBEHAVIOR_FLOOD,
    MISBEHAVIOR_INVALID_BLOCK,
    MISBEHAVIOR_INVALID_TX,
    MISBEHAVIOR_PROTOCOL,
)
from mhcoin.network.messages import (
    InventoryVector,
    decode_block,
    decode_getdata,
    decode_inv,
    decode_tx,
    encode_block,
    encode_getdata,
    encode_inv,
    encode_tx,
)
from mhcoin.network.security import RateLimiter
from mhcoin.network.serialization import ProtocolError
from mhcoin.transaction.transaction import Transaction
from mhcoin.utxo import UTXOSet

if TYPE_CHECKING:
    from mhcoin.network.ban import BanManager
    from mhcoin.network.peer import Peer
    from mhcoin.network.sync import SyncManager

logger = logging.getLogger("mhcoin.p2p")


class TxRelay:
    """
    Mempool TX relay + chain BLOCK relay with Stage 6 fork/reorg support.

    No private keys. Consensus values are always computed locally.
    """

    def __init__(
        self,
        *,
        mempool: Mempool,
        get_utxo: Callable[[], UTXOSet],
        get_height: Callable[[], int],
        get_peers: Callable[[], list[Peer]],
        chain: Blockchain | None = None,
    ):
        self.mempool = mempool
        self.chain = chain
        self._get_utxo = get_utxo
        self._get_height = get_height
        self._get_peers = get_peers
        self.sync: SyncManager | None = None
        self.bans: BanManager | None = None
        self._lock = threading.Lock()
        self._inv_rate = RateLimiter(limit=MAX_INV_RATE_PER_PEER, window=INV_RATE_WINDOW)
        self._getdata_rate = RateLimiter(
            limit=MAX_GETDATA_RATE_PER_PEER, window=GETDATA_RATE_WINDOW
        )
        self._known_tx: set[str] = set()
        self._requested_tx: set[str] = set()
        self._announced_tx: dict[str, set[str]] = {}
        self._from_peer_tx: dict[str, str] = {}
        self._known_block: set[str] = set()
        self._requested_block: set[str] = set()
        self._announced_block: dict[str, set[str]] = {}
        self._from_peer_block: dict[str, str] = {}
        for tx in mempool.list_txs():
            self._known_tx.add(tx.txid_hex())
        if chain is not None and chain.tip_hash is not None:
            for h in range(chain.height + 1):
                b = chain.get_block_by_height(h)
                if b is not None:
                    self._known_block.add(b.block_hash().hex())

    def attach_sync(self, sync: SyncManager) -> None:
        self.sync = sync

    def attach_ban_manager(self, bans: BanManager) -> None:
        self.bans = bans

    def _penalize(self, peer: Peer | None, points: int, reason: str) -> bool:
        """
        Score misbehavior. If banned, disconnect.
        Returns True if peer is banned after this call.
        """
        if peer is None or self.bans is None:
            return False
        banned = self.bans.misbehavior(peer.host, points, reason=reason)
        if banned:
            try:
                peer.close()
            except Exception:
                pass
        return banned

    def _note_invalid(self, peer: Peer | None, reason: str) -> None:
        self._penalize(peer, MISBEHAVIOR_INVALID_BLOCK, reason)

    # --- TX path (Stage 3) -------------------------------------------------

    def knows(self, txid_hex: str) -> bool:
        with self._lock:
            return txid_hex in self._known_tx or self.mempool.get(txid_hex) is not None

    def mark_known(self, txid_hex: str) -> None:
        with self._lock:
            self._known_tx.add(txid_hex)

    def broadcast_transaction(self, tx: Transaction, *, source_peer: Peer | None = None) -> str:
        raw = encode_tx(tx)
        txid_hex = Transaction.deserialize(raw).txid_hex()
        assert tx.txid_hex() == txid_hex
        existing = self.mempool.get(txid_hex)
        if existing is None:
            try:
                self.mempool.add(tx, self._get_utxo(), height=max(self._get_height(), 0))
            except MempoolError as e:
                if "duplicate" not in str(e).lower():
                    logger.info("Rejected TX txid=%s reason=%s", txid_hex, e)
                    raise
        self.mark_known(txid_hex)
        if source_peer is not None:
            with self._lock:
                self._from_peer_tx[txid_hex] = source_peer.addr
        self._announce_tx_inv(txid_hex, exclude=source_peer.addr if source_peer else None)
        return txid_hex

    def accept_local(self, tx: Transaction) -> str:
        return self.broadcast_transaction(tx, source_peer=None)

    def sync_from_disk(self) -> int:
        before = {t.txid_hex() for t in self.mempool.list_txs()}
        self.mempool.reload()
        after = {t.txid_hex() for t in self.mempool.list_txs()}
        new = after - before
        for hx in new:
            self.mark_known(hx)
            self._announce_tx_inv(hx, exclude=None)
        n = len(new)
        if self.chain is not None:
            try:
                new_blocks = self.chain.refresh_from_disk()
            except Exception:
                logger.debug("chain disk refresh failed", exc_info=True)
                new_blocks = []
            for block in new_blocks:
                hx = block.block_hash().hex()
                with self._lock:
                    if hx in self._known_block:
                        continue
                    self._known_block.add(hx)
                self.mempool.clear_included(block.transactions[1:])
                self._announce_block_inv(hx, exclude=None)
                n += 1
        return n

    # --- BLOCK path (Stage 4) ----------------------------------------------

    def accept_block(self, block: Block, *, source_peer: Peer | None = None) -> int:
        """
        Validate + accept block (tip extension, side chain, or reorg).
        Returns active-chain height after processing.
        """
        if self.chain is None:
            raise ProtocolError("block relay not configured")
        raw = encode_block(block)
        block = Block.deserialize(raw)
        bh = block.block_hash()
        hx = bh.hex()

        existing = self.chain.get_block_by_hash(bh)
        if existing is not None:
            with self._lock:
                self._known_block.add(hx)
            logger.info("Duplicate block hash=%s ignored", hx)
            return self.chain.height

        try:
            result = self.chain.accept_block(block, mempool=self.mempool)
        except ValidationError as e:
            logger.info("Rejected block hash=%s reason=%s", hx, e)
            raise
        except ChainError as e:
            logger.info("Rejected block hash=%s reason=%s", hx, e)
            raise

        with self._lock:
            self._known_block.add(hx)
            self._requested_block.discard(hx)
            if source_peer is not None:
                self._from_peer_block[hx] = source_peer.addr

        if result.orphan:
            logger.info(
                "Orphan block stored hash=%s prev=%s",
                hx,
                block.header.previous_block_hash.hex(),
            )
            # Request missing parent when we have a peer
            if source_peer is not None:
                parent = block.header.previous_block_hash
                phx = parent.hex()
                with self._lock:
                    need = phx not in self._known_block and phx not in self._requested_block
                    if need and self.chain.get_block_by_hash(parent) is None:
                        self._requested_block.add(phx)
                        need_send = True
                    else:
                        need_send = False
                if need_send:
                    try:
                        source_peer.send_raw(
                            "GETDATA",
                            encode_getdata([InventoryVector(INV_TYPE_BLOCK, parent)]),
                        )
                        logger.info("Requesting orphan parent hash=%s", phx)
                    except Exception:
                        logger.debug("orphan parent GETDATA failed", exc_info=True)
            return self.chain.height

        if result.reorg:
            logger.info(
                "Reorg activated tip=%s height=%s disconnect=%s connect=%s",
                result.tip_hash.hex(),
                result.height,
                len(result.disconnect),
                len(result.connect),
            )
            # Announce new tip (and newly connected blocks) via INV
            for ch in result.connect:
                self._announce_block_inv(
                    ch.hex(), exclude=source_peer.addr if source_peer else None
                )
            return result.height

        if result.activated:
            self.mempool.clear_included(block.transactions[1:])
            logger.info("Accepted block hash=%s height=%s", hx, result.height)
            self._announce_block_inv(hx, exclude=source_peer.addr if source_peer else None)
            return result.height

        if result.side_chain:
            logger.info(
                "Side-chain block accepted hash=%s (active tip unchanged height=%s)",
                hx,
                result.height,
            )
            # Still announce so peers can discover the fork; dedup prevents storms
            self._announce_block_inv(hx, exclude=source_peer.addr if source_peer else None)
            return result.height

        return result.height

    def broadcast_block(self, block: Block, *, source_peer: Peer | None = None) -> int:
        return self.accept_block(block, source_peer=source_peer)

    # --- Peer handlers -----------------------------------------------------

    def on_inv(self, peer: Peer, payload: bytes) -> None:
        if not self._inv_rate.allow(peer.addr):
            logger.info("INV rate-limited from %s", peer.addr)
            self._penalize(peer, MISBEHAVIOR_FLOOD, "INV flood")
            return
        try:
            items = decode_inv(payload)
        except ProtocolError as e:
            logger.warning("malformed INV from %s: %s", peer.addr, e)
            self._penalize(peer, MISBEHAVIOR_PROTOCOL, f"bad INV: {e}")
            peer.close()
            return
        want: list[InventoryVector] = []
        for inv in items:
            hx = inv.hash.hex()
            if inv.type == INV_TYPE_TX:
                logger.info("Received INV txid=%s from %s", hx, peer.addr)
                with self._lock:
                    known = hx in self._known_tx or self.mempool.get(hx) is not None
                    already = hx in self._requested_tx
                    if known or already:
                        continue
                    if len(self._requested_tx) >= MAX_REQUESTED_TX:
                        continue
                    self._requested_tx.add(hx)
                    self._from_peer_tx.setdefault(hx, peer.addr)
                want.append(inv)
            elif inv.type == INV_TYPE_BLOCK:
                logger.info("Received block INV hash=%s from %s", hx, peer.addr)
                with self._lock:
                    known = hx in self._known_block
                    if self.chain is not None and self.chain.get_block_by_hash(inv.hash) is not None:
                        known = True
                        self._known_block.add(hx)
                    already = hx in self._requested_block
                    if known or already:
                        continue
                    if len(self._requested_block) >= MAX_REQUESTED_BLOCKS:
                        continue
                    self._requested_block.add(hx)
                    self._from_peer_block.setdefault(hx, peer.addr)
                want.append(inv)
        if not want:
            return
        for inv in want:
            if inv.type == INV_TYPE_TX:
                logger.info("Requesting TX txid=%s", inv.hash.hex())
            else:
                logger.info("Requesting block hash=%s", inv.hash.hex())
        try:
            peer.send_raw("GETDATA", encode_getdata(want))
        except Exception:
            logger.exception("GETDATA send failed to %s", peer.addr)

    def on_getdata(self, peer: Peer, payload: bytes) -> None:
        if self.chain is not None and getattr(self.chain, "_closed", False):
            return
        if not self._getdata_rate.allow(peer.addr):
            logger.info("GETDATA rate-limited from %s", peer.addr)
            self._penalize(peer, MISBEHAVIOR_FLOOD, "GETDATA flood")
            return
        try:
            items = decode_getdata(payload)
        except ProtocolError as e:
            logger.warning("malformed GETDATA from %s: %s", peer.addr, e)
            self._penalize(peer, MISBEHAVIOR_PROTOCOL, f"bad GETDATA: {e}")
            peer.close()
            return
        for inv in items:
            hx = inv.hash.hex()
            if inv.type == INV_TYPE_TX:
                tx = self.mempool.get(hx)
                if tx is None:
                    logger.debug("GETDATA unknown txid=%s from %s", hx, peer.addr)
                    continue
                try:
                    peer.send_raw("TX", encode_tx(tx))
                    logger.info("Sent TX txid=%s to %s", hx, peer.addr)
                except ProtocolError as e:
                    logger.warning("cannot encode TX %s: %s", hx, e)
            elif inv.type == INV_TYPE_BLOCK:
                if self.chain is None or getattr(self.chain, "_closed", False):
                    continue
                try:
                    block = self.chain.get_block_by_hash(inv.hash)
                except Exception:
                    logger.debug("GETDATA block read failed hash=%s", hx, exc_info=True)
                    continue
                if block is None:
                    logger.debug("GETDATA unknown block hash=%s from %s", hx, peer.addr)
                    continue
                try:
                    peer.send_raw("BLOCK", encode_block(block))
                    logger.info("Sent BLOCK hash=%s to %s", hx, peer.addr)
                except ProtocolError as e:
                    logger.warning("cannot encode BLOCK %s: %s", hx, e)

    def on_tx(self, peer: Peer, payload: bytes) -> None:
        try:
            tx = decode_tx(payload)
        except ProtocolError as e:
            logger.warning("malformed TX from %s: %s", peer.addr, e)
            self._penalize(peer, MISBEHAVIOR_PROTOCOL, f"bad TX: {e}")
            peer.close()
            return
        txid_hex = tx.txid_hex()
        logger.info("Received TX txid=%s from %s", txid_hex, peer.addr)
        with self._lock:
            self._requested_tx.discard(txid_hex)
            if txid_hex in self._known_tx and self.mempool.get(txid_hex) is not None:
                return
        try:
            self.mempool.add(tx, self._get_utxo(), height=max(self._get_height(), 0))
        except MempoolError as e:
            if "duplicate" in str(e).lower():
                self.mark_known(txid_hex)
                return
            logger.info("Rejected TX txid=%s reason=%s", txid_hex, e)
            self._penalize(peer, MISBEHAVIOR_INVALID_TX, f"invalid TX: {e}")
            return
        logger.info("Accepted TX txid=%s", txid_hex)
        self.mark_known(txid_hex)
        with self._lock:
            self._from_peer_tx[txid_hex] = peer.addr
        self._announce_tx_inv(txid_hex, exclude=peer.addr)

    def on_block(self, peer: Peer, payload: bytes) -> None:
        try:
            block = decode_block(payload)
        except ProtocolError as e:
            logger.warning("malformed BLOCK from %s: %s", peer.addr, e)
            self._penalize(peer, MISBEHAVIOR_PROTOCOL, f"bad BLOCK: {e}")
            peer.close()
            return
        hx = block.block_hash().hex()
        logger.info("Received block hash=%s from %s", hx, peer.addr)
        with self._lock:
            self._requested_block.discard(hx)

        # Stage 5 ordered sync download
        if self.sync is not None and self.sync.handle_incoming_block(peer, block):
            return

        try:
            height = self.accept_block(block, source_peer=peer)
            logger.info("Validating/accepted block height=%s hash=%s", height, hx)
        except (ValidationError, ChainError, ProtocolError) as e:
            logger.info("Rejected block hash=%s reason=%s", hx, e)
            self._note_invalid(peer, str(e))
            return

    def _announce_tx_inv(self, txid_hex: str, *, exclude: str | None) -> None:
        inv = InventoryVector(type=INV_TYPE_TX, hash=bytes.fromhex(txid_hex))
        payload = encode_inv([inv])
        for p in self._get_peers():
            if exclude is not None and p.addr == exclude:
                continue
            with self._lock:
                sent = self._announced_tx.setdefault(txid_hex, set())
                if p.addr in sent:
                    continue
                sent.add(p.addr)
            try:
                p.send_raw("INV", payload)
                logger.info("Relaying INV txid=%s to %s", txid_hex, p.addr)
            except Exception:
                logger.debug("INV relay failed to %s", p.addr)

    def _announce_block_inv(self, block_hex: str, *, exclude: str | None) -> None:
        inv = InventoryVector(type=INV_TYPE_BLOCK, hash=bytes.fromhex(block_hex))
        payload = encode_inv([inv])
        for p in self._get_peers():
            if exclude is not None and p.addr == exclude:
                continue
            with self._lock:
                sent = self._announced_block.setdefault(block_hex, set())
                if p.addr in sent:
                    continue
                sent.add(p.addr)
            try:
                p.send_raw("INV", payload)
                logger.info("Relaying block INV hash=%s to %s", block_hex, p.addr)
            except Exception:
                logger.debug("block INV relay failed to %s", p.addr)

    # backward-compatible alias used by older Stage 3 tests
    def _announce_inv(self, txid_hex: str, *, exclude: str | None) -> None:
        self._announce_tx_inv(txid_hex, exclude=exclude)
