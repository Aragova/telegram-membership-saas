# ViMi Pay — Owner subscriber management (PR #55)

## Production-compatible scope

The connected owner's Telegram bot (never central `@vimipay_bot`) has a
**👥 Подписчики / Subscribers** section. This release uses existing tables
only — no migration, no change to checkout, payment approval, subscriptions,
Telegram worker, supervisor, bot tokens, or VM configuration.

### Directory

- Project-scoped, paginated customer list (8 per page), sorted by newest.
- Filters: **Все / Активные / Истекают / Истекли**. Active means there is
  an active paid subscription or an effective time-valid access entitlement.
  Expiring means a live paid subscription ends within 7 days.
  Expired means historical subscription/grant without any current paid or
  noncommercial entitlement. These are *database statuses*, not an unverified
  claim that Telegram API has already removed the member.
- Search by Telegram numeric ID or a registered `@username`.
  Telegram IDs are not treated as authentication secrets. Search is always
  scoped to the bot's own `ProjectCustomer` table, not global users.
- Cards show actual plan terms, up to 5 **confirmed historical payments per
  page**, current grants broken down by source (paid, manual, service, other)
  and Telegram desired vs recorded actual access. Historic payments remain
  available even after plan prices change or grants expire.
- All timestamps show the project's configured IANA timezone.

### Manual, noncommercial access

The **actual project owner** (not an ordinary channel collaborator) may
grant a separate entitlement for an existing customer of the bot to a
verified active channel or group, for 7 / 30 / 90 days or without an
expiry. The customer must already have opened the bot at least once:
Telegram generally cannot initiate a private conversation with a bot on
behalf of a never-seen user.

A second confirmation is mandatory. The write transaction acquires the
existing project checkout-retirement lock, checks ownership, the exact
project/customer/resource, active resource state and duplicate active manual
rights. It creates only:

- an `EntitlementGrant(source=MANUAL, valid_until=...)`;
- its normal `EffectiveAccess` recalculation and queued Telegram access
  work through the existing outbox/worker;
- an auditable owner decision.

**No `Payment`, `PaymentRequest` or `Subscription` is created, updated
or deleted.** There is no fake sale/revenue. An already active manual
entitlement on that resource cannot accidentally be duplicated; to replace
the duration, the owner first revokes that manual grant.

Revocation requires explicit confirmation and is restricted to
`source=MANUAL` and the same customer/project. Paid and `OWNER_SERVICE`
grants are never selected for this action. When another valid entitlement
remains, the access projection stays granted: a manual revoke never deletes
someone's paid Telegram access. The owner receives a status message;
Telegram physical access is completed asynchronously by the original worker,
not claimed successful at the moment of the database write.

### Security and operational behavior

- The project manager permission is required for read-only subscriber lists
  and histories. Only the verified project's owner may mutate manual access.
- Existing PostgreSQL tenant RLS continues to isolate all project reads and
  writes. Callback IDs carry compact UUIDs, but every mutation rechecks
  authorization, scope and current resource state under the transaction lock.
- The central connection registry does not expose subscriber callbacks.
  No new native Telegram `/menu` commands are introduced.
- No bulk cancellations or changes to paid subscriptions. Existing
  owner-obligation remediation remains the only deliberately exceptional
  workflow for early paid cancellations before safe bot retirement.
- No external API, extra polling service, paid VM tier, or long-lived
  per-customer database connection is required.
- Existing active bots can continue running during merge. **No staging or
  production deployment before explicit owner approval.**

## Acceptance

CI contains real PostgreSQL RLS checks on cross-project reads/writes,
search, filters, idempotent revoke, audit, resource validation, and simultaneous
paid+manual entitlements; UI checks verify compact callbacks, two-step
confirmation, role isolation, and backwards-compatible navigation.

On staging, open an existing customer card in a connected owner bot, confirm
payment history and actual vs desired access, create a *test* manual 7-day
grant in a nonfinancial test channel, then revoke that manual grant and
verify the real paid customer account remains unaffected. Do not manipulate
live paying customers merely to simulate a test.

### Additional safeguards verified by review

Manual grants recheck bot-retirement state under the existing checkout-retirement lock; once a bot is marked deleting, no new manual entitlement is created. Active access grants are paginated (8 per page), so a grant older than 30 newer paid or manual entitlements can still be found and revoked. Both cases are covered by dedicated PostgreSQL and UI regression tests.

## Stage 2 — buyer card / invoice navigation (PR after broadcasts)

- Reuse the existing `ProjectCustomer` card and `PaymentRequest` ledger.
  There is **no second customer database** and no new financial, payment
  approval, subscription or access-grant writes.
- The connected bot's existing subscriber card now shows counts of all buyer
  invoices and unpaid/review-pending invoices, plus one **Invoice history**
  action. History is paginated (8 per page) and includes unpaid, in-review,
  approved, rejected, cancelled and expired invoices, with status and the
  original request's frozen amount/currency.
- Each invoice in this buyer-scoped list opens the existing `own:invoice`
  detail. Detail displays request reference, current plan label, original
  duration and amount, payment method, creation time and status. **A new
  invoice is never represented as a confirmed payment.** From detail the
  admin can navigate back to the buyer card or invoice list.
- The existing Telegram manual-receipt review message receives a
  **Customer and invoice** shortcut below the original Approve/Reject
  actions. It is an informational shortcut only and never invokes approval.
  New-invoice notifications already link to the same screens.
- All callback reads are limited to this connected bot's project, require
  an authorized project manager, and are validated against `PaymentRequest`
  and `ProjectCustomer` in the existing tenant session with FORCE RLS.
  The central bot is not allowed to open these screens.
- Keep the manual access confirmation controls in the existing buyer card;
  do not add payment cancellation, subscription grants or refund actions to
  invoice detail. Actual payment review remains in the original safe path.
- UI RU/EN. No schema migration. Roll out the customer-supervisor only
  after exact-head checks, review, approval and staging tests.
