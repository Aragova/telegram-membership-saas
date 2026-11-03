# Atomic Payment Approval

This slice implements the first complete commercial settlement transaction.

## Invariant

A PaymentRequest may become `APPROVED` only in the same database transaction that creates all required
business records:

```text
PaymentRequest WAITING_REVIEW
  -> Payment
  -> Subscription create/renew
  -> EntitlementGrant(s)
  -> OutboxEvent(s)
  -> PaymentRequest APPROVED
```

If the transaction fails, none of these effects may remain committed.

## Exactly-once approval

The PaymentRequest row is locked with `SELECT ... FOR UPDATE` and refreshed from the database before its
state is evaluated.

`payments.payment_request_id` is unique. A repeated callback for an already-approved request loads and
returns the existing result instead of creating another Payment or extending the Subscription again.

Two administrators approving the same request therefore converge on the same Payment.

## Subscription concurrency

Approval also locks the `ProjectCustomer` row before loading/updating the Subscription.

This serializes commercial period updates for the same customer, including the harder case where two
different valid PaymentRequests for that customer are approved concurrently. It prevents a lost update
where both transactions would otherwise extend from the same old period end.

A Subscription is unique by:

```text
(project_id, customer_id, plan_id)
```

## Calendar periods

Plan duration is snapshotted into PaymentRequest when checkout is created:

- `duration_value`
- `duration_unit`

Payment copies the same immutable snapshot.

This is required because changing the Plan after checkout must not alter what an already-created request
buys.

MONTH and YEAR are calendar operations in the Project timezone. They are not converted to 30 or 365 days.

Examples:

- January 31 + 1 MONTH -> February 28 (or 29 in a leap year)
- February 29 + 1 YEAR -> February 28 next year
- LIFETIME -> `current_period_end = NULL`

For an active finite Subscription, renewal starts from the existing `current_period_end`. If the previous
period already ended, the new period starts at approval time.

## Payment

Payment is a durable fact created only after an administrator confirms that the money is actually present
in the bank/wallet.

It snapshots:

- project/customer;
- PaymentRequest;
- Plan and exact PlanPrice;
- PaymentMethod/provider;
- currency;
- regular/discount/final amounts;
- promotion reference;
- duration;
- approver and approval timestamp.

There are no proof/receipt file fields.

## Entitlement grants

Each Payment creates one independent `EntitlementGrant(source=PAYMENT)` for every Resource currently
linked to the Plan.

The grant validity is the purchased commercial period. Grants are independent access reasons; later
EffectiveAccess logic must evaluate all active grants instead of relying on a single `paid_until` field.

A unique `(project_id, source, source_id, resource_id)` constraint prevents duplicate grants for the same
Payment and Resource.

## Transactional outbox

Approval never calls Telegram directly.

For every new payment entitlement, the transaction inserts an `OutboxEvent` with
`event_type=access_recalculate`. A later worker will consume these events, calculate desired/effective
access, and call the Telegram access adapter after commit.

Outbox events have a tenant-scoped dedupe key and retry state.

## RLS

The following tables are tenant-owned and use forced PostgreSQL Row Level Security:

- `payments`
- `subscriptions`
- `entitlement_grants`
- `outbox_events`

All writes and reads require the active Project context.
