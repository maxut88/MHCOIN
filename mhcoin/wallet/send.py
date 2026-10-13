"""Build and sign a payment transaction from wallet UTXOs."""

from __future__ import annotations

from dataclasses import dataclass

from mhcoin.constants import DEFAULT_ADDRESS_HRP, DEFAULT_FEE_SATOSHIS, SATOSHI_PER_COIN
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.signing import sign_input
from mhcoin.transaction.transaction import Transaction, fee_of
from mhcoin.utxo import UTXOEntry, UTXOSet
from mhcoin.wallet.addresses import address_to_pubkey_hash


@dataclass
class SendResult:
    tx: Transaction
    txid_hex: str
    amount: int
    fee: int
    change: int
    status: str = "local / unbroadcast"
    change_pubkey_hash: bytes | None = None


def parse_amount_mhc(text: str) -> int:
    """Parse decimal MHC string to satoshis (integer)."""
    s = text.strip()
    if not s or s.startswith("-"):
        raise ValueError("invalid amount")
    if "." in s:
        whole, frac = s.split(".", 1)
        if not whole:
            whole = "0"
        if not whole.isdigit() or not frac.isdigit():
            raise ValueError("invalid amount")
        if len(frac) > 8:
            raise ValueError("too many decimals")
        frac = frac.ljust(8, "0")
        return int(whole) * SATOSHI_PER_COIN + int(frac)
    if not s.isdigit():
        raise ValueError("invalid amount")
    return int(s) * SATOSHI_PER_COIN


def format_mhc(sats: int) -> str:
    neg = sats < 0
    sats = abs(sats)
    whole = sats // SATOSHI_PER_COIN
    frac = sats % SATOSHI_PER_COIN
    out = f"{whole}.{frac:08d}"
    return ("-" if neg else "") + out


def select_coins(utxos: list[UTXOEntry], need: int) -> list[UTXOEntry]:
    selected: list[UTXOEntry] = []
    total = 0
    for e in sorted(utxos, key=lambda x: x.output.value, reverse=True):
        selected.append(e)
        total += e.output.value
        if total >= need:
            return selected
    raise ValueError("insufficient funds")


def build_send_tx(
    *,
    utxo: UTXOSet,
    from_pubkey_hash: bytes,
    private_key: bytes,
    public_key: bytes,
    to_address: str,
    amount_sats: int,
    fee_sats: int = DEFAULT_FEE_SATOSHIS,
    hrp: str = DEFAULT_ADDRESS_HRP,
    exclude_outpoints: set[str] | None = None,
    change_pubkey_hash: bytes | None = None,
) -> SendResult:
    """Single-key send (legacy). Change defaults to the spending address."""
    return build_account_send_tx(
        utxo=utxo,
        keys_by_pkh={from_pubkey_hash: (private_key, public_key)},
        to_address=to_address,
        amount_sats=amount_sats,
        fee_sats=fee_sats,
        change_pubkey_hash=change_pubkey_hash or from_pubkey_hash,
        hrp=hrp,
        exclude_outpoints=exclude_outpoints,
    )


def build_account_send_tx(
    *,
    utxo: UTXOSet,
    keys_by_pkh: dict[bytes, tuple[bytes, bytes]],
    to_address: str,
    amount_sats: int,
    fee_sats: int = DEFAULT_FEE_SATOSHIS,
    change_pubkey_hash: bytes,
    hrp: str = DEFAULT_ADDRESS_HRP,
    exclude_outpoints: set[str] | None = None,
) -> SendResult:
    """Spend UTXOs from any account key; send change to ``change_pubkey_hash``."""
    if amount_sats <= 0:
        raise ValueError("amount must be positive")
    if fee_sats < 0:
        raise ValueError("fee must be non-negative")
    if not keys_by_pkh:
        raise ValueError("no signing keys")
    to_hash = address_to_pubkey_hash(to_address, hrp=hrp)
    available: list[UTXOEntry] = []
    for pkh in keys_by_pkh:
        available.extend(utxo.all_for_pubkey_hash(pkh))
    if exclude_outpoints:
        available = [e for e in available if e.outpoint.key() not in exclude_outpoints]
    need = amount_sats + fee_sats
    coins = select_coins(available, need)
    total_in = sum(c.output.value for c in coins)
    change = total_in - need
    if change < 0:
        raise ValueError("insufficient funds")

    inputs = [
        TxIn(prev_txid=c.outpoint.txid, prev_vout=c.outpoint.vout) for c in coins
    ]
    outputs = [TxOut.p2pkh(amount_sats, to_hash)]
    if change > 0:
        outputs.append(TxOut.p2pkh(change, change_pubkey_hash))

    tx = Transaction(inputs=inputs, outputs=outputs)
    for i, coin in enumerate(coins):
        try:
            pkh = coin.output.pubkey_hash()
        except Exception as e:
            raise ValueError("unsupported input script") from e
        pair = keys_by_pkh.get(pkh)
        if pair is None:
            raise ValueError("missing key for selected coin")
        priv, pub = pair
        sign_input(
            tx,
            i,
            priv,
            pub,
            coin.output.script_pubkey,
            coin.output.value,
        )

    input_values = [c.output.value for c in coins]
    fee = fee_of(tx, input_values)
    return SendResult(
        tx=tx,
        txid_hex=tx.txid_hex(),
        amount=amount_sats,
        fee=fee,
        change=change,
        status="local / unbroadcast",
        change_pubkey_hash=change_pubkey_hash if change > 0 else None,
    )
