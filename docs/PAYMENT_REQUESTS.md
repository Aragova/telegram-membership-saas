# Payment Requests

This document defines the checkout request boundary before a real Payment exists.

## PaymentRequest is not Payment

Creating a checkout request does not mean money was received.

The lifecycle starts as:

```text
WAITING_CONFIRMATION
  -> WAITING_REVIEW
  -> REJECTED
```

The schema already reserves `APPROVED`, but this PR intentionally does not expose an approve command.
Approval will be implemented only together with one atomic transaction that creates exactly one Payment,
creates or renews exactly one Subscription, creates the intended Entitlement Grants, and enqueues the
outbox event. This prevents a premature `APPROVED` state that has no matching business effects.

`CANCELLED` and `EXPIRED` are terminal states for requests that were never submitted for review.

## Price snapshot

A PaymentRequest stores the selected commercial snapshot:

- `plan_price_id`;
- `plan_id`;
- `currency_code`;
- `regular_amount`;
- `discount_amount`;
- `final_amount`;
- nullable `promotion_id` reserved for the future promotions module;
- `payment_method_id`.

Changing a PlanPrice after request creation must never rewrite the request amounts. The request has a
composite FK to the exact PlanPrice identity so a mismatched Plan/currency cannot be attached by UUID.

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

After the request is created, later PlanPrice changes do not change the snapshot.

## No receipt/proof subsystem

PaymentRequest has no receipt, screenshot, document, file ID, URL, hash, OCR metadata, bank metadata or
fraud-analysis fields.

The future Telegram adapter will copy/forward the buyer's proof message directly to the project admin and
then move the request to `WAITING_REVIEW`. The admin checks the real bank/wallet balance and chooses
approve or reject.

## RLS and buyer-facing adapters

`project_customers` and `payment_requests` use forced PostgreSQL RLS on `project_id`.

Domain functions require a project-scoped session. A future Telegram buyer adapter must derive the
Project from trusted bot/resource routing (for example BotInstance -> Project), never from an arbitrary
project ID supplied by the buyer. The adapter/session boundary for customer traffic is intentionally kept
outside the domain model.

## Concurrency

State-changing functions lock the PaymentRequest row with `FOR UPDATE`.

Duplicate submit and duplicate reject calls are idempotent for the already-achieved state. Invalid
transitions are rejected. Approval idempotency is deferred to the atomic settlement PR because that is
where duplicate callbacks could otherwise create duplicate money/subscription effects.
