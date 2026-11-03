# Payment Requests

This document defines the checkout request boundary before a real Payment exists.

## PaymentRequest is not Payment

Creating a checkout request does not mean money was received.

The lifecycle is:

```text
WAITING_CONFIRMATION
  -> WAITING_REVIEW
  -> APPROVED
  -> Payment + Subscription + EntitlementGrant(s) + OutboxEvent(s)
```

A request may also finish as `REJECTED`, `CANCELLED` or `EXPIRED`.

`APPROVED` is reachable only through the atomic approval service. The same transaction creates the
Payment, creates or renews the Subscription, creates the intended Entitlement Grants and writes the
outbox events. A request cannot be committed as approved without those business effects.

## Commercial snapshot

A PaymentRequest stores the selected commercial snapshot:

- `plan_price_id`;
- `plan_id`;
- `currency_code`;
- `regular_amount`;
- `discount_amount`;
- `final_amount`;
- nullable `promotion_id` reserved for the future promotions module;
- `payment_method_id`;
- `duration_value`;
- `duration_unit`.

Changing a PlanPrice after request creation never rewrites the request amounts.

Changing the Plan duration after request creation also does not change what the request buys. MONTH and
YEAR are later applied as calendar units in the Project timezone, not as fixed day counts.

The request has a composite FK to the exact PlanPrice identity so a mismatched Plan/currency cannot be
attached by UUID.

For the current MVP slice `discount_amount = 0` and `final_amount = regular_amount`. Promotion logic is
not implemented yet.

## ProjectCustomer

`ProjectCustomer` links one global `TelegramUser` identity to one Project. It is tenant-owned and has a
unique `(project_id, user_id)` pair. This keeps subscriber/payment history project-scoped without
creating duplicate Telegram identities.

## Payment method compatibility

Request creation validates that:

- the Plan is ACTIVE;
- the selected PlanPrice is the current ACTIVE version;
- the currency asset is active;
- the PaymentMethod is active;
- the PaymentMethod currently supports the selected currency.

After the request is created, later PlanPrice or Plan-duration changes do not change its frozen
commercial terms.

## No receipt/proof subsystem

PaymentRequest has no receipt, screenshot, document, file ID, URL, hash, OCR metadata, bank metadata or
fraud-analysis fields.

The Telegram adapter will copy/forward the buyer's proof message directly to the project admin and then
move the request to `WAITING_REVIEW`. The admin checks the real bank/wallet balance and chooses approve
or reject.

## RLS and buyer-facing adapters

`project_customers` and `payment_requests` use forced PostgreSQL RLS on `project_id`.

Domain functions require a project-scoped session. A future Telegram buyer adapter must derive the
Project from trusted bot/resource routing (for example BotInstance -> Project), never from an arbitrary
project ID supplied by the buyer.

## Concurrency

State-changing functions lock the PaymentRequest row with `FOR UPDATE` and refresh an already-cached ORM
identity before evaluating state.

Duplicate submit/reject calls are idempotent for the already-achieved state. Duplicate approval is also
idempotent: exactly one Payment exists per PaymentRequest and the Subscription is extended only once.

Approval additionally serializes commercial period changes through the ProjectCustomer row so concurrent
approvals affecting the same customer do not lose a renewal period.
