# Telegram Stars: bot balance and native channel subscriptions

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

## Opt-in actual channel balance and withdrawal data

The previously unavailable channel balance/withdrawal fields can now be
populated from a separately authorized user-MTProto reader via
`payments.getStarsRevenueStats`. The reader does NOT run by default,
cannot use a bot token, verifies the channel creator, and provides fresh
current balance, withdrawable balance, withdrawal flag and earliest next
withdrawal time when the Telegram RPC supplies one. A missing or unauthorized
reader leaves the same fail-closed "unavailable" display.

First rollout is an operator-owned test channel only, with one isolated,
preauthorized user account, NEVER a customer-user login inside ViMi Pay.
See [CHANNEL_STARS_REVENUE_BRIDGE.md](CHANNEL_STARS_REVENUE_BRIDGE.md).
Do not automatically convert this balance into 5% commission invoices.

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
  balance or its withdrawal eligibility. Both fields explicitly show
  **unavailable / not verified**, not a fabricated amount or success flag.
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
