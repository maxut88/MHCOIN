"""Desktop local address book: address -> label, stored per network datadir.

Purely local UX (never broadcast / never synced): lets the user attach a
friendly name to a counterparty address so History / Send can show something
nicer than ``mhc1qf3…`` everywhere. Mirrors the simple JSON-file pattern used
by :mod:`mhcoin.desktop.prefs` (``wallet_transfers.json`` sibling file).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("mhcoin.desktop")

_FILENAME = "address_book.json"
MAX_LABEL_LEN = 64


def address_book_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / _FILENAME


def load_address_book(data_dir: Path | str) -> dict[str, str]:
    """Return ``{address: label}``. Missing/corrupt file -> empty dict (never raises)."""
    path = address_book_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("address book read failed", exc_info=True)
        return {}
    labels = raw.get("labels") if isinstance(raw, dict) else None
    if not isinstance(labels, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in labels.items()
        if isinstance(k, str) and isinstance(v, str) and k and v
    }


def save_address_book(data_dir: Path | str, labels: dict[str, str]) -> None:
    path = address_book_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"labels": labels}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def set_label(data_dir: Path | str, address: str, label: str) -> dict[str, str]:
    """Set (or clear, when ``label`` is empty) a label and return the full book."""
    address = (address or "").strip()
    label = (label or "").strip()
    if not address:
        raise ValueError("address required")
    if len(label) > MAX_LABEL_LEN:
        label = label[:MAX_LABEL_LEN]
    labels = load_address_book(data_dir)
    if label:
        labels[address] = label
    else:
        labels.pop(address, None)
    save_address_book(data_dir, labels)
    return labels


def remove_label(data_dir: Path | str, address: str) -> dict[str, str]:
    address = (address or "").strip()
    labels = load_address_book(data_dir)
    labels.pop(address, None)
    save_address_book(data_dir, labels)
    return labels
