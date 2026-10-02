"""Stage 5–6 — chain synchronization (HEADERS + block download + forks).

Stage 6: headers may describe a competing branch; blocks are validated locally;
fork choice uses cumulative work (never peer height).
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import TYPE_CHECKING, Callable

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.chain import Blockchain, BlockIndexEntry, ChainError
from mhcoin.blockchain.validation import ValidationError
from mhcoin.consensus.difficulty import bits_to_target, pow_limit_for_network
from mhcoin.consensus.proof_of_work import verify_proof_of_work
from mhcoin.constants import MAX_FUTURE_BLOCK_TIME, SUPPORTED_BLOCK_VERSIONS
from mhcoin.network.constants import (
    DEFAULT_HEADERS_TIMEOUT,
    DEFAULT_SYNC_TIMEOUT,
    GETHEADERS_RATE_WINDOW,
    INV_TYPE_BLOCK,
    MAX_GETHEADERS_RATE_PER_PEER,
    MAX_HEADERS,
    MAX_LOCATOR_HASHES,
    MAX_SYNC_BLOCK_BATCH,
    MISBEHAVIOR_FLOOD,
    MISBEHAVIOR_INVALID_BLOCK,
    MISBEHAVIOR_PROTOCOL,
    ZERO_HASH,
)
from mhcoin.network.messages import (
    InventoryVector,
    decode_getheaders,
    decode_headers,
    encode_getdata,
    encode_getheaders,
    encode_headers,
    encode_inv,
)
from mhcoin.network.security import RateLimiter
from mhcoin.network.serialization import ProtocolError

if TYPE_CHECKING:
    from mhcoin.network.peer import Peer
    from mhcoin.network.relay import TxRelay

logger = logging.getLogger("mhcoin.p2p")


class SyncState(enum.Enum):
    IDLE = "IDLE"
    REQUESTING_HEADERS = "REQUESTING_HEADERS"
    DOWNLOADING_BLOCKS = "DOWNLOADING_BLOCKS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class SyncManager:
    """
    Sync toward peers (IBD + competing chain discovery).

    Flow:
      GETHEADERS(locator) → HEADERS → GETDATA(BLOCK)×N → BLOCK×N
      → accept_block (may store side chain / reorg) → repeat.
    """

    def __init__(
        self,
        *,
        chain: Blockchain,
        relay: TxRelay,
        get_our_height: Callable[[], int],
    ):
        self.chain = chain
        self.relay = relay
        self._get_our_height = get_our_height
        self._lock = threading.Lock()
        self.state = SyncState.IDLE
        self.sync_peer: Peer | None = None
        self._pending_hashes: list[bytes] = []
        self._next_apply_index = 0
        self._inflight: set[str] = set()
        self._buffer: dict[str, Block] = {}
        self._last_activity = 0.0
        self._headers_timeout = DEFAULT_HEADERS_TIMEOUT
        self._sync_timeout = DEFAULT_SYNC_TIMEOUT
        self.progress_height = 0
        self.target_hint = 0
        self._getheaders_rate = RateLimiter(
            limit=MAX_GETHEADERS_RATE_PER_PEER, window=GETHEADERS_RATE_WINDOW
        )

    def _penalize(self, peer: Peer, points: int, reason: str) -> None:
        bans = getattr(self.relay, "bans", None)
        if bans is None:
            return
        banned = bans.misbehavior(peer.host, points, reason=reason)
        if banned:
            try:
                peer.close()
            except Exception:
                pass

    @property
    def is_syncing(self) -> bool:
        return self.state in (SyncState.REQUESTING_HEADERS, SyncState.DOWNLOADING_BLOCKS)

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state.value,
                "peer": self.sync_peer.addr if self.sync_peer else None,
                "height": self._get_our_height(),
                "progress_height": self.progress_height,
                "target_hint": self.target_hint,
                "pending_blocks": len(self._pending_hashes) - self._next_apply_index,
            }

    def maybe_start(self, peer: Peer) -> None:
        """Hint only: peer.start_height is NOT consensus authority."""
        remote_h = peer.remote_start_height or 0
        our_h = self._get_our_height()
        if remote_h <= our_h:
            # We are ahead (or equal): offer missing blocks so lagging peers catch up
            # even if their IBD aborted (e.g. duplicate WAN/LAN path closed mid-sync).
            self.offer_catchup(peer)
            return
        with self._lock:
            if self.is_syncing:
                sp = self.sync_peer
                # Allow restart if current sync peer is gone / not handshaked.
                if sp is not None and sp is not peer and getattr(sp, "state", None) is not None:
                    from mhcoin.network.peer import PeerState

                    if sp.state == PeerState.HANDSHAKED:
                        return
            self.sync_peer = peer
            self.target_hint = remote_h
            self.progress_height = our_h
        logger.info(
            "Starting sync with %s (us=%s peer_hint=%s — hint only)",
            peer.addr,
            our_h,
            remote_h,
        )
        self._request_headers(peer)

    def offer_catchup(self, peer: Peer) -> None:
        """INV active-chain blocks the peer is missing (seed → lagging client)."""
        their_h = int(peer.remote_start_height or 0)
        our_h = self._get_our_height()
        if their_h < 0 or their_h >= our_h:
            return
        # Cap batch to avoid huge INV on very stale peers (they still run GETHEADERS).
        start = their_h + 1
        end = min(our_h, their_h + 64)
        items: list[InventoryVector] = []
        for h in range(start, end + 1):
            try:
                block = self.chain.get_block_by_height(h)
            except Exception:
                block = None
            if block is None:
                continue
            items.append(InventoryVector(INV_TYPE_BLOCK, block.block_hash()))
        if not items:
            return
        logger.info(
            "Offering catch-up INV to %s blocks %s..%s (count=%s)",
            peer.addr,
            start,
            start + len(items) - 1,
            len(items),
        )
        try:
            peer.send_raw("INV", encode_inv(items))
        except Exception:
            logger.debug("catch-up INV failed to %s", peer.addr, exc_info=True)

    def on_peer_disconnected(self, peer: Peer) -> None:
        """Abort in-flight sync if the sync peer drops (WAN/LAN duplicate close)."""
        with self._lock:
            if self.sync_peer is not peer:
                return
            was_syncing = self.is_syncing
            self.state = SyncState.IDLE
            self.sync_peer = None
            self._pending_hashes = []
            self._inflight.clear()
            self._buffer.clear()
        if was_syncing:
            logger.info("Sync peer disconnected %s — reset to IDLE for retry", peer.addr)

    def consider_peer_headers(self, peer: Peer) -> None:
        """Optionally probe a peer for competing headers (fork discovery)."""
        with self._lock:
            if self.is_syncing:
                return
            self.sync_peer = peer
        self._request_headers(peer)

    def tick(self) -> None:
        if not self.is_syncing:
            return
        now = time.time()
        with self._lock:
            idle = now - self._last_activity if self._last_activity else 0
            peer = self.sync_peer
            state = self.state
        if peer is None:
            return
        timeout = (
            self._headers_timeout
            if state == SyncState.REQUESTING_HEADERS
            else self._sync_timeout
        )
        if self._last_activity and idle > timeout:
            logger.warning("Sync timeout with %s (state=%s)", peer.addr, state.value)
            self._fail("timeout")

    def on_getheaders(self, peer: Peer, payload: bytes) -> None:
        if not self._getheaders_rate.allow(peer.addr):
            logger.info("GETHEADERS rate-limited from %s", peer.addr)
            self._penalize(peer, MISBEHAVIOR_FLOOD, "GETHEADERS flood")
            return
        try:
            locator, hash_stop = decode_getheaders(payload)
        except ProtocolError as e:
            logger.warning("malformed GETHEADERS from %s: %s", peer.addr, e)
            self._penalize(peer, MISBEHAVIOR_PROTOCOL, f"bad GETHEADERS: {e}")
            peer.close()
            return
        headers = self.chain.headers_after_locator(
            locator, hash_stop=hash_stop, limit=MAX_HEADERS
        )
        logger.info("GETHEADERS from %s → sending %s headers", peer.addr, len(headers))
        try:
            peer.send_raw("HEADERS", encode_headers(headers))
        except Exception:
            logger.exception("HEADERS send failed to %s", peer.addr)

    def on_headers(self, peer: Peer, payload: bytes) -> None:
        try:
            headers = decode_headers(payload)
        except ProtocolError as e:
            logger.warning("malformed HEADERS from %s: %s", peer.addr, e)
            self._penalize(peer, MISBEHAVIOR_PROTOCOL, f"bad HEADERS: {e}")
            peer.close()
            return

        with self._lock:
            if self.sync_peer is not None and peer is not self.sync_peer and self.is_syncing:
                logger.debug("Ignoring HEADERS from non-sync peer %s", peer.addr)
                return
            self._last_activity = time.time()

        if not headers:
            logger.info("HEADERS empty from %s — sync complete or no more", peer.addr)
            self._complete()
            return

        if self.chain.tip_hash is None:
            logger.warning("Cannot sync: no local tip")
            self._fail("no tip")
            return

        first_prev = headers[0].previous_block_hash
        # Stage 6: parent must be known (active or side); need not be active tip
        if self.chain.get_index(first_prev) is None and first_prev != ZERO_HASH:
            logger.info(
                "HEADERS from %s: unknown parent %s — cannot validate yet",
                peer.addr,
                first_prev.hex(),
            )
            self._fail("unknown header parent")
            return

        try:
            hashes = self._validate_header_chain(headers, first_prev)
        except ValidationError as e:
            logger.info("Rejected HEADERS from %s: %s", peer.addr, e)
            self._penalize(peer, MISBEHAVIOR_INVALID_BLOCK, f"bad headers: {e}")
            self._fail(str(e))
            peer.close()
            return

        # Skip hashes we already have fully stored
        need = [h for h in hashes if self.chain.get_block_by_hash(h) is None]
        if not need:
            logger.info("HEADERS from %s already known — requesting more", peer.addr)
            with self._lock:
                self.sync_peer = peer
                self._last_activity = time.time()
            self._request_headers(peer)
            return

        logger.info(
            "Accepted %s headers (%s new) from %s — downloading blocks",
            len(hashes),
            len(need),
            peer.addr,
        )
        with self._lock:
            self._pending_hashes = need
            self._next_apply_index = 0
            self._inflight.clear()
            self._buffer.clear()
            self.state = SyncState.DOWNLOADING_BLOCKS
            self.sync_peer = peer
            self._last_activity = time.time()
        self._request_next_blocks(peer)

    def on_block_received(self, peer: Peer, block: Block) -> bool:
        return self.handle_incoming_block(peer, block)

    def handle_incoming_block(self, peer: Peer, block: Block) -> bool:
        hx = block.block_hash().hex()
        with self._lock:
            if not self.is_syncing or self.state != SyncState.DOWNLOADING_BLOCKS:
                return False
            if self.sync_peer is not None and peer is not self.sync_peer:
                return False
            pending_hex = {h.hex() for h in self._pending_hashes[self._next_apply_index :]}
            if hx not in pending_hex:
                return False
            self._inflight.discard(hx)
            self._buffer[hx] = block
            self._last_activity = time.time()

        self._drain_buffer(peer)
        return True

    def _drain_buffer(self, peer: Peer) -> None:
        while True:
            with self._lock:
                if self._next_apply_index >= len(self._pending_hashes):
                    break
                expect = self._pending_hashes[self._next_apply_index]
                ex = expect.hex()
                if self.chain.get_block_by_hash(expect) is not None:
                    self._next_apply_index += 1
                    self._buffer.pop(ex, None)
                    self._inflight.discard(ex)
                    continue
                block = self._buffer.pop(ex, None)
                if block is None:
                    break
                self._next_apply_index += 1
            try:
                height = self.relay.accept_block(block, source_peer=peer)
                self.progress_height = height
                # Keep sync heartbeat alive during slow local validation / disk IO
                # so IBD does not false-timeout mid-batch.
                with self._lock:
                    self._last_activity = time.time()
                logger.info(
                    "Sync applied block height=%s hash=%s",
                    height,
                    block.block_hash().hex(),
                )
            except (ValidationError, ChainError, ProtocolError) as e:
                logger.info(
                    "Sync rejected block hash=%s reason=%s",
                    block.block_hash().hex(),
                    e,
                )
                self._fail(str(e))
                return

        with self._lock:
            remaining = len(self._pending_hashes) - self._next_apply_index
            peer_ref = self.sync_peer
        if remaining <= 0:
            if peer_ref:
                self._request_headers(peer_ref)
            return
        if peer_ref:
            self._request_next_blocks(peer_ref)

    def _request_headers(self, peer: Peer) -> None:
        locator = self.chain.build_locator(max_hashes=MAX_LOCATOR_HASHES)
        with self._lock:
            self.state = SyncState.REQUESTING_HEADERS
            self.sync_peer = peer
            self._pending_hashes = []
            self._next_apply_index = 0
            self._inflight.clear()
            self._buffer.clear()
            self._last_activity = time.time()
        logger.info("Requesting headers from %s (locator=%s)", peer.addr, len(locator))
        try:
            peer.send_raw("GETHEADERS", encode_getheaders(locator))
        except Exception:
            logger.exception("GETHEADERS send failed")
            self._fail("send failed")

    def _request_next_blocks(self, peer: Peer) -> None:
        with self._lock:
            while self._next_apply_index < len(self._pending_hashes):
                h = self._pending_hashes[self._next_apply_index]
                if self.chain.get_block_by_hash(h) is None:
                    break
                self._next_apply_index += 1

            if self._next_apply_index >= len(self._pending_hashes):
                need_more_headers = True
                batch: list[InventoryVector] = []
            else:
                need_more_headers = False
                start = self._next_apply_index
                end = min(len(self._pending_hashes), start + MAX_SYNC_BLOCK_BATCH)
                batch = []
                for h in self._pending_hashes[start:end]:
                    hx = h.hex()
                    if hx in self._inflight or hx in self._buffer:
                        continue
                    if self.chain.get_block_by_hash(h) is not None:
                        continue
                    self._inflight.add(hx)
                    batch.append(InventoryVector(INV_TYPE_BLOCK, h))
                self._last_activity = time.time()

        if need_more_headers:
            self._request_headers(peer)
            return
        if not batch:
            return
        logger.info("Requesting %s blocks from %s", len(batch), peer.addr)
        try:
            peer.send_raw("GETDATA", encode_getdata(batch))
        except Exception:
            logger.exception("sync GETDATA failed")
            self._fail("getdata failed")

    def _validate_header_chain(
        self, headers: list[BlockHeader], expected_prev: bytes
    ) -> list[bytes]:
        prev = expected_prev
        parent = self.chain.get_index(expected_prev)
        if parent is None:
            raise ValidationError("unknown parent for header chain")
        overlay: dict[bytes, BlockIndexEntry] = {}
        hashes: list[bytes] = []
        limit = pow_limit_for_network(self.chain.network)
        for i, hdr in enumerate(headers):
            if hdr.previous_block_hash != prev:
                raise ValidationError(f"header chain broken at index {i}")
            if hdr.version not in SUPPORTED_BLOCK_VERSIONS:
                raise ValidationError(f"unsupported header version {hdr.version}")
            try:
                expected_bits = self.chain.get_next_work_for_parent(prev, overlay=overlay)
            except ChainError as e:
                raise ValidationError(str(e)) from e
            if hdr.bits != expected_bits:
                raise ValidationError(
                    f"unexpected header bits at index {i}: "
                    f"got 0x{hdr.bits:08x}, expected 0x{expected_bits:08x}"
                )
            try:
                target = bits_to_target(hdr.bits)
            except ValueError as e:
                raise ValidationError(str(e)) from e
            if target > limit:
                raise ValidationError("header target exceeds POW_LIMIT")
            if not verify_proof_of_work(hdr):
                raise ValidationError(f"invalid header PoW at index {i}")
            try:
                mtp = self.chain.median_time_past_for_parent(prev, overlay=overlay)
            except ChainError as e:
                raise ValidationError(str(e)) from e
            if hdr.timestamp <= mtp:
                raise ValidationError(
                    f"header timestamp {hdr.timestamp} <= median time past {mtp}"
                )
            if hdr.timestamp > int(time.time()) + MAX_FUTURE_BLOCK_TIME:
                raise ValidationError("header timestamp too far in future")
            h = hdr.block_hash()
            parent_entry = overlay.get(prev) or self.chain.get_index(prev)
            if parent_entry is None:
                raise ValidationError("missing parent index during header validation")
            overlay[h] = BlockIndexEntry(
                block_hash=h,
                prev_hash=prev,
                height=parent_entry.height + 1,
                chain_work=0,
                status=0,
                bits=hdr.bits,
                timestamp=hdr.timestamp,
            )
            hashes.append(h)
            prev = h
            if len(hashes) > MAX_HEADERS:
                raise ValidationError("too many headers")
        return hashes

    def _complete(self) -> None:
        with self._lock:
            self.state = SyncState.COMPLETE
            self.sync_peer = None
            self._pending_hashes = []
            self._inflight.clear()
            self._buffer.clear()
            self.progress_height = self._get_our_height()
        logger.info("Sync complete at height=%s", self.progress_height)

    def _fail(self, reason: str) -> None:
        with self._lock:
            self.state = SyncState.IDLE
            self.sync_peer = None
            self._pending_hashes = []
            self._inflight.clear()
            self._buffer.clear()
        logger.info("Sync failed: %s (back to IDLE)", reason)
