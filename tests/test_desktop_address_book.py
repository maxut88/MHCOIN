"""Address book (local labels) + mhcoin: payment URI parsing."""

from __future__ import annotations

from pathlib import Path

from mhcoin.desktop.address_book import load_address_book, remove_label, set_label
from mhcoin.desktop.controller import CoreController
from mhcoin.desktop.uri import build_payment_uri, parse_payment_uri


def test_address_book_set_list_remove(tmp_path: Path):
    addr = "mhc1qexampleaddressxxxxxxxxxxxxxxxxxxxx"
    assert load_address_book(tmp_path) == {}

    labels = set_label(tmp_path, addr, "Alice")
    assert labels == {addr: "Alice"}
    assert load_address_book(tmp_path) == {addr: "Alice"}

    # Re-labeling overwrites rather than duplicating.
    labels = set_label(tmp_path, addr, "Alice (exchange)")
    assert labels[addr] == "Alice (exchange)"

    # Empty label clears the entry (same as remove).
    labels = set_label(tmp_path, addr, "   ")
    assert addr not in labels

    set_label(tmp_path, addr, "Bob")
    labels = remove_label(tmp_path, addr)
    assert labels == {}
    assert load_address_book(tmp_path) == {}


def test_address_book_rejects_empty_address(tmp_path: Path):
    try:
        set_label(tmp_path, "", "Nobody")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for empty address")


def test_address_book_survives_corrupt_file(tmp_path: Path):
    path = tmp_path / "address_book.json"
    path.write_text("not json {{{", encoding="utf-8")
    assert load_address_book(tmp_path) == {}


def test_controller_address_book_validates_address(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    ctrl = CoreController(network="localnet", data_dir=tmp_path)
    addr = ctrl.create_wallet("book-pass")

    ctrl.set_address_label(addr, "My other wallet")
    assert ctrl.address_book() == {addr: "My other wallet"}

    try:
        ctrl.set_address_label("not-an-address", "x")
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected invalid-address error")

    ctrl.remove_address_label(addr)
    assert ctrl.address_book() == {}
    ctrl.shutdown()


def test_build_and_parse_payment_uri_roundtrip():
    addr = "mhc1qexampleaddressxxxxxxxxxxxxxxxxxxxx"
    uri = build_payment_uri(addr, "1.5")
    assert uri == f"mhcoin:{addr}?amount=1.5"

    parsed = parse_payment_uri(uri)
    assert parsed == {"address": addr, "amount": "1.5"}

    assert build_payment_uri(addr) == f"mhcoin:{addr}"
    assert parse_payment_uri(f"mhcoin:{addr}") == {"address": addr, "amount": None}


def test_parse_payment_uri_accepts_plain_address():
    addr = "mhc1qexampleaddressxxxxxxxxxxxxxxxxxxxx"
    # Explorer QR / older clients hand Desktop a bare address — must still work.
    assert parse_payment_uri(addr) == {"address": addr, "amount": None}
    assert parse_payment_uri("") == {"address": None, "amount": None}
    assert parse_payment_uri("   ") == {"address": None, "amount": None}


def test_parse_payment_uri_case_and_double_slash():
    addr = "mhc1qexampleaddressxxxxxxxxxxxxxxxxxxxx"
    assert parse_payment_uri(f"MHCOIN:{addr}") == {"address": addr, "amount": None}
    assert parse_payment_uri(f"mhcoin://{addr}?amount=2") == {
        "address": addr,
        "amount": "2",
    }


def test_controller_receive_qr_builds_uri_and_svg(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    ctrl = CoreController(network="localnet", data_dir=tmp_path)
    addr = ctrl.create_wallet("qr-pass")

    payload = ctrl.receive_qr(addr, "3.25")
    assert payload["address"] == addr
    assert payload["uri"] == f"mhcoin:{addr}?amount=3.25"
    assert "<svg" in payload["svg"]

    # No address given -> falls back to the active wallet's address.
    payload2 = ctrl.receive_qr()
    assert payload2["address"] == addr
    ctrl.shutdown()


def test_controller_send_accepts_payment_uri(tmp_path: Path, monkeypatch):
    from mhcoin.mining.miner import SoloMiner

    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    ctrl = CoreController(network="localnet", data_dir=tmp_path)
    addr = ctrl.create_wallet("send-uri-pass")
    ctrl.shutdown()

    SoloMiner(data_dir=tmp_path, network="localnet", hrp="mhc", address=addr).run(max_blocks=1)

    ctrl = CoreController(network="localnet", data_dir=tmp_path)
    from mhcoin.wallet.wallet import Wallet

    dest = Wallet(ctrl.paths, password="send-uri-pass").create(
        label="dest", password="send-uri-pass", make_default=False
    ).address

    uri = f"mhcoin:{dest}?amount=5"
    # Amount comes from the URI itself (empty amount arg).
    txid = ctrl.send(uri, "", "send-uri-pass")
    assert len(txid) == 64
    ctrl.shutdown()
