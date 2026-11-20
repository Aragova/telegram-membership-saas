# ViMi Pay — Telegram transient-outage resilience

## Incident motivating this PR (20 November 2026)

Production customer-supervisor logs showed repeated aiogram `getUpdates`
`Connection reset by peer`, request timeouts, `Bad Gateway` and
`Flood control exceeded ... Retry in 5 seconds`. The *container did not
restart* (`RestartCount=0, OOMKilled=false`, roughly 347 MiB in use).
Multiple individual bot workers nevertheless stopped, first with
`polling_api_failed`, then `identity_network_failed` during the next
startup `getMe`. The 90-second watchdog interrupted aiogram's own bounded
`getUpdates` retries and extended the incident by forcing every bot
through a new identity/webhook/resource handshake.

The VM trace **does not prove** whether the initial connection failure was
inside Telegram, at a provider gateway or along the VM's route to Telegram.
The fix is valuable in all three cases: do not amplify an upstream outage.

## Runtime invariants

- Every project keeps exactly one Telegram poller protected by the
  **unchanged PostgreSQL advisory lease**. No new polling processes,
  workers, queues or external dependencies.
- A completed `getUpdates`, including an empty response, proves running.
  **No answer** never proves running; a supervisor heartbeat is not a
  substitute for a successful Telegram response.
- Retryable `TelegramNetworkError`, `TelegramServerError` and
  `TelegramRetryAfter` retain the same aiogram dispatcher and lease.
  Health is `degraded` (yellow), with a token-safe fixed reason, until
  the next successful `getUpdates`. Then return to `running`.
- Retryable startup failures at getMe/webhook/channel-permissions are retried
  only as **read-only preflights** within the same worker with bounded
  increasing jitter. Verification is never bypassed, even if it takes hours.
  The startup watchdog resets after a successful preflight so command
  registration does not inherit an expired getMe recovery deadline.
- A revoked token, identity mismatch, active foreign webhook, refused channel
  permissions, polling conflict, lost lease or real stuck poller still fails
  closed, cleans up the dispatcher and releases the lease only AFTER cleanup.
- Genuine polling hang without ongoing error callbacks still terminates
  after the existing 90-second watchdog; it is not disguised as degraded.
  A repeated transient error is only exempt while a recent retry failure
  has been observed.
- Polling retry backoff uses aiogram's native bounded exponential mechanism
  (max 20s and nonzero jitter) to stagger reconnects across dozens of bots.
- No financial, entitlements, invoice, ledger or webhook routing changes.
  Telegram unavailability still means purchases/messages cannot be delivered;
  yellow is **not** a claim of buyer availability.

## Operator-facing monitoring

| State | Meaning | Buyer availability |
| --- | --- | --- |
| 🟢 Running | Recent successful getUpdates + current worker heartbeat | Confirmed polling |
| 🟡 Degraded | Supervisor alive, Telegram transport/rate limit retry in progress | Not confirmed |
| 🟠 Stale heartbeat | No fresh Supervisor record, even if last state was degraded | Not confirmed |
| 🔴 Error | Critical identity/permission/polling conflict, stuck worker or lost lease | Not confirmed |
| ⚪ Disabled | Intentional owner action | Disabled |

After a sustained degraded state, the existing single incident/outbox channel
sends an **amber** notice instead of a misleading critical red notice. The
existing verified recovery delay still applies; repeated scans do not create
new buttons or duplicate incident records.

## Acceptance and real VM verification

1. Run full CI including Alembic 0103, PostgreSQL tests, CodeQL and Sonar.
2. Inject a sustained 502, 429 and socket reset for **longer than 90 seconds**.
   Confirm `degraded`, **same process and lease**, no repeated getMe, then
   `running` within a successful poll after recovery. Test initial preflight
   outage longer than the startup window too.
3. Test an invalid token, revoked resource rights, competing poller and lost
   PostgreSQL lease. Confirm fail-closed and no duplicate update consumption.
4. After authorized staging deployment, use the existing monitor and read-only
   `docker logs` to compare `Customer worker stopped`, `polling_stalled`,
   `getMe` startup churn and `getUpdates` success over the incident. Check
   owner admin commands, purchase flow, receipts and access revocation using
   **real existing test bots**. No fake purchase buttons.
5. Observe actual end-to-end availability and VM pressure (CPU, memory,
   connections) before expanding beyond the current network; unit tests alone
   do not establish production reliability.

Migration 0103 is schema-only (adds a `degraded` value to an existing
state constraint). Its downgrade conservatively maps active degraded rows to
`error`, **never** to `running`. Execute rollback through the established
protected deployment process with supervisor version/schema coordinated.
