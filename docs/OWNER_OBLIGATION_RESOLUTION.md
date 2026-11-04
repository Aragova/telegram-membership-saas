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
