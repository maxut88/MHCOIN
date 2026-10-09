"""Decode MHCOIN blocks/TXs for the read-only LAN explorer."""

from __future__ import annotations

import time
from typing import Any

from mhcoin.blockchain.block import Block
from mhcoin.blockchain.readonly_chain import ReadOnlyChain
from mhcoin.consensus.chain_work import work_for_bits
from mhcoin.consensus.difficulty import bits_to_target
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.params import (
    HALVING_INTERVAL,
    MAX_SUPPLY_SATOSHIS,
    SATOSHI_PER_COIN,
    TARGET_BLOCK_TIME_SECONDS,
)
from mhcoin.constants import INITIAL_BLOCK_SUBSIDY, MAX_SEQUENCE
from mhcoin.transaction.transaction import Transaction
from mhcoin.wallet.addresses import address_to_pubkey_hash, pubkey_hash_to_address, validate_address
from mhcoin.wallet.send import format_mhc

DEFAULT_HRP = "mhc"
BLOCKS_PER_PAGE = 100
TXS_PER_PAGE = 100
ADDRESS_PER_PAGE = 50
ADDRESS_PER_PAGE_MAX = 200
# BIP125 opt-in threshold: sequence < MAX_SEQUENCE - 1 signals replaceability.
_RBF_SEQUENCE_THRESHOLD = MAX_SEQUENCE - 1


def tx_size_metrics(size_bytes: int) -> dict[str, Any]:
    """MHCOIN has no SegWit — weight/vsize follow Bitcoin legacy rules."""
    size = max(0, int(size_bytes))
    return {
        "witness": False,
        "size_bytes": size,
        "vsize": size,
        "weight": size * 4,
    }


def tx_signals_rbf(tx: Transaction) -> bool | None:
    """BIP125-style RBF signal from input nSequence. None for coinbase."""
    if tx.is_coinbase():
        return None
    return any(
        (not tin.is_coinbase()) and int(tin.sequence) < _RBF_SEQUENCE_THRESHOLD
        for tin in tx.inputs
    )


def payment_amount_sats(tx: Transaction, *, from_addr: str | None, hrp: str = DEFAULT_HRP) -> int:
    """Value sent to others (excludes change back to from_addr).

    For coinbase: full output value. For a normal send with change: payment only
    (e.g. 1 MHC), not input/output totals (~50 MHC).
    """
    if tx.is_coinbase():
        return int(tx.output_value())
    if not tx.outputs:
        return 0
    if not from_addr or from_addr == "coinbase":
        return int(tx.output_value())
    paid = 0
    for tout in tx.outputs:
        addr = out_address(tout, hrp=hrp)
        if addr and addr != from_addr:
            paid += int(tout.value)
    # Self-send / consolidation: no external output — show full output value.
    return paid if paid > 0 else int(tx.output_value())


def format_hps(hps: float | None) -> str | None:
    if hps is None or hps <= 0:
        return None
    if hps >= 1_000_000_000:
        return f"{hps / 1_000_000_000:.2f} GH/s"
    if hps >= 1_000_000:
        return f"{hps / 1_000_000:.2f} MH/s"
    if hps >= 1_000:
        return f"{hps / 1_000:.1f} kH/s"
    return f"{hps:,.0f} H/s"




def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    nb = " "
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{nb}{s % 60}s"
    if s < 86400:
        return f"{s // 3600}h{nb}{(s % 3600) // 60}m"
    return f"{s // 86400}d{nb}{(s % 86400) // 3600}h"


def difficulty_from_bits(bits: int, *, genesis_bits: int | None = None) -> dict[str, Any]:
    """BTC-style difficulty fields from compact bits."""
    target = bits_to_target(bits)
    work = work_for_bits(bits)
    genesis_work = work_for_bits(genesis_bits) if genesis_bits is not None else None
    rel = (work / genesis_work) if genesis_work else None
    tgt_hex = f"{target:064x}"
    return {
        "bits": f"0x{int(bits):08x}",
        "target": tgt_hex,
        "target_short": tgt_hex[:16] + "…" + tgt_hex[-8:],
        "work": work,
        "difficulty": round(rel, 4) if rel is not None else float(work),
        "difficulty_display": (
            f"{rel:,.2f}" if rel is not None and rel >= 1 else (f"{rel:.4f}" if rel else str(work))
        ),
        "implied_hashrate_hps": work / float(TARGET_BLOCK_TIME_SECONDS),
        "implied_hashrate": format_hps(work / float(TARGET_BLOCK_TIME_SECONDS)),
    }


def halving_info(tip: int) -> dict[str, Any]:
    tip = max(0, int(tip))
    era = tip // HALVING_INTERVAL
    next_h = (era + 1) * HALVING_INTERVAL
    cur = get_block_subsidy(tip)
    nxt = get_block_subsidy(next_h) if next_h < tip + HALVING_INTERVAL * 64 else 0
    return {
        "halving_interval": HALVING_INTERVAL,
        "era": era,
        "current_subsidy_sats": cur,
        "current_subsidy_mhc": format_mhc(cur),
        "next_halving_height": next_h,
        "blocks_to_halving": max(0, next_h - tip),
        "next_subsidy_sats": nxt,
        "next_subsidy_mhc": format_mhc(nxt),
        "initial_subsidy_mhc": format_mhc(INITIAL_BLOCK_SUBSIDY),
    }


def subsidy_schedule(*, max_eras: int = 40) -> list[dict[str, Any]]:
    """Full block-subsidy halving table (height ranges + reward) from consensus.

    Mirrors ``get_block_subsidy`` (mhcoin.consensus.block_reward): reward halves
    every ``HALVING_INTERVAL`` blocks until the integer right-shift hits zero
    (era 33 for a 50 MHC initial subsidy), at which point issuance stops.
    """
    rows: list[dict[str, Any]] = []
    for era in range(max(1, max_eras)):
        start = era * HALVING_INTERVAL
        end = start + HALVING_INTERVAL - 1
        subsidy = get_block_subsidy(start)
        rows.append(
            {
                "era": era,
                "from_height": start,
                "to_height": end,
                "subsidy_sats": subsidy,
                "subsidy_mhc": format_mhc(subsidy),
            }
        )
        if subsidy == 0:
            break
    return rows


def supply_info(
    tip: int,
    minted_sats: int,
    *,
    avg_interval_seconds: int | None = None,
) -> dict[str, Any]:
    """Supply/halving dashboard payload: current state + full subsidy schedule."""
    tip = max(0, int(tip))
    minted_sats = max(0, int(minted_sats))
    halv = halving_info(tip)
    blocks_to_halving = int(halv["blocks_to_halving"])
    spacing = avg_interval_seconds or TARGET_BLOCK_TIME_SECONDS
    eta_seconds = blocks_to_halving * spacing if spacing else None
    pct_mined = (
        round(100.0 * minted_sats / MAX_SUPPLY_SATOSHIS, 6) if MAX_SUPPLY_SATOSHIS else 0.0
    )
    return {
        "tip_height": tip,
        "current_subsidy_sats": halv["current_subsidy_sats"],
        "current_subsidy_mhc": halv["current_subsidy_mhc"],
        "minted_sats": minted_sats,
        "minted_mhc": format_mhc(minted_sats),
        "max_supply_sats": MAX_SUPPLY_SATOSHIS,
        "max_supply_mhc": format_mhc(MAX_SUPPLY_SATOSHIS),
        "remaining_sats": max(0, MAX_SUPPLY_SATOSHIS - minted_sats),
        "remaining_mhc": format_mhc(max(0, MAX_SUPPLY_SATOSHIS - minted_sats)),
        "minted_pct": pct_mined,
        "halving_interval": HALVING_INTERVAL,
        "era": halv["era"],
        "next_halving_height": halv["next_halving_height"],
        "blocks_to_halving": blocks_to_halving,
        "next_subsidy_mhc": halv["next_subsidy_mhc"],
        "next_halving_eta_seconds": eta_seconds,
        "next_halving_eta": format_duration(eta_seconds),
        "eta_basis": "avg block interval" if avg_interval_seconds else "target spacing (10m)",
        "schedule": subsidy_schedule(),
    }


def _mempool_parse_tx(key: Any, val: Any) -> Transaction | None:
    """Parse a mempool.json entry into a Transaction (hex or {hex:...})."""
    if isinstance(val, str) and len(val) > 64:
        try:
            return Transaction.deserialize(bytes.fromhex(val))
        except Exception:
            return None
    if isinstance(val, dict):
        hx = val.get("hex") or val.get("raw_hex")
        if isinstance(hx, str) and len(hx) > 64:
            try:
                return Transaction.deserialize(bytes.fromhex(hx))
            except Exception:
                return None
    return None


def load_mempool(
    data_dir,
    *,
    hrp: str = DEFAULT_HRP,
    chain: ReadOnlyChain | None = None,
) -> dict[str, Any]:
    """Read node mempool.json (unconfirmed txs) without opening the live node."""
    from pathlib import Path
    import json

    path = Path(data_dir) / "mempool.json"
    updated = None
    updated_age = None
    if path.is_file():
        try:
            mtime = int(path.stat().st_mtime)
            updated = mtime
            updated_age = format_age(mtime, now=int(time.time()))
        except OSError:
            pass
    empty = {
        "count": 0,
        "transactions": [],
        "path": str(path),
        "updated": updated,
        "updated_age": updated_age,
        "source": "mempool.json",
    }
    if not path.is_file():
        return empty
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    txs_raw = raw.get("txs") or raw.get("transactions") or {}
    rows: list[dict[str, Any]] = []
    if isinstance(txs_raw, dict):
        items = list(txs_raw.items())
    elif isinstance(txs_raw, list):
        items = [(None, x) for x in txs_raw]
    else:
        items = []

    tip = chain.height if chain is not None else None
    for key, val in items:
        tx = _mempool_parse_tx(key, val)
        if tx is None:
            if isinstance(val, dict):
                rows.append(
                    {
                        "txid": val.get("txid") or key,
                        "size_bytes": val.get("size") or val.get("size_bytes"),
                        "fee_mhc": val.get("fee_mhc") or val.get("fee"),
                        "note": "unparsed",
                    }
                )
            else:
                rows.append({"txid": key or str(val)[:64], "note": "unparsed"})
            continue
        if chain is not None:
            row = summarize_tx(chain, tx, tip=tip, hrp=hrp)
        else:
            size = len(tx.serialize())
            metrics = tx_size_metrics(size)
            to_addr = out_address(tx.outputs[0], hrp=hrp) if tx.outputs else None
            pay = payment_amount_sats(tx, from_addr=None, hrp=hrp)
            row = {
                "txid": tx.txid().hex(),
                "coinbase": tx.is_coinbase(),
                "size_bytes": size,
                "vsize": metrics["vsize"],
                "weight": metrics["weight"],
                "witness": False,
                "rbf": tx_signals_rbf(tx),
                "output_value_mhc": format_mhc(int(tx.output_value())),
                "amount_mhc": format_mhc(pay),
                "amount_sats": pay,
                "output_count": len(tx.outputs),
                "input_count": len(tx.inputs),
                "from": None,
                "to": to_addr,
                "fee_sats": None,
                "fee_mhc": None,
                "fee_per_byte": None,
                "fee_rate": None,
            }
        row["unconfirmed"] = True
        row["in_mempool"] = True
        row["confirmations"] = 0
        row["status"] = "unconfirmed"
        # Compact list payload (full detail via /tx/<id>).
        rows.append(
            {
                "txid": row.get("txid"),
                "size_bytes": row.get("size_bytes"),
                "vsize": row.get("vsize"),
                "weight": row.get("weight"),
                "input_count": row.get("input_count"),
                "output_count": row.get("output_count"),
                "amount_mhc": row.get("amount_mhc") or row.get("output_value_mhc"),
                "fee_mhc": row.get("fee_mhc"),
                "fee_rate": row.get("fee_rate"),
                "fee_per_byte": row.get("fee_per_byte"),
                "from": row.get("from")
                or next(
                    (
                        i.get("address")
                        for i in (row.get("inputs") or [])
                        if i.get("address")
                    ),
                    None,
                ),
                "to": row.get("to")
                or next(
                    (
                        o.get("address")
                        for o in (row.get("outputs") or [])
                        if o.get("address")
                    ),
                    None,
                ),
                "coinbase": bool(row.get("coinbase")),
                "rbf": row.get("rbf"),
                "unconfirmed": True,
                "in_mempool": True,
            }
        )
    rows.sort(key=lambda r: (-(r.get("fee_per_byte") or 0), str(r.get("txid") or "")))
    return {
        "count": len(items),
        "transactions": rows,
        "path": str(path),
        "updated": updated,
        "updated_age": updated_age,
        "source": "mempool.json",
    }


def top_miners(
    chain: ReadOnlyChain, *, window: int = 100, tip: int | None = None, hrp: str = DEFAULT_HRP
) -> list[dict[str, Any]]:
    tip = chain.height if tip is None else tip
    counts: dict[str, dict[str, Any]] = {}
    start = max(0, tip - window + 1)
    scanned = 0
    for h in range(start, tip + 1):
        b = chain.get_block_by_height(h)
        if b is None or not b.transactions:
            continue
        cb = b.transactions[0]
        if not cb.is_coinbase() or not cb.outputs:
            continue
        addr = out_address(cb.outputs[0], hrp=hrp) or "?"
        reward = sum(int(o.value) for o in cb.outputs)
        ent = counts.setdefault(addr, {"address": addr, "blocks": 0, "reward_sats": 0})
        ent["blocks"] += 1
        ent["reward_sats"] += reward
        scanned += 1
    rows = sorted(counts.values(), key=lambda x: (-x["blocks"], -x["reward_sats"]))
    for r in rows:
        r["reward_mhc"] = format_mhc(r["reward_sats"])
        r["share_pct"] = round(100.0 * r["blocks"] / scanned, 1) if scanned else 0.0
    return rows[:20]


def chart_series(
    chain: ReadOnlyChain, *, window: int = 30, tip: int | None = None
) -> dict[str, Any]:
    tip = chain.height if tip is None else tip
    intervals: list[int] = []
    hashrates: list[float] = []
    heights: list[int] = []
    prev_ts: int | None = None
    start = max(0, tip - window)
    for h in range(start, tip + 1):
        b = chain.get_block_by_height(h)
        if b is None:
            continue
        ts = int(b.header.timestamp)
        w = work_for_bits(int(b.header.bits))
        if prev_ts is not None and h > 0:
            dt = ts - prev_ts
            if 0 < dt < 86_400:
                intervals.append(dt)
                hashrates.append(w / float(dt))
                heights.append(h)
        prev_ts = ts
    return {
        "heights": heights,
        "intervals": intervals,
        "hashrate_hps": hashrates,
        "window": window,
    }


def estimate_network_hashrate(
    chain: ReadOnlyChain, *, tip: int | None = None, window: int = 30
) -> dict[str, Any]:
    """Estimate network hashrate from tip bits + recent block intervals."""
    tip = chain.height if tip is None else tip
    if tip < 0:
        return {
            "hashrate_hps": None,
            "hashrate": None,
            "hashrate_window_hps": None,
            "hashrate_window": None,
            "bits": None,
            "target_block_time_seconds": TARGET_BLOCK_TIME_SECONDS,
        }
    tip_block = chain.get_block_by_height(tip)
    bits = int(tip_block.header.bits) if tip_block else None
    hps = None
    if bits is not None:
        # Expected hashes/block ≈ work; divide by target spacing.
        hps = work_for_bits(bits) / float(TARGET_BLOCK_TIME_SECONDS)

    # Observed: mean work / mean interval over last N blocks (skip absurd gaps).
    works: list[int] = []
    intervals: list[int] = []
    prev_ts: int | None = None
    start = max(0, tip - window)
    for h in range(start, tip + 1):
        b = chain.get_block_by_height(h)
        if b is None:
            continue
        ts = int(b.header.timestamp)
        works.append(work_for_bits(int(b.header.bits)))
        if prev_ts is not None and h > 0:
            dt = ts - prev_ts
            if 0 < dt < 86_400:
                intervals.append(dt)
        prev_ts = ts
    hps_win = None
    hps_short = None
    if works and intervals:
        hps_win = (sum(works) / len(works)) / (sum(intervals) / len(intervals))

    # Short observed window (closer to live solo miner).
    hps_short = None
    if len(intervals) >= 2:
        n = min(6, len(intervals), len(works) - 1)
        # last n intervals correspond to last n works entries (works aligned with blocks)
        iv = intervals[-n:]
        # works[-n:] are the blocks that closed those intervals (approx last n of works[1:])
        ww = works[-n:]
        if iv and ww:
            hps_short = (sum(ww) / len(ww)) / (sum(iv) / len(iv))

    return {
        "hashrate_hps": hps,
        "hashrate": format_hps(hps),
        "hashrate_label": "difficulty-implied",
        "hashrate_window_hps": hps_win,
        "hashrate_window": format_hps(hps_win),
        "hashrate_window_label": f"observed ({window})",
        "hashrate_short_hps": hps_short,
        "hashrate_short": format_hps(hps_short),
        "bits": f"0x{bits:08x}" if bits is not None else None,
        "difficulty_target": bits_to_target(bits) if bits is not None else None,
        "target_block_time_seconds": TARGET_BLOCK_TIME_SECONDS,
    }


def out_address(tout, *, hrp: str = DEFAULT_HRP) -> str | None:
    try:
        return pubkey_hash_to_address(tout.pubkey_hash(), hrp=hrp)
    except Exception:
        return None


def txid_hex(tx: Transaction) -> str:
    return tx.txid().hex()


def format_utc(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(ts)))
    except (OverflowError, OSError, ValueError):
        return None


def format_age(ts: int | None, *, now: int | None = None) -> str | None:
    if ts is None:
        return None
    now = int(now if now is not None else time.time())
    delta = max(0, now - int(ts))
    nb = "\u00a0"
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m{nb}{delta % 60}s"
    if delta < 86400:
        h = delta // 3600
        m = (delta % 3600) // 60
        return f"{h}h{nb}{m}m"
    d = delta // 86400
    h = (delta % 86400) // 3600
    return f"{d}d{nb}{h}h"


def confirmations(tip: int, height: int | None) -> int | None:
    if height is None or tip < 0 or height < 0:
        return None
    return max(0, tip - height + 1)


def _block_size(block: Block) -> int:
    try:
        return len(block.serialize())
    except Exception:
        return sum(len(tx.serialize()) for tx in block.transactions)


def _coinbase_reward(tx: Transaction) -> int:
    return sum(int(o.value) for o in tx.outputs)


def summarize_block(
    chain: ReadOnlyChain,
    block: Block,
    *,
    height: int | None = None,
    tip: int | None = None,
    hrp: str = DEFAULT_HRP,
    include_tx_summaries: bool = False,
) -> dict[str, Any]:
    tip = chain.height if tip is None else tip
    bh = block.block_hash().hex()
    ts = int(block.header.timestamp)
    now = int(time.time())
    conf = confirmations(tip, height)

    prev_interval: int | None = None
    if height is not None and height > 0:
        prev = chain.get_block_by_height(height - 1)
        if prev is not None:
            prev_interval = ts - int(prev.header.timestamp)

    next_hash: str | None = None
    if height is not None and tip is not None and height < tip:
        nxt = chain.get_block_by_height(height + 1)
        if nxt is not None:
            next_hash = nxt.block_hash().hex()

    miner: str | None = None
    reward_sats: int | None = None
    txids: list[str] = []
    tx_rows: list[dict[str, Any]] = []
    total_out = 0
    for i, tx in enumerate(block.transactions):
        tid = txid_hex(tx)
        txids.append(tid)
        out_v = int(tx.output_value())
        total_out += out_v
        if i == 0 and tx.is_coinbase():
            reward_sats = _coinbase_reward(tx)
            if tx.outputs:
                miner = out_address(tx.outputs[0], hrp=hrp)
        if include_tx_summaries:
            fee_per_byte = None
            from_a, to_a, fee_sats, _in_v = _transfer_endpoints(chain, tx, hrp=hrp)
            if fee_sats is not None:
                sz = len(tx.serialize())
                if sz > 0:
                    fee_per_byte = round(fee_sats / sz, 3)
            pay_sats = payment_amount_sats(tx, from_addr=from_a, hrp=hrp)
            tx_rows.append(
                {
                    "txid": tid,
                    "coinbase": tx.is_coinbase(),
                    "input_count": len(tx.inputs),
                    "output_count": len(tx.outputs),
                    "output_value_sats": out_v,
                    "output_value_mhc": format_mhc(out_v),
                    "amount_sats": pay_sats,
                    "amount_mhc": format_mhc(pay_sats),
                    "size_bytes": len(tx.serialize()),
                    "fee_sats": fee_sats,
                    "fee_mhc": format_mhc(fee_sats) if fee_sats is not None else None,
                    "fee_per_byte": fee_per_byte,
                    "from": from_a,
                    "to": to_a,
                }
            )

    size = _block_size(block)
    genesis = chain.get_block_by_height(0)
    genesis_bits = int(genesis.header.bits) if genesis else None
    diff = difficulty_from_bits(int(block.header.bits), genesis_bits=genesis_bits)
    return {
        "height": height,
        "hash": bh,
        "previous": block.header.previous_block_hash.hex(),
        "next": next_hash,
        "merkle_root": block.header.merkle_root.hex(),
        "timestamp": ts,
        "time_utc": format_utc(ts),
        "age": format_age(ts, now=now),
        "confirmations": conf,
        "bits": diff["bits"],
        "target": diff["target"],
        "target_short": diff["target_short"],
        "difficulty": diff["difficulty"],
        "difficulty_display": diff["difficulty_display"],
        "nonce": int(block.header.nonce),
        "version": int(block.header.version),
        "tx_count": len(block.transactions),
        "txids": txids,
        "transactions": tx_rows,
        "size_bytes": size,
        "interval_seconds": prev_interval,
        "target_block_time_seconds": TARGET_BLOCK_TIME_SECONDS,
        "miner": miner,
        "reward_sats": reward_sats,
        "reward_mhc": format_mhc(reward_sats) if reward_sats is not None else None,
        "output_total_sats": total_out,
        "output_total_mhc": format_mhc(total_out),
        "implied_hashrate": diff["implied_hashrate"],
    }


def _prev_output(chain: ReadOnlyChain, prev_txid: bytes, prev_vout: int):
    """Find spent output by scanning active chain for prev_txid (small chains OK)."""
    if prev_txid == b"\x00" * 32:
        return None
    tip = chain.height
    for h in range(tip, -1, -1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        for tx in block.transactions:
            if tx.txid() != prev_txid:
                continue
            if 0 <= prev_vout < len(tx.outputs):
                return tx.outputs[prev_vout], h
            return None
    return None


def _txid_index(
    chain: ReadOnlyChain, tip: int
) -> dict[bytes, tuple[Transaction, int]]:
    """Map txid → (tx, height) for the active chain tip-down range ``0..tip``."""
    idx, _spent = _chain_lookup(chain, tip)
    return idx


def _chain_lookup(
    chain: ReadOnlyChain, tip: int
) -> tuple[dict[bytes, tuple[Transaction, int]], set[tuple[str, int]]]:
    """One tip scan: txid index + spent outpoints ``(txid_hex, vout)``."""
    idx: dict[bytes, tuple[Transaction, int]] = {}
    spent: set[tuple[str, int]] = set()
    for h in range(0, tip + 1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        for tx in block.transactions:
            idx[tx.txid()] = (tx, h)
            if tx.is_coinbase():
                continue
            for tin in tx.inputs:
                if tin.is_coinbase():
                    continue
                spent.add((tin.prev_txid.hex(), int(tin.prev_vout)))
    return idx, spent


def _prev_from_index(
    idx: dict[bytes, tuple[Transaction, int]], prev_txid: bytes, prev_vout: int
):
    if prev_txid == b"\x00" * 32:
        return None
    hit = idx.get(prev_txid)
    if hit is None:
        return None
    tx, height = hit
    if 0 <= prev_vout < len(tx.outputs):
        return tx.outputs[prev_vout], height
    return None


def _transfer_endpoints(
    chain: ReadOnlyChain,
    tx: Transaction,
    *,
    hrp: str = DEFAULT_HRP,
    idx: dict[bytes, tuple[Transaction, int]] | None = None,
) -> tuple[str | None, str | None, int | None, int | None]:
    """Return ``(from_addr, to_addr, fee_sats, input_value_sats)`` for a non-coinbase tx.

    Fee = sum(resolved inputs) − sum(outputs). Requires every prevout to resolve;
    otherwise fee is ``None`` (shown as — in the UI).
    """
    if tx.is_coinbase():
        to_addr = out_address(tx.outputs[0], hrp=hrp) if tx.outputs else None
        return "coinbase", to_addr, None, None

    out_v = int(tx.output_value())
    in_sum = 0
    resolved = 0
    from_addr: str | None = None
    for tin in tx.inputs:
        if tin.is_coinbase():
            continue
        prev = (
            _prev_from_index(idx, tin.prev_txid, tin.prev_vout)
            if idx is not None
            else _prev_output(chain, tin.prev_txid, tin.prev_vout)
        )
        if prev is None:
            continue
        tout, _ = prev
        resolved += 1
        in_sum += int(tout.value)
        if from_addr is None:
            from_addr = out_address(tout, hrp=hrp)

    fee_sats: int | None = None
    non_cb_inputs = sum(1 for tin in tx.inputs if not tin.is_coinbase())
    if non_cb_inputs > 0 and resolved == non_cb_inputs and in_sum >= out_v:
        fee_sats = in_sum - out_v

    to_addr: str | None = None
    if tx.outputs:
        for tout in tx.outputs:
            cand = out_address(tout, hrp=hrp)
            if cand and cand != from_addr:
                to_addr = cand
                break
        if to_addr is None:
            to_addr = out_address(tx.outputs[0], hrp=hrp)

    return from_addr, to_addr, fee_sats, (in_sum if resolved else None)


def summarize_tx(
    chain: ReadOnlyChain,
    tx: Transaction,
    *,
    block_height: int | None = None,
    block_hash: str | None = None,
    block_timestamp: int | None = None,
    tip: int | None = None,
    hrp: str = DEFAULT_HRP,
) -> dict[str, Any]:
    tip = chain.height if tip is None else tip
    now = int(time.time())
    coinbase = tx.is_coinbase()
    raw = tx.serialize()
    size = len(raw)
    tx_idx, spent_ops = _chain_lookup(chain, tip)
    this_txid = txid_hex(tx)

    inputs: list[dict[str, Any]] = []
    in_value = 0
    for tin in tx.inputs:
        if tin.is_coinbase():
            inputs.append({"coinbase": True, "script_sig_hex": tin.script_sig.hex()})
            continue
        prev = _prev_from_index(tx_idx, tin.prev_txid, tin.prev_vout)
        entry: dict[str, Any] = {
            "coinbase": False,
            "prev_txid": tin.prev_txid.hex(),
            "prev_vout": int(tin.prev_vout),
            "sequence": int(tin.sequence),
            "script_sig_hex": tin.script_sig.hex(),
        }
        if prev is not None:
            tout, src_h = prev
            entry["value_sats"] = int(tout.value)
            entry["value_mhc"] = format_mhc(int(tout.value))
            entry["address"] = out_address(tout, hrp=hrp)
            entry["source_height"] = src_h
            in_value += int(tout.value)
        inputs.append(entry)

    outputs: list[dict[str, Any]] = []
    out_value = 0
    for i, tout in enumerate(tx.outputs):
        out_value += int(tout.value)
        is_spent = (this_txid, i) in spent_ops
        outputs.append(
            {
                "n": i,
                "value_sats": int(tout.value),
                "value_mhc": format_mhc(int(tout.value)),
                "address": out_address(tout, hrp=hrp),
                "script_pubkey_hex": tout.script_pubkey.hex(),
                "spent": is_spent,
                "status": "spent" if is_spent else "unspent",
            }
        )

    fee_sats: int | None = None
    fee_per_byte: float | None = None
    if not coinbase and in_value >= out_value:
        fee_sats = in_value - out_value
        if size > 0:
            fee_per_byte = fee_sats / size

    from_addr = None
    if not coinbase:
        for entry in inputs:
            if entry.get("address"):
                from_addr = entry["address"]
                break
    pay_sats = payment_amount_sats(tx, from_addr=from_addr, hrp=hrp)

    conf = confirmations(tip, block_height)
    return {
        "txid": txid_hex(tx),
        "coinbase": coinbase,
        "block_height": block_height,
        "block_hash": block_hash,
        "block_timestamp": block_timestamp,
        "time_utc": format_utc(block_timestamp),
        "age": format_age(block_timestamp, now=now),
        "confirmations": conf,
        "status": (
            "confirmed"
            if conf is not None and conf > 0
            else ("unconfirmed" if block_height is None else "unknown")
        ),
        "version": int(tx.version),
        "locktime": int(tx.locktime),
        "raw_hex": raw.hex(),
        "size_bytes": size,
        "input_count": len(tx.inputs),
        "output_count": len(tx.outputs),
        "inputs": inputs,
        "outputs": outputs,
        "input_value_sats": in_value if not coinbase else None,
        "input_value_mhc": format_mhc(in_value) if not coinbase else None,
        "output_value_sats": out_value,
        "output_value_mhc": format_mhc(out_value),
        "amount_sats": pay_sats,
        "amount_mhc": format_mhc(pay_sats),
        "fee_sats": fee_sats,
        "fee_mhc": format_mhc(fee_sats) if fee_sats is not None else None,
        "fee_per_byte": round(fee_per_byte, 3) if fee_per_byte is not None else None,
        "fee_rate": (
            f"{fee_per_byte:.3f} sat/byte" if fee_per_byte is not None else None
        ),
        "satoshi_per_coin": SATOSHI_PER_COIN,
        **tx_size_metrics(size),
        "rbf": tx_signals_rbf(tx),
        "fiat": None,
    }


def find_tx(
    chain: ReadOnlyChain,
    txid_hex_str: str,
    *,
    hrp: str = DEFAULT_HRP,
    data_dir=None,
) -> dict[str, Any] | None:
    want = bytes.fromhex(txid_hex_str)
    tip = chain.height
    for h in range(tip, -1, -1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        for tx in block.transactions:
            if tx.txid() == want:
                return summarize_tx(
                    chain,
                    tx,
                    block_height=h,
                    block_hash=block.block_hash().hex(),
                    block_timestamp=int(block.header.timestamp),
                    tip=tip,
                    hrp=hrp,
                )
    if data_dir is not None:
        mp = find_mempool_tx(data_dir, txid_hex_str, chain=chain, hrp=hrp)
        if mp is not None:
            return mp
    return None


def find_mempool_tx(
    data_dir,
    txid_hex_str: str,
    *,
    chain: ReadOnlyChain | None = None,
    hrp: str = DEFAULT_HRP,
) -> dict[str, Any] | None:
    """Resolve an unconfirmed tx from mempool.json (hex-encoded entries)."""
    from pathlib import Path
    import json

    path = Path(data_dir) / "mempool.json"
    if not path.is_file():
        return None
    want = txid_hex_str.lower()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    txs_raw = raw.get("txs") or raw.get("transactions") or {}
    if isinstance(txs_raw, dict):
        items = list(txs_raw.items())
    elif isinstance(txs_raw, list):
        items = [(None, x) for x in txs_raw]
    else:
        return None
    from mhcoin.transaction.transaction import Transaction

    for key, val in items:
        tx = None
        if isinstance(val, str) and len(val) > 64:
            try:
                tx = Transaction.deserialize(bytes.fromhex(val))
            except Exception:
                continue
        elif isinstance(val, dict) and isinstance(val.get("hex"), str):
            try:
                tx = Transaction.deserialize(bytes.fromhex(val["hex"]))
            except Exception:
                continue
        if tx is None:
            continue
        if tx.txid().hex() != want and str(key or "").lower() != want:
            continue
        if chain is not None:
            out = summarize_tx(chain, tx, tip=chain.height, hrp=hrp)
        else:
            out = {
                "txid": tx.txid().hex(),
                "coinbase": tx.is_coinbase(),
                "confirmations": 0,
                "size_bytes": len(tx.serialize()),
                "output_value_mhc": format_mhc(int(tx.output_value())),
            }
        out["unconfirmed"] = True
        out["in_mempool"] = True
        out["confirmations"] = 0
        return out
    return None


def address_history(
    chain: ReadOnlyChain,
    address: str,
    *,
    hrp: str = DEFAULT_HRP,
    page: int = 1,
    per_page: int = ADDRESS_PER_PAGE,
    limit: int | None = None,
) -> dict[str, Any]:
    """Full-chain address scan with paginated output list.

    Totals / balance always cover the whole history. ``limit`` is accepted as a
    legacy alias for ``per_page`` (capped at ADDRESS_PER_PAGE_MAX).
    """
    if not validate_address(address, hrp=hrp):
        raise ValueError("invalid address")

    if limit is not None:
        per_page = int(limit)
    per_page = max(1, min(int(per_page or ADDRESS_PER_PAGE), ADDRESS_PER_PAGE_MAX))
    page = max(1, int(page or 1))

    pkh = address_to_pubkey_hash(address, hrp=hrp)
    received: list[dict[str, Any]] = []
    owned: dict[tuple[str, int], int] = {}  # (txid, vout) -> value
    total_in = 0
    total_spent_inputs = 0  # raw UTXO spend (includes change cycle)
    total_paid_external = 0  # MHC paid to other addresses (NOT fee, NOT change)
    total_fees_paid = 0  # network fees when we fully funded the tx
    tip = chain.height
    now = int(time.time())
    # Balance after each block that touches this address (sparkline).
    balance_series: list[int] = []
    for h in range(0, tip + 1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        bh = block.block_hash().hex()
        ts = int(block.header.timestamp)
        touched = False
        for tx in block.transactions:
            txh = txid_hex(tx)
            spent_here = 0
            if not tx.is_coinbase():
                for tin in tx.inputs:
                    key = (tin.prev_txid.hex(), int(tin.prev_vout))
                    if key in owned:
                        spent_here += owned.pop(key)
                        touched = True
                if spent_here:
                    total_spent_inputs += spent_here
                    paid_ext = 0
                    out_sum = 0
                    for tout in tx.outputs:
                        out_sum += int(tout.value)
                        try:
                            if tout.pubkey_hash() != pkh:
                                paid_ext += int(tout.value)
                        except Exception:
                            paid_ext += int(tout.value)
                    total_paid_external += paid_ext
                    # Fee only when this address funded the whole tx (typical wallet send).
                    if spent_here >= out_sum:
                        total_fees_paid += max(0, spent_here - out_sum)
            for n, tout in enumerate(tx.outputs):
                try:
                    if tout.pubkey_hash() != pkh:
                        continue
                except Exception:
                    continue
                val = int(tout.value)
                # Change back to self is not "Received" — keeps Balance ≈ Received − Sent.
                is_change = bool(spent_here) and not tx.is_coinbase()
                if not is_change:
                    total_in += val
                owned[(txh, n)] = val
                touched = True
                received.append(
                    {
                        "height": h,
                        "block_hash": bh,
                        "txid": txh,
                        "vout": n,
                        "value_sats": val,
                        "value_mhc": format_mhc(val),
                        "coinbase": tx.is_coinbase(),
                        "change": is_change,
                        "timestamp": ts,
                        "time_utc": format_utc(ts),
                        "age": format_age(ts, now=now),
                        "confirmations": confirmations(tip, h),
                    }
                )
        if touched:
            balance_series.append(sum(owned.values()))
    balance = sum(owned.values())
    for row in received:
        key = (row["txid"], int(row["vout"]))
        is_spent = key not in owned
        row["spent"] = is_spent
        row["status"] = "spent" if is_spent else "unspent"
    received.reverse()  # newest first
    total = len(received)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    page_rows = received[start : start + per_page]
    series = balance_series
    if len(series) > 120:
        step = max(1, (len(series) + 119) // 120)
        series = series[::step]
        if series and series[-1] != balance_series[-1]:
            series.append(balance_series[-1])
    return {
        "address": address,
        "received_count": total,
        "total_received_sats": total_in,
        "total_received_mhc": format_mhc(total_in),
        # Sent = paid to others only (e.g. 1.00000000). Fee is separate.
        "total_sent_sats": total_paid_external,
        "total_sent_mhc": format_mhc(total_paid_external),
        "total_fees_sats": total_fees_paid,
        "total_fees_mhc": format_mhc(total_fees_paid),
        "total_spent_inputs_sats": total_spent_inputs,
        "total_spent_inputs_mhc": format_mhc(total_spent_inputs),
        "balance_sats": balance,
        "balance_mhc": format_mhc(balance),
        "utxo_count": len(owned),
        "tip_height": tip,
        "outputs": page_rows,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "truncated": False,
        "balance_series_sats": series,
        "flow": {
            "received_sats": total_in,
            "sent_sats": total_paid_external,
            "fees_sats": total_fees_paid,
            "balance_sats": balance,
        },
    }


def recent_blocks(
    chain: ReadOnlyChain, *, count: int = 25, tip: int | None = None, hrp: str = DEFAULT_HRP
) -> list[dict[str, Any]]:
    tip = chain.height if tip is None else tip
    out: list[dict[str, Any]] = []
    for h in range(tip, max(-1, tip - count), -1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        out.append(summarize_block(chain, block, height=h, tip=tip, hrp=hrp))
    return out


def blocks_page(
    chain: ReadOnlyChain,
    *,
    page: int = 1,
    per_page: int = BLOCKS_PER_PAGE,
    hrp: str = DEFAULT_HRP,
) -> dict[str, Any]:
    tip = chain.height
    if tip < 0:
        return {
            "tip_height": tip,
            "page": 1,
            "per_page": per_page,
            "total_blocks": 0,
            "total_pages": 0,
            "blocks": [],
        }
    total_blocks = tip + 1  # heights 0..tip
    total_pages = max(1, (total_blocks + per_page - 1) // per_page)
    page = max(1, min(int(page), total_pages))
    # page 1 = newest (tip); last page includes genesis (0)
    start = tip - (page - 1) * per_page
    end = max(-1, start - per_page)
    blocks: list[dict[str, Any]] = []
    for h in range(start, end, -1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        blocks.append(summarize_block(chain, block, height=h, tip=tip, hrp=hrp))
    return {
        "tip_height": tip,
        "tip_hash": chain.tip_hash.hex() if chain.tip_hash else None,
        "page": page,
        "per_page": per_page,
        "total_blocks": total_blocks,
        "total_pages": total_pages,
        "from_height": blocks[-1]["height"] if blocks else None,
        "to_height": blocks[0]["height"] if blocks else None,
        "blocks": blocks,
        "target_block_time_seconds": TARGET_BLOCK_TIME_SECONDS,
    }



def _count_transactions(
    chain: ReadOnlyChain, *, tip: int, transfers_only: bool = False
) -> int:
    total = 0
    for h in range(0, tip + 1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        for tx in block.transactions:
            if transfers_only and tx.is_coinbase():
                continue
            total += 1
    return total


def transactions_page(
    chain: ReadOnlyChain,
    *,
    page: int = 1,
    per_page: int = TXS_PER_PAGE,
    hrp: str = DEFAULT_HRP,
    transfers_only: bool = False,
) -> dict[str, Any]:
    """Paginated newest-first transaction list (page slice only — no full materialize)."""
    tip = chain.height
    total = _count_transactions(chain, tip=tip, transfers_only=transfers_only)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page = max(1, min(int(page), total_pages))
    skip = (page - 1) * per_page
    # Walk tip-down; reuse compact row builder via recent_transactions window + skip.
    # Collect only the requested page by scanning with an offset.
    now = int(time.time())
    out: list[dict[str, Any]] = []
    seen = 0
    tx_idx = _txid_index(chain, tip)
    for h in range(tip, -1, -1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        bh = block.block_hash().hex()
        ts = int(block.header.timestamp)
        for tx_i in range(len(block.transactions) - 1, -1, -1):
            tx = block.transactions[tx_i]
            cb = tx.is_coinbase()
            if transfers_only and cb:
                continue
            if seen < skip:
                seen += 1
                continue
            out_v = int(tx.output_value())
            from_addr, to_addr, fee_sats, _in_v = _transfer_endpoints(
                chain, tx, hrp=hrp, idx=tx_idx
            )
            pay_sats = payment_amount_sats(tx, from_addr=from_addr, hrp=hrp)
            out.append(
                {
                    "txid": txid_hex(tx),
                    "coinbase": cb,
                    "height": h,
                    "block_hash": bh,
                    "timestamp": ts,
                    "time_utc": format_utc(ts),
                    "age": format_age(ts, now=now),
                    "confirmations": confirmations(tip, h),
                    "input_count": len(tx.inputs),
                    "output_count": len(tx.outputs),
                    "output_value_sats": out_v,
                    "output_value_mhc": format_mhc(out_v),
                    "amount_sats": pay_sats,
                    "amount_mhc": format_mhc(pay_sats),
                    "fee_sats": fee_sats,
                    "fee_mhc": format_mhc(fee_sats) if fee_sats is not None else None,
                    "from": from_addr,
                    "to": to_addr,
                    "size_bytes": len(tx.serialize()),
                    "index_in_block": tx_i,
                }
            )
            seen += 1
            if len(out) >= per_page:
                break
        if len(out) >= per_page:
            break
    return {
        "tip_height": tip,
        "page": page,
        "per_page": per_page,
        "total_transactions": total,
        "total_pages": total_pages,
        "transfers_only": transfers_only,
        "transactions": out,
        "target_block_time_seconds": TARGET_BLOCK_TIME_SECONDS,
    }


def recent_transactions(
    chain: ReadOnlyChain,
    *,
    count: int = 25,
    tip: int | None = None,
    hrp: str = DEFAULT_HRP,
    transfers_only: bool = False,
) -> list[dict[str, Any]]:
    """Newest confirmed txs tip-down (compact rows for home lists)."""
    tip = chain.height if tip is None else tip
    now = int(time.time())
    out: list[dict[str, Any]] = []
    tx_idx = _txid_index(chain, tip)
    for h in range(tip, -1, -1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        bh = block.block_hash().hex()
        ts = int(block.header.timestamp)
        # Newest-first within block: reverse index order
        for tx_i in range(len(block.transactions) - 1, -1, -1):
            tx = block.transactions[tx_i]
            cb = tx.is_coinbase()
            if transfers_only and cb:
                continue
            out_v = int(tx.output_value())
            from_addr, to_addr, fee_sats, _in_v = _transfer_endpoints(
                chain, tx, hrp=hrp, idx=tx_idx
            )
            pay_sats = payment_amount_sats(tx, from_addr=from_addr, hrp=hrp)
            out.append(
                {
                    "txid": txid_hex(tx),
                    "coinbase": cb,
                    "height": h,
                    "block_hash": bh,
                    "timestamp": ts,
                    "time_utc": format_utc(ts),
                    "age": format_age(ts, now=now),
                    "confirmations": confirmations(tip, h),
                    "input_count": len(tx.inputs),
                    "output_count": len(tx.outputs),
                    "output_value_sats": out_v,
                    "output_value_mhc": format_mhc(out_v),
                    "amount_sats": pay_sats,
                    "amount_mhc": format_mhc(pay_sats),
                    "fee_sats": fee_sats,
                    "fee_mhc": format_mhc(fee_sats) if fee_sats is not None else None,
                    "from": from_addr,
                    "to": to_addr,
                    "size_bytes": len(tx.serialize()),
                    "index_in_block": tx_i,
                }
            )
            if len(out) >= count:
                return out
    return out


def qr_svg(data: str, *, border: int = 2) -> str:
    """Offline QR SVG for wallet addresses (Nayuki qrcodegen, MIT)."""
    from mhcoin.explorer.qrcodegen import QrCode

    qr = QrCode.encode_text(data, QrCode.Ecc.MEDIUM)
    n = qr.get_size()
    dim = n + border * 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'role="img" aria-label="QR code">',
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>',
    ]
    for y in range(n):
        for x in range(n):
            if qr.get_module(x, y):
                parts.append(
                    f'<rect x="{x + border}" y="{y + border}" width="1" height="1" fill="#111418"/>'
                )
    parts.append("</svg>")
    return "".join(parts)


def _rich_list_chain_scan(
    chain: ReadOnlyChain, *, hrp: str = DEFAULT_HRP, limit: int = 100
) -> dict[str, Any]:
    """Rebuild UTXO balances by scanning the active chain (slow fallback)."""
    from collections import defaultdict

    owned: dict[tuple[str, int], tuple[str, int]] = {}
    tip = chain.height
    for h in range(0, tip + 1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        for tx in block.transactions:
            txh = txid_hex(tx)
            if not tx.is_coinbase():
                for tin in tx.inputs:
                    owned.pop((tin.prev_txid.hex(), int(tin.prev_vout)), None)
            for n, tout in enumerate(tx.outputs):
                addr = out_address(tout, hrp=hrp)
                if addr:
                    owned[(txh, n)] = (addr, int(tout.value))
    balances: dict[str, int] = defaultdict(int)
    utxo_counts: dict[str, int] = defaultdict(int)
    for addr, val in owned.values():
        balances[addr] += val
        utxo_counts[addr] += 1
    return _rich_list_rows(balances, utxo_counts, limit=limit, source="chain_scan")


def _rich_list_rows(
    balances: dict[str, int],
    utxo_counts: dict[str, int],
    *,
    limit: int,
    source: str,
) -> dict[str, Any]:
    total_supply = sum(balances.values())
    rows: list[dict[str, Any]] = []
    for rank, (addr, sats) in enumerate(
        sorted(balances.items(), key=lambda x: (-x[1], x[0]))[: max(1, int(limit))],
        start=1,
    ):
        rows.append(
            {
                "rank": rank,
                "address": addr,
                "balance_sats": sats,
                "balance_mhc": format_mhc(sats),
                "utxo_count": utxo_counts.get(addr, 0),
                "share_pct": round(100.0 * sats / total_supply, 4) if total_supply else 0.0,
            }
        )
    return {
        "addresses": rows,
        "count": len(rows),
        "total_utxos": sum(utxo_counts.values()),
        "total_supply_sats": total_supply,
        "total_supply_mhc": format_mhc(total_supply),
        "source": source,
        "limit": limit,
    }


def _rich_list_from_sqlite(
    data_dir, *, hrp: str = DEFAULT_HRP, limit: int = 100
) -> dict[str, Any] | None:
    """Prefer live ``utxo.sqlite`` (node may leave LMDB chainstate behind)."""
    import sqlite3
    from collections import defaultdict
    from pathlib import Path

    from mhcoin.transaction.input import TxOut

    path = Path(data_dir) / "utxo.sqlite"
    if not path.is_file():
        return None
    balances: dict[str, int] = defaultdict(int)
    utxo_counts: dict[str, int] = defaultdict(int)
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute("SELECT value, script_pubkey FROM utxo").fetchall()
        finally:
            db.close()
    except Exception:
        return None
    if not rows:
        return None
    for value, script in rows:
        try:
            tout = TxOut(value=int(value), script_pubkey=bytes(script))
            addr = out_address(tout, hrp=hrp)
        except Exception:
            continue
        if not addr:
            continue
        balances[addr] += int(value)
        utxo_counts[addr] += 1
    if not balances:
        return None
    return _rich_list_rows(
        dict(balances), dict(utxo_counts), limit=limit, source="utxo.sqlite"
    )


def rich_list(
    data_dir,
    *,
    chain: ReadOnlyChain | None = None,
    hrp: str = DEFAULT_HRP,
    limit: int = 100,
) -> dict[str, Any]:
    """Top addresses by on-chain UTXO balance (``utxo.sqlite`` preferred)."""
    from collections import defaultdict
    from pathlib import Path

    from mhcoin.utxo import UTXOSet

    limit = max(1, min(int(limit), 500))
    from_sqlite = _rich_list_from_sqlite(data_dir, hrp=hrp, limit=limit)
    if from_sqlite is not None:
        return from_sqlite
    cs = Path(data_dir) / "chainstate"
    if cs.is_dir():
        try:
            utxo = UTXOSet(cs)
            balances: dict[str, int] = defaultdict(int)
            utxo_counts: dict[str, int] = defaultdict(int)
            for entry in utxo.all_entries():
                addr = out_address(entry.output, hrp=hrp)
                if not addr:
                    continue
                val = int(entry.output.value)
                balances[addr] += val
                utxo_counts[addr] += 1
            if balances:
                return _rich_list_rows(
                    dict(balances), dict(utxo_counts), limit=limit, source="chainstate"
                )
        except Exception:
            pass
    if chain is not None:
        return _rich_list_chain_scan(chain, hrp=hrp, limit=limit)
    return {
        "addresses": [],
        "count": 0,
        "total_utxos": 0,
        "total_supply_sats": 0,
        "total_supply_mhc": format_mhc(0),
        "source": "unavailable",
        "limit": limit,
    }


def orphan_blocks(
    chain: ReadOnlyChain, *, hrp: str = DEFAULT_HRP, limit: int = 100
) -> dict[str, Any]:
    """Best-effort fork/orphan view.

    Gap this does NOT cover: the node's ``OrphanPool`` (mhcoin.blockchain.orphans)
    holds blocks whose *parent is unknown yet* — it is a small in-memory, bounded
    cache (``MAX_ORPHAN_BLOCKS``) that is never written to ``chain.sqlite`` and is
    lost on restart. There is no on-disk record of those once they expire or the
    node restarts, so a read-only explorer process cannot reconstruct them after
    the fact without a protocol/storage change upstream.

    What IS persisted and shown here: every block that was fully *validated* and
    connected to the tree but then lost a reorg — stored in ``block_index`` with
    ``status=STATUS_SIDE`` (see ``mhcoin.blockchain.chain``). These are genuine
    stale/non-canonical blocks ("fork tips") with real proof-of-work behind them,
    which is the useful, honest signal for an explorer: how often (and how deep)
    the chain has forked.
    """
    now = int(time.time())
    tip = chain.height
    genesis = chain.get_block_by_height(0)
    genesis_bits = int(genesis.header.bits) if genesis else None
    entries = chain.get_side_chain_entries(limit=limit)

    # A side block that is nobody's prev_hash is a dead-end fork tip (not since
    # superseded by another stored side block at height+1). Mark those distinctly.
    prevs = {e["prev_hash"] for e in entries}

    rows: list[dict[str, Any]] = []
    for e in entries:
        h = e["height"]
        active = chain.get_block_by_height(h)
        active_hash = active.block_hash().hex() if active else None
        diff = difficulty_from_bits(e["bits"], genesis_bits=genesis_bits)
        ts = e["timestamp"]
        rows.append(
            {
                "height": h,
                "hash": e["hash"],
                "prev_hash": e["prev_hash"],
                "active_hash_at_height": active_hash,
                "replaced_by_active": bool(active_hash) and active_hash != e["hash"],
                "chain_work": e["chain_work"],
                "bits": diff["bits"],
                "difficulty_display": diff["difficulty_display"],
                "timestamp": ts,
                "time_utc": format_utc(ts),
                "age": format_age(ts, now=now),
                "blocks_behind_tip": max(0, tip - h) if tip >= 0 else None,
                "is_fork_tip": e["hash"] not in prevs,
            }
        )
    rows.sort(key=lambda r: (-r["height"], -r["chain_work"]))
    return {
        "tip_height": tip,
        "count": len(rows),
        "fork_tip_count": sum(1 for r in rows if r["is_fork_tip"]),
        "blocks": rows,
        "source": "block_index (status=SIDE)",
        "gap_note": (
            "Unknown-parent orphans live only in the node's in-memory OrphanPool "
            "(bounded, not persisted) and cannot be listed after the fact. This "
            "page lists validated side-chain blocks that lost a reorg instead — "
            "the durable, on-disk signal of forking activity."
        ),
    }


def chain_stats(chain: ReadOnlyChain, *, hrp: str = DEFAULT_HRP) -> dict[str, Any]:
    tip = chain.height
    tip_hash = chain.tip_hash.hex() if chain.tip_hash else None
    tip_block = chain.get_block_by_height(tip) if tip >= 0 else None
    genesis = chain.get_block_by_height(0) if tip >= 0 else None
    now = int(time.time())
    tip_ts = int(tip_block.header.timestamp) if tip_block else None
    gen_ts = int(genesis.header.timestamp) if genesis else None

    total_tx = 0
    transfer_tx = 0
    minted_sats = 0
    intervals: list[int] = []
    prev_ts: int | None = None
    for h in range(0, tip + 1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        ts = int(block.header.timestamp)
        if prev_ts is not None and h > 0:
            intervals.append(ts - prev_ts)
        prev_ts = ts
        # Issued supply = sum of consensus subsidies (not coinbase outputs:
        # those include fees and made remaining look like 20965149.99995999).
        minted_sats += int(get_block_subsidy(h))
        for tx in block.transactions:
            total_tx += 1
            if not tx.is_coinbase():
                transfer_tx += 1

    recent_iv = [x for x in intervals[-30:] if 0 < x < 86_400]
    avg_interval = int(sum(recent_iv) / len(recent_iv)) if recent_iv else None
    net = estimate_network_hashrate(chain, tip=tip, window=30)

    genesis_bits = int(genesis.header.bits) if genesis else None
    tip_bits_i = int(tip_block.header.bits) if tip_block else None
    diff = (
        difficulty_from_bits(tip_bits_i, genesis_bits=genesis_bits)
        if tip_bits_i is not None
        else {}
    )
    tip_age_sec = (now - tip_ts) if tip_ts is not None else None
    target = TARGET_BLOCK_TIME_SECONDS
    eta_sec = None
    if tip_age_sec is not None:
        # remaining vs target spacing (0 if overdue)
        eta_sec = max(0, target - tip_age_sec)
    overdue = bool(tip_age_sec is not None and tip_age_sec > target)
    charts = chart_series(chain, tip=tip, window=30)
    miners = top_miners(chain, tip=tip, window=100, hrp=hrp)
    halv = halving_info(tip if tip >= 0 else 0)

    return {
        "tip_height": tip,
        "tip_hash": tip_hash,
        "total_blocks": tip + 1 if tip >= 0 else 0,
        "total_transactions": total_tx,
        "transfer_transactions": transfer_tx,
        "coinbase_transactions": total_tx - transfer_tx,
        "minted_sats": minted_sats,
        "minted_mhc": format_mhc(minted_sats),
        "avg_block_interval_seconds": avg_interval,
        "network_hashrate": net.get("hashrate"),
        "network_hashrate_hps": net.get("hashrate_hps"),
        "network_hashrate_label": net.get("hashrate_label"),
        "network_hashrate_window": net.get("hashrate_window"),
        "network_hashrate_window_hps": net.get("hashrate_window_hps"),
        "network_hashrate_window_label": net.get("hashrate_window_label"),
        "network_hashrate_short": net.get("hashrate_short"),
        "network_hashrate_short_hps": net.get("hashrate_short_hps"),
        "tip_bits": net.get("bits") or diff.get("bits"),
        "tip_target": diff.get("target"),
        "tip_target_short": diff.get("target_short"),
        "tip_difficulty": diff.get("difficulty"),
        "tip_difficulty_display": diff.get("difficulty_display"),
        "tip_time_utc": format_utc(tip_ts),
        "tip_age": format_age(tip_ts, now=now),
        "tip_age_seconds": tip_age_sec,
        "next_block_eta_seconds": eta_sec,
        "next_block_eta": format_duration(eta_sec),
        "next_block_overdue": overdue,
        "next_block_hint": (
            (
                f"last block {format_age(tip_ts, now=now)} ago · target {format_duration(target)}"
                + (f" · ETA {format_duration(eta_sec)}" if not overdue else " · overdue")
            )
            if tip_age_sec is not None
            else None
        ),
        "genesis_hash": genesis.block_hash().hex() if genesis else None,
        "genesis_time_utc": format_utc(gen_ts),
        "target_block_time_seconds": TARGET_BLOCK_TIME_SECONDS,
        "satoshi_per_coin": SATOSHI_PER_COIN,
        "halving": halv,
        "top_miners": miners,
        "charts": charts,
        "recent": recent_blocks(
            chain, count=min(100, (tip + 1) if tip >= 0 else 0), tip=tip, hrp=hrp
        ),
        "blocks_per_page": 100,
        "recent_txs": recent_transactions(
            chain, count=min(100, max(total_tx, 1)), tip=tip, hrp=hrp
        ),
        "txs_per_page": 100,
        "recent_transfers": recent_transactions(
            chain,
            count=min(100, max(transfer_tx, 1)),
            tip=tip,
            hrp=hrp,
            transfers_only=True,
        ),
        "transfers_per_page": 100,
        "live_finds": recent_blocks(chain, count=8, tip=tip, hrp=hrp),
    }
