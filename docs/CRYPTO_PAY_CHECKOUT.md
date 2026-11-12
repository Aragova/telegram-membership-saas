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
