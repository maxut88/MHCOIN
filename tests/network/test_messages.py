"""Stage 1 — message envelope smoke tests (alias coverage)."""

from mhcoin.network import encode_envelope, decode_envelope, magic_for_network
from mhcoin.network.messages import encode_message, try_decode_message


def test_public_package_exports():
    magic = magic_for_network("localnet")
    raw = encode_envelope(magic, "mempool", b"")
    msg = decode_envelope(magic, raw)
    assert msg.command == "mempool"
    assert msg.payload == b""


def test_messages_module_aliases_envelope():
    magic = magic_for_network("regtest")
    raw = encode_message(magic, "verack", b"")
    msg, rest = try_decode_message(magic, raw)
    assert rest == b""
    assert msg is not None
    assert msg.command == "verack"
