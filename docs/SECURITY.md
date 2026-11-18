# Security Baseline

## 1. Repository visibility

This repository is public. Treat every committed byte as public information.

Never commit:

- Telegram bot tokens;
- API keys;
- passwords;
- private keys;
- `.env` files with secrets;
- database credentials;
- VM/SSH credentials;
- real bank/card/account details;
- real customer personal/financial data;
- production webhook secrets;
- cloud credentials.

Use placeholders and documented environment variable names only.

## 2. Secret handling

Secrets must live outside git in environment/secret storage appropriate to the deployment platform.

Bot/provider credentials should be referenced indirectly where practical (`credential_ref`) and must never appear in structured logs, traces, exception messages or fixtures.

Support rotation/revocation.

## 3. Tenant isolation

Project/Tenant is a security boundary.

Required layers:

1. authenticated identity;
2. project membership check;
3. permission check;
4. object-to-project ownership check;
5. repository/query scoping;
6. PostgreSQL Row Level Security for tenant-owned tables where feasible.

Never rely only on a client-provided `project_id` or object ID.

## 4. Authorization

Deny by default.

UI roles can be OWNER, ADMIN, PAYMENT_MANAGER, SUPPORT, VIEWER, but domain actions check explicit permissions.

Examples:

- `payment.view`
- `payment.approve`
- `payment.reject`
- `plan.edit`
- `subscriber.grant`
- `subscriber.revoke`
- `analytics.view`
- `admins.manage`
- `project.delete`

Every Telegram callback or API request must re-check current authorization; never trust the fact that a button was previously rendered.

## 5. State transition security

Important transitions must validate current state atomically.

Examples:

- only WAITING_REVIEW can become APPROVED/REJECTED;
- only one approval may produce business effects;
- repeat callbacks/webhooks are idempotent.

## 6. Payment confirmation

ManualSettlement does not trust screenshot/receipt content.

The administrator independently verifies the real incoming balance in the external bank/payment app and then approves/rejects.

The platform does not store, OCR, analyze or archive the receipt file.

## 7. File handling

For ManualSettlement confirmation, keep handling inside Telegram where possible. Do not introduce receipt storage/object storage unless product requirements change and a separate security review is completed.

## 8. Database

- PostgreSQL should not be publicly exposed unless strictly required and protected.
- Use least-privilege DB roles.
- Use migrations for schema changes.
- Encrypt transport to remote DBs.
- Backups must be protected as sensitive data.
- Test restores periodically.

## 9. Application logging

Logs should be useful for operations but must not leak secrets or unnecessary personal data.

Log important security/business actions:

- payment approve/reject;
- permissions/admin changes;
- manual access grants/revokes;
- pricing changes;
- critical project configuration changes;
- repeated failed authorization attempts where useful.

Audit log should be append-oriented and separated conceptually from debug/application logs.

## 10. Telegram access

Prefer temporary invite links/join requests over long-lived reusable invite links.

Before granting join, verify expected Telegram identity and current Effective Access.

Loss of required bot admin rights should produce a visible operational error/alert rather than silently corrupt access state.

## 11. External calls

Telegram and payment providers are external systems and may fail or retry.

Use:

- timeouts;
- retry/backoff;
- idempotency;
- transactional outbox for critical external side effects;
- reconciliation for desired vs actual access state.

## 12. Dependency and CI security

CI should eventually include at least:

- unit/integration tests;
- lint/format checks;
- dependency vulnerability scanning where practical;
- secret scanning where available;
- migration checks.

Never print environment secrets in CI output.

## 13. Staging vs production

Use isolated credentials, bots, databases and environments.

Staging must never receive production secrets just for convenience.

Production rollout requires:

- backups;
- monitoring;
- error tracking;
- documented rollback/recovery path;
- secure secret injection.

## 14. Security review triggers

Revisit this document before introducing any of the following:

- automatic card/bank payment processing;
- Stripe/YooKassa/PayPal or other provider webhooks;
- crypto custody or automatic crypto settlement;
- receipt/file storage;
- OCR/AI processing of financial documents;
- web dashboard authentication;
- public REST API;
- user-uploaded arbitrary files;
- cross-platform access providers;
- KYC/payout functionality.

## 13. Optional Telegram user-session revenue reads (PR #193)

The channel Stars revenue API is **USER MTProto only**, and user sessions are
not read-only at the Telegram protocol layer. One isolated optional container
may hold an operator-authorized session for an explicitly allowlisted channel
they own. This connection is disabled by default; it may **never** be used
to collect users' QR login codes, 2FA passwords or sessions through any
ViMi Pay bot. No Telegram user credentials may be stored in PostgreSQL,
customer-supervisor settings, public API, Git, CI fixtures or chat messages.

The separate sidecar reads its credentials from secret mounts, authorizes
its expected user ID, checks Telegram channel creator permissions for every
read, uses only non-mutating MTProto calls and never exposes a host port.
Customer supervisors hold only an independent bridge key, verify the local
project owner / Telegram channel creator, reject stale or mismatched data,
and present unknown balances/statuses as unavailable.

The opt-in session remains an account-wide credential and must be revoked
in Telegram Settings → Devices on disconnection. This limited first-stage
integration is not approval for multi-tenant session custody. Before onboarding
third-party channel owners, require a separate architecture, security review,
explicit informed consent, per-owner revocation, and audit controls.
