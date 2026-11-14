## 2026-11-14 — Phase-aware polling watchdog (#166)

The per-project supervisor now distinguishes startup from active polling.
The first actual getUpdates call starts the polling clock; preflight calls
and database checks cannot exhaust it. Startup has a separate 120-second
deadline. After polling begins, timeout is 90 seconds since the first
getUpdates call or latest successful response. This exceeds the aiogram
default 60-second session + 10-second long-poll request timeout, avoiding
premature worker termination. A missing next getUpdates caused by slow
sequential update handling is `polling_handler_stalled`; a still-pending
getUpdates is `polling_stalled`; real intercepted Telegram network and
conflict exceptions retain their specific existing codes. These appear in
the existing RU/EN Monitoring without exposing raw tokens or URLs.

No webhook deletion, extra polling process, migration, menu, financial
switch change or advisory-lock shortcut. Existing backoff and full
shutdown-before-lease-release are preserved.

---

## 2026-11-14 — Exact-stage diagnostics (PR #165)

The existing customer worker restart and polling threshold are unchanged.
The worker reports explicit `identity_*`, `webhook_*` and `polling_*` codes
rather than merging all of them under legacy `telegram_check_failed`.
Network/Telegram API errors are categorized by exception TYPE only. A
getUpdates failure does not immediately mark a bot dead: aiogram retains
its normal retries; if no successful poll is recorded for 45 seconds,
the supervisor retains the last safe reason, or `polling_stalled` if there
was no observed error. Safe reason codes appear in the existing owner
Monitoring dashboard and central alerts in RU/EN. Previous stored
`telegram_check_failed` rows remain readable.

No webhook is deleted automatically, no bot token is rotated, no new
supervisor or monitoring queue is started, and no database migration
is necessary. A confirmed Telegram network failure still needs network/
provider investigation; improved error labels alone do not repair connectivity.

---

# ViMi Pay — automatic customer bot runtime

This closes the infrastructure gap between owner onboarding and live buyer
access. No per-owner SSH, `.env`, SQL insert, project UUID, or Docker restart
is needed after the platform operator deploys the supervisor **once**.

## Owner journey

1. Open central ViMi Pay → create/select project.
2. Register one Telegram bot using BotFather (Fernet-encrypted).
3. Add the bot to the intended channel/group with invite + ban permissions;
   connect/verify that channel through central ViMi Pay.
4. Configure and publish a priced plan and compatible manual payment method.
5. The supervisor discovers every active registered bot automatically,
   validates Telegram identity, webhook and resource rights, and keeps the
   buyer-facing bot **and access-expiration/revocation worker** running.
   A published payable catalog is **not required to keep enforcement alive**.
6. `🤖 Мой бот` reads a project-scoped health record based on **successful
   Telegram getUpdates responses** (including empty polls) and a recent DB
   heartbeat. A buyer link appears only if the correct bot is actively
   polling **and** there is an actual priced checkout.
   A registered/idle bot is never advertised as ready for purchases.
7. Token rotation restarts only that bot; suspension/disabling the bot
   stops the worker. Withdrawing prices or payment methods hides buyer
   checkout and the public buyer link, **without suspending subscription
   expiry, revocation and access reconciliation**. Outages clear the live
   status after a bounded polling-response timeout.

## Safety design

- `public.list_customer_bot_targets()` is a narrow SECURITY DEFINER discovery
  API returning only `project_id`, `telegram_bot_id` and `credential_version`.
  It is **revoked from PUBLIC** and granted solely to `membership_runtime`.
  Existing RLS remains mandatory for all project-bound secrets and operations.
- `customer_bot_runtime_health` is a project-RLS table containing only
  liveness/state and safe error codes. No tokens or Telegram error URLs stored.
- PostgreSQL session-scoped advisory lock per project protects **both** new
  automatically managed workers and the old central/single-customer pollers.
  The supervisor uses a **separate SQLAlchemy NullPool lease engine**, so
  long-lived locks can never occupy the bounded operational pool reserved for
  payments, access reconciliation, subscriber expiry and liveness.
  A dedicated lease connection verifies ownership every 3 seconds.
- One eligible registered bot per project; multiple independent projects
  supported by a single supervisor process. Multiple supervisor replicas
  cannot simultaneously hold the same project's advisory lock.
- Worker runs buyer commerce + access handlers only; the central bot stays
  management-only. No project-wide token or buyer data is leaked to another
  owner's runtime. Polling is explicitly stopped and awaited before the
  per-project advisory lock is released, including for old central workers.
- Supervisor discovers active registrations every 10 seconds, provides
  45-second backoff after a genuine worker failure, and promptly restarts
  after planned token rotation/configuration changes. Telegram getUpdates
  responses must remain fresh; database heartbeat alone is insufficient.
- Active project admission is bounded by
  `TMS_CUSTOMER_SUPERVISOR_MAX_BOTS` (default 10; validated maximum 32).
  Excess registrations show `🟡 В очереди на запуск` and are admitted when
  capacity is freed. Queue updates cannot overwrite another replica's fresh
  running status. Size PostgreSQL connection limits and scale supervisor
  replicas before increasing the cap. An optional **four-shard topology for
  100 simultaneously active bots** is described in
  [SUPERVISOR_SCALE_100.md](SUPERVISOR_SCALE_100.md). It uses bounded
  deterministic placement and requires a separate explicit Compose overlay,
  database connection budget and real-bot load acceptance. A new **lightweight
  single-supervisor option** using one shared lease connection for up to 100
  low-traffic bots (without changing PostgreSQL server settings) is documented
  in [SUPERVISOR_LIGHT_100.md](SUPERVISOR_LIGHT_100.md). Both overlays are
  opt-in; the default single-supervisor staging configuration is unchanged.
- Docker `SIGTERM` and `SIGINT` are translated into a single root
  cancellation; a cancellation-resistant shutdown drains Telegram polling
  and access handlers before releasing advisory locks. All three Telegram
  containers have a 60-second Docker stop grace period. This is exercised
  by an actual SIGTERM subprocess regression test.
- Access, checkout and payment proofs use existing RLS and state transitions.
  Receipts are not stored; owner verifies actual bank settlement manually.

## One-time staging deployment after approved merge

1. Back up PostgreSQL and `.env`, verify exact merged HEAD; preserve the
   Fernet key and existing registration. Never print the key or token.
2. Apply migration `0015_customer_runtime_supervisor` through the already
   documented bind-mounted Alembic files (Dockerfile currently doesn't
   contain migrations).
3. Verify runtime DB role has execute permission on discovery function.
4. Before starting the supervisor, stop the old standalone `customer-bot`
   profile (if used). Set `TMS_TELEGRAM_CENTRAL_ACCESS_ENABLED=false` in
   the **central** bot environment and redeploy central bot. Verify the
   central `/start` remains owner-only; avoid advertising legacy buyer links.
5. Start `docker compose --profile supervisor up -d --build customer-supervisor`.
   The supervisor has the same Fernet key and database role but no global
   Telegram token or per-owner project ID. Set
   `TMS_CUSTOMER_SUPERVISOR_MAX_BOTS` only after reviewing the VM and
   PostgreSQL connection budget.
6. Observe startup of registered projects and the *actual* owner-menu
   heartbeat; verify Telegram `getMe`, permissions and catalog readiness.
7. Only after the full setup path works, conduct **one complete final owner
   + buyer E2E** as agreed, including the confirmation and expiration flow.
8. Rollback: stop supervisor **first**, then restore the central access flag
   only for the legacy project and restart central bot. Never run both
   access workers for the same project.

## Non-goals / constraints

- Multiple independent customer projects are supported, but a process has a
  deliberate maximum concurrency (10 by default). One **independently pooled**
  PostgreSQL lease session is held per running project. High-density
  deployment needs capacity planning, database connection budgeting and
  scale-out orchestration; this revision is not an unlimited-capacity worker.
- Multi-bot-per-**project** access routing remains explicitly unsupported
  until access actions identify BotInstance.
- Health is checked when owner reopens/refreshed the bot view; no background
  Telegram spam is sent to owners for transient errors.
- A registered bot does not imply ready or live. A valid priced catalog,
  matching payments, correct admin rights, a clear webhook, and fresh
  successful getUpdates responses are required **before publishing its
  buyer link**. The worker itself stays active without new sales to maintain
  existing subscriber access.
