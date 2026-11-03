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

A non-commercial access source may enter directly at EntitlementGrants without creating a fake Payment/Subscription.

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

Management identity, customer identity and service/content access are separate concerns even when they reference the same Telegram user.

## 7. Projects

Project stores at least:

- owner/tenant reference;
- name;
- timezone (default `Europe/Moscow`);
- default language;
- status;
- timestamps.

All timestamps are stored in UTC.

## 8. Owner / Service Accounts

The platform/network owner may operate many bots/projects and must not purchase or manually activate access for each one.

Model this as a platform-level service-access assignment, not as a commercial Plan.

Conceptually:

```text
owner_service_accounts
  id
  owner_user_id / platform_owner_id
  telegram_user_id
  scope_type        # ALL_OWNER_PROJECTS / SELECTED_PROJECTS / SELECTED_RESOURCES
  auto_include_future_projects
  status
  created_at
  revoked_at
```

The exact table shape may evolve when the Entitlement module is implemented, but these invariants are mandatory:

- owner/service access creates `EntitlementGrant(source=OWNER_SERVICE)` directly;
- permanent service access uses `valid_until = NULL`;
- it does not create PaymentRequest, Payment or Subscription;
- it does not count toward revenue, paid conversions or paid subscriber metrics;
- `ALL_OWNER_PROJECTS` may automatically cover future Projects/Resources owned by that platform owner;
- revoke of management permissions must not implicitly revoke content access, and revoke of content access must not implicitly remove management permissions;
- a project owner/admin role does not itself mean paid-content access;
- a service account may have content access without any admin permissions.

When new Projects/Resources are created, a reconciliation/projection step must materialize or otherwise make effective the required OWNER_SERVICE grants for matching assignments. This should be idempotent.

## 9. Resources

MVP resource types:

- TELEGRAM_CHANNEL
- TELEGRAM_GROUP

Design resource abstraction so future types do not require rewriting Plan/Entitlement logic.

## 10. Plans and resources

Plans and resources should be many-to-many from the start (`plan_resources`), even if MVP UI initially encourages a simple one-resource plan.

This avoids a later migration when bundles are introduced.

## 11. Currency and pricing

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

## 12. Payment Methods

Payment Method belongs to a Project and is linked to supported currencies.

MVP provider type: MANUAL_SETTLEMENT.

Future providers must implement a stable application interface so core Subscription/Entitlement code does not know provider-specific details.

## 13. PaymentRequest

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

## 14. Payment approval concurrency

Approval must be an atomic state transition. Conceptually:

```sql
UPDATE payment_requests
SET status = 'APPROVED', reviewed_at = ...
WHERE id = ... AND status = 'WAITING_REVIEW';
```

Business effects happen exactly once when the transition succeeds.

Use transaction boundaries/locking or optimistic concurrency as appropriate.

## 15. Idempotency

Any command/event can be retried. Reprocessing must not duplicate effects.

Examples:

- duplicate Telegram callback;
- duplicate future provider webhook;
- worker retry;
- HTTP retry;
- repeated service-account reconciliation for a newly created project/resource.

The result of retrying the same logical operation must be the same as processing it once.

## 16. Subscription

Subscription stores commercial lifecycle, not direct Telegram membership.

Calendar durations must use proper calendar units (MONTH/YEAR), not a fixed day approximation.

Active renewal extends from current period end when appropriate.

## 17. Entitlement grants

Store independent reasons for access.

An entitlement grant references a customer + resource + source + validity/status.

Expected sources include PAYMENT, MANUAL, OWNER_SERVICE, PROMOCODE, TRIAL, REFERRAL, IMPORT and COMPENSATION.

`valid_until = NULL` means the grant is permanent until explicitly revoked.

Effective access is derived from all active grants.

## 18. Access state

Maintain desired state independently from actual external state.

Example:

```text
desired_access = false
actual Telegram state = member
```

This is a reconciliation problem, not a reason to corrupt domain truth.

## 19. Transactional Outbox

Do not perform critical external Telegram actions as the only side effect inside a DB transaction.

Approval transaction writes:

- Payment;
- Subscription changes;
- Entitlement changes;
- Outbox event(s).

Service-account assignment/materialization may similarly create entitlement changes + outbox events without any payment artifacts.

Worker processes outbox after commit.

## 20. Workers

Initial worker responsibilities:

- GRANT_ACCESS;
- REVOKE_ACCESS;
- subscription expiration;
- reminders;
- promotion start/end where needed;
- reconciliation;
- retryable external actions;
- service-account access reconciliation for current/future resources.

No Kafka is required for MVP. PostgreSQL-backed outbox/worker is sufficient.

## 21. Access action state

Track background operations with states such as:

- PENDING
- PROCESSING
- SUCCEEDED
- FAILED_RETRYABLE
- FAILED_PERMANENT

## 22. Safe Telegram access

Prefer temporary invite links / join requests and verify the expected Telegram user ID before granting entry.

Avoid permanent reusable links where possible.

## 23. Permissions

UI roles may include OWNER, ADMIN, PAYMENT_MANAGER, SUPPORT, VIEWER.

Core authorization should check permissions such as:

- payment.view/approve/reject;
- plan.view/edit;
- subscriber.view/grant/revoke;
- analytics.view;
- admins.manage;
- project.manage/delete.

Deny by default.

Management permissions must remain separate from Entitlement/content access.

## 24. Audit

Append-only audit trail for important administrative/domain changes:

- payment approved/rejected;
- prices changed;
- currencies added/removed;
- plan changed/deleted;
- manual access grant/revoke;
- owner/service account added/revoked/scope changed;
- administrator/permission changes;
- project settings changes.

## 25. Bot credentials

Bot tokens and future provider secrets are secret material.

MVP bring-your-own-bot architecture:

- The central ViMi Pay bot manages project owners and their settings; individual
  customer-facing Telegram bots belong to a specific tenant Project.
- `BotInstance` contains globally unique Telegram bot ID, project ID, username,
  display metadata, `registered/disabled` state and Fernet-encrypted token.
- The first registration slice performs bot `getMe`, owner authorization and
  encrypted persistence, but does not start a dispatcher; registered is not running.
- Bot credentials must be encrypted using a separately provisioned secret,
  never echoed into chats or logs, and never committed. Incoming BotFather token
  messages are deleted before validation or database writes; Telegram private
  chats still are not end-to-end encrypted.
- The future multi-bot runtime must resolve an incoming bot identity to the
  exact Project before handling buyers, and must enforce tenant context for
  every request, callback, access event and worker job. A project may have
  multiple resources; bot/runtime routing may not rely on a global fixed
  `TMS_TELEGRAM_PROJECT_ID` once onboarding is enabled.
- Do not expose the buyer link until registered bots have a working dispatcher
  and a tested Telegram end-to-end flow. Plan credentials rotation and recovery.

Database stores a credential reference or encrypted secret mechanism, never
exposes secret values in logs or git.

## 26. Localization

Domain/application logic uses message keys rather than Russian strings embedded in services.

MVP can ship RU only while preserving an i18n boundary.

## 27. Sales policy boundary

Payment Provider is separate from the surface on which a sale is offered.

Keep a policy layer such as:

- sales_surface;
- provider availability;
- currency support;
- recurring capability;
- platform/policy restrictions.

This avoids hard-wiring Telegram-specific commerce rules into subscription core.

## 28. Failure philosophy

External systems can fail. Domain state must remain recoverable.

Use:

- transactions;
- idempotency;
- retries/backoff;
- outbox;
- reconciliation;
- alerts;
- manual operational recovery paths.

## 29. Preliminary technology stack

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