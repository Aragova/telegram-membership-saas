# Telegram Access Integration

## Scope

This module is the Telegram-specific edge of the generic access runtime.

The domain remains provider-neutral:

```text
EntitlementGrant
 -> EffectiveAccess
 -> AccessState (desired vs actual)
 -> AccessAction
 -> Telegram access worker
 -> Telegram Bot API
```

Telegram handlers and workers must not create commercial state. They only execute or confirm
access already decided by the entitlement/access domain.

## Grant flow

A Telegram `GRANT` does not mean that creating an invite link has already granted membership.

For a user who is not already a member:

1. the access worker claims the `AccessAction`;
2. the Telegram adapter removes a previous ban if necessary;
3. it resolves the resource to Telegram's canonical numeric chat ID;
4. it creates a short-lived invite link with `creates_join_request=True`;
5. the link name contains the project UUID hex only for routing;
6. a tenant-scoped `TelegramJoinTicket` is committed with the expected user, numeric chat ID,
   invite link and claim generation;
7. only after that commit is the link sent to the exact Telegram user;
8. `AccessState.actual_state` remains unchanged while the request is pending;
9. on a join request, the service checks the exact ticket, Telegram user ID, chat ID, expiry,
   and current desired access;
10. only then does the bot approve the join request;
11. the original access action is completed and actual access becomes `GRANTED`.

The invite name is not authorization. It is only a routing hint. Authorization comes from the
ticket and current access state.

If the user is already a current chat member, no invite is needed and the action can complete
immediately.

## Revoke flow

A Telegram `REVOKE` uses `banChatMember` through aiogram. This removes the current member and
prevents rejoining until a future grant explicitly unbans the user.

Pending join tickets for the same customer/resource are closed in the database. Even if Telegram
cannot immediately revoke an old invite link, a closed ticket cannot authorize another join.

## Invite expiry and retries

Invite links are deliberately short-lived. A pending ticket keeps the access action in
`PROCESSING`; sending a link alone is not success.

When a ticket expires before approval:

- the ticket becomes `EXPIRED`;
- the matching claim generation becomes retryable when it is still current;
- a later worker attempt can issue a fresh invite;
- the ticket records the new claim generation for fencing.

The Telegram worker lease defaults to the invite TTL plus a safety margin, so a healthy pending
join flow is not reclaimed before its invite normally expires.

## Concurrency and late outcomes

External Telegram calls are made outside database transactions.

The access runtime remains the source of concurrency control:

- claim generations fence stale failures and stale invite results;
- an older worker cannot overwrite a ticket already refreshed by a newer claim generation;
- late success is still observed as a real external side effect;
- if desired access changes while Telegram approval is in flight, completing the old GRANT updates
  actual access and immediately queues the corrective REVOKE;
- expired uncertain worker side effects are reconciled by the generic access runtime.

## System project scope

Background workers and Telegram join-request ingress do not have an interactive project member
identity. They use `system_project_session_scope(project_id)`.

This scope:

- is only for trusted internal worker/Telegram ingress paths;
- does not disable PostgreSQL RLS;
- binds `app.current_project_id` to exactly one project;
- clears the interactive `app.current_user_id`;
- must never replace normal authenticated tenant sessions for user/API actions.

## OWNER_SERVICE

`EntitlementSource.OWNER_SERVICE` is non-commercial access.

For the MVP, `ensure_owner_service_access()` idempotently gives the project owner a permanent
entitlement for an active Telegram resource:

- `source = OWNER_SERVICE`;
- `valid_until = NULL`;
- no PaymentRequest;
- no Payment;
- no Subscription;
- no revenue attribution.

`ensure_owner_service_for_project()` reconciles this across all active Telegram resources in a
project. The broader service-account assignment model described in `ARCHITECTURE.md` can be added
later without changing the entitlement/access chain.

## Secrets and logging

Telegram bot tokens stay in deployment secret storage (`TMS_TELEGRAM_BOT_TOKEN`) and must never
be committed or logged.

Do not log invite links as durable business data outside the tenant-scoped join ticket. Avoid
logging bot tokens, private customer data or full exception payloads that may contain secrets.
