# Bandit triage — 24 November 2026

Baseline: PR #236 head `92dc5ed`, merged as `deb6270`; full
[architecture audit](https://github.com/Aragova/telegram-membership-saas/actions/runs/45379550096),
artifact `vimi-quality-reports` → `quality-reports/bandit.json`.
Scope is `src/telegram_membership_saas` and `ops`, not tests.
Baseline: 0 HIGH, 1 MEDIUM, 15 LOW. No confirmed exploitable vulnerability
among these 16 findings from the reviewed paths. Do not infer this is a
penetration test or proof that the entire application is vulnerability-free.

| Rule / baseline location | Finding | Disposition |
|---|---|---|
| B105 ops/backup/session_broker/server.py:18 | Metadata token endpoint URL, not credential | Keep |
| B104 ops/backup/session_broker/server.py:140 | Cloud Run container listens on all interfaces behind TLS/IAM | Keep visible; verify private deployment |
| B311 customer_runtime.py:104 | Jitter for reconnect delay | Keep |
| B311 customer_supervisor.py:513 | Per-project retry jitter | Keep |
| B110 modules/bots/service.py:123 | Session close failure silently ignored | Add sanitized warning; preserve getMe result |
| B311 modules/bots/telegram_preflight_retry.py:48 | Rate-limit jitter | Keep |
| B311 modules/bots/telegram_preflight_retry.py:49 | Bounded full jitter for read-only requests | Keep |
| B106 modules/file_sales/handlers.py:1017 | Empty provider token for XTR invoices | Keep |
| B105 modules/owner_setup/handlers.py:10071 | FSM destination projects | Keep |
| B105 modules/owner_setup/handlers.py:10072 | FSM destination bots | Keep |
| B105 modules/platform_backup_monitor.py:45 | Metadata endpoint URL, not credential; HTTPX trust_env=False | Keep |
| B105 modules/platform_dashboard_handlers.py:1605 | Page token literal 0; owner check precedes callback dispatch | Keep |
| B110 modules/telegram_access/worker.py:122 | Failed stale/undelivered invite revocation is silent | Add sanitized warning; preserve DB reconciliation |
| B106 modules/telegram_commerce/handlers.py:871 | Empty provider token for XTR invoices | Keep |
| B101 telegram_runtime.py:351 | Project ID assertion after explicit required-project check | Keep; not an authorization boundary |
| B101 termination_signals.py:20 | asyncio.current_task assertion in task-driven process entrypoints | Keep; not an authorization boundary |

## Rationale and boundaries

- See SECURITY.md, ARCHITECTURE.md, AUTOMATIC_CUSTOMER_BOT_SUPERVISOR.md and
  TELEGRAM_RUNTIME_RESILIENCE.md. Randomness is scheduling only, not a
  credential or cryptographic nonce. Do not change retry deadlines.
- See STARS_SUBSCRIPTION_CHECKOUT.md: XTR invoices use an empty
  provider token; do not alter billing or Telegram invoice fields.
- See TELEGRAM_ACCESS.md and prepare_join_request_decision in
  modules/telegram_access/service.py. Pending tickets must match project,
  link, chat, user, expiry and desired access. A failed best-effort remote
  revoke does not replace DB authorization. Keep failure handling and
  reconciliation intact. Log only action UUID and exception class, never
  invite URL, exception string or traceback. Cancellation still propagates.
- Registration verifies getMe and encrypts credentials; close failures
  should be visible but must not replace successful verification or the
  sanitized getMe failure. Do not log tokens or exception bodies.
- See BACKUP_RECOVERY.md. B104 is not suppressed. On 24 November the
  deployed 00002-pmd revision returned 403 without auth, 200 for health
  with auth, 400 for invalid session; switched to 100% traffic, then VM
  encrypted backup completed successfully. Restore verification is
  separate. Metadata endpoint strings are not secrets.
- B101 checks are defensive invariants, not the only authorization
  protection. The central-access project ID is checked explicitly before
  runtime construction; the process entrypoints run the signal wrapper
  in an asyncio task. Do not modify lifecycle behavior for a scanner metric.
- No nosec annotations or analyzer exclusions are introduced. Expected
  remaining Bandit: 0 HIGH, 1 MEDIUM and 13 LOW; verify the actual CI report.
  Mypy, Radon and other findings outside the Bandit list are separate work.
