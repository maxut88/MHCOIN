#!/usr/bin/env python3
"""Accelerated isolated soak for Coinbase Maturity 100 (no production touch).

Usage:
  PYTHONPATH=. python3 scripts/coinbase_maturity_isolated_soak.py [--minutes N] [--datadir PATH]

Default: 10 minutes of mine / sync / disconnect / reconnect / restart cycles
under regtest with maturity activation=1 (in-process only).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

# Ensure worktree imports
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mhcoin.blockchain.fingerprint import utxo_fingerprint
from mhcoin.blockchain.genesis import mine_regtest_genesis
from mhcoin.consensus.params import COINBASE_MATURITY_ACTIVATION
from mhcoin.node.local_node import LocalNode
from mhcoin.wallet.addresses import address_to_pubkey_hash
from mhcoin.wallet.wallet import Wallet, WalletPaths


def _activate():
    patched = dict(COINBASE_MATURITY_ACTIVATION)
    patched["regtest"] = 1
    import mhcoin.consensus.params as params

    params.COINBASE_MATURITY_ACTIVATION = patched
    os.environ["MHCOIN_NETWORK"] = "regtest"


def _sync(src: LocalNode, dst: LocalNode) -> None:
    while dst.chain.height < src.chain.height:
        blk = src.chain.get_block_by_height(dst.chain.height + 1)
        assert blk is not None
        dst.chain.accept_block(blk, mempool=dst.mempool)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument(
        "--datadir",
        type=Path,
        default=Path("/tmp/mhcoin_maturity_isolated_soak"),
    )
    args = ap.parse_args()
    _activate()

    if args.datadir.exists():
        shutil.rmtree(args.datadir)
    args.datadir.mkdir(parents=True)

    report = {
        "start": time.time(),
        "minutes_requested": args.minutes,
        "cycles": 0,
        "blocks_mined": 0,
        "reconnects": 0,
        "restarts": 0,
        "errors": [],
        "status": "RUNNING",
    }
    report_path = Path("/tmp/mhcoin_maturity_isolated_soak_status.json")

    def flush():
        report["elapsed_sec"] = time.time() - report["start"]
        report_path.write_text(json.dumps(report, indent=2))

    try:
        wpaths = WalletPaths(data_dir=args.datadir / "wallet", network="regtest", hrp="mhc")
        w = Wallet(wpaths)
        alice = w.create(password="soak").address
        pkh = address_to_pubkey_hash(alice, hrp="mhc")
        genesis = mine_regtest_genesis(pubkey_hash=pkh)

        def make_node(name: str) -> LocalNode:
            d = args.datadir / name
            d.mkdir(parents=True, exist_ok=True)
            n = LocalNode(d, hrp="mhc", network="regtest")
            if n.chain.height < 0:
                n.chain.init_with_genesis(genesis)
            return n

        a = make_node("a")
        b = make_node("b")
        c = make_node("c")
        deadline = time.time() + args.minutes * 60.0

        while time.time() < deadline:
            report["cycles"] += 1
            # Mine a few on A
            for _ in range(3):
                a.mine_one(alice)
                report["blocks_mined"] += 1
            _sync(a, b)
            # Disconnect C every other cycle
            if report["cycles"] % 2 == 0:
                for _ in range(2):
                    a.mine_one(alice)
                    report["blocks_mined"] += 1
                _sync(a, b)
                _sync(a, c)
                report["reconnects"] += 1
            else:
                _sync(a, c)

            assert a.chain.tip_hash == b.chain.tip_hash == c.chain.tip_hash
            assert utxo_fingerprint(a.chain.utxo) == utxo_fingerprint(c.chain.utxo)

            # Periodic restart of C
            if report["cycles"] % 5 == 0:
                tip_h, tip_hash = a.chain.height, a.chain.tip_hash
                c.close()
                c = make_node("c")
                _sync(a, c)
                assert c.chain.height == tip_h and c.chain.tip_hash == tip_hash
                report["restarts"] += 1

            flush()

        a.close()
        b.close()
        c.close()
        report["status"] = "PASS"
        report["end"] = time.time()
        flush()
        print(json.dumps(report, indent=2))
        return 0
    except Exception as e:
        report["status"] = "FAIL"
        report["errors"].append(f"{e}\n{traceback.format_exc()}")
        report["end"] = time.time()
        flush()
        print(json.dumps(report, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
