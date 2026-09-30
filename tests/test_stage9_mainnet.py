"""Stage 9 — production params, frozen genesis, network separation."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.blockchain.chain import Blockchain, ChainError
from mhcoin.blockchain.genesis import (
    get_network_genesis,
    mine_genesis,
    mine_regtest_genesis,
    validate_genesis_block,
    verify_frozen_genesis,
)
from mhcoin.consensus.params import (
    CONSENSUS_CRITICAL_PER_NETWORK,
    CONSENSUS_CRITICAL_SHARED,
    DECIMALS,
    HALVING_INTERVAL,
    INITIAL_BLOCK_REWARD_COINS,
    MAX_SUPPLY_COINS,
    MAX_SUPPORTED_PROTOCOL_VERSION,
    MIN_SUPPORTED_PROTOCOL_VERSION,
    POW_ALGORITHM,
    PROTOCOL_VERSION,
    SOFTWARE_VERSION,
    TARGET_BLOCK_TIME_SECONDS,
    get_network_params,
    list_networks,
)
from mhcoin.crypto.hashing import hash160
from mhcoin.network.constants import DEFAULT_P2P_PORT, NETWORK_MAGIC
from mhcoin.node.runtime import NodeRuntime


def test_production_consensus_table():
    assert MAX_SUPPLY_COINS == 21_000_000
    assert DECIMALS == 8
    assert INITIAL_BLOCK_REWARD_COINS == 50
    assert HALVING_INTERVAL == 210_000
    assert TARGET_BLOCK_TIME_SECONDS == 600
    assert POW_ALGORITHM == "HASH256"
    assert PROTOCOL_VERSION == 1
    assert MIN_SUPPORTED_PROTOCOL_VERSION == 1
    assert MAX_SUPPORTED_PROTOCOL_VERSION == 1
    assert SOFTWARE_VERSION == "0.3.0"
    assert "MAX_SUPPLY_COINS" in CONSENSUS_CRITICAL_SHARED
    assert "genesis_hash_hex" in CONSENSUS_CRITICAL_PER_NETWORK


@pytest.mark.parametrize("network", list_networks())
def test_frozen_genesis_verifies(network: str):
    info = verify_frozen_genesis(network)
    assert info["verified"] is True
    assert info["genesis_hash"] == get_network_params(network).genesis_hash_hex
    block = get_network_genesis(network)
    validate_genesis_block(block, network)


def test_mainnet_genesis_constants():
    p = get_network_params("mainnet")
    assert p.genesis_timestamp == 1_735_689_600
    assert p.genesis_bits == 0x1E0FFFFF
    assert p.genesis_nonce == 646_812
    assert p.magic == NETWORK_MAGIC["mainnet"] == bytes.fromhex("4D48434E")
    assert p.default_port == DEFAULT_P2P_PORT["mainnet"] == 8333
    assert p.allow_custom_genesis_bootstrap is False
    assert p.genesis_coinbase_extra == b"MHCOIN/mainnet genesis"


def test_networks_fully_separated():
    magics = {get_network_params(n).magic for n in list_networks()}
    assert len(magics) == 4
    assert get_network_params("mainnet").genesis_hash_hex != get_network_params(
        "testnet"
    ).genesis_hash_hex
    assert get_network_params("mainnet").default_port != get_network_params(
        "testnet"
    ).default_port


def test_node_installs_frozen_mainnet_genesis(tmp_path: Path):
    rt = NodeRuntime(
        data_dir=tmp_path / "mn",
        network="mainnet",
        host="127.0.0.1",
        port=18333,
    )
    try:
        assert rt.chain.height == 0
        assert rt.chain.tip_hash is not None
        assert rt.chain.tip_hash.hex() == get_network_params("mainnet").genesis_hash_hex
    finally:
        rt.chain.close()


def test_node_does_not_remine_genesis_on_restart(tmp_path: Path):
    d = tmp_path / "ln"
    rt = NodeRuntime(data_dir=d, network="localnet", host="127.0.0.1", port=18444)
    tip = rt.chain.tip_hash
    rt.chain.close()
    rt2 = NodeRuntime(data_dir=d, network="localnet", host="127.0.0.1", port=18444)
    try:
        assert rt2.chain.tip_hash == tip
        assert rt2.chain.height == 0
    finally:
        rt2.chain.close()


def test_wrong_network_datadir_rejected(tmp_path: Path):
    d = tmp_path / "cross"
    chain = Blockchain(d, network="localnet")
    chain.init_with_genesis(get_network_genesis("localnet"))
    chain.close()
    with pytest.raises(ChainError, match="genesis verification|network"):
        Blockchain(d, network="mainnet")


def test_mainnet_rejects_custom_genesis(tmp_path: Path):
    from mhcoin.crypto.keys import generate_keypair

    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    custom = mine_regtest_genesis(pubkey_hash=pkh)
    chain = Blockchain(tmp_path / "m", network="mainnet")
    with pytest.raises(ValueError):
        chain.init_with_genesis(custom)
    chain.close()


def test_regtest_allows_custom_genesis(tmp_path: Path):
    from mhcoin.crypto.keys import generate_keypair

    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    custom = mine_regtest_genesis(pubkey_hash=pkh)
    chain = Blockchain(tmp_path / "r", network="regtest")
    chain.init_with_genesis(custom)
    assert chain.height == 0
    assert chain.tip_hash == custom.block_hash()
    chain.close()
    # reload verifies structure
    chain2 = Blockchain(tmp_path / "r", network="regtest")
    assert chain2.tip_hash == custom.block_hash()
    chain2.close()


def test_get_network_genesis_never_mines():
    a = get_network_genesis("mainnet")
    b = get_network_genesis("mainnet")
    assert a.block_hash() == b.block_hash()
    assert a.header.nonce == get_network_params("mainnet").genesis_nonce


def test_mine_prepare_candidate_differs_until_frozen():
    """mine_genesis is tooling-only; frozen path must stay stable."""
    params = get_network_params("regtest")
    # Re-mining same template should reproduce frozen nonce/hash (deterministic search)
    block = mine_genesis(params)
    assert block.block_hash().hex() == params.genesis_hash_hex
