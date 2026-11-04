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
  A dedicated connection retains the lock; 3-second verification detects
  connection-loss and cancels polling if another process owns it.
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
   Telegram token or per-owner project ID.
6. Observe startup of registered projects and the *actual* owner-menu
   heartbeat; verify Telegram `getMe`, permissions and catalog readiness.
7. Only after the full setup path works, conduct **one complete final owner
   + buyer E2E** as agreed, including the confirmation and expiration flow.
8. Rollback: stop supervisor **first**, then restore the central access flag
   only for the legacy project and restart central bot. Never run both
   access workers for the same project.

## Non-goals / constraints

- Running any number of customer projects is architecturally supported, but
  this single-process version holds one PostgreSQL connection per running
  project for advisory locking. Capacity planning/pooling must be added before
  high-density SaaS deployment.
- Multi-bot-per-**project** access routing remains explicitly unsupported
  until access actions identify BotInstance.
- Health is checked when owner reopens/refreshed the bot view; no background
  Telegram spam is sent to owners for transient errors.
- A registered bot does not imply ready or live. A valid priced catalog,
  matching payments, correct admin rights, a clear webhook, and fresh
  successful getUpdates responses are required **before publishing its
  buyer link**. The worker itself stays active without new sales to maintain
  existing subscriber access.
