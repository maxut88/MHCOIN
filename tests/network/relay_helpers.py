"""Helpers for Stage 3–4 relay tests with spendable shared genesis."""

from __future__ import annotations

import time
from pathlib import Path

from mhcoin.blockchain.chain import Blockchain
from mhcoin.blockchain.genesis import mine_regtest_genesis
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import KeyPair
from mhcoin.mempool import Mempool
from mhcoin.mining.block_template import build_block_template
from mhcoin.network.p2p import P2PConfig, P2PManager
from mhcoin.network.relay import TxRelay
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.signing import sign_input
from mhcoin.transaction.transaction import Transaction
from tests.network.conftest import free_port


def seed_spendable_chain(data_dir: Path, kp: KeyPair):
    """Initialize chain with genesis paying to kp."""
    data_dir.mkdir(parents=True, exist_ok=True)
    pkh = hash160(kp.public_key_compressed)
    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    chain = Blockchain(data_dir, network="localnet")
    if chain.height < 0:
        chain.init_with_genesis(genesis)
    tip = chain.height
    chain.close()
    return genesis, tip


def make_payment_tx(
    genesis,
    kp: KeyPair,
    *,
    to_pkh: bytes | None = None,
    amount: int = 1_000_000,
    fee: int = 1000,
) -> Transaction:
    cb = genesis.transactions[0]
    subsidy = cb.outputs[0].value
    dest = to_pkh or hash160(kp.public_key_compressed)
    change = subsidy - amount - fee
    assert change >= 0
    tx = Transaction(
        inputs=[
            TxIn(
                prev_txid=cb.txid(),
                prev_vout=0,
                script_sig=b"",
                sequence=0xFFFFFFFF,
            )
        ],
        outputs=[
            TxOut.p2pkh(amount, dest),
            TxOut.p2pkh(change, hash160(kp.public_key_compressed)),
        ],
    )
    sign_input(
        tx,
        0,
        kp.private_key,
        kp.public_key_compressed,
        cb.outputs[0].script_pubkey,
        cb.outputs[0].value,
    )
    return tx


def make_relay_node(
    data_dir: Path, *, port: int | None = None, network: str = "localnet", max_peers: int = 32
):
    """P2PManager + TxRelay + SyncManager + Mempool + Blockchain seeded at data_dir."""
    from mhcoin.network.sync import SyncManager

    p = port if port is not None else free_port()
    chain = Blockchain(data_dir, network=network)
    mempool = Mempool(data_dir / "mempool.json")
    mgr = P2PManager(
        P2PConfig(
            network=network,
            host="127.0.0.1",
            port=p,
            max_peers=max_peers,
            connect_timeout=3.0,
            handshake_timeout=30.0,
            ping_interval=3600.0,
            start_height=max(chain.height, 0),
            data_dir=data_dir,
            outbound_target=0,
            reconnect_interval=3600.0,
        )
    )
    relay = TxRelay(
        mempool=mempool,
        get_utxo=lambda: chain.utxo,
        get_height=lambda: max(chain.height, 0),
        get_peers=mgr.handshaked_peers,
        chain=chain,
    )
    sync = SyncManager(
        chain=chain,
        relay=relay,
        get_our_height=lambda: max(chain.height, 0),
    )
    relay.attach_sync(sync)
    mgr.attach_relay(relay)
    return mgr, chain, mempool, relay


def mine_extending_block(
    chain: Blockchain, mempool: Mempool, kp: KeyPair, *, timestamp: int | None = None
):
    """Build + PoW a tip-extending block paying kp."""
    assert chain.tip_hash is not None
    height = chain.height + 1
    tip_hash = chain.tip_hash
    bits = chain.get_next_work_for_parent(tip_hash)
    mtp = chain.median_time_past_for_parent(tip_hash)
    if timestamp is None:
        timestamp = int(time.time())
    if timestamp <= mtp:
        timestamp = mtp + 1
    pkh = hash160(kp.public_key_compressed)
    block = build_block_template(
        height=height,
        previous_hash=tip_hash,
        timestamp=timestamp,
        bits=bits,
        mempool=mempool,
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(block)
    return block
