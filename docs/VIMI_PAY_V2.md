# ViMi Pay v2.0 — approved product and control-plane contract

Status: **approved by product owner on 2026-11-04**. This document supersedes every
earlier description of central-bot owner setup. Existing
[CONCEPT.md](CONCEPT.md), [ARCHITECTURE.md](ARCHITECTURE.md), and
[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) must conform to this contract.
UnionBot screenshots supplied by the product owner illustrate the intended
interaction pattern, **not** a request to copy source, text or visuals.

## 1. Two distinct bot surfaces, three distinct user roles

### Central `@vimipay_bot`: control plane
For an ordinary customer of the SaaS, central is **only** a bot connection
registry. It welcomes the owner, offers supported bot types, explains BotFather,
accepts a bot token in a private message and promptly deletes the message,
verifies Telegram identity, encrypts credentials, creates/links the internal
Project, displays status, allows safe removal/rotation/reconnection, and offers
a prominent **Open your bot** button. A BotInstance starts polling for owner
setup *immediately after registration*, not after checkout readiness.

Central must NOT contain channel/group setup, plan/pricing editing, payment
methods, subscribers, payment moderation, or customer checkout for ordinary
owners. Connection-level safety obligations for bot retirement remain accessible
in central, and must not be weakened or hide active subscriber obligations.

### Connected owner bot (e.g. `@rus_chan_prem_bot`): tenant plane
One physical Telegram bot hosts two **separate permission-checked modes**:

1. Verified tenant owner/authorized collaborator `/start` opens a polished
   administration menu: channels/groups; tariffs; payment methods; subscribers;
   payment requests; statistics; timezone; notifications; bot status; settings;
   support; **View as guest**.
2. Buyer `/start` opens only the public catalog, selectable currencies, plans
   and compatible methods, purchases, payment confirmation, active subscriptions
   and access.

Admins have a read-only guest preview that cannot generate fictitious access or
bypass payment. Owner payment reviews, Telegram receipt forwarding and buyer
notifications run in **that same bot**, not in central. The current trusted
domain core is reused (Project, Plan, PaymentRequest, Payment, Subscription,
EntitlementGrant, EffectiveAccess, Outbox, Worker, Telegram adapter).

### Platform-owner mode inside central
The **platform's own operator** sees a closed, server-authorized mode:
`👑 Platform` / `🤖 My bots`. The same account may be Platform Owner and
tenant owner and switch UI modes without granting extra tenant permissions.
Ordinary tenants cannot see or invoke platform mode. Prepare separate roles
Platform Owner / Platform Admin / Platform Support; assign none implicitly.
Platform superuser is **not** an automatic subscriber, channel administrator,
or payment reviewer in tenants.

## 2. Onboarding / E2E

Central welcome -> select bot type -> BotFather instructions -> securely verify
and connect token -> display username/connection status -> **Open owner bot** ->
admin menu appears even with zero resources -> connect and verify channel ->
configure payment currencies/methods -> create resource-linked duration plans
and prices -> publish after readiness -> buyer enters same bot -> changes
currency -> selects one of eligible offers -> selects compatible payment method ->
transfers externally -> sends Telegram proof to owner -> owner checks real bank
credit and approves/rejects -> atomic payment/subscription/grants -> safe
invitation -> join -> renewal/expiry/revoke. Bot detach and reattach to a new
project must work repeatedly without SQL or destroying the financial ledger.

Customer-facing sales are gated on project readiness, while admin setup
remains possible whenever the bot token has been verified. One running
Telegram poller/worker per BotInstance/project, no second worker from central.

## 3. Currency and purchase screen — reference UX

- **No primary currency**. Every PlanPrice is an independent amount+currency;
  preserve snapshots and immutable payment history.
- Buyer sees a compact **currency switcher** at the top of the offer screen.
  Changing currency immediately changes eligible plans/prices and compatible
  payment methods; never show incompatible payment options.
- Examples: RUB -> SBP / MIR / Russian bank; USD -> PayPal or other configured
  USD method; EUR -> eligible EUR methods; BYN -> configured BYN provider.
  Compatibility comes from stored PaymentMethodCurrency links, **not**
  hardcoded country/brand defaults. One method may support multiple currencies.
- Multiple durations (1 month / 3 months / lifetime) may be shown together for
  one channel; each is a distinct Plan linked to that resource. Calendar
  months are not hardcoded as 30 days.
- A missing compatible payment method must block publication but provide a
  **direct action** to add one, not a technical alert, and the draft/price
  must remain saved.
- Payment proof is delivered through Telegram to the appropriate channel
  owner; do not store, OCR, analyze or archive receipts.

## 4. Platform owner analytics — separately authorized control plane

Implementation and staging activation:
[PLATFORM_OWNER.md](PLATFORM_OWNER.md). The operator is selected by a
trusted numeric Telegram ID on the central bot, explicitly provisioned by a
privileged migration credential, and cross-project reports use only a fixed,
audited, authorization-gated aggregate database function. Bot registration,
personal bot administration, and platform analytics remain separate contexts.

## 4. Platform owner analytics — later independent workstream

Closed platform dashboard provides counts/trends for connected, operating,
stopped, deleted and newly attached bots; tenant accounts and activity; active
resources; drafts vs published plans; requests; owner-confirmed paid events by
currency; active/renewed/expired subscriptions; service health, retries and
access action failures. Per-bot drilldowns only through audited permissions.
Period filters (today / 7 / 30 days / monthly / yearly) and time zones.

**Two distinct financial concepts**:
1. `tenant_confirmed_gmv`: amounts of payments **reported approved by
   tenants** in each original currency; not independently bank-verified.
2. `platform_revenue`: ViMi Pay's own future subscription/feature/usage
   charges. Never relabel tenant GMV as platform revenue.

Do not sum unlike currencies without documented FX rate, source and timestamp.
Define append-only usage events before charging: connected bot, active bot time,
published plans, verified sales, modules, resource counts and admin seats. No
platform billing charges or fake revenue until an actual pricing product is
approved. Platform analytics must use tightly controlled read/report
interfaces, minimal personal data, strict audit and **never disable tenant RLS
for arbitrary user-interface queries**.

## 5. Security, lifecycle and quality rules

- Bot token never appears in git/logs; delete BotFather message before
  validation; encrypt at rest and rotate safely.
- Every callback and reply checks the owning BotInstance's project,
  Telegram identity, tenant roles, permission and state transition. A central
  selected-project preference must **never** alter which project a connected
  bot can administer. Owner setup flows must be isolated by bot context.
- Separate platform and tenant permissions, deny by default, no shared
  super-admin payment approval. Keep owner/service grants distinct from paid
  subscribers.
- Preserve multi-tenant RLS, advisory locks, idempotency, audit, outbox,
  effective-access reconciliation and real Telegram revocation; avoid
  temporary dual-pollers and destructive DB workarounds.
- Interface should look clean, compact and premium from the first release:
  restrained emojis, short human text, consistent 2-column Telegram buttons
  where suitable, clear status and progress, sane navigation, contextual help,
  no internal IDs, schemas or raw runtime errors in end-user messages.
  Telegram-native inline keyboards are the MVP; web/mini-app can follow.
- Do not confuse a verified token with checkout readiness; explicitly label
  `connected/setup required` and `ready for sales`.
- Development in **meaningful vertical slices**, not many micro-PRs and not
  enormous multi-hour changes. Critical correctness/security failures block
  merge; cosmetic issues may be scheduled later. Exact HEAD green CI,
  review, zero unresolved threads, mergeable and owner-authorized merge.
  Work with GitHub connector; no production changes before staging E2E.

## 6. Implementation stages (dependency order)

| Stage | Deliverable / acceptance |
| --- | --- |
| A. Documentation + bot routing | Central remains bot connector and status; token success links directly to owner bot; connected bot identifies tenant owner, presents premium admin menu and runs setup without a published tariff; buyers keep their catalog. Shared wizard state cannot bleed between central and connected bots. |
| B. Tenant onboarding polish | Channel verification, method/currency first, draft tariff, price, matching method, publish with actionable dependency navigation; subscriber/payment review lives in connected bot; no owner settings in central. |
| C. Buyer currency/catalog UX | Native currency switcher, grouped resource/duration plans, method compatibility and buyer review; end-to-end multi-currency test with one real owner bot. |
| D. Platform-owner mode | Server-authorized operator/tenant mode switching, aggregate metrics with period and per-bot status, stable audit and isolated report queries. |
| E. Metering and SaaS-ready foundation | Append-only usage events, distinctions between tenant GMV and platform revenue, reconciliation and observability; no charges until product approval. |
| F. Full staging acceptance | Delete/reconnect from zero, owner channel/payment/plan setup, buyer checkout, Telegram proof, admin bank approval, subscription, invitations, expiry/revoke and control-plane metrics. |

This file is the **authoritative behavioral contract** if older docs or
existing code disagree. Update old documents and tests alongside each stage.
