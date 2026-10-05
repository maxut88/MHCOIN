"""Decode MHCOIN blocks/TXs for the read-only LAN explorer."""

from __future__ import annotations

import time
from typing import Any

from mhcoin.blockchain.block import Block
from mhcoin.blockchain.readonly_chain import ReadOnlyChain
from mhcoin.consensus.chain_work import work_for_bits
from mhcoin.consensus.difficulty import bits_to_target
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.params import HALVING_INTERVAL, SATOSHI_PER_COIN, TARGET_BLOCK_TIME_SECONDS
from mhcoin.constants import INITIAL_BLOCK_SUBSIDY
from mhcoin.transaction.transaction import Transaction
from mhcoin.wallet.addresses import address_to_pubkey_hash, pubkey_hash_to_address, validate_address
from mhcoin.wallet.send import format_mhc

DEFAULT_HRP = "mhc"
BLOCKS_PER_PAGE = 100
TXS_PER_PAGE = 100


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


def load_mempool(data_dir, *, hrp: str = DEFAULT_HRP) -> dict[str, Any]:
    """Read node mempool.json (unconfirmed txs) without opening the live node."""
    from pathlib import Path
    import json

    path = Path(data_dir) / "mempool.json"
    empty = {"count": 0, "transactions": [], "path": str(path)}
    if not path.is_file():
        return empty
    try:
        raw = json.loads(path.read_text())
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
    for key, val in items[:50]:
        # mempool may store hex or nested dict — keep compact
        if isinstance(val, dict):
            txid = val.get("txid") or key
            size = val.get("size") or val.get("size_bytes")
            fee = val.get("fee_mhc") or val.get("fee")
            rows.append({"txid": txid, "size_bytes": size, "fee_mhc": fee, "raw": False})
        elif isinstance(val, str) and len(val) > 64:
            try:
                from mhcoin.transaction.transaction import Transaction
                tx = Transaction.deserialize(bytes.fromhex(val))
                out_v = int(tx.output_value())
                rows.append(
                    {
                        "txid": tx.txid().hex(),
                        "size_bytes": len(tx.serialize()),
                        "output_value_mhc": format_mhc(out_v),
                        "coinbase": tx.is_coinbase(),
                        "output_count": len(tx.outputs),
                        "input_count": len(tx.inputs),
                    }
                )
            except Exception:
                rows.append({"txid": key or "?", "note": "unparsed"})
        else:
            rows.append({"txid": key or str(val)[:64]})
    return {"count": len(items), "transactions": rows, "path": str(path)}


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
            fee_sats = None
            fee_per_byte = None
            from_a = "coinbase" if tx.is_coinbase() else None
            to_a = out_address(tx.outputs[0], hrp=hrp) if tx.outputs else None
            if not tx.is_coinbase() and tx.inputs:
                tin = tx.inputs[0]
                prev = _prev_output(chain, tin.prev_txid, tin.prev_vout)
                if prev is not None:
                    tout, _ = prev
                    from_a = out_address(tout, hrp=hrp)
                    # approx fee when single-input
                    if len(tx.inputs) == 1 and int(tout.value) >= out_v:
                        fee_sats = int(tout.value) - out_v
                        sz = len(tx.serialize())
                        if sz > 0:
                            fee_per_byte = round(fee_sats / sz, 3)
                if tx.outputs:
                    to_a = None
                    for tout in tx.outputs:
                        cand = out_address(tout, hrp=hrp)
                        if cand and cand != from_a:
                            to_a = cand
                            break
                    if to_a is None:
                        to_a = out_address(tx.outputs[0], hrp=hrp)
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

    inputs: list[dict[str, Any]] = []
    in_value = 0
    for tin in tx.inputs:
        if tin.is_coinbase():
            inputs.append({"coinbase": True, "script_sig_hex": tin.script_sig.hex()})
            continue
        prev = _prev_output(chain, tin.prev_txid, tin.prev_vout)
        entry: dict[str, Any] = {
            "coinbase": False,
            "prev_txid": tin.prev_txid.hex(),
            "prev_vout": int(tin.prev_vout),
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
        outputs.append(
            {
                "n": i,
                "value_sats": int(tout.value),
                "value_mhc": format_mhc(int(tout.value)),
                "address": out_address(tout, hrp=hrp),
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
        # Explicit N/A for Bitcoin-only concepts (documented in UI)
        "witness": False,
        "weight": None,
        "rbf": None,
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
    chain: ReadOnlyChain, address: str, *, hrp: str = DEFAULT_HRP, limit: int = 5000
) -> dict[str, Any]:
    if not validate_address(address, hrp=hrp):
        raise ValueError("invalid address")

    pkh = address_to_pubkey_hash(address, hrp=hrp)
    received: list[dict[str, Any]] = []
    owned: dict[tuple[str, int], int] = {}  # (txid, vout) -> value
    total_in = 0
    total_spent_inputs = 0  # raw UTXO spend (includes change cycle)
    total_paid_external = 0  # MHC paid to other addresses (NOT fee, NOT change)
    total_fees_paid = 0  # network fees when we fully funded the tx
    tip = chain.height
    now = int(time.time())
    for h in range(0, tip + 1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        bh = block.block_hash().hex()
        ts = int(block.header.timestamp)
        for tx in block.transactions:
            txh = txid_hex(tx)
            spent_here = 0
            if not tx.is_coinbase():
                for tin in tx.inputs:
                    key = (tin.prev_txid.hex(), int(tin.prev_vout))
                    if key in owned:
                        spent_here += owned.pop(key)
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
    balance = sum(owned.values())
    received.reverse()  # newest first
    return {
        "address": address,
        "received_count": len(received),
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
        "outputs": received[:limit],
        "truncated": len(received) > limit,
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
    for h in range(tip, -1, -1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        bh = block.block_hash().hex()
        ts = int(block.header.timestamp)
        for idx in range(len(block.transactions) - 1, -1, -1):
            tx = block.transactions[idx]
            cb = tx.is_coinbase()
            if transfers_only and cb:
                continue
            if seen < skip:
                seen += 1
                continue
            out_v = int(tx.output_value())
            from_addr: str | None = None
            to_addr: str | None = None
            fee_sats: int | None = None
            if cb:
                to_addr = out_address(tx.outputs[0], hrp=hrp) if tx.outputs else None
                from_addr = "coinbase"
            else:
                tin = tx.inputs[0]
                prev = _prev_output(chain, tin.prev_txid, tin.prev_vout)
                if prev is not None:
                    tout, _ = prev
                    from_addr = out_address(tout, hrp=hrp)
                    in_sum = int(tout.value)
                    if len(tx.inputs) == 1 and in_sum >= out_v:
                        fee_sats = in_sum - out_v
                if tx.outputs:
                    to_addr = None
                    for tout in tx.outputs:
                        cand = out_address(tout, hrp=hrp)
                        if cand and cand != from_addr:
                            to_addr = cand
                            break
                    if to_addr is None:
                        to_addr = out_address(tx.outputs[0], hrp=hrp)
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
                    "index_in_block": idx,
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
    for h in range(tip, -1, -1):
        block = chain.get_block_by_height(h)
        if block is None:
            continue
        bh = block.block_hash().hex()
        ts = int(block.header.timestamp)
        # Newest-first within block: reverse index order
        for idx in range(len(block.transactions) - 1, -1, -1):
            tx = block.transactions[idx]
            cb = tx.is_coinbase()
            if transfers_only and cb:
                continue
            out_v = int(tx.output_value())
            from_addr: str | None = None
            to_addr: str | None = None
            fee_sats: int | None = None
            if cb:
                to_addr = out_address(tx.outputs[0], hrp=hrp) if tx.outputs else None
                from_addr = "coinbase"
            else:
                tin = tx.inputs[0]
                prev = _prev_output(chain, tin.prev_txid, tin.prev_vout)
                if prev is not None:
                    tout, _ = prev
                    from_addr = out_address(tout, hrp=hrp)
                    in_sum = int(tout.value)
                    if len(tx.inputs) == 1 and in_sum >= out_v:
                        fee_sats = in_sum - out_v
                if tx.outputs:
                    # Prefer first non-change output as the payment destination.
                    to_addr = None
                    for tout in tx.outputs:
                        cand = out_address(tout, hrp=hrp)
                        if cand and cand != from_addr:
                            to_addr = cand
                            break
                    if to_addr is None:
                        to_addr = out_address(tx.outputs[0], hrp=hrp)
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
                    "index_in_block": idx,
                }
            )
            if len(out) >= count:
                return out
    return out


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
        for tx in block.transactions:
            total_tx += 1
            if tx.is_coinbase():
                minted_sats += sum(int(o.value) for o in tx.outputs)
            else:
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
            chain, count=max(transfer_tx, 1), tip=tip, hrp=hrp, transfers_only=True
        ),
        "live_finds": recent_blocks(chain, count=8, tip=tip, hrp=hrp),
    }
