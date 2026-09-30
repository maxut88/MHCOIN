#!/usr/bin/env python3
"""Reproducible MHCOIN genesis tooling (Stage 9).

Does NOT mutate frozen constants. Use `mine-prepare` to produce candidate JSON;
operators must review and manually freeze values into mhcoin.consensus.params.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mhcoin.blockchain.genesis import (
    mine_genesis,
    validate_genesis_block,
    verify_frozen_genesis,
)
from mhcoin.consensus.params import get_network_params, list_networks


def cmd_show(args: argparse.Namespace) -> int:
    info = verify_frozen_genesis(args.network)
    print(json.dumps(info, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    info = verify_frozen_genesis(args.network)
    params = get_network_params(args.network)
    print(f"OK  network={params.name}")
    print(f"    genesis_hash={info['genesis_hash']}")
    print(f"    merkle_root={info['merkle_root']}")
    print(f"    nonce={info['nonce']}  timestamp={info['timestamp']}")
    print(f"    bits={info['bits_hex']}  magic={info['magic']}  port={info['default_port']}")
    return 0


def cmd_mine_prepare(args: argparse.Namespace) -> int:
    """Mine a candidate genesis for review — does not freeze into source."""
    params = get_network_params(args.network)
    if args.network == "mainnet" and not args.i_understand:
        print(
            "Refusing to mine mainnet candidate without --i-understand.\n"
            "This tool never auto-launches mainnet or writes frozen constants.",
            file=sys.stderr,
        )
        return 2
    pkh = bytes.fromhex(args.pubkey_hash) if args.pubkey_hash else params.genesis_pubkey_hash
    extra = args.extra.encode() if args.extra else params.genesis_coinbase_extra
    # Temporary params clone via mining with overrides on template
    from mhcoin.blockchain.genesis import build_genesis_template
    from mhcoin.consensus.proof_of_work import mine_block
    from dataclasses import replace

    candidate_params = replace(
        params,
        genesis_coinbase_extra=extra,
        genesis_pubkey_hash=pkh,
        genesis_timestamp=args.timestamp or params.genesis_timestamp,
        genesis_bits=int(args.bits, 0) if args.bits else params.genesis_bits,
    )
    print(f"Mining candidate genesis for {args.network} …", file=sys.stderr)
    block = build_genesis_template(candidate_params)
    block = mine_block(block)
    info = {
        "network": args.network,
        "NOTE": "CANDIDATE ONLY — manually freeze into mhcoin.consensus.params if approved",
        "genesis_hash": block.block_hash().hex(),
        "merkle_root": block.header.merkle_root.hex(),
        "nonce": block.header.nonce,
        "timestamp": block.header.timestamp,
        "bits": candidate_params.genesis_bits,
        "bits_hex": f"0x{candidate_params.genesis_bits:08x}",
        "pubkey_hash": pkh.hex(),
        "coinbase_extra": extra.decode("utf-8", errors="replace"),
        "magic": params.magic.hex(),
        "default_port": params.default_port,
        "coinbase_txid": block.transactions[0].txid_hex(),
    }
    text = json.dumps(info, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    return 0


def cmd_verify_all(_: argparse.Namespace) -> int:
    ok = True
    for name in list_networks():
        try:
            verify_frozen_genesis(name)
            print(f"OK  {name}")
        except Exception as e:
            ok = False
            print(f"FAIL {name}: {e}", file=sys.stderr)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MHCOIN genesis show / verify / mine-prepare")
    sub = p.add_subparsers(dest="cmd", required=True)

    show = sub.add_parser("show", help="Print frozen genesis parameters")
    show.add_argument("--network", required=True, choices=list_networks())
    show.set_defaults(func=cmd_show)

    verify = sub.add_parser("verify", help="Rebuild and verify frozen genesis")
    verify.add_argument("--network", required=True, choices=list_networks())
    verify.set_defaults(func=cmd_verify)

    fall = sub.add_parser("verify-all", help="Verify frozen genesis for every network")
    fall.set_defaults(func=cmd_verify_all)

    mine = sub.add_parser(
        "mine-prepare",
        help="Mine a candidate genesis JSON (does not freeze / does not launch)",
    )
    mine.add_argument("--network", required=True, choices=list_networks())
    mine.add_argument("--pubkey-hash", default=None, help="40-hex burn/dev pubkey hash")
    mine.add_argument("--extra", default=None, help="Coinbase extra string")
    mine.add_argument("--timestamp", type=int, default=None)
    mine.add_argument("--bits", default=None, help="Compact bits (hex or int)")
    mine.add_argument("--out", default=None, help="Write candidate JSON")
    mine.add_argument(
        "--i-understand",
        action="store_true",
        help="Required for mainnet candidate mining",
    )
    mine.set_defaults(func=cmd_mine_prepare)

    # Legacy: bare invocation mines regtest (compat with docs/GENESIS.md)
    if argv is None and len(sys.argv) > 1 and sys.argv[1] not in (
        "show",
        "verify",
        "verify-all",
        "mine-prepare",
        "-h",
        "--help",
    ):
        # Old flags: --pubkey-hash / --out → mine regtest candidate
        legacy = argparse.ArgumentParser()
        legacy.add_argument("--pubkey-hash", default=None)
        legacy.add_argument("--out", default=None)
        la = legacy.parse_args()
        ns = argparse.Namespace(
            network="regtest",
            pubkey_hash=la.pubkey_hash,
            extra=None,
            timestamp=None,
            bits=None,
            out=la.out,
            i_understand=True,
        )
        return cmd_mine_prepare(ns)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
