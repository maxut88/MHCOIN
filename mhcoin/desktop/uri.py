"""BIP21-style payment URIs for MHCOIN: ``mhcoin:<address>?amount=<mhc>``.

The explorer's receive QR still encodes a plain address (see
``mhcoin.explorer.decode.qr_svg`` callers) — Desktop prefers the richer
``mhcoin:`` URI for its own QR/copy links but must keep accepting plain
addresses pasted from the explorer or any other wallet.
"""

from __future__ import annotations

from urllib.parse import parse_qs

SCHEME = "mhcoin"


def build_payment_uri(address: str, amount_mhc: str | None = None) -> str:
    """``mhcoin:<address>`` with an optional ``?amount=`` query param."""
    address = (address or "").strip()
    if not address:
        return ""
    uri = f"{SCHEME}:{address}"
    amount_mhc = (amount_mhc or "").strip()
    if amount_mhc:
        uri += f"?amount={amount_mhc}"
    return uri


def parse_payment_uri(text: str) -> dict[str, str | None]:
    """Parse ``mhcoin:<address>[?amount=...]`` or accept a plain address.

    Always returns ``{"address": str | None, "amount": str | None}`` and never
    raises — callers decide what "not a valid address" means for them.
    """
    s = (text or "").strip()
    if not s:
        return {"address": None, "amount": None}
    prefix = f"{SCHEME}:"
    if s.lower().startswith(prefix):
        rest = s[len(prefix):]
        if rest.startswith("//"):  # tolerate mhcoin://addr too
            rest = rest[2:]
        if "?" in rest:
            addr, _, query = rest.partition("?")
            params = parse_qs(query)
            amount = (params.get("amount") or [None])[0]
        else:
            addr, amount = rest, None
        addr = addr.strip() or None
        return {"address": addr, "amount": amount}
    return {"address": s, "amount": None}
