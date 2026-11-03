# Effective Access and Owner Service Accounts

## Purpose

Commercial records and Telegram membership are intentionally separate.

The access chain is now:

```text
EntitlementGrant(s)
  -> EffectiveAccess.desired_access
  -> OutboxEvent(ACCESS_SYNC)
  -> future Telegram adapter
  -> actual Telegram state
```

`EffectiveAccess` is a projection of all currently valid grants for one customer/resource pair. It is not a replacement for grant history.

## Additive grants

Access is allowed when at least one grant is active at the evaluation time:

- status is `ACTIVE`;
- `valid_from <= now`;
- `valid_until` is NULL or later than now.

Revoking or expiring one grant must not revoke access while another valid grant remains. This is required for combinations such as PAYMENT + MANUAL or PAYMENT + OWNER_SERVICE.

## OWNER_SERVICE

A network/platform owner may register a Telegram identity as an `OwnerServiceAccount`.

The assignment is not a Plan and creates no PaymentRequest, Payment or Subscription.

For each owned Project and active Resource, reconciliation materializes:

```text
EntitlementGrant(
  source = OWNER_SERVICE,
  valid_until = NULL,
)
```

This gives permanent content access until the service assignment/grant is explicitly revoked.

The assignment has `auto_include_future_projects=true` by default. Project/resource creation flows must invoke owner-service reconciliation so future owned Projects/Resources receive the same service access idempotently.

Management permission and content access remain separate: an OwnerServiceAccount may have content access without any admin role.

## Outbox processing

Payment approval and owner-service reconciliation enqueue `ACCESS_RECALCULATE` events.

The recalculation worker:

1. locks the event;
2. derives desired access from all grants;
3. upserts `EffectiveAccess`;
4. when desired state changes, writes a separate `ACCESS_SYNC` event;
5. marks the recalculation event succeeded.

The worker does not call Telegram.

The next adapter slice will consume `ACCESS_SYNC`, compare desired state with actual Telegram membership, perform grant/revoke, and support retries/reconciliation.

## Multi-tenancy

`effective_access` is Project-owned and protected by forced PostgreSQL RLS.

`owner_service_accounts` is owner-scoped rather than Project-scoped because one assignment may span many owned Projects. PostgreSQL RLS restricts those rows to `owner_user_id = app.current_user_id`.

No service assignment can be used to materialize grants in a Project owned by another user.
