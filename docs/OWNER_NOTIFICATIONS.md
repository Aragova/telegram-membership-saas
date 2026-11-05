# ViMi Pay — owner notifications (approved 2026-11-05)

## Product boundary and rollout

All tenant notifications go from the **connected project bot**, NOT central
`@vimipay_bot`. Only the verified owner or explicitly eligible project
managers can see actionable purchase information. Existing banking/approval,
Payment, Subscription, entitlements, worker, outbox and Telegram reconciliation
must remain authoritative and unchanged.

The concept below was explicitly approved by the project owner. Implementation
must be delivered in reviewable stages: **invoice notification and opt-in
settings/delivery foundation first**; expiry/access/daily scheduling only when
covered by their own robust tests and operational checks. Do NOT advertise a
configured toggle as operational before its producer exists.

## Event catalogue

| Type | Trigger | Default | Delivery |
| --- | --- | --- | --- |
| invoice | Purchase creates a NEW payment request, even before transfer/proof | on | immediate; one message per request, `awaiting payment` |
| receipt | Buyer submits photo/document proof | on | reuse existing original actionable receipt copy and approve/reject controls; NO second receipt alert |
| decision | Admin approves/rejects request | on | reuse existing admin+buyer result (recipient language), no duplicate owner broadcast |
| expiry | Paid subscription ends within 7, 3 or 1 calendar day | on | one owner digest at 10:00 project time, distinct users, excludes renewed coverage |
| expired | Paid term ends without renewal | on | digest, avoid claiming Telegram revocation before worker confirms |
| access | Verified persistent access delivery/revocation error or resource permission loss | on | alert once per incident after retries, not every transient Telegram error |
| recovered | Access/resource incident resolved | on | once per incident |
| daily | Sales and subscription health summary at 10:00 project time | off | optional; monetary amounts by original currency, never FX conversion |

A buyer cancelling an unpaid invoice does not send a separate alert by default.
A new invoice is NOT a confirmed payment, sale, bank balance receipt, SaaS
revenue, or an invitation.

## Templates and localization

Russian example:
`🧾 Счёт сформирован\n👤 @buyer\n📦 Подписка: 1 месяц\n💳 500 RUB · Полуавтомат\n🕒 Ожидает оплату\n№ …`

English equivalent:
`🧾 Payment invoice created\n👤 @buyer\n📦 Subscription: 1 month\n💳 500 RUB · Manual payment\n🕒 Awaiting payment\nRef …`

Use a short displayed invoice reference derived from a persistent request
identifier, never a new financial ID that could conflict with the original.
Provide safe in-bot navigation to subscriber/request where authorized; callback
authorization MUST recheck the project, role and ownership.

Every recipient message uses that recipient's preferred/detected RU/EN language,
not the buyer's. Every label and error on owner notifications, subscriber
management and statistics must be checked in both languages. Dates follow
the project IANA timezone (default Europe/Moscow), UTC in storage.

## Settings

One compact 🔔 Notifications / 🔔 Уведомления section in the CONNECTED bot,
per project; editable by verified project owner only. Grouped controls for
invoice, receipt, decision, expiry, access and optional daily reports; daily
summary time 10:00 project local. Changing these settings must not change
financial truth, paid access, or the existing safe admin review workflow.
Until receipt review can be recovered without losing proof, mandatory
actionable review messages remain available to authorized reviewers.

## Operational invariants

- Do not send duplicate informational alerts for an already created request.
- Persist event/recipient keys transactionally, replay on retry with an
  idempotent queue. Telegram itself has no atomic exactly-once send; record
  delivery attempts and use bounded backoff. Never block checkout or access
  because Telegram delivery failed.
- Project RLS on all persistent settings/queue rows. No per-project global
  lookup with unbounded privileges. No raw bot tokens, bank credentials,
  receipts, stack traces or long internal UUIDs in messages.
- A customer who has renewed before the current term end must NOT be treated
  as expired. Manual and service grants are never commercial sales.
- Deadline digests are keyed by project-local date and rechecked against the
  latest paid Subscription state at dispatch. The digest must not claim that
  Telegram channel removal is complete just because a paid term ended.
- All settings/alerts use RU/EN keys; CI verifies roles, tenant isolation,
  creation vs. actual approval, idempotency, recovery and calendar boundaries.
- Migration required BEFORE deploying any new customer bot runtime that reads
  the notification persistence tables. No production promotion without
  staging acceptance. Keep `compose.light100.yaml` and never restart Postgres
  to merely deploy customer-bot code.

## PR #59 — paid subscription expiry digest (implementation)

- The existing connected-bot project runtime schedules one scan at project-
  local **10:00**, using IANA timezones and daylight-saving transitions.
  A cheap project-scoped timezone refresh once a minute detects live timezone
  edits and invalidates the cached 10:00 UTC wake-up. Startup after 10:00
  runs today's digest (no backdated replay flood).
- Classifies live paid Subscription end dates into 7 / 3 / 1 **calendar days**
  ahead, and terms ending within the 24 hours before 10:00. An expired term
  is not automatically a revoked Telegram membership. Before classification,
  the worker excludes a customer's old plan when another active **paid** plan
  (or combination of plans) covers **all the old plan's channels** past its
  end date, including lifetime coverage. Partial replacement is not enough.
- Each category deduplicates customers; manual grants and pending/unconfirmed
  invoices never become paid deadlines. Lifetime paid terms are excluded.
- A single owner-only digest per project and local date uses the existing
  `owner_notification_deliveries` table with `kind='expiry'` and its
  project/day/recipient uniqueness key. No migrations or new services.
- The queued record contains **only the date and timezone**, never a stale
  list of subscribers. At dispatch the worker rechecks the owner's `expiry`
  setting, owner identity, current project timezone, local date and paid-term
  SQL state; renewed subscribers disappear. Stale or now-empty messages are
  silently handled as completed.
- UI adds a RU/EN `⏳ Subscription expiry` toggle to the existing
  **⚙️ Settings → 🔔 Notifications** connected-owner screen. Default ON;
  owner only, no central-bot setting.
- Message contains a compact summary grouped by time to expiry and a
  link to the authorized **👥 Subscribers** directory. Telegram message
  cap enforced by limiting displayed customer details; all totals remain
  based on real rows.
- Buyer notifications, daily revenue reports, and access incidents/recovery
  are NOT part of PR #59. Existing approvals, subscriptions and access
  decisions are unchanged. Maintain staging-first deployment and
  `compose.light100.yaml`.

## PR #60 — confirmed access-action incidents and recovery

- Tenant-scoped connected project bot only; central bot unchanged.
- Warning on `AccessAction.failed_permanent` immediately, or a retryable
  failure after >=3 failed attempts **AND an additional 10-minute observation
  period**. Observation begins when the scanner first detects the third failed
  attempt, stored as `owner_notification_deliveries.available_at`. Never
  infer failure duration from the action's original queue creation time.
  Do not warn for transient retries or superseded desired revisions.
- Scan once per minute inside the existing project worker, bounded by 100
  previously *unhandled, current* failed actions from the past 7 days. SQL
  excludes already queued/delivered incidents **before LIMIT** to prevent
  long-running tenant projects from starving older failures. The delivery
  queue uses existing `owner_notification_deliveries` with `kind='access'`
  and the canonical action UUID string as its stable event key. One warning
  per actual action/owner/project.
- The message shows the customer, resource and grant/revoke operation,
  not the Telegram error string, traceback or bot token. Buttons navigate
  through the existing, owner-checked subscriber callback.
- Recheck tenant/project owner, project status, access preferences and current
  action+desired/actual state immediately BEFORE sending a warning. A resolved
  or superseded error must be silently suppressed.
- A recovery is created ONLY if the initial alert was confirmed sent to
  Telegram (not silently skipped), and a **same-direction successful access
  action** for the original customer/channel was persisted *after* that
  alert, and desired+actual states both agree with the original operation.
  Subscription expiry/cancellation or plan changes alone are NOT evidence
  that a failed grant or revocation recovered. Recovery candidates are also
  prefiltered in SQL before LIMIT. No recovery spam; one event per incident.
- Owner can mute/unmute both warning and recovery in the existing bilingual
  `⚙️ Settings → 🔔 Notifications` screen. Defaults ON per project.
- No schema migrations, payment/approval/access state mutations, new services
  or consumer messages. Continue to use `compose.light100.yaml` at staging.
- Scope boundary: unavailable bot pollers or missing channel admin privileges
  that prevent the bot from starting are a separate operations/health-alert
  path; do not claim this PR can send from a bot that cannot start.
