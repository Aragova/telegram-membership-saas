# Native Stars subscriptions · ViMi Pay PR #130

This is the first fully automatic subscription payment rail. It reuses
the EXISTING Subscription row and EntitlementGrant + ACCESS_RECALCULATE
outbox used by manual payments, rather than a second subscriber database.
It does not affect existing manual payment methods, pending bank receipts,
customer access or platform-owner settlement.

## Connected bot buyer journey (RU/EN)

1. Merchant opens a tariff in the connected admin bot and adds **⭐ XTR**
   as one of its normal immutable price versions. Stars require a positive
   integer (up to 250000); no bank method, API key or invoice button is added
   to the merchant payment-method settings. Draft plans with only Stars can
   be published.
2. Buyer opens the original /plans catalog, selects XTR and the normal
   tariff. Existing Rich/mobile presentation is reused.
3. Buyer selects **⭐ Telegram Stars**. App creates an isolated native
   `subscription_star_orders` pending record with a historical plan name,
   duration, XTR amount, buyer and bot. Sends single-use XTR invoice,
   `provider_token=""`, no 30-day recurring subscription_period: supports
   existing day/week/month/year/lifetime finite or perpetual tariff lengths.
4. Bot API pre_checkout is verified for actual buyer, bot, project, exact
   Stars amount, pending state, expiry, active plan and channel. Reject
   invalid, old or changed quote before charging.
5. ONLY authenticated Telegram `successful_payment` converts the matching
   order to `paid`, then creates/renews the same Subscription and inserts
   `stars_payment` EntitlementGrant and outbox events IN ONE database
   transaction. Duplicate Telegram events cannot extend access twice.
   A retry after a DB outage is recovered using the bot-authenticated
   `getStarTransactions` API via **/starsrecover**. User should NEVER pay
   again solely because a processing message was lost.
6. The existing customer account /subscriptions and access worker show/
   deliver the resulting paid membership. Plan name/seller price changes
   after Telegram has already charged do NOT strand the buyer.

All Stars in this PR are **one-time invoices**; there is no hidden recurring
subscription. Recurring Telegram Stars subscriptions are constrained to
30 days and require separate cancellation/reversal policies before activation.

## Accounting and controls

Migration `0057_subscription_stars` adds the native order source,
XTR currency with precision 0, nullable manual Payment reference on
Subscription with an alternative paid Stars order reference, new grant
source `stars_payment`, and appends native Stars subscriptions to the
EXISTING immutable official-rate 0055 snapshot journal. USD reward and
5% are calculated at 0.013 USD/Star only for verified payments, and only
after the prelaunch cutoff; no retrobilling, no new SaaS invoices.

Commercial holds use the same 0046 owner/bot exemption validation as other
purchases. Do NOT turn on commercial billing, holds or quotas in this PR.
Platform financial settlement remains disabled. For Stars subscriptions
the bot also verifies official `refunded_payment` notifications against
the exact original buyer/bot/charge/Stars amount; it preserves immutable
paid evidence, records a separate original-rate USD and 5% commission
credit, revokes only returned-purchase grants and recomputes the remaining
paid subscription period from surviving manual/Stars entitlements.
Owner-initiated cancellation and refunds use the same project checkout
lock to avoid deadlocks.

A lost `successful_payment` or `refunded_payment` is independently
reconciled using the *connected bot's* official `getStarTransactions`
ledger: outgoing refunds have a positive amount in Bot API JSON and
reuse the original incoming charge ID. An in-process task scans up to
500 recent transactions per connected bot about every 15–18 minutes;
`/starsrecover` checks up to 2000 for the specific buyer. These bounds
are operational protections, **not a claim that no older unmatched
chargeback can exist**. Broader reconciliation and independent app-store
chargebacks remain a pre-live-billing requirement. Crypto Bot remains
a separate payment-provider stage. Keep the tenant/buyer menu compact;
no FX screen, manual receipts or duplicate bank settings for Stars.

## Next rail: Crypto Bot / Crypto Pay

Use a verified provider webhook/API ledger, per-merchant encrypted API
key/account binding, exact paid invoice reference and currency/amount,
idempotent payment-to-Entitlement transaction shared with the Stars and
manual paths. NEVER present a Crypto Bot payment button until the full
payment and delivery loop is implemented and reviewed.
Telegram's published rules currently require Stars for DIGITAL GOODS
inside Telegram bots; this remains a deployment/product compliance gate.

## Chronological recovery hardening (PR #130 follow-up)

All authenticated Stars payments and refunds share the same per-project
checkout/retirement serialization lock. When Telegram recovers an older paid
charge after a newer one has been confirmed, the historical paid_at timestamp
is used to place the older entitlement; later still-paid Stars periods are
recomputed in payment order. Refunding an earlier charge applies the same
reconstruction. Prepaid manual grants remain unchanged and anchor subsequent
Stars periods by their actual Payment.approved_at, even if the manual
EntitlementGrant.valid_from is in the future. No duplicate sale, grant, invoice,
UI action, or new supervisor is introduced. Regression coverage includes
a missed old Stars charge, a later confirmed purchase, and a manual prepayment
anchor. Live commercial billing and staging rollout remain OFF.

A returned Stars order remains a *historical* anchor for a renewal that
was purchased before the verified refund time. Later backfills may not
silently shorten that already-prepaid interval. Orders bought after the
refund do not inherit the returned period. This is covered by a real
PostgreSQL regression with A purchased, B prepaid, A refunded, and older
C recovered afterward.

If an authenticated successful Stars charge predates an explicit, audited
owner `subscription_end` decision but its delivery is delayed, recovery
persists the original charge as paid financial evidence without reactivating
access or reversing the owner termination. A truly new charge following
the termination can start a new subscription period. The buyer does not
receive a false "access activated" success message for a charge whose grant
was already superseded; they are directed to support for refund resolution.

## Corrected delayed-events timeline and owner termination security

Migration 0058 adds only a read-only SECURITY DEFINER lookup for the
timestamps of `subscription_end` owner decisions, confined to the active
project scope and exact subscription UUID. The existing owner-only audit
RLS policy and source rows remain unchanged. The function is not available
to PUBLIC and has no cross-tenant or arbitrary-table access.

Out-of-order historical Stars purchases consult BOTH still-paid manual/Stars
sources and already-returned Stars sources when the refund actually happened
after the renewal purchase. Owner end events suppress old anchors across a
cancellation. Revoked `EntitlementGrant.revoked_at` is deliberately NOT
considered the real owner termination time because future-dated grants
clamp that timestamp. A late Telegram refund repairs later manually approved
renewals as well as Stars renewals, preserving manual Payment records and
the original financial ledger while correcting access windows.
