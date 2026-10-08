"""CLI pool miner client — talks JSON protocol, HASH256 shares locally."""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import time
from typing import Any

from mhcoin.blockchain.merkle import merkle_root
from mhcoin.consensus.difficulty import bits_to_target
from mhcoin.crypto.hashing import hash256
from mhcoin.mining.share import (
    EXTRANONCE2_SIZE,
    merkle_root_from_branch,
)

logger = logging.getLogger("mhcoin.pool.client")


class PoolClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        address: str,
        worker: str = "default",
    ):
        self.host = host
        self.port = int(port)
        self.address = address
        self.worker = worker or "default"
        self._sock: socket.socket | None = None
        self._rfile = None
        self._wfile = None
        self._id = 0
        self._stop = False
        self.hashrate = 0.0

    def request_stop(self, *_args) -> None:
        self._stop = True

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=30)
        self._sock.settimeout(60)
        self._rfile = self._sock.makefile("rb")
        self._wfile = self._sock.makefile("wb")

    def close(self) -> None:
        try:
            if self._rfile:
                self._rfile.close()
            if self._wfile:
                self._wfile.close()
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        assert self._wfile and self._rfile
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        self._wfile.write((json.dumps(msg) + "\n").encode("utf-8"))
        self._wfile.flush()
        line = self._rfile.readline()
        if not line:
            raise RuntimeError("pool connection closed")
        return json.loads(line.decode("utf-8"))

    def login(self) -> dict:
        return self._rpc(
            "login", {"address": self.address, "worker": self.worker}
        )

    def getjob(self) -> dict:
        res = self._rpc(
            "getjob", {"address": self.address, "worker": self.worker}
        )
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "getjob failed")
        return res["job"]

    def submit(
        self, job_id: str, extranonce2: bytes, nonce: int, ntime: int
    ) -> dict:
        return self._rpc(
            "submit",
            {
                "address": self.address,
                "worker": self.worker,
                "job_id": job_id,
                "extranonce2": extranonce2.hex(),
                "nonce": f"{nonce:08x}",
                "ntime": f"{ntime:08x}",
            },
        )

    def report_hashrate(self, hps: float) -> None:
        try:
            self._rpc(
                "hashrate",
                {
                    "address": self.address,
                    "worker": self.worker,
                    "hashrate": float(hps),
                },
            )
        except Exception:
            pass

    def mine_forever(self) -> None:
        import signal

        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        self.connect()
        login = self.login()
        if not login.get("ok"):
            raise RuntimeError(login.get("error") or "login failed")
        print(
            f"  pool login ok  worker={self.worker}  share_factor={login.get('share_factor')}",
            flush=True,
        )
        shares = 0
        blocks = 0
        while not self._stop:
            try:
                job = self.getjob()
            except Exception as e:
                print(f"  getjob error: {e}", flush=True)
                time.sleep(3)
                try:
                    self.close()
                    self.connect()
                    self.login()
                except Exception:
                    time.sleep(5)
                continue
            found = self._mine_job(job)
            if found is None:
                continue
            en2, nonce, ntime, hps = found
            self.hashrate = hps
            try:
                res = self.submit(job["job_id"], en2, nonce, ntime)
            except Exception as e:
                print(f"  submit error: {e}", flush=True)
                continue
            if res.get("ok"):
                shares += 1
                if res.get("block"):
                    blocks += 1
                    print(
                        f"  BLOCK height={res.get('height')} hash={res.get('hash')}",
                        flush=True,
                    )
                else:
                    print(
                        f"  share accepted  total={shares}  ~{hps/1000:.1f} kH/s",
                        flush=True,
                    )
                if shares % 5 == 0:
                    self.report_hashrate(hps)
            else:
                print(f"  share rejected: {res.get('error')}", flush=True)

    def _mine_job(self, job: dict[str, Any]) -> tuple[bytes, int, int, float] | None:
        """Search nonces until a share is found or job should refresh."""
        coinb1 = bytes.fromhex(job["coinb1"])
        coinb2 = bytes.fromhex(job["coinb2"])
        branches = [bytes.fromhex(x) for x in job.get("merkle_branches") or []]
        version = int(job["version"])
        prevhash = bytes.fromhex(job["prevhash"])
        nbits = int(job["nbits"], 16)
        ntime = int(job["ntime"], 16)
        share_factor = int(job.get("share_factor") or 1)
        en2 = secrets_en2()
        raw_cb = coinb1 + en2 + coinb2
        leaf = hash256(raw_cb)
        if branches:
            merkle = merkle_root_from_branch(leaf, branches, 0)
        else:
            merkle = merkle_root([leaf])

        header = bytearray(
            struct.pack("<I", version)
            + prevhash
            + merkle
            + struct.pack("<III", ntime, nbits, 0)
        )
        target = share_target_int(nbits, share_factor)
        t0 = time.perf_counter()
        hashes = 0
        # Refresh job after ~30s of searching.
        deadline = time.perf_counter() + 30.0
        nonce = 0
        while nonce <= 0xFFFFFFFF and not self._stop:
            if time.perf_counter() >= deadline:
                hps = hashes / max(1e-6, time.perf_counter() - t0)
                return None
            struct.pack_into("<I", header, 76, nonce)
            digest = hashlib.sha256(hashlib.sha256(header).digest()).digest()
            hashes += 1
            if int.from_bytes(digest, "little") <= target:
                hps = hashes / max(1e-6, time.perf_counter() - t0)
                return en2, nonce, ntime, hps
            nonce += 1
            if hashes & 0xFFFF == 0 and self._stop:
                break
        hps = hashes / max(1e-6, time.perf_counter() - t0)
        return None


def secrets_en2() -> bytes:
    import secrets

    return secrets.token_bytes(EXTRANONCE2_SIZE)


def share_target_int(nbits: int, share_factor: int) -> int:
    return bits_to_target(nbits) * max(1, int(share_factor))


def parse_pool_url(url: str) -> tuple[str, int]:
    """Parse host:port or stratum+tcp://host:port or json://host:port."""
    u = (url or "").strip()
    for prefix in ("stratum+tcp://", "stratum://", "json://", "tcp://"):
        if u.startswith(prefix):
            u = u[len(prefix) :]
            break
    if "://" in u:
        u = u.split("://", 1)[1]
    if "/" in u:
        u = u.split("/", 1)[0]
    if ":" in u:
        host, port_s = u.rsplit(":", 1)
        return host, int(port_s)
    return u, 3333
