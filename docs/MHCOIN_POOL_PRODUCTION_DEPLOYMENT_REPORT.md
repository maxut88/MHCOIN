# MHCOIN Mining Pool — Production Deployment Report

**Project:** MHCOIN (MHC)  
**Deployment UTC:** 2026-10-09T14:16:17Z → 2026-10-09T14:18:01Z  
**Local:** 2026-10-09 ~17:16–17:18 (UTC+3)  
**Production:** `/data/MHCOIN/`  
**Source worktree:** `/data/MHCOIN-worktrees/pool-launch-stress`  
**Operator mode:** Controlled Pool-only upgrade  

---

## Final deployment verdict

# DEPLOYED — WITH LIMITATIONS

Pool upgraded, SQLite migrated, financials preserved, Stratum rate limits live-verified. Mainnet Node never restarted. Private Pilot (≤10 external workers) **not** started — awaits separate owner approval.

---

## 1. Deployment date/time

| Event | Time (UTC) |
|-------|------------|
| Pre-check / backup start | 2026-10-09T14:16:17Z |
| Pool stopped | ~14:17Z (PID 1837683) |
| Modules installed | ~14:17Z |
| Pool started | ~14:17Z (PID **1898657**) |
| Post-verify complete | 2026-10-09T14:18:01Z |

---

## 2. Production Node PID before/after

| | PID | Status |
|--|-----|--------|
| Before | **235704** | `run_mainnet_node.py`, listen `:8333` |
| After | **235704** | **unchanged** (no restart) |

Evidence: process check before stop, after install, after start — same PID throughout.

---

## 3. Pool PID before/after

| | PID | Status |
|--|-----|--------|
| Before | **1837683** | `run_pool.py` on 3333/3334/8888 |
| After | **1898657** | `run_pool.py` on 3333/3334/8888 |

Stop via `rc1_ops/stop_pool.sh` (SIGTERM). Start via `rc1_ops/start_pool.sh`.

---

## 4. Git revision before/after

| | Value |
|--|--------|
| Commit (before & after) | `e1e4555d7fef0403297142449dc11062cb9232ea` |
| Message | Polish pool stats UI to match explorer and link from nav. |

Working tree after deploy (not committed):

- Modified: `mhcoin/pool/{config,db,engine,stratum}.py`
- Added: `mhcoin/pool/rate_limit.py`, `mhcoin/pool/monitoring.py`
- Also installed (content = worktree): `client.py`, `server.py`, `web.py` (match HEAD blobs; prior assume-unchanged / dirty state resolved to worktree)

---

## 5. Backup location and verification

**Directory:** `/data/MHCOIN-deploy-backups/deploy_20261009_171617/`

| Artifact | Purpose |
|----------|---------|
| `pool.sqlite.backup` | Consistent SQLite Backup API snapshot (pre-stop) |
| `pool.sqlite.post_stop.backup` | Post-stop consistent snapshot |
| `verify/restored.sqlite` | Restore drill copy |
| `code/pool/` | Full pre-deploy Pool code tree |
| `config/pool.env` | Environment (mode 600) |
| `git_revision_before.txt` | Git HEAD |
| `backup_verification.json` | Integrity + finance gate |

**Backup verification:**

| Check | Result |
|-------|--------|
| Integrity | `ok` |
| Restore integrity | `ok` |
| FK | empty |
| Dup `block_hash` | 0 |
| Balances / credits / payouts | match live pre-deploy |
| `BACKUP_OK` gate | **true** → deployment allowed |

---

## 6. SQLite migration result

Automatic migrate on new `PoolDB` open after Pool start.

| Object | Result |
|--------|--------|
| `pending_accepts` | **created** (0 rows) |
| `share_submissions` | **created** (0 rows) |
| `idx_rounds_block_hash_unique` | **active** |
| Integrity | `ok` |
| Foreign keys | `ok` (0 violations) |
| Historical rounds/credits/payouts/shares/balances | **preserved** (exact match to pre-deploy backup) |

---

## 7. Financial invariants before/after

All values in **integer satoshis (MHC minimal units)**.

| Metric | Before | After | Verdict |
|--------|--------|-------|---------|
| rounds | 112 | 112 | PASS |
| credits count / Σ | 112 / 549450024749 | identical | PASS |
| payouts count / Σ | 25 / 440397942142 | identical | PASS |
| balances immature | 99000005940 | identical | PASS |
| balances matured | 10052076667 | identical | PASS |
| balances paid | 440397942142 | identical | PASS |
| immature credits = bal.immature | yes | yes | PASS |
| matured credits = matured+paid | yes | yes | PASS |
| duplicate rounds/credits/payouts | 0 | 0 | PASS |
| Round 1 credits (historical −1 sat) | 4949999999 | **unchanged** | PASS (not “fixed”) |
| Fee config | 1% | 1% | PASS |
| Maturity | 20 | 20 | PASS |

Tip at verify: height **1196**, hash `1e53817b8520692ef31397ae93848faed2861b446738859a755037f6c9000000` (same as pre-deploy). No unexplained new financial rows.

---

## 8. Pending accepts status

| Item | Value |
|------|-------|
| Table exists | yes |
| Rows | 0 |
| Open (`intent`/`accepted_node`) | 0 |
| Double-credit risk observed | none |

Reconciliation path deployed with canonical gate (non-canonical → no credits; UNKNOWN → DEFER).

---

## 9. Stratum smoke test

| Test | Result |
|------|--------|
| `mining.subscribe` | PASS |
| `mining.authorize` (known address `.deploy`) | PASS (`result: true`) |
| Rate limit per IP (max 8) | PASS — connections 9–10 rejected: `too many connections from IP` |
| Listen log | `Stratum listen 0.0.0.0:3334 (max_conn=64)` |

Configured / default limits (match TZ):

| Parameter | Value |
|-----------|-------|
| Max connections | 64 |
| Max connections/IP | 8 |
| Requests/IP/sec | 40 |
| Submits/worker/sec | 30 |
| Max JSON message | 65536 |
| Read timeout | 120 s |
| Idle timeout | 300 s |

No new public exposure added; ports unchanged.

---

## 10. API health

| Check | Result |
|-------|--------|
| `GET http://127.0.0.1:8888/api/stats` | PASS |
| Tip height | 1196 |
| Tip hash | `1e53817b…000000` |
| `current_round` | 112 |
| `workers_active` | 0 |
| JSON pool `:3333` | listening |
| Sync on start | `Sync complete at height=1196`; handshake with `192.168.0.221:8333` |

---

## 11. Monitoring status

| Capability | Status |
|------------|--------|
| `mhcoin/pool/monitoring.py` installed | yes |
| Importable | yes |
| Hooked into Pool server loop / alert dispatcher | **no** (not claimed active) |
| Live metrics available now | `/api/stats` (tip, workers, hashrate, shares, blocks/rounds), Pool log, process uptime |
| Rate-limit rejects | observable via Stratum error responses / limiter counters when called |

---

## 12. Errors/warnings

| Item | Severity | Notes |
|------|----------|-------|
| Outbound connect failures to various peer addrs | info/warn | Normal probe noise; primary peer `192.168.0.221:8333` OK |
| Duplicate peer nonce close `176.38.3.168` | info | Expected when same node identity |
| Initial verify script `rate_limit_not_wired` | false positive | Looked in `server.py`; limiter is in `stratum.py`; live probe PASS |

No financial errors. No migration errors.

---

## 13. Rollback readiness

| Asset | Ready |
|-------|-------|
| Pre-deploy Pool code | `/data/MHCOIN-deploy-backups/deploy_20261009_171617/code/pool/` |
| Pre-deploy `pool.env` | `…/config/pool.env` |
| Pre-deploy SQLite backups | `pool.sqlite.backup`, `pool.sqlite.post_stop.backup` |
| Policy | Code rollback OK **keeping** new tables/indexes; **no** automatic DB restore |
| Auto-payouts | threshold still `10000000000` sats; if double-pay risk → raise/block manually |

Procedure: stop Pool only → restore code from backup → keep migrated schema → start Pool → verify finances. Mainnet Node untouched.

---

## 14. Remaining limitations

1. Soft-launch Phase 1 (≤10 external workers) **not authorized** by this deploy — separate approval required.  
2. Monitoring module present but **not** integrated into server alert loop.  
3. Deep reorg after payout: no auto-clawback (existing policy).  
4. Historical round-1 −1 sat fee dust left intentionally.  
5. Production git working tree dirty (pool modules) until operator commits.  
6. NAT: max 8 connections per IP may constrain large shared-IP farms — tune env if needed after pilot.

---

## 15. Soft-launch status

| Phase | Status |
|-------|--------|
| Phase 0 — Production verification | **DONE** (this report) |
| Phase 1 — Private pilot ≤10 workers / 24h | **NOT STARTED** — await owner permission |
| Phase 2 — ≤25 workers | blocked on Phase 1 |
| Phase 3 — Public launch | blocked on Phase 1–2 |

---

## Evidence index

```
/data/MHCOIN-deploy-backups/deploy_20261009_171617/
  backup_verification.json
  post_stop_db.json
  post_deploy_verification.json
  post_deploy_fails.json          # [] after corrected rate-limit proof
  module_hashes_before.json
  module_hashes_final.json
  pool_log_after_start.txt
  git_revision_before.txt
  git_status_after.txt
  pool.sqlite.backup
  pool.sqlite.post_stop.backup
  code/pool/
  config/pool.env
```

Pre-deploy audit: `docs/MHCOIN_POOL_PRE_DEPLOY_AUDIT.md`

---

## Confirmation

| Constraint | Honored |
|------------|---------|
| Mainnet Node not restarted | YES |
| Consensus / genesis / PoW / DAA / reward untouched | YES |
| No historical finance “fixes” | YES |
| No test payouts | YES |
| No automatic external miner invite / public launch | YES |
| No automatic old-DB restore | YES |

**STOP.** Awaiting separate owner permission for Private Pilot (Phase 1).
