"""python -m mhcoin.audit — RC1 pre-launch audit CLI."""

from __future__ import annotations

import argparse
import json
import sys

from mhcoin.audit.rc1 import (
    consensus_identity,
    consensus_source_fingerprint,
    format_audit_report,
    independent_recompute_genesis,
    run_rc1_audit,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MHCOIN RC1 pre-launch audit (does not launch mainnet)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("rc1", help="Run automated RC1 checklist")
    g = sub.add_parser("genesis", help="Independent genesis recompute")
    g.add_argument("--network", default="mainnet")
    sub.add_parser("identity", help="Print consensus identity JSON (compare across machines)")
    sub.add_parser("fingerprint", help="Print consensus source fingerprint")

    args = p.parse_args(argv)
    if args.cmd == "rc1":
        report = run_rc1_audit()
        print(format_audit_report(report))
        print(json.dumps({"ok": report["ok"], "fingerprint": report["consensus_fingerprint"]}, indent=2))
        return 0 if report["ok"] else 1
    if args.cmd == "genesis":
        info = independent_recompute_genesis(args.network)
        print(json.dumps(info, indent=2))
        return 0
    if args.cmd == "identity":
        print(json.dumps(consensus_identity(), indent=2, sort_keys=True))
        return 0
    if args.cmd == "fingerprint":
        print(json.dumps(consensus_source_fingerprint(), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
