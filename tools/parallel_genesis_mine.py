#!/usr/bin/env python3
"""Parallel nonce search for mainnet genesis candidate (does not freeze params)."""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import struct
import sys
import time
from dataclasses import replace
from pathlib import Path

from mhcoin.blockchain.genesis import build_genesis_template
from mhcoin.consensus.difficulty import bits_to_target
from mhcoin.consensus.params import get_network_params

_NONCE_OFF = 76


def _worker(args: tuple) -> tuple[int, bytes, float] | None:
    header_bytes, target, start, end, progress_every = args
    buf = bytearray(header_bytes)
    sha256 = hashlib.sha256
    pack_nonce = struct.pack_into
    t0 = time.time()
    checked = 0
    for nonce in range(start, end):
        pack_nonce("<I", buf, _NONCE_OFF, nonce)
        h = sha256(sha256(buf).digest()).digest()
        checked += 1
        if int.from_bytes(h, "little") <= target:
            elapsed = max(time.time() - t0, 1e-9)
            return nonce, h, checked / elapsed
        if progress_every and checked % progress_every == 0:
            # parent polls via shared counter optional; keep CPU hot
            pass
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="Parallel mainnet genesis mine-prepare")
    p.add_argument("--network", default="mainnet")
    p.add_argument("--bits", default="0x1d00ffff")
    p.add_argument("--extra", default="MHCOIN/mainnet genesis v2")
    p.add_argument("--timestamp", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=0, help="0 = cpu_count()")
    p.add_argument("--i-understand", action="store_true")
    p.add_argument("--status-every", type=float, default=30.0)
    args = p.parse_args()

    if args.network == "mainnet" and not args.i_understand:
        print("Need --i-understand for mainnet", file=sys.stderr)
        return 2

    params = get_network_params(args.network)
    bits = int(args.bits, 0)
    candidate = replace(
        params,
        genesis_coinbase_extra=args.extra.encode(),
        genesis_timestamp=args.timestamp,
        genesis_bits=bits,
    )
    block = build_genesis_template(candidate)
    header = bytearray(block.header.serialize())
    target = bits_to_target(bits)
    workers = args.workers or os.cpu_count() or 1
    nonce_space = 0x100000000
    chunk = (nonce_space + workers - 1) // workers

    print(
        f"Parallel genesis mine network={args.network} bits=0x{bits:08x} "
        f"timestamp={args.timestamp} workers={workers} target≈2^{target.bit_length()}",
        file=sys.stderr,
        flush=True,
    )
    print(f"merkle={block.header.merkle_root.hex()}", file=sys.stderr, flush=True)

    tasks = []
    for i in range(workers):
        start = i * chunk
        end = min(nonce_space, start + chunk)
        if start >= end:
            continue
        tasks.append((bytes(header), target, start, end, 0))

    t0 = time.time()
    found: tuple[int, bytes, float] | None = None
    # Use imap_unordered so first hit can stop early via terminate
    with mp.Pool(processes=workers) as pool:
        async_results = [pool.apply_async(_worker, (t,)) for t in tasks]
        last_status = t0
        pending = list(async_results)
        while pending and found is None:
            still = []
            for ar in pending:
                if ar.ready():
                    res = ar.get()
                    if res is not None:
                        found = res
                        break
                else:
                    still.append(ar)
            pending = still
            now = time.time()
            if now - last_status >= args.status_every:
                elapsed = max(now - t0, 1e-9)
                # Rough: assume uniform progress unknown; report elapsed only
                print(
                    f"[genesis-mine] still searching… elapsed={elapsed/3600:.2f}h "
                    f"workers={workers}",
                    file=sys.stderr,
                    flush=True,
                )
                last_status = now
            if found is None and pending:
                time.sleep(1.0)
        pool.terminate()
        pool.join()

    if found is None:
        print("nonce space exhausted", file=sys.stderr)
        return 1

    nonce, h, _hs = found
    block.header.nonce = nonce
    elapsed = max(time.time() - t0, 1e-9)
    info = {
        "network": args.network,
        "NOTE": "CANDIDATE ONLY — manually freeze into mhcoin.consensus.params if approved",
        "genesis_hash": block.block_hash().hex(),
        "merkle_root": block.header.merkle_root.hex(),
        "nonce": nonce,
        "timestamp": args.timestamp,
        "bits": bits,
        "bits_hex": f"0x{bits:08x}",
        "pubkey_hash": candidate.genesis_pubkey_hash.hex(),
        "coinbase_extra": args.extra,
        "magic": params.magic.hex(),
        "default_port": params.default_port,
        "coinbase_txid": block.transactions[0].txid_hex(),
        "elapsed_seconds": elapsed,
        "workers": workers,
    }
    text = json.dumps(info, indent=2)
    print(text)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(f"Wrote {args.out} in {elapsed/3600:.2f}h", file=sys.stderr)
    return 0


if __name__ == "__main__":
    # Avoid nested pools / fork issues after imports
    mp.set_start_method("fork", force=True)
    raise SystemExit(main())
