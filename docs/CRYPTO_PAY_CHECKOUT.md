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
