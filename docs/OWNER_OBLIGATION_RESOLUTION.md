# Owner resolution of bot-retirement blockers

The bot deletion guard from PR #25 remains strict. This feature does not purge a
project or wipe subscribers. `@vimipay_bot` now offers **Управлять
обязательствами** when the owner opens a delete confirmation or encounters a
block. It lists the specific project-scoped pending payments, active
subscriptions, non-payment grants and access-state targets.

Every action requires explicit confirmation. Only the project owner (not a
payment manager or collaborator) can perform destructive actions. All queries
are inside PostgreSQL tenant RLS, and state transitions use the existing
checkout/retirement advisory lock.

- Unsubmitted request: `waiting_confirmation -> cancelled` using the
  original payment cancellation service. The bank transaction is **not**
  checked automatically and no refund is recorded.
- Submitted request: `waiting_review -> rejected` using the original
  payment-review service. Owner must check actual bank balance first.
- Active subscription: `active -> cancelled`, revokes only corresponding
  `payment` grants for this customer and plan across all paid periods. Old
  payment records and date snapshots remain. Other independent grants still
  count toward effective access. The user must handle paid-period settlements
  personally; ViMi Pay never claims to refund.
- Non-payment entitlement: explicit owner-confirmed revoke. It does not
  change unrelated grants.
- Access state: a per-target **Synchronize** action uses
  `reconcile_access_target`; the Telegram worker remains responsible for
  physical revocation. Action queues and pending invite tickets keep bot
  deletion blocked until resolved. Do not force-delete state to make the UI
  green.

Each actual owner change creates a row in `owner_obligation_audit`. Migration
`0017` applies RLS and allows only owner-scoped insert and read. Notifications
are best-effort from the correct customer bot; failure is shown to the owner,
not misrepresented as a rolled-back database operation. Bot tokens are never
written to logs.

## Acceptance / staging

After exact-HEAD CI and Codex review and an authorized merge:

1. Run migration 0017 and restart central bot; keep existing supervisor.
2. In central bot, select test project and try deleting the customer bot.
3. Open obligations; resolve each real request one by one, checking the bank
   before rejection or cancellation.
4. If a paid period remains, make a deliberate owner decision about early
   termination and any owed settlement. Check that independent rights survive.
5. Recheck counts and let access worker actually revoke membership; refresh
   after Telegram confirms. Never manually modify DB to fake cleanup.
6. With zero blockers, delete bot, connect it to a new Telegram-created project,
   complete configuration and run buyer E2E.

**Known guardrail:** a pending invite ticket or failed access worker may still
require resolution in the customer-bot runtime; the owner interface never marks
physical revocation complete on its own.


## BotFather deletion / undeliverable orphan access action (PR #62)

A registered connected bot may be deleted in BotFather *before* ViMi Pay
disconnects it. Its worker then cannot deliver queued Telegram access actions.
Even with no active payments, subscriptions, grants, unrevoked customer access
or pending invitations, the old retirement guard counted a `pending` action
and blocked deletion forever.

**Owner-only escape hatch**, available in the CENTRAL `@vimipay_bot` under
`Управление подключением → Отключить бота → Управлять обязательствами`.
When **only** access actions remain and the verified owner explicitly confirms
the warning, ViMi Pay can retire the unreachable bot and abandon those
undeliverable actions in one protected transaction. This is not automatic:

- The owner confirms the Telegram bot is permanently unreachable/deleted.
  Current `CustomerBotRuntimeHealth` must NOT show a recent running heartbeat.
- Recheck the actual owner, selected project and BotInstance with tenant RLS,
  transaction advisory lock and project/bot `FOR UPDATE` locks.
- All other blockers **MUST** be zero: pending payments, active subscriptions,
  grants, desired/unrevoked access and unprocessed invite tickets.
- Only unfinished `pending`, `failed_retryable`, or stale `processing`
  access actions qualify. A freshly processing Telegram action is denied.
- Preserve each action row and its old error/attempt history; record
  `status='superseded'` plus an immutable `owner_obligation_audit` entry
  naming the owner and action. **Never** update `AccessState.actual_state`
  or claim physical revocation/grant succeeded.
- In the SAME transaction, mark the BotInstance `deleting`. The existing
  supervisor must finish poller teardown before removing the encrypted token.
  A final blockers count ensures no new obligation escaped. A second click
  does not perform the operation again.
- Historical customer/access rows mean the resulting disconnected project is
  **archivable, not permanently deletable**. The owner can hide it from the
  main list via Archive after bot teardown, retaining billing and access audit
  records. Other connected bots and the central platform remain unaffected.
- Does not require Telegram `getMe` to succeed: a dead or revoked BotFather
  token cannot be used to perform a normal access cleanup. The owner makes an
  explicit, auditable declaration; this is a *termination of ViMi Pay work*,
  not verification of channel membership.

This exception does NOT let an owner abandon a genuine paid subscription or
an unrevoked customer membership to make the deletion button green. It is
strictly for orphaned, unexecutable commands after all customer obligations
are already clear. No migration is required; existing RLS/audit tables and
normal BotInstance retirement are reused. First deploy to staging, preserving
`compose.light100.yaml`; only central `bot` needs rebuild for its new UI.
