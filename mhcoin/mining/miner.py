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
from mhcoin.mining.abortable_pow import MiningAborted, mine_block_cancellable, resolve_worker_count
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
    *, network: str, address: str, data_dir: Path, tip: int, workers: int = 1
) -> str:
    s = _Style()
    lines = [
        s.c(s.cyan + s.bold, "╔════════════════════════════════════════════════════════════╗"),
        s.c(s.cyan + s.bold, "║")
        + s.c(s.bold, "  MHCOIN Solo Miner")
        + s.c(s.dim, f"  ·  {network:<36}")
        + s.c(s.cyan + s.bold, "║"),
        s.c(s.cyan + s.bold, "╚════════════════════════════════════════════════════════════╝"),
        f"  {s.c(s.dim, 'tip')}      {s.c(s.bold, '#' + str(tip))}",
        f"  {s.c(s.dim, 'reward')}   {address}",
        f"  {s.c(s.dim, 'workers')}  {s.c(s.bold, str(workers))} CPU process(es)",
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
        workers: int | None = None,
    ):
        params = get_network_params(network)
        if not validate_address(address, hrp=hrp):
            raise ValueError(f"invalid address for hrp={hrp}")
        self.network = params.name
        self.hrp = hrp
        self.address = address
        self.pkh = address_to_pubkey_hash(address, hrp=hrp)
        self.node = LocalNode(data_dir, hrp=hrp, network=params.name)
        self.workers = resolve_worker_count(workers)
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
        height = self.node.chain.height + 1
        assert self.node.chain.tip_hash is not None
        tip_hash = self.node.chain.tip_hash
        bits = self.node.chain.get_next_work_for_parent(tip_hash)
        mtp = self.node.chain.median_time_past_for_parent(tip_hash)
        ts = int(time.time())
        if ts <= mtp:
            ts = mtp + 1
        block = build_block_template(
            height=height,
            previous_hash=tip_hash,
            timestamp=ts,
            bits=bits,
            mempool=self.node.mempool,
            utxo=self.node.chain.utxo,
            miner_pubkey_hash=self.pkh,
        )

        def _progress(nonce: int, _h: bytes, hps: float) -> None:
            if self._stop:
                raise KeyboardInterrupt("stop requested")
            if self.workers > 1:
                print(format_mine_progress(height=height, nonce=nonce, hps=hps), flush=True)
                return
            if nonce > 0 and nonce % 500_000 == 0:
                print(format_mine_progress(height=height, nonce=nonce, hps=hps), flush=True)

        def _abort() -> bool:
            if self._stop:
                return True
            tip = self.node.chain.tip_hash
            return tip is not None and tip != tip_hash

        try:
            mine_block_cancellable(
                block,
                progress=_progress,
                abort_check=_abort,
                workers=self.workers,
            )
        except MiningAborted:
            if self._stop:
                raise RuntimeError("stopped") from None
            raise RuntimeError("stale tip") from None
        except KeyboardInterrupt:
            self._stop = True
            raise RuntimeError("stopped") from None
        if self._stop:
            raise RuntimeError("stopped") from None
        # Tip may have moved in the last iteration.
        if self.node.chain.tip_hash != tip_hash:
            raise RuntimeError("stale tip")
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
        session_reward = 0
        print(
            format_mine_banner(
                network=self.network,
                address=self.address,
                data_dir=self.node.data_dir,
                tip=int(self.node.chain.height),
                workers=self.workers,
            ),
            flush=True,
        )
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
            self.node.close()
        return found

    def close(self) -> None:
        self.node.close()
