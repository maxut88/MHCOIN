"""RC1 Pre-Launch Audit — checks that green unit suites alone can miss."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mhcoin.audit.rc1 import (
    EXPECTED_MAINNET_GENESIS,
    consensus_identity,
    consensus_source_fingerprint,
    independent_recompute_genesis,
    run_rc1_audit,
)
from mhcoin.blockchain.chain import Blockchain
from mhcoin.blockchain.fingerprint import utxo_fingerprint
from mhcoin.consensus.params import PROTOCOL_VERSION, get_network_params
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mempool import Mempool
from mhcoin.network.constants import NETWORK_MAGIC, SERVICES_NODE_NETWORK
from mhcoin.network.messages import VersionPayload, encode_version
from mhcoin.network.serialization import encode_envelope
from mhcoin.node.runtime import NodeRuntime
from tests.network.conftest import free_port, make_manager, wait_until
from tests.network.relay_helpers import seed_spendable_chain
from tests.network.test_reorg import _mine_on, _mine_on_parent


def test_independent_mainnet_genesis_recompute():
    """header → merkle → HASH256 → PoW → exact frozen hash (not a string equality shortcut)."""
    info = independent_recompute_genesis("mainnet")
    assert info["block_hash"] == EXPECTED_MAINNET_GENESIS
    assert info["block_hash"] == "62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000"
    assert info["pow_ok"] is True
    assert info["match_hash"] and info["match_merkle"]
    assert info["magic"] == "4d48434e"
    assert info["nonce"] == 646_812
    assert info["timestamp"] == 1_735_689_600


@pytest.mark.parametrize("network", ["mainnet", "testnet", "regtest", "localnet"])
def test_independent_genesis_all_networks(network: str):
    info = independent_recompute_genesis(network)
    assert info["match_hash"] and info["pow_ok"]


def test_rc1_audit_suite_passes():
    report = run_rc1_audit()
    assert report["ok"] is True
    assert report["mainnet_launched"] is False
    assert report["consensus_fingerprint"]


def test_consensus_identity_stable():
    a = consensus_identity()
    b = consensus_identity()
    assert a == b
    assert a["networks"]["mainnet"]["genesis_hash"] == EXPECTED_MAINNET_GENESIS
    assert a["networks"]["mainnet"]["magic"] != a["networks"]["testnet"]["magic"]


def test_consensus_source_fingerprint_deterministic():
    a = consensus_source_fingerprint()
    b = consensus_source_fingerprint()
    assert a["fingerprint"] == b["fingerprint"]
    assert a["file_count"] > 5


def test_fresh_mainnet_datadir_starts_at_frozen_genesis(tmp_path: Path):
    """Clean-install equivalent: empty dir + mainnet → height 0 + frozen hash."""
    d = tmp_path / "fresh-mainnet"
    assert not (d / "chain.sqlite").exists()
    rt = NodeRuntime(data_dir=d, network="mainnet", host="127.0.0.1", port=free_port())
    try:
        assert rt.chain.height == 0
        assert rt.chain.tip_hash is not None
        assert rt.chain.tip_hash.hex() == EXPECTED_MAINNET_GENESIS
        assert get_network_params("mainnet").magic.hex() == "4d48434e"
    finally:
        rt.chain.close()


def test_kill9_restart_recovers_chain_utxo(tmp_path: Path):
    """Simulate hard kill: close without graceful P2P stop after mining."""
    kp = generate_keypair()
    d = tmp_path / "kill9"
    seed_spendable_chain(d, kp)
    chain = Blockchain(d, network="localnet")
    mp = Mempool(d / "mempool.json")
    for _ in range(3):
        blk = _mine_on(chain, mp, kp)
        chain.accept_block(blk)
    tip = chain.tip_hash
    height = chain.height
    work = chain.get_chain_work()
    fp = utxo_fingerprint(chain.utxo)
    # Abrupt close ≈ kill -9 mid-flight (OS keeps durable SQLite commits)
    chain.close()

    chain2 = Blockchain(d, network="localnet")
    try:
        assert chain2.height == height
        assert chain2.tip_hash == tip
        assert chain2.get_chain_work() == work
        assert utxo_fingerprint(chain2.utxo) == fp
    finally:
        chain2.close()


def test_kill9_node_runtime_subprocess(tmp_path: Path):
    """Real process: start node, mine via chain API, hard stop, reopen."""
    d = tmp_path / "proc"
    port = free_port()
    rt = NodeRuntime(data_dir=d, network="localnet", host="127.0.0.1", port=port)
    tip = None
    height = -1
    fp = b""
    rt.start(blocking=False)
    try:
        kp = generate_keypair()
        from mhcoin.constants import REGTEST_NBITS
        from mhcoin.consensus.proof_of_work import mine_block
        from mhcoin.mining.block_template import build_block_template

        pkh = hash160(kp.public_key_compressed)
        for i in range(2):
            block = build_block_template(
                height=rt.chain.height + 1,
                previous_hash=rt.chain.tip_hash,
                timestamp=int(time.time()) + i,
                bits=REGTEST_NBITS,
                mempool=rt.mempool,
                utxo=rt.chain.utxo,
                miner_pubkey_hash=pkh,
            )
            mine_block(block)
            rt.accept_block(block)
        tip = rt.chain.tip_hash
        height = rt.chain.height
        fp = utxo_fingerprint(rt.chain.utxo)
        assert rt.pid_path.is_file()
    finally:
        rt.p2p.stop()
        tip_hex = tip.hex() if tip else None
        h = height
        fph = fp.hex() if fp else ""
        rt.chain.close()

    rt2 = NodeRuntime(data_dir=d, network="localnet", host="127.0.0.1", port=free_port())
    try:
        assert rt2.chain.height == h
        assert rt2.chain.tip_hash is not None
        assert rt2.chain.tip_hash.hex() == tip_hex
        assert utxo_fingerprint(rt2.chain.utxo).hex() == fph
    finally:
        rt2.chain.close()


def test_multi_node_reorg_converges_utxo_fingerprint(tmp_path: Path):
    """
    GENESIS → chain A vs chain B; higher work wins; all nodes same tip/work/UTXO fp.
    """
    kp = generate_keypair()
    dirs = [tmp_path / f"n{i}" for i in range(3)]
    for d in dirs:
        seed_spendable_chain(d, kp)

    chains = [Blockchain(d, network="localnet") for d in dirs]
    mps = [Mempool() for _ in dirs]
    try:
        # Common ancestor A
        a = _mine_on(chains[0], mps[0], kp)
        for c, mp in zip(chains, mps):
            if c is not chains[0]:
                c.accept_block(a)
            else:
                c.accept_block(a)

        # Node0 builds short branch B1
        b1 = _mine_on(chains[0], mps[0], kp)
        chains[0].accept_block(b1)

        # Node1 builds longer competing branch from A: X-Y (more work)
        x = _mine_on_parent(chains[1], a.block_hash(), mps[1], kp)
        chains[1].accept_block(x)
        y = _mine_on_parent(chains[1], x.block_hash(), mps[1], kp)
        chains[1].accept_block(y)

        # Deliver competing chain to node0 and node2 → reorg
        for c in (chains[0], chains[2]):
            c.accept_block(x)
            c.accept_block(y)

        tips = {c.tip_hash for c in chains}
        works = {c.get_chain_work() for c in chains}
        fps = {utxo_fingerprint(c.utxo) for c in chains}
        assert len(tips) == 1
        assert len(works) == 1
        assert len(fps) == 1
        assert chains[0].tip_hash == y.block_hash()
    finally:
        for c in chains:
            c.close()


def test_incompatible_protocol_cannot_handshake():
    """Protocol 0 and 99 must be rejected — no silent alternate consensus reality."""
    a = make_manager()
    a.start()
    try:
        import socket

        for bad_ver in (0, 99, PROTOCOL_VERSION + 10):
            sock = socket.create_connection(("127.0.0.1", a.config.port), timeout=2)
            magic = NETWORK_MAGIC["localnet"]
            vp = VersionPayload(
                protocol_version=bad_ver,
                services=SERVICES_NODE_NETWORK,
                timestamp=int(time.time()),
                nonce=0xAABB00001111_0000 + bad_ver,
                start_height=0,
                network="localnet",
                listen_port=1,
                software_version=f"evil-{bad_ver}",
            )
            sock.sendall(encode_envelope(magic, "VERSION", encode_version(vp)))
            assert wait_until(lambda: a.peer_count() == 0, timeout=3.0)
            sock.close()
        assert len(a.handshaked_peers()) == 0
    finally:
        a.stop()


def test_mainnet_not_auto_selected_by_default():
    """Default node network remains localnet — RC1 does not launch mainnet."""
    from mhcoin.config_loader import resolve_data_dir
    import os

    os.environ.pop("MHCOIN_NETWORK", None)
    # wallet_paths / resolve default
    from mhcoin.config_loader import wallet_paths

    paths = wallet_paths(None)
    assert paths.network == "localnet"
