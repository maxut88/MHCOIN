"""Solo miner loop for end-user CLI (no consensus changes)."""

from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from mhcoin.blockchain.genesis import get_network_genesis
from mhcoin.consensus.params import get_network_params
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.mining.abortable_pow import MiningAborted, mine_block_cancellable
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


def _tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


class _Style:
    """Lightweight ANSI styling for mining console (no-op when not a TTY)."""

    def __init__(self) -> None:
        on = _tty()
        self.reset = "\033[0m" if on else ""
        self.dim = "\033[2m" if on else ""
        self.bold = "\033[1m" if on else ""
        self.green = "\033[92m" if on else ""
        self.cyan = "\033[96m" if on else ""
        self.yellow = "\033[93m" if on else ""
        self.mag = "\033[95m" if on else ""

    def c(self, color: str, text: str) -> str:
        if not color:
            return text
        return f"{color}{text}{self.reset}"


def _fmt_hps(hps: float) -> str:
    if hps >= 1_000_000:
        return f"{hps / 1_000_000:.2f} MH/s"
    if hps >= 1_000:
        return f"{hps / 1_000:.1f} kH/s"
    return f"{hps:,.0f} H/s"


def _rule(s: _Style, char: str = "─", width: int = 62) -> str:
    return s.c(s.dim, char * width)


def format_mine_banner(
    *,
    network: str,
    address: str,
    data_dir: Path,
    tip: int,
    mode: str = "live",
    peers: int = 0,
    peer_tip: int | None = None,
) -> str:
    s = _Style()
    title = "MHCOIN Live Miner" if mode == "live" else "MHCOIN Solo Miner"
    peer_bit = f"peers {peers}"
    if peer_tip is not None:
        peer_bit += f" · net #{peer_tip}"
    lines = [
        s.c(s.cyan + s.bold, "╔════════════════════════════════════════════════════════════╗"),
        s.c(s.cyan + s.bold, "║")
        + s.c(s.bold, f"  {title}")
        + s.c(s.dim, f"  ·  {network:<32}")
        + s.c(s.cyan + s.bold, "║"),
        s.c(s.cyan + s.bold, "╚════════════════════════════════════════════════════════════╝"),
        f"  {s.c(s.dim, 'mode')}     {s.c(s.green if mode == 'live' else s.yellow, mode)}  ({peer_bit})",
        f"  {s.c(s.dim, 'tip')}      {s.c(s.bold, '#' + str(tip))}",
        f"  {s.c(s.dim, 'reward')}   {address}",
        f"  {s.c(s.dim, 'data')}     {data_dir}",
        f"  {s.c(s.dim, 'stop')}     Ctrl+C",
        _rule(s),
    ]
    return "\n".join(lines)


def format_mine_progress(*, height: int, nonce: int, hps: float) -> str:
    s = _Style()
    return (
        f"  {s.c(s.dim, '·')} height {s.c(s.cyan, str(height))}  "
        f"nonce {nonce:>10,}  {_fmt_hps(hps)}"
    )


def format_mine_found(
    *,
    height: int,
    block_hash: str,
    reward_sats: int,
    elapsed: float,
    session_blocks: int,
    session_reward_sats: int,
) -> str:
    s = _Style()
    lines = [
        "",
        s.c(s.green + s.bold, "▸ BLOCK FOUND"),
        f"  {s.c(s.dim, 'height')}   {s.c(s.bold, '#' + str(height))}",
        f"  {s.c(s.dim, 'hash')}     {s.c(s.yellow, block_hash)}",
        f"  {s.c(s.dim, 'reward')}   {s.c(s.green, format_mhc(reward_sats) + ' MHC')}",
        f"  {s.c(s.dim, 'time')}     {elapsed:.2f}s",
        f"  {s.c(s.dim, 'session')}  {session_blocks} blocks · "
        f"{format_mhc(session_reward_sats)} MHC",
        _rule(s),
    ]
    return "\n".join(lines)


def format_mine_plain_found(
    *,
    height: int,
    block_hash: str,
    reward_sats: int,
    elapsed: float,
) -> str:
    """Plain (no ANSI) multi-line FOUND — used by Desktop log panel."""
    return (
        f"▸ BLOCK FOUND  #{height}\n"
        f"  hash    {block_hash}\n"
        f"  reward  {format_mhc(reward_sats)} MHC\n"
        f"  time    {elapsed:.2f}s"
    )


class SoloMiner:
    """
    Mine MHC blocks.

    Default on networks with seeds (mainnet): live P2P — sync tip, mine, broadcast
    BLOCK INV so everyone competes on the same chain (same as Desktop).

    Offline/local (regtest/localnet, or --offline): LocalNode only, no broadcast.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        network: str,
        hrp: str,
        address: str,
        connect: list[str] | None = None,
        offline: bool = False,
    ):
        import threading

        from mhcoin.network.seeds import default_connect_peers

        params = get_network_params(network)
        if not validate_address(address, hrp=hrp):
            raise ValueError(f"invalid address for hrp={hrp}")
        self.network = params.name
        self.hrp = hrp
        self.address = address
        self.pkh = address_to_pubkey_hash(address, hrp=hrp)
        self.data_dir = Path(data_dir)
        self._stop = False
        self._rt = None
        self._node_thread = None
        self._connect = list(connect) if connect is not None else None

        seeds = default_connect_peers(self.network) if self._connect is None else list(self._connect)
        # Live when we have dial targets (or user forced connect list), unless offline.
        self._live = (not offline) and bool(seeds)
        self._local = None
        self._hashrate = 0.0
        self._status_last_broadcast = 0.0
        if not self._live:
            self._local = LocalNode(self.data_dir, hrp=hrp, network=params.name)

    @property
    def node(self):
        """Backward-compatible handle — LocalNode offline, else NodeRuntime-like chain owner."""
        if self._local is not None:
            return self._local
        if self._rt is not None:
            return self._rt
        raise RuntimeError("miner not started")

    def ensure_chain(self) -> None:
        if self._live:
            self._ensure_live()
            return
        assert self._local is not None
        if self._local.chain.height >= 0:
            return
        genesis = get_network_genesis(self.network)
        self._local.chain.init_with_genesis(genesis)
        logger.info(
            "Installed frozen %s genesis %s",
            self.network,
            genesis.block_hash().hex(),
        )

    def _peer_tip(self) -> tuple[int, int]:
        """Return (handshaked_peers, max_peer_height)."""
        rt = self._rt
        if rt is None:
            return 0, -1
        peers = rt.p2p.get_peers() or []
        hs = [p for p in peers if str(p.get("state") or "") == "HANDSHAKED"]
        heights = [int(p.get("height") or 0) for p in hs]
        return len(hs), (max(heights) if heights else -1)

    def _ensure_live(self) -> None:
        import os
        import threading

        from mhcoin.network.seeds import default_connect_peers
        from mhcoin.node.runtime import NodeRuntime

        if self._rt is not None and not getattr(self._rt, "_stopped", False):
            return

        pid_path = self.data_dir / "node.pid"
        if pid_path.is_file():
            try:
                old_pid = int(pid_path.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            alive = False
            if old_pid > 0:
                try:
                    os.kill(old_pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive and old_pid != os.getpid():
                raise RuntimeError(
                    f"another MHCOIN node/desktop is using {self.data_dir} (pid {old_pid}). "
                    "Stop it first, or pass a separate --data-dir."
                )

        peers = (
            list(self._connect)
            if self._connect is not None
            else default_connect_peers(self.network)
        )
        params = get_network_params(self.network)
        rt = NodeRuntime(
            data_dir=self.data_dir,
            network=self.network,
            host="0.0.0.0",
            port=params.default_port,
            connect=peers,
            # Outbound-only — same as Desktop; many miners can run without fighting :8333.
            enable_listen=False,
        )
        self._rt = rt

        def _run() -> None:
            try:
                rt.start(blocking=True)
            except Exception:
                logger.exception("live mining node stopped")

        self._node_thread = threading.Thread(target=_run, name="mhcoin-mine-node", daemon=True)
        self._node_thread.start()

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not self._stop:
            if getattr(rt, "_running", False) and not getattr(rt, "_stopped", False):
                break
            time.sleep(0.05)
        if getattr(rt, "_stopped", False) or not getattr(rt, "_running", False):
            raise RuntimeError("failed to start live P2P node for mining")

        # Dial + sync toward network tip before hashing.
        print("  connecting to network…", flush=True)
        sync_deadline = time.monotonic() + 120.0
        last_print = 0.0
        while time.monotonic() < sync_deadline and not self._stop:
            n_peers, peer_h = self._peer_tip()
            our_h = int(rt.chain.height)
            now = time.time()
            if now - last_print >= 2.0:
                last_print = now
                print(
                    f"  sync  local #{our_h}  peers {n_peers}"
                    + (f"  net #{peer_h}" if peer_h >= 0 else ""),
                    flush=True,
                )
            if n_peers > 0 and peer_h >= 0 and our_h >= peer_h:
                print(f"  synced to network tip #{our_h}", flush=True)
                return
            # If we have peers but still catching up, keep waiting.
            if n_peers > 0 and peer_h >= 0 and our_h < peer_h:
                time.sleep(0.4)
                continue
            # No peers yet — keep trying briefly.
            time.sleep(0.4)
        n_peers, peer_h = self._peer_tip()
        our_h = int(rt.chain.height)
        if n_peers <= 0:
            print(
                "  warning: no peers yet — mining may stay local until connected",
                flush=True,
            )
        elif peer_h >= 0 and our_h < peer_h:
            print(
                f"  warning: still behind net #{peer_h} (local #{our_h}) — continuing",
                flush=True,
            )

    def _broadcast_miner_status(self, *, force: bool = False, mining: bool | None = None) -> None:
        """Advise peers of terminal mining hashrate (STATUS). Throttled ~10s."""
        if not self._live or self._rt is None:
            return
        now = time.time()
        if not force and (now - float(self._status_last_broadcast or 0.0)) < 10.0:
            return
        is_mining = (not self._stop) if mining is None else bool(mining)
        rt = self._rt
        if getattr(rt, "_stopped", False):
            return
        p2p = getattr(rt, "p2p", None)
        if p2p is None:
            return
        try:
            from mhcoin.network.messages import StatusPayload, encode_status

            hps = int(max(0.0, float(self._hashrate or 0.0))) if is_mining else 0
            try:
                height = int(rt.chain.height)
            except Exception:
                height = -1
            n = p2p.broadcast(
                "STATUS",
                encode_status(StatusPayload(mining=is_mining, hps=hps, height=height)),
            )
            self._status_last_broadcast = now
            if n:
                logger.debug(
                    "STATUS broadcast mining=%s hps=%s height=%s peers=%s",
                    is_mining,
                    hps,
                    height,
                    n,
                )
        except Exception:
            logger.debug("STATUS broadcast failed", exc_info=True)

    def request_stop(self, *_args) -> None:
        self._stop = True

    def mine_one(self) -> MineResult:
        self.ensure_chain()
        if self._stop:
            raise RuntimeError("stopped")
        if self._live:
            return self._mine_one_live()
        return self._mine_one_local()

    def _mine_one_local(self) -> MineResult:
        assert self._local is not None
        height = self._local.chain.height + 1
        assert self._local.chain.tip_hash is not None
        tip_hash = self._local.chain.tip_hash
        bits = self._local.chain.get_next_work_for_parent(tip_hash)
        mtp = self._local.chain.median_time_past_for_parent(tip_hash)
        ts = int(time.time())
        if ts <= mtp:
            ts = mtp + 1
        block = build_block_template(
            height=height,
            previous_hash=tip_hash,
            timestamp=ts,
            bits=bits,
            mempool=self._local.mempool,
            utxo=self._local.chain.utxo,
            miner_pubkey_hash=self.pkh,
        )

        def _progress(nonce: int, _h: bytes, hps: float) -> None:
            if self._stop:
                raise KeyboardInterrupt("stop requested")
            if nonce > 0 and nonce % 500_000 == 0:
                print(format_mine_progress(height=height, nonce=nonce, hps=hps), flush=True)

        def _abort() -> bool:
            if self._stop:
                return True
            tip = self._local.chain.tip_hash
            return tip is not None and tip != tip_hash

        try:
            mine_block_cancellable(block, progress=_progress, abort_check=_abort)
        except MiningAborted:
            if self._stop:
                raise RuntimeError("stopped") from None
            raise RuntimeError("stale tip") from None
        except KeyboardInterrupt:
            self._stop = True
            raise RuntimeError("stopped") from None
        if self._stop:
            raise RuntimeError("stopped") from None
        if self._local.chain.tip_hash != tip_hash:
            raise RuntimeError("stale tip")
        connected = self._local.chain.connect_block(block)
        self._local.mempool.clear_included(block.transactions[1:])
        reward = block.transactions[0].outputs[0].value
        return MineResult(
            height=connected,
            block_hash=block.block_hash().hex(),
            reward_sats=reward,
            address=self.address,
        )

    def _mine_one_live(self) -> MineResult:
        rt = self._rt
        if rt is None or getattr(rt, "_stopped", False):
            raise RuntimeError("live node not running")
        try:
            block, height, bits = rt.prepare_block_template(self.address, hrp=self.hrp)
        except Exception as e:
            if self._stop:
                raise RuntimeError("stopped") from e
            raise
        parent = block.header.previous_block_hash
        epoch0 = int(rt.chain.tip_epoch)

        def _progress(nonce: int, _h: bytes, hps: float) -> None:
            if self._stop:
                raise KeyboardInterrupt("stop requested")
            # 0.4.1.10-style: live measured H/s (no 50k cap / EMA lag).
            self._hashrate = float(hps or 0.0)
            self._broadcast_miner_status()
            if nonce > 0 and nonce % 500_000 == 0:
                print(format_mine_progress(height=height, nonce=nonce, hps=hps), flush=True)

        def _abort() -> bool:
            if self._stop or getattr(rt, "_stopped", False):
                return True
            if int(rt.chain.tip_epoch) != epoch0:
                return True
            tip = rt.chain.tip_hash
            return tip is not None and tip != parent

        try:
            mine_block_cancellable(block, progress=_progress, abort_check=_abort)
        except MiningAborted:
            if self._stop:
                raise RuntimeError("stopped") from None
            raise RuntimeError("stale tip") from None
        except KeyboardInterrupt:
            self._stop = True
            raise RuntimeError("stopped") from None
        if self._stop:
            raise RuntimeError("stopped") from None
        if rt.chain.tip_hash != parent:
            raise RuntimeError("stale tip")
        try:
            connected = rt.accept_block(block)
        except Exception as e:
            # Ctrl+C / shutdown races — do not dump a traceback for lock/stop.
            import sqlite3

            if self._stop:
                raise RuntimeError("stopped") from e
            if isinstance(e, sqlite3.OperationalError) and (
                "locked" in str(e).lower() or "busy" in str(e).lower()
            ):
                raise RuntimeError("stale tip") from e
            raise
        reward = block.transactions[0].outputs[0].value
        return MineResult(
            height=int(connected),
            block_hash=block.block_hash().hex(),
            reward_sats=reward,
            address=self.address,
        )

    def run(self, *, max_blocks: int | None = None) -> list[MineResult]:
        """Mine until Ctrl+C or max_blocks found."""
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        found: list[MineResult] = []
        session_reward = 0
        # Banner first (tip may still be catching up in live mode).
        tip = 0
        try:
            if self._local is not None and self._local.chain.height >= 0:
                tip = int(self._local.chain.height)
        except Exception:
            tip = 0
        print(
            format_mine_banner(
                network=self.network,
                address=self.address,
                data_dir=self.data_dir,
                tip=tip,
                mode="live" if self._live else "offline",
                peers=0,
                peer_tip=None,
            ),
            flush=True,
        )
        self.ensure_chain()
        try:
            if self._live and self._rt is not None:
                n_peers, peer_h = self._peer_tip()
                print(
                    f"  ready  tip #{int(self._rt.chain.height)}  peers {n_peers}"
                    + (f"  net #{peer_h}" if peer_h >= 0 else ""),
                    flush=True,
                )
                self._broadcast_miner_status(force=True, mining=True)
        except Exception:
            pass
        try:
            while not self._stop:
                if max_blocks is not None and len(found) >= max_blocks:
                    break
                t0 = time.time()
                try:
                    result = self.mine_one()
                except RuntimeError as e:
                    msg = str(e)
                    if msg == "stopped" or self._stop:
                        break
                    if msg == "stale tip":
                        continue
                    raise
                except Exception as e:
                    import sqlite3

                    if self._stop:
                        break
                    if isinstance(e, sqlite3.OperationalError) and (
                        "locked" in str(e).lower() or "busy" in str(e).lower()
                    ):
                        print("  database busy — retrying…", flush=True)
                        time.sleep(0.2)
                        continue
                    raise
                elapsed = time.time() - t0
                found.append(result)
                session_reward += int(result.reward_sats)
                print(
                    format_mine_found(
                        height=result.height,
                        block_hash=result.block_hash,
                        reward_sats=result.reward_sats,
                        elapsed=elapsed,
                        session_blocks=len(found),
                        session_reward_sats=session_reward,
                    ),
                    flush=True,
                )
        except KeyboardInterrupt:
            self._stop = True
        finally:
            s = _Style()
            print(
                f"\n{s.c(s.yellow, '■ Stopped.')}  session {len(found)} blocks · "
                f"{format_mhc(session_reward)} MHC",
                flush=True,
            )
            try:
                self.close()
            except Exception:
                logger.debug("miner close failed", exc_info=True)
        return found

    def close(self) -> None:
        self._stop = True
        try:
            self._hashrate = 0.0
            self._broadcast_miner_status(force=True, mining=False)
        except Exception:
            pass
        rt = self._rt
        if rt is not None:
            try:
                rt.stop()
            except Exception:
                logger.debug("runtime stop failed", exc_info=True)
            th = self._node_thread
            if th is not None and th.is_alive():
                th.join(timeout=3.0)
            self._rt = None
            self._node_thread = None
        if self._local is not None:
            try:
                self._local.close()
            except Exception:
                logger.debug("local close failed", exc_info=True)
