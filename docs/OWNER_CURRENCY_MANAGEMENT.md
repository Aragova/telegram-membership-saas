# ViMi Pay — Self-service currencies for owner bots

## Immediate new currencies

UAH (Ukrainian hryvnia, 2 decimals) and BYN (Belarusian ruble, 2 decimals)
are now listed alongside RUB/USD/EUR/KZT/AED/USDT/TON in both the tariff-price
screen and the payment-method currency picker. They have existed as
`currency_assets` since migration 0004; the fixed seven-code UI whitelist
was the actual problem.

## Owner-driven additional currencies

Admins with project-manage permission in the **connected customer bot**
(not in the central registrar) can select **➕ Другая валюта** from a tariff,
a new manual payment method, or an existing method's currency editor. The
bot asks for the international code (for example `GBP`, `CNY`, `JPY`,
`KWD`, `BTC`). A registered active code is enabled in only that project,
not as a global configuration change.

After selecting the code:
- Tariff: enter the price, which is validated using the catalog currency's
  minor-unit precision; existing prices and payment history are unchanged.
- New payment method: enter its name and buyer payment instructions,
  including bank details; creates an independent manual-settlement method
  for the chosen currency.
- Existing payment method: attach the currency, preserving the existing
  payment instructions (which the admin can edit separately).

**No currency conversions, exchange rates, automated bank debits or provider
API integration.** Payment receipt review continues to use manual settlement
and the existing admin approvals. Merchant is responsible for choosing
usable banking instructions and actual accepted currencies.

## Data model and security

Migration `0024_owner_currency_selection` seeds missing standard ISO 4217
alphabetic currency codes. Existing currency metadata, precision and
historical price records are never overwritten. No new runtime dependency,
currency FX service, background job or VM resource change. An opt-in
`project_enabled_currencies` table has PostgreSQL FORCE RLS by current
project. User-supplied arbitrary codes/precision cannot modify the global
catalog: only active known currency codes can be enabled. Tenant manager
permissions are rechecked server-side. A new currency is idempotent and
per-project preferences have a bounded limit of 24 extra codes.

Already-saved prices and payment currencies in a cloned Network Profile
remain visible in the target bot even when the source's explicit selection
has not been copied, and existing buyer checkout continues to resolve
prices and active payment methods through the same domain tables.

## Rollout

Requires **backup -> code update -> Alembic upgrade to 0024 -> build/restart
customer-supervisor** on staging. Central bot and PostgreSQL do not need
restarting; the lightweight `compose.light100.yaml` overlay remains in use.
No production change. Confirm UAH/BYN tariff creation and manual payment
method creation in an existing project. CI includes PostgreSQL RLS/precision
regression tests and customer bot callback tests.
