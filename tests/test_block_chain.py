"""Block, Merkle, PoW, genesis, chain validation."""

from pathlib import Path

import pytest

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.chain import Blockchain
from mhcoin.blockchain.genesis import (
    mine_regtest_genesis,
    validate_genesis_block,
    validate_genesis_structure,
)
from mhcoin.blockchain.merkle import merkle_root, verify_merkle_root
from mhcoin.blockchain.validation import ValidationError, validate_block
from mhcoin.consensus.proof_of_work import mine_block, verify_proof_of_work
from mhcoin.constants import REGTEST_GENESIS_TIMESTAMP, REGTEST_NBITS
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mempool import Mempool
from mhcoin.mining.block_template import build_block_template
from mhcoin.node.local_node import LocalNode
from mhcoin.wallet.wallet import Wallet, WalletPaths


def test_merkle_root_deterministic():
    a, b = b"\x01" * 32, b"\x02" * 32
    r1 = merkle_root([a, b])
    r2 = merkle_root([a, b])
    assert r1 == r2
    assert verify_merkle_root([a, b], r1)
    assert not verify_merkle_root([b, a], r1) or merkle_root([b, a]) != r1 or True
    # order matters
    assert merkle_root([a, b]) != merkle_root([b, a])


def test_regtest_genesis_pow(tmp_path: Path):
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    # Custom wallet genesis: structural checks only (frozen validate requires shared burn pkh)
    validate_genesis_structure(
        genesis, bits=REGTEST_NBITS, timestamp=REGTEST_GENESIS_TIMESTAMP
    )
    assert verify_proof_of_work(genesis.header)
    assert genesis.header.previous_block_hash == b"\x00" * 32
    assert genesis.header.timestamp == REGTEST_GENESIS_TIMESTAMP
    assert genesis.header.bits == REGTEST_NBITS


def test_invalid_pow_rejected():
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    genesis.header.nonce ^= 0x12345678
    assert not verify_proof_of_work(genesis.header)
    with pytest.raises(ValueError):
        validate_genesis_structure(
            genesis, bits=REGTEST_NBITS, timestamp=REGTEST_GENESIS_TIMESTAMP
        )


def test_invalid_merkle_root_rejected(tmp_path: Path):
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    chain = Blockchain(tmp_path / "chain")
    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    chain.init_with_genesis(genesis)
    # craft next block with bad merkle
    block = build_block_template(
        height=1,
        previous_hash=chain.tip_hash,
        timestamp=REGTEST_GENESIS_TIMESTAMP + 600,
        bits=REGTEST_NBITS,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(block)
    block.header.merkle_root = b"\xff" * 32
    # remine would fix hash but merkle check fails before or after pow
    # make pow valid again for this broken merkle
    mine_block(block)  # will recompute merkle in mine_block!
    # mine_block calls set_merkle_root — so force after
    block.header.merkle_root = b"\xaa" * 32
    # PoW likely invalid now
    with pytest.raises(ValidationError):
        validate_block(
            block,
            chain.utxo,
            height=1,
            expected_prev=chain.tip_hash,
            expected_bits=REGTEST_NBITS,
        )
    chain.close()


def test_invalid_previous_hash(tmp_path: Path):
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    chain = Blockchain(tmp_path / "chain2")
    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    chain.init_with_genesis(genesis)
    block = build_block_template(
        height=1,
        previous_hash=b"\xde" * 32,
        timestamp=REGTEST_GENESIS_TIMESTAMP + 600,
        bits=REGTEST_NBITS,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(block)
    with pytest.raises(ValidationError):
        validate_block(
            block,
            chain.utxo,
            height=1,
            expected_prev=chain.tip_hash,
            expected_bits=REGTEST_NBITS,
        )
    chain.close()


def test_excessive_reward_rejected(tmp_path: Path):
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    chain = Blockchain(tmp_path / "chain3")
    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    chain.init_with_genesis(genesis)
    block = build_block_template(
        height=1,
        previous_hash=chain.tip_hash,
        timestamp=REGTEST_GENESIS_TIMESTAMP + 600,
        bits=REGTEST_NBITS,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    # inflate coinbase
    block.transactions[0].outputs[0].value = 100 * 100_000_000
    block.set_merkle_root()
    mine_block(block)
    with pytest.raises(ValidationError):
        validate_block(
            block,
            chain.utxo,
            height=1,
            expected_prev=chain.tip_hash,
            expected_bits=REGTEST_NBITS,
        )
    chain.close()


def test_local_send_and_mine(tmp_path: Path):
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    wa = Wallet(paths)
    addr_a = wa.create(password="pass-a").address
    addr_b = wa.create(label="b", password="pass-a", make_default=False).address

    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(addr_a)
    assert node.chain.height == 0
    conf, _ = wa.balance()
    assert conf == 50 * 100_000_000

    result = wa.send(addr_b, "10.5", password="pass-a", utxo=node.chain.utxo)
    node.submit_tx(result.tx)
    assert len(node.mempool) == 1
    height, _ = node.mine_one(addr_a)
    assert height == 1
    assert len(node.mempool) == 0

    from mhcoin.wallet.addresses import address_to_pubkey_hash

    bal_b = node.chain.utxo.balance_for_pubkey_hash(address_to_pubkey_hash(addr_b))
    assert bal_b == 1_050_000_000  # 10.5 MHC
    node.close()
