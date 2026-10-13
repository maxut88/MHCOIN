"""Peer ban / misbehavior scoring (node policy)."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mhcoin.network.constants import (
    BAN_SCORE_THRESHOLD,
    DEFAULT_BAN_DURATION,
    MISBEHAVIOR_FLOOD,
    MISBEHAVIOR_HANDSHAKE,
    MISBEHAVIOR_INVALID_BLOCK,
    MISBEHAVIOR_INVALID_TX,
    MISBEHAVIOR_PROTOCOL,
)

logger = logging.getLogger("mhcoin.p2p")


@dataclass
class BanRecord:
    host: str
    until: float
    reason: str
    score: int


class BanManager:
    """
    Host-based bans (IP/hostname). Persisted as JSON.
    Not consensus — local node safety policy.

    Pipeline:
      detect abuse → misbehavior points → threshold → ban → on_ban(host) kick
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        threshold: int = BAN_SCORE_THRESHOLD,
        default_duration: float = DEFAULT_BAN_DURATION,
        on_ban: Callable[[str], None] | None = None,
    ):
        self.path = path
        self.threshold = threshold
        self.default_duration = default_duration
        self.on_ban = on_ban
        self._lock = threading.RLock()
        self._score: dict[str, int] = {}
        self._bans: dict[str, BanRecord] = {}
        self._protected: set[str] = set()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    def protect(self, host: str) -> None:
        """Never ban this host (configured seed / trusted peer)."""
        host = str(host).strip()
        if not host:
            return
        with self._lock:
            self._protected.add(host)
            self._bans.pop(host, None)
            self._score.pop(host, None)
            self._save()

    def _is_protected(self, host: str) -> bool:
        return str(host) in self._protected

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = time.time()
        for host, entry in (raw.get("bans") or {}).items():
            until = float(entry.get("until", 0))
            if until > now:
                self._bans[host] = BanRecord(
                    host=host,
                    until=until,
                    reason=str(entry.get("reason", "")),
                    score=int(entry.get("score", self.threshold)),
                )
        for host, score in (raw.get("scores") or {}).items():
            self._score[str(host)] = int(score)

    def _save(self) -> None:
        if self.path is None:
            return
        now = time.time()
        bans = {
            h: {"until": b.until, "reason": b.reason, "score": b.score}
            for h, b in self._bans.items()
            if b.until > now
        }
        data = {"bans": bans, "scores": dict(self._score)}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def is_banned(self, host: str) -> bool:
        with self._lock:
            if self._is_protected(host):
                return False
            rec = self._bans.get(host)
            if rec is None:
                return False
            if rec.until <= time.time():
                del self._bans[host]
                self._save()
                return False
            return True

    def ban(
        self,
        host: str,
        *,
        reason: str,
        duration: float | None = None,
        score: int | None = None,
    ) -> None:
        if self._is_protected(host):
            logger.info("Skip ban for protected seed host=%s reason=%s", host, reason)
            return
        until = time.time() + (self.default_duration if duration is None else duration)
        with self._lock:
            self._bans[host] = BanRecord(
                host=host,
                until=until,
                reason=reason,
                score=score if score is not None else self.threshold,
            )
            # Clear progressive score once banned (ban record carries score)
            self._score.pop(host, None)
            self._save()
        logger.info("Banned host=%s until=%.0f reason=%s", host, until, reason)
        if self.on_ban is not None:
            try:
                self.on_ban(host)
            except Exception:
                logger.debug("on_ban callback failed", exc_info=True)

    def unban(self, host: str) -> bool:
        with self._lock:
            removed = self._bans.pop(host, None) is not None
            self._score.pop(host, None)
            if removed:
                self._save()
            return removed

    def misbehavior(self, host: str, points: int, *, reason: str) -> bool:
        """
        Add misbehavior points. Returns True if the host is banned after this call.
        Scores persist across reconnects (no handshake score wipe).
        """
        if points <= 0:
            return False
        if self._is_protected(host):
            logger.debug("Ignore misbehavior for protected seed host=%s reason=%s", host, reason)
            return False
        newly_banned = False
        with self._lock:
            if self.is_banned(host):
                return True
            score = self._score.get(host, 0) + points
            self._score[host] = score
            self._save()
            if score >= self.threshold:
                newly_banned = True
        logger.info("Misbehavior host=%s +%s score=%s reason=%s", host, points, score, reason)
        if newly_banned:
            self.ban(host, reason=reason, score=score)
            return True
        return False

    def clear_score(self, host: str) -> None:
        """Manual / administrative only — not called on handshake (ban-bypass fix)."""
        with self._lock:
            self._score.pop(host, None)
            self._save()

    def list_bans(self) -> list[BanRecord]:
        with self._lock:
            now = time.time()
            expired = [h for h, b in self._bans.items() if b.until <= now]
            for h in expired:
                del self._bans[h]
            if expired:
                self._save()
            return list(self._bans.values())

    def score_of(self, host: str) -> int:
        with self._lock:
            return int(self._score.get(host, 0))


__all__ = [
    "BanManager",
    "BanRecord",
    "MISBEHAVIOR_FLOOD",
    "MISBEHAVIOR_HANDSHAKE",
    "MISBEHAVIOR_INVALID_BLOCK",
    "MISBEHAVIOR_INVALID_TX",
    "MISBEHAVIOR_PROTOCOL",
]
