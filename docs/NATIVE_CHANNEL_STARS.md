## P0 mixed-access hardening (current PR)

Before any automatic channel revoke, read the exact member through Bot API.
A ChatMemberMember subscription timestamp (even one already passed while
Telegram still reports membership) protects the member from removal.
Persist the revoke as pending with a bounded next-check time in the existing
access action queue, not as a failed or successful removal. Telegram owns
native renewal/expiry. An already kicked member is left untouched. A left
member in a channel with native-link history remains able to buy native access;
ordinary channels retain the existing permanent re-entry barrier.
For a normal member of a channel with historical ViMi paid links, removal
uses Telegram's non-banning unbanChatMember operation; ordinary channels
without this history and groups retain the existing ban behavior.

The customer dispatcher and the legacy access-enabled central dispatcher with a
configured project receive project-scoped channel membership updates. A native expiry
departure (old member with elapsed subscription timestamp, new left) is
persisted in the existing outbox before the project worker checks it live.
The worker retries failed reads with bounded backoff, honors FloodWait and rechecks observations
older than a local completion. Projection and access state are locked before
advancing either revision, so duplicate events do not supersede a corrective grant.
An already-member grant records the start of its Telegram membership read;
a newer departure fences old read-only completions (even when local state
was already revoked). Actual join approvals and revokes still record effects.
An administrator/creator or unsupported membership state fails permanently with
an explicit safe code instead of claiming a successful removal. Permission and
token errors retry at most once per hour; at 48 total attempts they move to a
slow recovery schedule (one attempt per day), retaining a distinct safe error
code and the original observation. They remain claimable automatically after
rights return; no manual database update or reset of claim generation is needed.
Only malformed payloads are terminal. The membership read has a 10-second
async timeout, after which the worker persists a retry and continues processing
other access work. No database lock is held across the read.
Warnings include the exception class and safe application location, never raw
Telegram error text or traceback contents. Unknown physical access is reconciled
like granted access when a fresh departure is confirmed. Only an existing scoped local entitlement can enqueue a new
safe join-request invitation. Explicit kicked status is not restored by
this handler. No grants or payments are fabricated from membership evidence.

This is deliberately not atomic with Telegram renewals or administrator
actions; Bot API has no conditional removal RPC. The live acceptance gate
must cover both races. Updates lost before the database commit (e.g. extended Telegram/DB outage),
legacy bans and native links created outside ViMi require separate verification; no automatic commission accrual.
Before rollback, stop the access workers and reconcile pending native departures
and deferred revokes; never let an older worker consume protected pending revokes.
The 0108 downgrade intentionally refuses to discard native departure evidence.
See the new experiment protocol below.

## Financial API experiment (not executed, not a release of billing)

Official references:
- https://core.telegram.org/method/payments.getStarsTransactions
- https://core.telegram.org/api/subscriptions
- https://core.telegram.org/bots/api#chatmembermember

Use an isolated test bot and private test channel with a known paid invite
and controlled genuine transaction. An MTProto bot authorization requires
a private API ID/hash and bot token; no personal user session. Use a
temporary local session only; resolve the exact channel input peer (including
access hash) through verified Telegram data. Request payments.getStarsTransactions
with that channel peer and paginate; record only whether permission was granted,
whether a known subscription charge/renewal/refund is identifiable, and whether
the operation can be uniquely attributed to the original bot/merchant. Do not
paste keys, sessions, raw transaction data or buyer IDs into GitHub or chat.
If access is denied or attribution is ambiguous, automatic commission stays off.
Do not introduce a production MTProto dependency until this is demonstrated.

### Operator-only diagnostic (PR 2 prerequisite)

`ops/audit/channel_stars_probe.py` implements the bounded read-only history
experiment, not an accounting integration. Run only from an isolated local
terminal for a disposable test bot/channel. Install Telethon 1.x separately
in a disposable environment after verifying its package provenance; do not
install it into the application environment or add it to production locks.
Invoke `python ops/audit/channel_stars_probe.py`. Enter test API credentials,
bot token, positive MTProto channel ID, verified access hash and known test
transaction IDs at hidden prompts. Never obtain the access hash by guessing
or joining a channel with a personal account. Leave renewal ID blank if not
yet available. A known sale ID must be independently observed in the test.

The script uses bot-only authorization and an in-memory session, disables
provider logging, checks bot and broadcast-channel identity, limits reads to
20 pages of 50 transactions and has bounded timeouts. It allows one retry
per request for Telegram DC migration or a transient RPC failure. It prints aggregate
counts and booleans only; no IDs, buyer data, payloads or raw provider errors.
Exit 0 means the history pagination ended; 2 means incomplete; 1 means the
probe failed. Disconnect is not session revocation: revoke the disposable
test bot token when the experiment is finished.

Official method eligibility does not establish permission for a specific
channel. A subscription-period flag, matching ID, or presence of a bot
payload is not evidence of merchant attribution. Refunds lacking a period
or matching ID will not be recognized; their absence is not proof that no
refunds occurred. The probe always reports commission_authorized=false,
even when a known sale/renewal/refund is visible. No ledger write, commission
activation, migration, runtime dependency or buyer/admin UI change.
Before production implementation, independently verify exact transaction
identity, amounts, signs, refund linkage, paid-link/bot attribution and
ownership history, and record a sanitized acceptance decision.

---

# Telegram Stars: bot balance and native channel subscriptions

## Recipient-first new Stars plan onboarding (2026-11-22)

When there are **no plans**, the connected-bot Stars screen offers one
**Create a plan** action. This now opens a distinct choice before asking for
an amount:

- **Bot balance:** one-month ViMi Pay price of 1–250000 whole Stars. ViMi Pay
  owns buyer checkout, entitlement delivery and purchase accounting.
- **Channel balance:** one 30-day Telegram-owned subscription price of
  1–10000 whole Stars. Only a linked private **channel** with bot invite
  permissions qualifies; Telegram owns renewals and membership.
  A supergroup is not an eligible native target.

The private channel and rights are verified again after price confirmation.
Creating a channel paid invite happens *before* an atomic, authorized write
that saves the native link and publishes the first plan together. On any
database failure the just-issued invite is revoked; failed revocation is
reported to the administrator with its URL. Double submissions cannot
publish another plan and must not convert a channel choice into a bot-invoice
price. The owner must confirm native activation; prior payments and active
legacy plans are never auto-migrated.

Merchant-facing cards do **not** discuss the internal platform commission
reconciliation gap. That constraint remains internal to this document and
the existing financial controls below. No invoice or commission accounting
changes are part of this UI onboarding PR.

This first-plan wizard uses Rich RU/EN cards with classic HTML fallback.
Each arrow advances one screen, which edits or retires the previous
bot-owned card. Text inputs have one bound **Back** button only; no
unrelated **Main menu** action is shown.

---


This is an **opt-in** checkout rail. No existing Telegram Stars invoice,
confirmed payment, subscriber grant, subscription history, or commission
ledger is migrated or modified.

## Two separate balances

- **Bot invoices** (`sendInvoice` in Stars) credit the connected bot.
  The connected bot's administrator can view **the actual live balance**
  on the **connected bot's main Admin dashboard**, fetched with the connected bot's
  `getMyStarBalance`. A true zero displays as **0**; Telegram errors show
  **unavailable**, not an invented zero. This is not the channel balance.
- **Native paid channel invite links** (`createChatSubscriptionInviteLink`)
  charge the buyer's Stars balance and credit the **channel's** Stars balance.
  The bot's balance does not increase. Telegram admits the user and renews
  the subscription every 30 days.

## Enable native channel Stars

1. In the connected bot, choose **Payment methods → ⭐ Telegram Stars**.
2. Use an existing **active, 1-month plan** with one associated private
   Telegram channel and a whole-Star price **1 to 10,000**.
3. Tap **⭐ To channel balance** and explicitly confirm. The bot must be
   a channel administrator with the `can_invite_users` permission.
4. The existing buyer catalog now shows **⭐ Pay and join channel** for
   this price. It opens the paid Telegram invitation, *not a bot invoice*.
5. Tap **↩️ Return Stars to bot** to stop publishing that link in ViMi Pay.
   The bot also tries to revoke the link in Telegram. If revocation fails,
   the owner is alerted with the exact link for manual revocation.

Native paid-link activation and buyer catalog publication are blocked when
ViMi Pay reports a SaaS commerce hold for the connected bot. Owner activation
uses the authenticated owner-only commerce RPC; buyer catalog uses the
narrow `platform_saas_native_buyer_sales_allowed(project,bot_id)` boolean
security-definer RPC. The buyer runtime has no owner actor GUC, and may never
call the privileged `platform_saas_project_commerce_state` RPC. A real
runtime-role CI test checks both project context isolation and executable grants. A bot also
cannot be retired while any project-owned native paid link remains active.
The owner must disable/revoke the link before deleting its bot or channel
or archiving the linked plan. A Telegram invite URL saved outside ViMi Pay
can still be used directly even if ViMi Pay later hides it on a billing hold:
this requires an independent external-link revocation policy before this
mode can be considered an enforced merchant-wide hold.

Existing bot invoices already paid and the payment history stay intact.
Prices with an active native link cannot be changed, retired, archived,
hard-deleted, or have their linked channel removed until the native mode is
disabled. If a historic plan is already archived, its original paid link can
still be disabled and revoked from the existing Stars payment-method screen. A Telegram-paid subscriber is **not** created as
a ViMi Pay-paid order, since the bot has no verified channel transaction
record. Do not equate Telegram member counts with confirmed payments.

## Deliberate limitations / release gate

**Channel Stars are NOT yet a verified source for ViMi Pay's 5% commission**:
the bot token's `getStarTransactions` exposes *bot* transactions, not a
channel's full financial ledger. These native channel sales therefore are
not reported as confirmed bot sales or silently charged to the administrator.
A financial reconciliation mechanism and its refund/dispute handling are
required before treating them as part of automatic monthly commission bills.

**Access coexistence needs isolated testing**: Telegram controls native paid
memberships, while ViMi Pay still manages legacy/manual/Crypto Pay paid
entitlements. A customer who buys native access and also has a legacy ViMi Pay
entitlement for the *same channel* may be affected by an old queued access
revocation. Run first on a **new separate channel without existing ViMi Pay
grants**. Do not automatically switch active legacy channels to native mode
until reconciliation and mixed-entitlement access semantics are covered.

Banned customers cannot see native paid invite URLs in ViMi Pay's buyer
catalog; their existing paid subscriptions and access-history controls remain
available. However, Telegram paid invitation URLs are **shareable**. A person
who receives a valid link from someone else may buy directly through Telegram
outside the bot's ban-checked checkout, and Bot API cannot reject that native
payment before it is charged. Do not rely on this rail for an irreversible
per-buyer purchase ban without a separate Telegram membership policy.

A native paid invitation can remain valid outside ViMi Pay if a user saved
the URL. Removing a plan from ViMi Pay does not necessarily cancel a
Telegram-owned subscription. To stop externally distributed invitations,
revoke them in Telegram as well; existing subscriptions are handled by
Telegram.

## Official API contracts

- https://core.telegram.org/bots/api#createchatsubscriptioninvitelink
- https://core.telegram.org/bots/api#getmystarbalance
- https://core.telegram.org/api/subscriptions

## Compact administrator dashboard

The connected bot's main panel uses Telegram Rich HTML tables rather than
large stacks of status lines:

- Row 1: Channel / group, Plan.
- Row 2: Payment method, Bot running.
- The redundant 'Connected bot' check is intentionally omitted inside the
  already connected bot; it remains on the central setup/readiness screen.
- Use compact ON / OFF in both Russian and English, with additional spacing
  around emoji. The bot-running status is based on the live supervisor
  heartbeat, independently of configured prices/payment methods.
- Channel / group ON also requires current Telegram administrator permissions
  to invite and restrict members (not just an active row in the database).
  Verification uses bounded live checks on up to eight associated resources;
  unavailable/unverified rights are not displayed as ON.
- Each readiness check preserves its existing factual source and existing
  setup action when incomplete.
- A single compact finance area shows the live connected-bot Stars balance
  via `getMyStarBalance`, channel owner identity from
  `getChatAdministrators` (only a `creator` record counts), and the
  currently configured Stars checkout recipient.
- Telegram Bot API does **not** expose the channel's actual Stars revenue
  balance or withdrawal eligibility. The dashboard intentionally omits those
  fields rather than showing unverified placeholders or fabricated values.
- At most three linked channels appear on the dashboard. The existing
  **Channels** button remains the route to the full catalog.
- Existing callback IDs and both RU/EN rendering remain functional.
  The old Stars balance line was removed from Statistics to avoid duplication.
- The dashboard only uses data visible to the authorized project manager.
  It never stores Telegram account credentials or guesses channel balances.

Native Rich does not support arbitrary browser CSS or image-mockup gradients:
the implemented design uses supported compact native Rich tables and regular
Telegram inline button styles.

- Bot name and timezone share a single line in the connected admin header.
  Verified Telegram channel creator usernames appear as clickable @username
  links; where no username is available, a user-ID mention can open a profile
  if Telegram privacy settings permit. Deleted owners are never shown as
  functional profile links.
- With native channel Stars, combine the channel owner and channel Stars
  recipient into one row. With bot/mixed/unconfigured Stars routes, keep
  beneficiary information distinct. This never implies the owner's
  personal Stars wallet and the channel wallet are the same balance.
