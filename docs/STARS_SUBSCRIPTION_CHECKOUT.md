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
Platform financial settlement remains disabled. Native Stars refunds and
crypto checkout are separate required safety stages before turning on
live commission issuance. Keep current tenant/buyer menu compact; no FX
screen, manual receipts or duplicate bank settings for Stars.

## Next rail: Crypto Bot / Crypto Pay

Use a verified provider webhook/API ledger, per-merchant encrypted API
key/account binding, exact paid invoice reference and currency/amount,
idempotent payment-to-Entitlement transaction shared with the Stars and
manual paths. NEVER present a Crypto Bot payment button until the full
payment and delivery loop is implemented and reviewed.
Telegram's published rules currently require Stars for DIGITAL GOODS
inside Telegram bots; this remains a deployment/product compliance gate.
