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
Financial replay/checkout refactors need independent transactional
regressions; no changes to those five functions here. Audit heartbeat
freshness consistency separately (future timestamps are handled
differently by the UI and incident scanner); do not silently change
semantics during extraction. Broader menu decomposition, query profiling,
dead-code analysis and production load measurements are not completed.
