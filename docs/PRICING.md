# Pricing Core

This document defines the currency and PlanPrice invariants.

## Equal currencies — no primary/base currency

A Plan has no `primary_currency` and no `base_price`.

Every active `PlanPrice(plan, currency)` pair is equal in the product model. Example:

```text
RUB  666
USD   10
EUR    9
USDT  10
```

No price is derived from another price and no exchange-rate conversion is implied.

## Currency registry

`currency_assets` is a global registry, not tenant-owned commercial data.

Each asset stores:

- `code`;
- FIAT/CRYPTO type;
- name/symbol;
- precision;
- lifecycle status.

The initial registry seeds RUB, USD, EUR, GBP, KZT, BYN, UAH, GEL, AMD, AED, BTC, ETH, USDT, USDC, TON and TRX.

USDT is an asset/currency. Networks such as TRC20, TON or ERC20 are future Payment Methods, not separate currencies.

Money is never stored as floating point. `PlanPrice.amount` uses fixed-point numeric storage.

## A currency exists on a Plan only together with a price

There is no separate empty `plan_currency` row.

Adding a currency to a Plan means creating its active `PlanPrice`. Therefore a currency without a price is impossible in the current offer.

The future owner UI follows this rule:

- `🌍 Валюта` adds/removes currencies; adding immediately requires an amount;
- `💰 Цена` edits existing currency-price pairs only;
- a new currency is not created from the Price screen.

Removing a currency retires its active PlanPrice; history remains intact.

## Versioning

Prices are historical records, not mutable values.

For each `plan + currency`:

- versions start at 1 and increase monotonically;
- exactly one row may be ACTIVE;
- replacing a price retires the active row and creates the next version;
- retired rows are never rewritten as the new current price;
- a partial unique PostgreSQL index enforces one active version.

`replace_active_plan_price()` locks the Plan row before allocating a version. This serializes concurrent changes for one Plan and prevents two administrators from independently producing competing active versions.

Example:

```text
USD v1 = 10  RETIRED
USD v2 = 12  ACTIVE
RUB v1 = 666 ACTIVE
```

## Tenant isolation

`plan_prices` carries `project_id`, uses a composite FK to `(plans.id, plans.project_id)`, and has forced PostgreSQL RLS.

A price cannot be attached to a Plan from another Project by supplying a mismatched project ID.

`currency_assets` is global reference data and therefore is intentionally not protected by tenant RLS.

## Future PaymentRequest snapshots

A future PaymentRequest must snapshot the selected commercial offer rather than depend on the current PlanPrice after checkout.

At minimum the snapshot will preserve:

- selected `plan_price_id` / version identity;
- `currency_code`;
- regular amount;
- discount amount;
- final amount;
- promotion identity where applicable.

Changing USD 10 → 12 after a buyer created a request must never rewrite that existing request.
