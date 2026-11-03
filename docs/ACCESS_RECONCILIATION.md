# Effective Access and Reconciliation

This slice separates domain access truth from the external Telegram membership state.

## Layers

```text
EntitlementGrant(s)
    -> EffectiveAccess
    -> AccessState.desired_access
    -> AccessAction
    -> AccessAdapter
    -> external platform
    -> AccessState.actual_state
```

`EntitlementGrant` remains the source of reasons for access. `EffectiveAccess` is a materialized
projection only; it must never replace or destroy the independent grants that produced it.

## Effective access

For one `(project, customer, resource)` target, access is effective when at least one non-revoked grant
is valid at the calculation moment:

```text
valid_from <= now
AND (valid_until IS NULL OR valid_until > now)
```

The projection stores:

- `has_access`;
- `active_grant_count`;
- `next_change_at`;
- `computed_at`;
- a monotonically increasing `revision`.

`next_change_at` is the nearest future `valid_from` or `valid_until` boundary among active grant records.
This lets a scheduler enqueue a recalculation at entitlement boundaries. A renewal that starts exactly
when the previous grant expires therefore does not briefly revoke access.

## Desired vs actual state

`AccessState` deliberately keeps two different concepts:

- `desired_access`: what the domain currently requires;
- `actual_state`: `UNKNOWN`, `GRANTED`, or `REVOKED` on the external platform.

A successful payment changes domain truth first. Telegram failures must not rewrite Payment,
Subscription, or Entitlement records.

## Actions

A mismatch between desired and actual state creates an `AccessAction`:

- `GRANT`;
- `REVOKE`.

Actions have retryable worker states and an idempotency/dedupe key. Only one action for a target should
be actively processing at a time. A pending action whose direction no longer matches current desired
state is marked `SUPERSEDED` before any external call is made.

A processing action can be reclaimed after a lease timeout. The future Telegram adapter must therefore
treat `GRANT` and `REVOKE` as idempotent operations.

## Adapter boundary

The core exposes an `AccessAdapter` protocol. The adapter receives an `AccessCommand` containing stable
internal IDs and the action ID. The domain layer does not import aiogram and does not know bot tokens,
invite-link details, or Telegram API error classes.

The real Telegram adapter is intentionally a later slice because BotInstance/credential routing is a
separate concern.

## Outbox

`ACCESS_RECALCULATE` outbox events are processed transactionally inside a project scope. Processing an
event recalculates current grant truth and enqueues an external action only when needed.

The event can be retried safely because the projection is recomputed from current grants rather than
applying an incremental `+1/-1` mutation.

## Expiry and future starts

`enqueue_due_access_recalculations()` converts `next_change_at` boundaries into deduplicated outbox
events. This covers both:

- grant expiration;
- a future grant becoming active.

If one grant expires while another remains active, effective access stays true and no revoke action is
created.

## Multi-tenancy

`effective_access`, `access_states`, and `access_actions` use forced PostgreSQL RLS and tenant-safe
composite foreign keys.

Worker primitives in this slice operate inside an already trusted Project scope. A future global worker
dispatcher must not weaken the normal application runtime role or grant it `BYPASSRLS`; dispatcher
credentials/scope will be designed separately.

## Failure model

External failures update only the AccessAction/AccessState operational layer.

- retryable failure -> `FAILED_RETRYABLE` with a future `available_at`;
- permanent failure -> `FAILED_PERMANENT`;
- success -> action `SUCCEEDED` and `actual_state` updated.

Payment and entitlement history remains intact regardless of adapter availability.
