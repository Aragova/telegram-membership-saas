# Development Workflow

## 1. Goal

Keep development incremental, reviewable and reproducible. Avoid giant PRs that mix architecture, infrastructure and product behavior.

## 2. Standard flow

```text
GitHub issue / agreed slice
 -> feature branch
 -> implementation
 -> tests
 -> PR
 -> CI
 -> Codex/exact-head review
 -> resolve all review threads
 -> verify exact HEAD + mergeability
 -> explicit owner approval
 -> merge
 -> next slice
```

## 3. Merge policy

Never merge a PR without explicit owner approval.

Before merge verify all of the following:

1. PR HEAD SHA is known.
2. CI completed successfully for that exact HEAD.
3. Codex/review applies to that exact HEAD.
4. Unresolved review threads = 0.
5. PR is mergeable.
6. No hidden/uncommitted follow-up is required for the slice acceptance criteria.

If HEAD changes, previous CI/review is considered stale until revalidated.

## 4. PR sizing

Prefer small logical slices.

Examples of appropriate early PRs:

- PR #1 Foundation: project skeleton, configuration, Docker, DB connection, migrations, test/CI skeleton.
- PR #2 Tenant/Project foundation.
- PR #3 Resource and BotInstance model.
- PR #4 Plans/Currency/Versioned Pricing.
- PR #5 ManualSettlement/PaymentRequest domain.
- PR #6 Approval/Payment/Subscription.
- PR #7 Entitlement Grants/Effective Access.
- PR #8 Telegram access worker/outbox/reconciliation.
- PR #9 Owner/admin Telegram UX.
- PR #10 Customer purchase flow.

Exact slicing may change if implementation reveals a better dependency order.

## 5. First production-code principle

Do not implement UI before domain boundaries are clear. Conversely, do not over-design speculative abstractions without a concrete MVP use case.

Each slice should include the minimum architecture needed for the feature plus tests proving domain invariants.

## 6. Testing expectations

Critical scenarios must have automated tests where practical:

- duplicate approve -> only one Payment/effect;
- two admins approving concurrently -> one winner;
- price change after PaymentRequest -> request amount unchanged;
- promotion ends after PaymentRequest -> request amount unchanged;
- active subscription renewal extends from current period end;
- one entitlement expires while another remains -> no revoke;
- tenant A cannot access tenant B object;
- worker retry does not duplicate grant/revoke;
- outbox survives process failure after DB commit;
- timezone/calendar duration cases;
- permission denial cases.

## 7. Documentation discipline

When a PR changes a product invariant or architecture decision, update the relevant document in the same PR:

- `docs/PROJECT_CONTEXT.md`
- `docs/CONCEPT.md`
- `docs/ARCHITECTURE.md`
- `docs/SECURITY.md`

New chats should never need old conversation history to understand current project truth.

## 8. Branching

Use short-lived feature branches from current `main`.

Suggested naming:

- `feat/foundation`
- `feat/projects`
- `feat/pricing`
- `feat/manual-settlement`
- `feat/entitlements`
- `fix/...`
- `docs/...`

Avoid long-lived parallel branches that drift far from main.

## 9. Staging

Do not treat the first VM deployment as production.

After enough MVP pieces exist, deploy a dedicated staging environment:

```text
main
 -> staging VM
 -> staging database
 -> test Telegram bot
 -> test private channel/group
```

Run real end-to-end scenarios:

- create/configure project;
- create plan/currencies/prices;
- create payment method;
- customer purchase flow;
- submit screenshot/document;
- admin approve/reject;
- grant access;
- renewal;
- expiration;
- revoke;
- concurrent/duplicate actions;
- Telegram temporary failure/retry;
- loss/restoration of bot admin permissions.

Use test/fake payment data. Do not expose real banking credentials in git or test fixtures.

## 10. Production promotion

Production comes only after staging is stable and acceptance scenarios pass.

Production must have separate:

- bot token(s);
- database;
- environment/secrets;
- deployment configuration;
- backups;
- monitoring/error tracking.

Never reuse staging credentials as production credentials.

## 11. VM operations

Server changes should be executed as small logical steps. After each material staging/production command, inspect output before continuing.

Never ask users to paste secrets/tokens into chat. Secrets should be entered directly into secure environment/secret storage on the target system.

## 12. Definition of done for a slice

A slice is done when:

- implementation matches acceptance criteria;
- tests pass;
- migration/state changes are reproducible;
- documentation is current;
- exact HEAD CI/review is clean;
- unresolved threads are zero;
- owner explicitly approves merge.
