"""MHCOIN Release Candidate 1 — Pre-Launch Audit tooling.

Does NOT launch mainnet. Verifies frozen production protocol invariants that
unit tests alone can miss (independent genesis recompute, consensus fingerprint,
cross-build identity).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.genesis import build_genesis_coinbase
from mhcoin.blockchain.merkle import merkle_root
from mhcoin.consensus.params import (
    BLOCK_VERSION,
    CONSENSUS_CRITICAL_PER_NETWORK,
    CONSENSUS_CRITICAL_SHARED,
    DECIMALS,
    DIFFICULTY_ADJUSTMENT_INTERVAL,
    DIFFICULTY_BTC_ACTIVATION_HEIGHT,
    DIFFICULTY_LEGACY_DAMPING_DENOMINATOR,
    DIFFICULTY_LEGACY_DAMPING_NUMERATOR,
    DIFFICULTY_LEGACY_WINDOW,
    DIFFICULTY_WINDOW,
    HALVING_INTERVAL,
    INITIAL_BLOCK_REWARD_COINS,
    MAX_FUTURE_BLOCK_TIME,
    MAX_SUPPLY_COINS,
    MAX_SUPPORTED_PROTOCOL_VERSION,
    MIN_SUPPORTED_PROTOCOL_VERSION,
    MTP_WINDOW,
    POW_ALGORITHM,
    PROTOCOL_VERSION,
    SOFTWARE_VERSION,
    TARGET_BLOCK_TIME_SECONDS,
    get_network_params,
    list_networks,
)
from mhcoin.consensus.difficulty import POW_LIMIT_MAINNET, POW_LIMIT_MAINNET_BITS
from mhcoin.consensus.proof_of_work import verify_proof_of_work
from mhcoin.crypto.hashing import hash256
from mhcoin.transaction.serialize import write_hash32, write_u32

# Modules whose contents participate in the consensus freeze fingerprint.
# Changing any of these is a potential consensus change until RC/mainnet policy says otherwise.
CONSENSUS_SOURCE_GLOBS = (
    "mhcoin/consensus/*.py",
    "mhcoin/blockchain/block.py",
    "mhcoin/blockchain/merkle.py",
    "mhcoin/blockchain/validation.py",
    "mhcoin/blockchain/genesis.py",
    "mhcoin/blockchain/undo.py",
    "mhcoin/transaction/*.py",
    "mhcoin/utxo/*.py",
    "mhcoin/crypto/hashing.py",
    "mhcoin/crypto/keys.py",
    "mhcoin/crypto/signatures.py",
)

EXPECTED_MAINNET_GENESIS = "62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def independent_recompute_genesis(network: str = "mainnet") -> dict:
    """
    Rebuild genesis from raw NetworkParams fields without trusting
    get_network_genesis() equality shortcuts.

    Steps: coinbase → merkle → header serialize → HASH256 → PoW check.
    """
    params = get_network_params(network)
    coinbase = build_genesis_coinbase(
        pubkey_hash=params.genesis_pubkey_hash,
        extra=params.genesis_coinbase_extra,
        height=0,
    )
    txids = [coinbase.txid()]
    merkle = merkle_root(txids)

    # Serialize header manually (same layout as BlockHeader.serialize)
    header_bytes = (
        write_u32(BLOCK_VERSION)
        + write_hash32(b"\x00" * 32)
        + write_hash32(merkle)
        + write_u32(params.genesis_timestamp)
        + write_u32(params.genesis_bits)
        + write_u32(params.genesis_nonce)
    )
    block_hash = hash256(header_bytes)

    header = BlockHeader.deserialize(header_bytes)
    pow_ok = verify_proof_of_work(header)

    # Cross-check Block API path
    block = Block(header=header, transactions=[coinbase])
    block.set_merkle_root()
    assert block.header.merkle_root == merkle

    result = {
        "network": params.name,
        "merkle_root": merkle.hex(),
        "header_hex": header_bytes.hex(),
        "block_hash": block_hash.hex(),
        "pow_ok": pow_ok,
        "expected_hash": params.genesis_hash_hex,
        "expected_merkle": params.genesis_merkle_hex,
        "match_hash": block_hash.hex() == params.genesis_hash_hex,
        "match_merkle": merkle.hex() == params.genesis_merkle_hex,
        "magic": params.magic.hex(),
        "default_port": params.default_port,
        "nonce": params.genesis_nonce,
        "timestamp": params.genesis_timestamp,
        "bits": params.genesis_bits,
        "bits_hex": f"0x{params.genesis_bits:08x}",
    }
    if not (result["match_hash"] and result["match_merkle"] and pow_ok):
        raise RuntimeError(
            f"independent genesis recompute FAILED for {network}: {json.dumps(result, indent=2)}"
        )
    return result


def consensus_identity() -> dict:
    """Stable identity blob for comparing two independent builds/machines."""
    params_main = get_network_params("mainnet")
    shared = {
        "MAX_SUPPLY_COINS": MAX_SUPPLY_COINS,
        "DECIMALS": DECIMALS,
        "INITIAL_BLOCK_REWARD_COINS": INITIAL_BLOCK_REWARD_COINS,
        "HALVING_INTERVAL": HALVING_INTERVAL,
        "TARGET_BLOCK_TIME_SECONDS": TARGET_BLOCK_TIME_SECONDS,
        "DIFFICULTY_ADJUSTMENT_INTERVAL": DIFFICULTY_ADJUSTMENT_INTERVAL,
        "DIFFICULTY_BTC_ACTIVATION_HEIGHT": DIFFICULTY_BTC_ACTIVATION_HEIGHT,
        "DIFFICULTY_LEGACY_WINDOW": DIFFICULTY_LEGACY_WINDOW,
        "DIFFICULTY_LEGACY_DAMPING_NUMERATOR": DIFFICULTY_LEGACY_DAMPING_NUMERATOR,
        "DIFFICULTY_LEGACY_DAMPING_DENOMINATOR": DIFFICULTY_LEGACY_DAMPING_DENOMINATOR,
        "DIFFICULTY_WINDOW": DIFFICULTY_WINDOW,
        "MTP_WINDOW": MTP_WINDOW,
        "MAX_FUTURE_BLOCK_TIME": MAX_FUTURE_BLOCK_TIME,
        "POW_LIMIT_MAINNET": POW_LIMIT_MAINNET,
        "POW_LIMIT_MAINNET_BITS": POW_LIMIT_MAINNET_BITS,
        "POW_ALGORITHM": POW_ALGORITHM,
        "PROTOCOL_VERSION": PROTOCOL_VERSION,
        "MIN_SUPPORTED_PROTOCOL_VERSION": MIN_SUPPORTED_PROTOCOL_VERSION,
        "MAX_SUPPORTED_PROTOCOL_VERSION": MAX_SUPPORTED_PROTOCOL_VERSION,
        "SOFTWARE_VERSION": SOFTWARE_VERSION,
        "CONSENSUS_CRITICAL_SHARED": list(CONSENSUS_CRITICAL_SHARED),
        "CONSENSUS_CRITICAL_PER_NETWORK": list(CONSENSUS_CRITICAL_PER_NETWORK),
    }
    networks = {}
    for name in list_networks():
        p = get_network_params(name)
        networks[name] = {
            "magic": p.magic.hex(),
            "default_port": p.default_port,
            "genesis_hash": p.genesis_hash_hex,
            "genesis_merkle": p.genesis_merkle_hex,
            "genesis_nonce": p.genesis_nonce,
            "genesis_timestamp": p.genesis_timestamp,
            "genesis_bits": p.genesis_bits,
            "address_hrp": p.address_hrp,
        }
    return {
        "shared": shared,
        "networks": networks,
        "mainnet_genesis_expected": EXPECTED_MAINNET_GENESIS,
        "mainnet_genesis_actual": params_main.genesis_hash_hex,
    }


def consensus_source_fingerprint(*, root: Path | None = None) -> dict:
    """
    HASH256 over sorted relative paths + file contents of consensus-critical sources.
    Two machines with identical trees must produce the same fingerprint.
    """
    base = root or _repo_root()
    files: list[Path] = []
    for pattern in CONSENSUS_SOURCE_GLOBS:
        files.extend(sorted(base.glob(pattern)))
    # Deduplicate and skip __pycache__ / __init__ noise where empty
    seen: set[Path] = set()
    ordered: list[Path] = []
    for f in sorted(files, key=lambda p: str(p.relative_to(base))):
        if not f.is_file():
            continue
        if "__pycache__" in f.parts:
            continue
        if f in seen:
            continue
        seen.add(f)
        ordered.append(f)

    h = hashlib.sha256()
    listing: list[dict] = []
    for f in ordered:
        rel = str(f.relative_to(base)).replace("\\", "/")
        data = f.read_bytes()
        file_digest = hashlib.sha256(data).hexdigest()
        h.update(rel.encode())
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
        listing.append({"path": rel, "sha256": file_digest, "size": len(data)})

    return {
        "fingerprint": h.hexdigest(),
        "file_count": len(listing),
        "files": listing,
    }


def run_rc1_audit() -> dict:
    """Run all automated RC1 checks that do not require external VPS hosts."""
    checks: list[dict] = []

    def _add(name: str, ok: bool, detail: dict | str | None = None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        gen = independent_recompute_genesis("mainnet")
        _add(
            "independent_mainnet_genesis",
            gen["block_hash"] == EXPECTED_MAINNET_GENESIS and gen["pow_ok"],
            {
                "block_hash": gen["block_hash"],
                "merkle_root": gen["merkle_root"],
                "expected": EXPECTED_MAINNET_GENESIS,
            },
        )
    except Exception as e:
        _add("independent_mainnet_genesis", False, str(e))

    for net in list_networks():
        try:
            independent_recompute_genesis(net)
            _add(f"independent_genesis:{net}", True, None)
        except Exception as e:
            _add(f"independent_genesis:{net}", False, str(e))

    identity = consensus_identity()
    _add(
        "mainnet_hash_constant",
        identity["mainnet_genesis_actual"] == EXPECTED_MAINNET_GENESIS,
        identity["mainnet_genesis_actual"],
    )
    _add(
        "protocol_bounds",
        (
            PROTOCOL_VERSION == 2
            and MIN_SUPPORTED_PROTOCOL_VERSION == 2
            and MAX_SUPPORTED_PROTOCOL_VERSION == 2
        ),
        {
            "protocol": PROTOCOL_VERSION,
            "min": MIN_SUPPORTED_PROTOCOL_VERSION,
            "max": MAX_SUPPORTED_PROTOCOL_VERSION,
        },
    )

    magics = {identity["networks"][n]["magic"] for n in identity["networks"]}
    _add("network_magic_unique", len(magics) == 4, sorted(magics))

    ports = {identity["networks"][n]["default_port"] for n in ("mainnet", "testnet")}
    _add("mainnet_testnet_ports_differ", len(ports) == 2, sorted(ports))

    fp = consensus_source_fingerprint()
    _add(
        "consensus_source_fingerprint",
        bool(fp["fingerprint"]) and fp["file_count"] > 0,
        {"fingerprint": fp["fingerprint"], "file_count": fp["file_count"]},
    )

    # Mainnet must never allow custom genesis bootstrap
    p = get_network_params("mainnet")
    _add("mainnet_no_custom_genesis", p.allow_custom_genesis_bootstrap is False, None)
    _add(
        "testnet_no_custom_genesis",
        get_network_params("testnet").allow_custom_genesis_bootstrap is False,
        None,
    )

    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "release": "RC1",
        "software_version": SOFTWARE_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "consensus_fingerprint": fp["fingerprint"],
        "checks": checks,
        "identity": identity,
        "mainnet_launched": False,
        "note": "Automated RC1 audit only — does not launch mainnet or claim VPS soak complete",
    }


def format_audit_report(report: dict) -> str:
    lines = [
        f"MHCOIN {report.get('release', 'RC1')} Pre-Launch Audit",
        f"software={report.get('software_version')} protocol={report.get('protocol_version')}",
        f"consensus_fingerprint={report.get('consensus_fingerprint')}",
        f"mainnet_launched={report.get('mainnet_launched')}",
        f"overall={'PASS' if report.get('ok') else 'FAIL'}",
        "",
    ]
    for c in report.get("checks", []):
        mark = "OK  " if c["ok"] else "FAIL"
        lines.append(f"  [{mark}] {c['name']}")
        if not c["ok"] and c.get("detail"):
            lines.append(f"         {c['detail']}")
    lines.append("")
    lines.append(report.get("note", ""))
    return "\n".join(lines)
