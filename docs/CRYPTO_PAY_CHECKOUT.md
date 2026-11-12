# Crypto Pay / @CryptoBot — approved next rail, foundation PR #133

**Current status: backend foundation ONLY. No live Crypto Pay buyer button yet.**
Existing Stars and manual payment methods remain unchanged.

Official reference: https://help.cr.bot/ru/articles/10279948-crypto-pay-api

## Owner and funds
Each connected bot uses its tenant owner's own Crypto Pay app in
@CryptoBot or testnet @CryptoTestnetBot. The merchant receives funds in
their own app, NOT ViMi Pay's account. The current verified tenant owner
authorizes and sets the Crypto Pay token through a future private wizard.
getMe validates credentials over HTTPS before any encrypted persistence.
The existing Fernet bot credential key encrypts the app token at rest.
No token may appear in Telegram response, user logs, GitHub or callbacks.
Rotating the app records a *new* credential version. Retired credentials
remain for invoices created before rotation; owner transfer must not
silently expose the old owner's merchant credential to the new owner.

## Technical boundaries
- Mainnet: https://pay.crypt.bot/api/
- Testnet: https://testnet-pay.crypt.bot/api/
- Auth: Crypto-Pay-API-Token header only; HTTPS only.
- Verified API calls: getMe, createInvoice, getInvoices.
- Supported customer price currencies include RUB, USD, EUR, UAH, BYN
  as fiat invoices, plus USDT/TON as crypto invoices. Provider, NOT ViMi
  Pay, determines which coin a buyer pays with and the conversion rate.
- Frozen invoice stores project, original bot, buyer, credential app,
  subscription/file target, original amount and code, unique payload,
  provider invoice ID and provider link.
- No payment can be marked paid, grant access, or release a file from the
  mere creation of an invoice, a screenshot or an unsigned callback.
- Next full slice must verify provider getInvoices on its exact app token:
  status paid, invoice id, payload, amount, currency, paid_at; perform
  atomic Subscription+Entitlement+Outbox or paid FilePurchase+delivery.
- Old invoices are recoverable after lost webhook, app rotation and
  currency/plan edits. Use idempotent reconciliation and signature verify
  HMAC-SHA256(key=SHA256(token), message=raw webhook bytes).
- Never accept Telegram user identity merely from Crypto Pay webhook.
  Merchant-owned app + local immutable buyer/order are the authority.
- The provider's transfer/check endpoints are NOT used.
- No duplicate paid menu/buttons, no automatic platform withdrawal.
  RU/EN and Rich UX will be provided with full checkout, not a dead link.

## Planned acceptance gates
1. Encrypt/connect/rotate/disconnect merchant app in owner bot.
2. Buyer sees Crypto Pay alongside manual and Stars only where fiat/crypto
   plan/file price exists; create provider link on click.
3. Provider-verified paid event delivers access/file automatically, exactly
   once; delayed payment, duplicate webhook, false paid status, merchant
   rotation, cancellation, expiry and wrong buyer/project/bot tested.
4. Background getInvoices polling recovers lost webhook and supports 100 bots
   with bounded network usage; no second access/subscriber database.
5. Exact-head PostgreSQL CI, fresh supervisor cold import, Codex review,
   explicit owner approval before any merge/staging/activation.

### Provider wire compatibility (PR #133)

The official Crypto Pay API documentation describes getInvoices as returning
an invoice array. Provider integrations also return an envelope
`result: {count, items: [...]}`. Both are accepted, while duplicate,
unexpected and malformed invoice IDs are rejected; a paid grant still requires
matching all immutable local invoice attributes. Legacy Crypto Bot invoice
links on `https://t.me/send?start=...` and current
`https://t.me/CryptoBot?start=...` are accepted only on mainnet; third-party
domains, usernames and nonstandard ports are rejected.

### Ownership-transfer safety

The same verified `finalize_transfer` transaction that disables old manual
payment instructions also retires the outgoing owner's active Crypto Pay app
before changing project ownership. Historic encrypted API token versions
remain for reconciliation of invoices paid prior to the transfer, but the
incoming merchant cannot see or reuse them and can register their own app
without hitting the active-app uniqueness constraint. A real PostgreSQL
transfer→reconnect test enforces this contract.

### Owner setup UX (PR #134)

A single 💎 Crypto Pay entry is linked under **💳 Способы оплаты /
Payment methods** in the *connected administrator bot*, not the central
registry. It displays a non-sensitive current app label and environment,
provides mainnet/testnet selection, secure one-time API token input and
versioned disconnect/rotation. The bot deletes the original token message
BEFORE attempting API validation, refuses to store if deletion fails, and
never prints the token or ciphertext to Telegram. Setup is owner-only via
tenant permission and BotInstance identity; moderators cannot substitute
merchant credentials. File sales and subscription billing will use this
SAME credential, not separate Crypto Pay settings. No buyer payment option
is added until automatic verified fulfillment works.

### Secret input precedence vs stale editors (PR #134)

File sales draft editors and file price editors are registered BEFORE owner
setup in aiogram. They now check the active owner setup prompt for the same
user AND connected bot, and drop their stale in-memory editor when any
current owner wizard is awaiting input. This prevents merchant secrets being
saved as a product title, description or price. The actual owner input
handler deletes token messages BEFORE contacting Crypto Pay. Regressions
exercise both file-sale routers and ensure normal editors remain usable.

### Priority secret-ingress security (PR #134)

The first connected-bot router after language handles ONLY plausible
merchant API credential messages with an authenticated, unexpired, bot- and
project-scoped `crypto_pay_token` setup state. It runs BEFORE moderator
message composers, file editors, manual receipt workflows and the buyer/owner
conversation replies. Thus even an old quoted subscriber notification cannot
forward a secret, and old product drafts cannot store it. This handler
deletes the Telegram token message BEFORE consuming the wizard or validating
the token. Failed deletion blocks registration. Non-token chat messages
avoid an extra credential-flow DB lookup for efficient 50-100 bot operation.
All existing file editor checks remain as defense in depth.

### Switching back to file editors

When the merchant deliberately opens a new file title/description/upload
editor or a manual file price editor, the bot first clears any previously
active merchant credential/owner setup wizard for that bot (using the same
persistent, generation-safe cancel function as /cancel). The new editor can
then accept ordinary file text without accidentally deleting it as a stale
Crypto Pay token. Entering /files also cancels the previous owner wizard.
The priority secret router still protects any actual token sent WHILE
a Crypto Pay credential wizard is active, including replies to old buyer
notifications and stale transfer forms.

### Preserve token secrecy across provider failures (PR #134)

The priority secret handler reads but does **not consume** the durable,
bot-scoped Crypto Pay wizard before calling getMe. When provider verification
fails (invalid token, timeout, temporary outage), the same private prompt
and generation remain active; a retry that quotes an old buyer notification
is still captured and deleted before any other message router. On successful
getMe and encrypted credential registration, the handler atomically
discards only that specific wizard generation. If the owner navigated to
a newer setup while getMe was pending, the newer flow is left untouched.
Only generic error messages reach Telegram, never raw API tokens or secrets.

### Strict secret interception across expired/alternate flows (PR #134)

The secret guard is the first connected-bot message route after language.
Any credential-shaped private text is **always deleted first**, even if the
owner's 15-minute wizard expired, was cancelled, or was successfully consumed.
This avoids forwarding late API tokens through stale Telegram buyer replies.
During an active `crypto_pay_token` flow, the guard also intercepts
malformed or truncated non-command input: a wrong first attempt cannot
make the alternate owner setup handler consume the durable secret wizard
and expose the subsequent valid retry. The provider is never contacted
unless deletion succeeds AND the current verified bot-scoped wizard is live.
Ordinary commands such as /cancel remain routed normally. Buyer payments
and access entitlement routes are unchanged.

### Avoid destructive wallet-address false positives (PR #134)

The unconditional post-expiry secret scrubber matches only colon-delimited,
numeric-prefix Crypto Pay API credential shapes. Generic alphanumeric strings,
Ethereum/TRON wallet addresses, transaction hashes and ordinary links are NOT
deleted. When the owner actually has a live bot-scoped Crypto Pay token wizard,
ALL private non-command text is intercepted and deleted before verification,
regardless of provider token format. This separates credential protection from
normal buyer support/payment evidence.

### Owner-only hot-path optimization (PR #134)

The first secret router still deletes a correctly shaped API token before all
other message handlers without any database lookup. For ordinary private text,
it checks a short-lived, project-scoped owner identity cache and immediately
skips buyer messages. On cache miss, it performs ONE read-only project-owner
identity query (no Telegram user upsert) and caches it briefly. Only the
identified current owner's text incurs a tenant-scoped wizard read. An
authorized Crypto Pay setup callback warms the local owner hint immediately
after binding the durable prompt, including after ownership transfer. All
authorization and active generation checks still occur in the database;
the in-process hint is NEVER used to grant credentials or payment rights.

### Checkout reservation and provider verification service (in progress, #135)

Backend-only `crypto_pay.checkout` creates a **frozen local reservation** for a
published file with actual assets or a live subscription plan/price/resource,
checking the connected bot, buyer ban, active merchant credential, supported
currency, and up to 10 outstanding Crypto Pay orders per buyer. Subscription
reservations capture the project customer and original plan/duration/price.
The generated `cp:<UUID>` payload is unique. The reservation is committed
before any external invoice call. An issuance transaction revalidates the
published product and its assets, or the active subscription and resources,
then persists `status='issuing'` before contacting the provider. A 15-minute
reservation TTL and bounded outstanding-order count protect old quotes. If
Crypto Pay times out or the DB commit fails after provider acceptance, the
`issuing` state MUST be recovered by querying the original provider app;
blind re-creation is forbidden. A bounded `getInvoices` listing (five
100-invoice pages by default) can recover a matching payload from the
original credential version, after validating the same amount, currency
and payload. Provider-confirmed `expired` invoices are recorded with the
original provider ID and a local terminal `expired` status, WITHOUT exposing
a payable link or granting a file/subscription. This releases the buyer's
pending-invoice slot. Expired provider invoices may omit payment links, so
only active/paid invoices require a safe official URL. Conflicting paid
evidence on a supposedly expired invoice is rejected. If the invoice cannot
be found, its uncertain state is retained and no second provider bill is
created.
Successful responses store the provider ID
and URL, and repeating an already-issued request returns the same frozen
link. Rotation/disconnection prevents new issuance from a retired app, while
already issued invoices remain bound to their historic credential version.
Migration `0060_crypto_issuing_guard` adds the durable uncertain state. Read-only provider verification uses
`getInvoices` and validates immutable invoice ID/payload/amount/currency.

**Important safety gate:** this service deliberately does not mark an order
paid, credit platform fees, create entitlements, or deliver a file, and NO
buyer-facing Crypto Pay option is registered. The remaining #135 work is the
single-transaction fulfillment path, delivery outbox, periodic reconciliation,
UX integration and exact-head safety tests. Do not switch on a payable buyer
button until every step works after a crash/retry.

### Verified atomic payment ledger and historical resources (next #135 slice)

Crypto Pay settlement is now implemented as a **trusted backend-only** function
that performs an authoritative `getInvoices` lookup on the exact issuing
merchant credential before it may touch customer purchases. It revalidates
provider ID, unique payload, amount, currency, paid timestamp and the
project-scoped invoice under the shared checkout/retirement lock. No Telegram
buyer message, screenshot, or unchecked webhook can settle an invoice.

For files, the existing `crypto_pay_invoices` record is the one canonical
purchase. A verified paid invoice unlocks the existing /myfiles paid-history,
download and delivery-mark flow; no additional files purchase database is
introduced. For subscriptions, migration `0061_crypto_paid_sources` adds a
native `last_crypto_invoice_id` source to the shared Subscription projection
and `crypto_payment` to EntitlementGrant sources. Resource IDs sold at
reservation time are persisted; exact grants and access recalculation outbox
events are committed with the paid invoice in one project transaction.
Manual, Stars and Crypto sources share the same historical renewal timeline
to avoid silently losing cross-rail access on a later Stars refund.

**Rollout gate remains CLOSED**: there is no buyer Crypto Pay button, no
automated provider reconciliation polling or file push worker yet. A new
automated fulfillment/recovery worker will call the verified service and
deliver file media after the transaction commits. No commercial SaaS billing
switch is changed. Never expose a payable invoice until periodic recovery,
payment history, file delivery and cross-rail tests are complete.
