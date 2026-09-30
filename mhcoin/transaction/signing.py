"""Transaction signing and signature verification."""

from __future__ import annotations

from mhcoin.constants import SCRIPT_P2PKH_VERSION
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.signatures import SignatureError, sign_digest, verify_digest
from mhcoin.transaction.serialize import Reader, write_bytes
from mhcoin.transaction.transaction import Transaction


def pack_script_sig(signature: bytes, public_key: bytes) -> bytes:
    return write_bytes(signature) + write_bytes(public_key)


def unpack_script_sig(script_sig: bytes) -> tuple[bytes, bytes]:
    r = Reader(script_sig)
    sig = r.bytes_blob()
    pubkey = r.bytes_blob()
    if r.remaining() != 0:
        raise ValueError("trailing bytes in scriptSig")
    return sig, pubkey


def sign_input(
    tx: Transaction,
    input_index: int,
    private_key: bytes,
    public_key: bytes,
    script_pubkey: bytes,
    value: int,
) -> None:
    digest = tx.sighash(input_index, script_pubkey, value=value)
    signature = sign_digest(private_key, digest)
    tx.inputs[input_index].script_sig = pack_script_sig(signature, public_key)


def verify_input(
    tx: Transaction,
    input_index: int,
    script_pubkey: bytes,
    value: int,
) -> bool:
    tin = tx.inputs[input_index]
    if tin.is_coinbase():
        return True
    try:
        signature, public_key = unpack_script_sig(tin.script_sig)
    except (ValueError, IndexError):
        return False
    if len(script_pubkey) != 21 or script_pubkey[0] != SCRIPT_P2PKH_VERSION:
        return False
    if hash160(public_key) != script_pubkey[1:]:
        return False
    digest = tx.sighash(input_index, script_pubkey, value=value)
    return verify_digest(public_key, digest, signature)


def verify_input_strict(
    tx: Transaction,
    input_index: int,
    script_pubkey: bytes,
    value: int,
) -> None:
    if not verify_input(tx, input_index, script_pubkey, value):
        raise SignatureError("input signature verification failed")
