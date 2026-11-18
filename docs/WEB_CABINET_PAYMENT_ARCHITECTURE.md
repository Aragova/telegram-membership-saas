# ViMi Pay: target web-cabinet payments and provider webhook architecture

**Decision date:** 2026-11-17. **Status:** approved product direction; architecture only.
**Not live.** No bot menu, payment routing, public API, finance, customer access,
or current provider credentials are changed by this decision.

## 1. Product contract and scope

ViMi Pay remains a platform of independent owner-operated connected bots
(one connected paid bot per private channel), one CENTRAL `@vimipay_bot`, and
one canonical financial/subscription/access backend. The future design separates
**payments performed inside Telegram** from **payments handled by an external
web account**.

- **Within Telegram:** preserve the native `⭐ Telegram Stars` checkout for
  digital subscriptions/files, plus one neutral **🌐 Личный кабинет / Account**
  entry for reviewing one's account and managing payments. Don't clutter the
  bot with copies of web-only manual checkout, receipt uploads, or provider
  payment menus after the web flow is genuinely ready.
- **Outside Telegram, in a separately authenticated website:** buyer chooses
  allowed external methods (merchant bank details / SBP / card where permitted,
  Lava.top, Crypto Pay as configured); views payment instructions; submits
  "Я оплатил / I paid" and receipt evidence; checks the payment's status.
- **Canonical result:** only a verified payment produces the *same* existing
  Payment -> Subscription / File Purchase -> Entitlement -> Telegram access.
  Clicking a link, a browser redirect, a customer assertion, a screenshot, or
  receipt upload does **not** settle anything.

**Do not confuse two different financial relationships.**

1. **Buyer -> channel administrator:** per-merchant plans/file sales, per-bot
   prices and merchant-specific methods, receipts reviewed only by that bot's
   authorized owner/moderators according to existing role permissions.
2. **Channel administrator -> ViMi Pay platform:** one per-bot service invoice
   (currently 150 RUB or $5, 5% source-based commission), platform-owned Lava,
   Stars, Crypto or operator-reviewed manual transfer, **approved only by the
   platform operator**. Do not accept tenant credentials for this debt.

Both roles can use the *same website and identity*, but must see separate
scoped pages and read/modify only their own records.

## 2. Target buyer experience

1. Buyer opens the connected bot, selects a plan or file; Telegram Stars can
   be purchased natively in Telegram.
2. For an external account, a short-lived authenticated website session maps
   the Telegram identity and the exact original offer/bot. The web page
   fetches the currently valid price and methods from the authoritative DB.
3. The browser shows the merchant's enabled methods. The buyer selects one;
   the system reserves an immutable invoice/quote with currency, amount,
   method, owner and expiry. For manual methods it displays the correct
   instructions; for providers it launches a provider-created checkout URL.
4. Manual: the buyer attaches receipt evidence to the *same canonical order*;
   the merchant receives a Telegram notification. Merchant reviews in the
   web cabinet with existing RBAC and selects approve/reject. Server rechecks
   ownership, order status and proof before committing existing ledger action.
5. Automatic: only the verified provider payment and exact original-order
   settlement can activate access. Bot and web reflect the same state; delivery
   and revocation/expiry remain in the existing bot worker/outbox.

**Do not create a parallel Web order, parallel balance, or independent buyer
subscription database.** Map web actions to current authorized service
operations. All list/detail endpoints require both actor and project scope;
opaque IDs alone never grant access. Existing buyers/access must survive
switching interfaces, bot restarts and payment method outages.

## 3. Target administrator experience

- Merchant dashboard: its own subscriptions, files, buyers, payment methods,
  outstanding buyer proofs, approval/rejection, analytics and Telegram links.
- Platform bill page: the same current SaaS invoice, balance, original
  allocations, refundable credits and selectable rails. The platform
  operator's review queue is entirely separate from merchant buyer proofs.
- Existing Telegram notifications remain useful: new proof, review result,
  renewal, blocked/new purchases and restoration. Prefer linking to the
  relevant scoped website record; do not duplicate whole admin menus.
- Language parity: RU/EN throughout, preserve original role and locale.
- Don't silently change existing account-wide overdue policy, service
  anniversary, source-date FX, commission calculation or buyer entitlements.

## 4. Web authentication, security and identity

- Prefer **Telegram Login with OpenID Connect** for the independent website;
  use current official Telegram documentation for configuration and signature/
  token verification. If a Telegram Mini App is later used, validate Telegram
  `initData` server-side independently rather than treating the URL as proof.
- Server-only sessions: secure HttpOnly/SameSite cookie, CSRF defence for
  mutations, state/nonce, short expiration, session revocation, login/logout,
  and step-up confirmation before sensitive account/payment changes.
- Link Telegram IDs to existing `telegram_users` without trusting a claimed
  username. A confirmed email is an optional, separate account recovery flow
  and must not be substituted for Telegram authorization.
- RLS + authorized service methods protect owner/moderator/platform
  separation. All messages, links and evidence are tenant scoped.
- Encrypt sensitive provider tokens; audit all payment/approval actions;
  accept receipts as untrusted media (size/type limits, private access,
  malware/content screening where required). Never log provider tokens,
  Telegram login proofs, personal emails or raw receipt URLs.

## 5. Payment webhooks and public API

**Terminology:** the requested webhooks are primarily **payment provider ->
ViMi Pay HTTPS notifications**. They are separate from how Telegram bots
receive Bot API updates. Keep existing Telegram long polling unchanged unless
there is a separate reviewed reason to change it.

Current Stage 4 `pay-api.usevimi.ru` is **health-only** (HTTPS `/health`);
its public payment routes and provider webhook endpoints do not exist yet.
The isolated FastAPI deployment is a sensible future *ingress*, not a live
checkout or reason to expose the 51-bot supervisor.

Target provider ingress (routes and names to be finalized by implementation):
- Authenticate webhook according to the **actual provider contract**:
  dedicated secret/header, signature when available, strict source checks,
  bounded payload, TLS, request method and timestamp/replay checks as
  supported; never trust a browser return URL as a paid signal.
- Store a bounded, opaque provider event/receipt ID for idempotency, then
  **fetch the actual transaction from the provider API using the platform
  or merchant's own authorized credentials** before changing finance.
- Verify original invoice/order/payer/recipient, payment currency and
  amount, event status, refund link, and immutable provider ID. Apply through
  existing atomic SQL ledger procedures. Duplicates, out-of-order notifications,
  refunds and cross-tenant attempts must be harmless.
- Respond quickly after authenticated event acceptance; use bounded retry/
  dead-letter handling and an operator audit trail; never activate a
  subscription directly from an untrusted request payload.
- **Keep the existing 60-second authenticated GET/getInvoices scanner as
  recovery**, so dropped webhooks and operator retries still settle payments.
  Telegram Stars continue via Bot API `successful_payment`, pre-checkout,
  refunds and their existing reconciliation logic, not an invented
  standalone web settlement endpoint.
- Exact public URLs, authentication, network/routing and provider
  availability require independent provider acceptance, threat review,
  staged deployment and rollback. No public ingress change is authorized
  by this design document.

References for implementation-time verification:
- Telegram login/OIDC: https://core.telegram.org/bots/telegram-login
- Lava.top v3 provider docs: https://developers.lava.top/en
- Verify current Crypto Pay provider API and event authentication against its
  official documentation before creating a webhook handler.

## 6. Phased delivery and cutover, no broken 51-bot fleet

**Now (Stage A):** finish and verify current central ViMi Pay SaaS invoice
buttons: `💳 Lava.top`, `⭐ Telegram Stars`, `💎 Crypto Pay`,
`🏦 Полуавтомат` in the existing **2 x 2** selector. Test real provider
configuration and small, controlled transactions only with operator approval.
Check provider reconciliation, refunds, notifications and RU/EN. Current
tenant buyer manual methods remain in Telegram until web parity is proven.
Do not claim that these four current SaaS rails have already been migrated.

**Stage B:** separate FastAPI security/auth ingress and one web-cabinet shell.
Begin with read-only buyer + merchant + platform invoice/history views;
no external funds, payment mutation or public callbacks.

**Stage C:** implement payment proof lifecycle through the web using
existing original orders, tenant RBAC, immutable receipt review and
Telegram notices. Add authenticated provider webhooks with GET verification
and replay-safe reconciliation; rollout behind explicit feature flags.

**Stage D:** compare live/test buyer paths for all prices, currencies, files,
subscription renewal, admin rejection, refunds, hold/unhold, restoration and
access after outages. Only then show **Stars + Account** in buyer bots and
retire *redundant* manual and external-provider buyer payment controls from
Telegram. Preserve the ability to rollback routing without deleting proofs,
charge records or previously purchased access.

**Release gates:** owner approval on each merge/deployment; CI + Codex review,
isolated DB E2E, provider sandbox acceptance where supported, precise
Nginx/API-only staging rollout, no surprise restart of central/customer bots,
zero duplicate menu items, RU/EN parity and operational rollback.

## 7. Current blockers / next operator actions

- Stars: operator must set a **verified**, not invented, RUB-per-Star payout
  quote before platform SaaS checkout can issue a payable native invoice.
- Lava.top: secure platform API key + published dynamic-price offer ID,
  payer email, provider acceptance/GET confirmation and refund review.
- Crypto Pay: separate platform application API token and correct mainnet/
  testnet environment; provider invoice creation, independent GET and
  receipt reconciliation.
- Provider credentials go into approved private staging secrets/environment,
  **never GitHub code, PR comments or Telegram payment messages**.

**This is an approved future product concept, not permission to activate
unverified payment endpoints or replace current working flows now.**
