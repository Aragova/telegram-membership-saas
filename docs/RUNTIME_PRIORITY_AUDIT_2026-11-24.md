# Runtime priority audit — 2026-11-24

Base main: `98cf05b` (PR #238). Reviewed repository inventory (699 files,
204 src, 267 tests), full mypy artifact for PR #238, 233 PR metadata entries,
changed-file inventories of 18 related PRs, key runtime/lease commit histories,
runtime architecture documents and relevant current source/tests. This is a
scoped runtime review, not a line-by-line audit of every historical diff.

## Accepted

- Observe termination of enabled indefinite customer/central management
  background tasks; use existing per-project backoff on customer failure.
  Do not restart for ordinary handled loop errors or disabled tasks.
- Send independent shutdown cancellations before awaiting their drains.
  Preserve polling-before-sibling cleanup, shared lease mutex and lease
  release ordering. Keep shared connection fail-closed policy.
- Align health claim and SQL result types with shared and dedicated leases,
  type observed Telegram calls and explicit membership permissions,
  accept Future in the documented Awaitable cleanup contract.
- Keep safe command-menu failure diagnostics without traceback contents.

## Deliberately unchanged

- PR #24/#48/#49 lock isolation, RLS, bounded admission and capacity.
- PR #165–168/#201 phase-aware watchdog, Telegram retries and degraded state.
- PR #218/#221 removed channel and owner command behavior.
- PR #229 repeated cancellation barrier; PR #237/#238 payment/access contracts.
- No new jobs/services, financial changes, DB migrations, timeout tuning,
  pool sizing, shard changes, or mass type suppression.
- Legacy central access mode, operation-progress watchdogs and broader
  exception/logging audit remain outside this change. Production fault
  frequency and real 100-bot throughput have not been established.

Tests must confirm child termination, parent cancellation, complete sibling
drain and existing PostgreSQL isolation/100-lock and token-rotation cases.
