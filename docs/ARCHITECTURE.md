# Architecture Blueprint

## 1. Style

Use a modular monolith. Do not introduce microservices unless real scale/organizational constraints justify them later.

## 2. Layers

### Interfaces

- Telegram Bot (first interface)
- FastAPI HTTP endpoints/webhooks
- Future: Web Dashboard, REST API, Mini App

### Application Core

- Projects/Tenants
- Resources
- Plans
- Pricing
- Promotions
- Payment Methods
- Payment Requests
- Payments
- Subscriptions
- Entitlements
- Access
- Notifications
- Permissions
- Audit

### Adapters

Payment adapters:

- ManualSettlement (MVP)
- Telegram Stars (future)
- Stripe (future)
- YooKassa (future)
- other providers

Access adapters:

- Telegram (MVP)
- future Discord/Web/etc.

### Persistence

PostgreSQL is the primary source of business truth.

## 3. Module boundaries

Suggested packages:

```text
app/
  identity/
  tenants/
  projects/
  bots/
  resources/
  plans/
  pricing/
  promotions/
  payment_methods/
  payment_requests/
  payments/
  subscriptions/
  entitlements/
  access/
  admins/
  permissions/
  notifications/
  audit/
  localization/
  workers/
```

Telegram handlers must not own domain logic.

Bad:

```python
async def approve_payment(callback_query):
    # all business logic here
```

Preferred conceptually:

```text
Telegram callback
 -> ApprovePaymentRequest command
 -> application service
 -> domain transition
 -> persistence/outbox
```

## 4. Core domain chain

```text
PaymentRequest
  -> Payment
  -> Subscription
  -> EntitlementGrants
  -> EffectiveAccess
  -> Outbox
  -> Worker
  -> AccessAdapter
  -> ActualAccess
```

Never collapse this into a single `paid_until` field.

## 5. Multi-tenancy

Security boundary: Project/Tenant.

Preferred starting model:

- one PostgreSQL cluster/database;
- shared tables;
- explicit project/tenant ownership;
- PostgreSQL Row Level Security for tenant-owned data;
- application-level authorization as another layer.

Never trust an object ID alone. Every operation must verify project ownership and permission.

## 6. Identity model

Global Telegram identity and per-project customer are separate.

```text
telegram_users
  telegram_user_id
  username
  first_name

project_customers
  project_id
  telegram_user_id
```

One Telegram user may be a customer of many projects.

## 7. Projects

Project stores at least:

- owner/tenant reference;
- name;
- timezone (default `Europe/Moscow`);
- default language;
- status;
- timestamps.

All timestamps are stored in UTC.

## 8. Resources

MVP resource types:

- TELEGRAM_CHANNEL
- TELEGRAM_GROUP

Design resource abstraction so future types do not require rewriting Plan/Entitlement logic.

## 9. Plans and resources

Plans and resources should be many-to-many from the start (`plan_resources`), even if MVP UI initially encourages a simple one-resource plan.

This avoids a later migration when bundles are introduced.

## 10. Currency and pricing

Currency registry:

```text
currency_assets
  code
  type
  name
  symbol
  precision
  status
```

Plan prices are versioned. All currencies within a plan are peers; there is no primary currency.

Historical PaymentRequests/Payments must not be changed when a current price changes.

## 11. Payment Methods

Payment Method belongs to a Project and is linked to supported currencies.

MVP provider type: MANUAL_SETTLEMENT.

Future providers must implement a stable application interface so core Subscription/Entitlement code does not know provider-specific details.

## 12. PaymentRequest

PaymentRequest stores the frozen commercial offer accepted by the customer:

- project/customer;
- plan;
- price/version reference;
- payment method;
- currency;
- regular amount;
- discount amount;
- final amount;
- promotion reference if any;
- status;
- timestamps/reviewer.

It does NOT store a receipt/proof file.

## 13. Payment approval concurrency

Approval must be an atomic state transition. Conceptually:

```sql
UPDATE payment_requests
SET status = 'APPROVED', reviewed_at = ...
WHERE id = ... AND status = 'WAITING_REVIEW';
```

Business effects happen exactly once when the transition succeeds.

Use transaction boundaries/locking or optimistic concurrency as appropriate.

## 14. Idempotency

Any command/event can be retried. Reprocessing must not duplicate effects.

Examples:

- duplicate Telegram callback;
- duplicate future provider webhook;
- worker retry;
- HTTP retry.

The result of retrying the same logical operation must be the same as processing it once.

## 15. Subscription

Subscription stores commercial lifecycle, not direct Telegram membership.

Calendar durations must use proper calendar units (MONTH/YEAR), not a fixed day approximation.

Active renewal extends from current period end when appropriate.

## 16. Entitlement grants

Store independent reasons for access.

An entitlement grant references a customer + resource + source + validity/status.

Effective access is derived from all active grants.

## 17. Access state

Maintain desired state independently from actual external state.

Example:

```text
desired_access = false
actual Telegram state = member
```

This is a reconciliation problem, not a reason to corrupt domain truth.

## 18. Transactional Outbox

Do not perform critical external Telegram actions as the only side effect inside a DB transaction.

Approval transaction writes:

- Payment;
- Subscription changes;
- Entitlement changes;
- Outbox event(s).

Worker processes outbox after commit.

## 19. Workers

Initial worker responsibilities:

- GRANT_ACCESS;
- REVOKE_ACCESS;
- subscription expiration;
- reminders;
- promotion start/end where needed;
- reconciliation;
- retryable external actions.

No Kafka is required for MVP. PostgreSQL-backed outbox/worker is sufficient.

## 20. Access action state

Track background operations with states such as:

- PENDING
- PROCESSING
- SUCCEEDED
- FAILED_RETRYABLE
- FAILED_PERMANENT

## 21. Safe Telegram access

Prefer temporary invite links / join requests and verify the expected Telegram user ID before granting entry.

Avoid permanent reusable links where possible.

## 22. Permissions

UI roles may include OWNER, ADMIN, PAYMENT_MANAGER, SUPPORT, VIEWER.

Core authorization should check permissions such as:

- payment.view/approve/reject;
- plan.view/edit;
- subscriber.view/grant/revoke;
- analytics.view;
- admins.manage;
- project.manage/delete.

Deny by default.

## 23. Audit

Append-only audit trail for important administrative/domain changes:

- payment approved/rejected;
- prices changed;
- currencies added/removed;
- plan changed/deleted;
- manual access grant/revoke;
- administrator/permission changes;
- project settings changes.

## 24. Bot credentials

Bot tokens and future provider secrets are secret material.

Database should store a credential reference or encrypted secret mechanism, never expose secret values in logs or git.

## 25. Localization

Domain/application logic uses message keys rather than Russian strings embedded in services.

MVP can ship RU only while preserving an i18n boundary.

## 26. Sales policy boundary

Payment Provider is separate from the surface on which a sale is offered.

Keep a policy layer such as:

- sales_surface;
- provider availability;
- currency support;
- recurring capability;
- platform/policy restrictions.

This avoids hard-wiring Telegram-specific commerce rules into subscription core.

## 27. Failure philosophy

External systems can fail. Domain state must remain recoverable.

Use:

- transactions;
- idempotency;
- retries/backoff;
- outbox;
- reconciliation;
- alerts;
- manual operational recovery paths.

## 28. Preliminary technology stack

- Python 3.12+
- aiogram 3
- FastAPI
- PostgreSQL
- SQLAlchemy 2
- Alembic
- Pydantic
- pytest
- Docker

Add Redis/job queues only when a concrete need appears.