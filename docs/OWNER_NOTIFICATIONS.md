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
  latest Subscription and access state at dispatch.
- All settings/alerts use RU/EN keys; CI verifies roles, tenant isolation,
  creation vs. actual approval, idempotency, recovery and calendar boundaries.
- Migration required BEFORE deploying any new customer bot runtime that reads
  the notification persistence tables. No production promotion without
  staging acceptance. Keep `compose.light100.yaml` and never restart Postgres
  to merely deploy customer-bot code.
