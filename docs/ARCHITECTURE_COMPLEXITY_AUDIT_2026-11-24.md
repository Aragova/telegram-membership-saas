# Architecture complexity audit: monitoring (first slice)

Baseline main fe68234; PR #239 quality artifact 14922094790.
Radon E/F: 7 production functions and 12 tests. Production candidates:
- Stars ledger replay (42/F), paid timeline rebuild (42/F)
- File checkout display (36/E), Crypto Pay settlement (35/E)
- Platform incident scan (35/E), Crypto Pay issuance (33/E)
- Monitoring diagnosis (31/E).

Inventory and review of the seven functions; detailed current code and
history for platform monitoring, including PRs #109/#110/#111/#165/#201.
This is not a full historical audit of every file and diff.

## This PR
Extract state-specific diagnosis helpers and isolate incident resolution
and unhealthy observation from scan orchestration. Keep query order,
transaction and advisory-lock scope, RLS, notification leases and
thresholds, all RU/EN text, callback routing and error codes. No schema,
payment, retry, deployment topology or user-facing changes. Preserve
existing query count: no claim of runtime throughput gain.

Add bilingual boundary and incident transition regression tests; existing
PostgreSQL integration tests remain intact. Do not split complex tests
merely for Radon grades.

## Deferred
## Final risk-based scope (owner direction)
Do not refactor five financial/checkout functions or large menus merely
to reduce Radon grades. No demonstrated throughput bottleneck justifies
changing financial transactions here. Twelve complex test functions are
not defects by themselves.

Two concrete follow-up risks are addressed in the same PR:
- UI heartbeat freshness now rejects observations more than 60 seconds in
  the future, consistent with the incident scanner. Invalid observations
  cannot confirm running, active retry or recent restart in detail/list.
- Stars and Crypto Pay periodic recovery no longer log raw exception
  tracebacks; keep project ID and exception type, retry and cancellation.

Bandit B104 MEDIUM in ops/backup/session_broker/server.py is the Cloud Run
container listener (0.0.0.0 is required; authentication is external Cloud
Run IAM). Do not change it to localhost or suppress the finding. Live IAM
configuration is not reverified by this source review. B105 metadata URL
is not a secret. Other type/style findings are not blanket-fixed or
suppressed; the full 740 mypy findings and remaining LOW Bandit items
were not individually reviewed in this pass. No production benchmark or
complete historical audit is claimed. The scoped changes do not alter
billing, payment fulfillment, permissions, menu structure, DB schema,
retry schedules or deployment topology.
