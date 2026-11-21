# ViMi Pay — Central connection management

## Why this exists

`@vimipay_bot` previously had "My bots" and a bot-token retirement button,
but had no way to rename/delete the *project/connection* underneath it.
Projects used for E2E tests remained in the list after their bot was detached.
A network owner with 50–100 bots also needs paginated discovery.

## Owner UX

After either native Telegram Managed Bot creation or BotFather token onboarding,
the central bot sends **one success card** with (1) a green link opening the
new customer bot to finish channel/payment/plan setup, and (2) a blue
**Manage connection** callback targeting the exact newly created project.
Management is **not** opened automatically on registration. Telegram's
mandatory system reply keyboard for managed creation is removed and its
short cleanup message is deleted after the success card is sent.

Connection success and management cards have **Rich HTML** and **classic HTML**
fallback, RU/EN localization, contextual instructions, and no technical IDs
in user-facing text. The management view groups up to two actions per row:
green for opening/checking, blue for settings/navigation/invoices, red for
protected destructive operations. The existing bot retirement, archive,
RLS owner authorization, and payment history semantics are unchanged.

In central **Мои боты · личный режим → Мои подключения**, each connection
opens one compact **Управление подключением** card:

- Open attached Telegram bot, if present.
- Connect/rotate its token (original verified-token workflow).
- Rename the **internal ViMi Pay connection label** using a private ForceReply.
  This does **not** change the bot's Telegram username or BotFather metadata,
  customer-facing plan names, prices, subscriber access, or financial history.
- Detach a still-connected bot using the **existing** protected, asynchronous
  bot-retirement flow. Pending payments, active subscriptions and grants are
  hard blockers. The project's configuration and history survive retirement.
- After bot retirement finishes, the now-unconnected project can be
  permanently removed **only if there is no customer, payment, access, audit or
  other operational history whatsoever**. A two-click confirmation precedes
  deletion. Config-only plans, payment methods and resource associations are
  removed by database cascades, not by manual maintenance SQL.
- If records exist, show **Архивировать** instead. Archive hides the project
  from the active list without deleting any history, and only after its bot
  has been detached and live obligations are settled. Archive can be restored
  from **Архив подключений**.

The active list paginates 10 per page, including networks of 100+ bots. Archive
list is likewise paginated. No extra Telegram native menu commands.

## Security and data integrity

- **Actual project owner only** may rename, permanently remove, archive or
  restore a connection. Collaborator/manager role alone is not enough.
- All operations require an explicit `project_id`; stale callbacks targeting
  another selected project are rejected before any write.
- Every mutation uses tenant-scoped PostgreSQL RLS and locks the project row.
  Delete/archive also acquire the existing checkout-retirement transaction
  lock, serializing with new payment requests and bot retirement.
- No direct project deletion while a `BotInstance` exists (including
  disabled/deleting). The supervisor must complete its own safe retirement
  first. That avoids deleting a credential while a poller is still running.
- Permanent deletion checks for **any** payment request, payment, customer,
  subscription, entitlement, access state/action, Telegram join ticket,
  outbox event or owner obligation audit row — including expired, rejected and
  resolved rows. No financial history is silently purged.
- Audit-only/financial-history projects are **archived**, not purged.
- PostgreSQL immutably retained platform usage events are not erased.
- The network profile belongs to the Telegram owner, not the source project;
  removing a source connection does not delete saved reusable settings.
- No new schema or migration is necessary. Current `projects.status='archived'`,
  existing RLS policies and FK cascades are reused.

## Rollout

Merge only after exact HEAD CI, Codex review, 0 unresolved threads, mergeable,
and explicit owner approval. Only central `bot` container needs rebuilding
for the UI and owner actions; customer-supervisor, PostgreSQL, and
`compose.light100.yaml` do not need to be restarted.

Staging verification: visit a disconnected `ViMi Pay E2E` connection,
rename it and then delete it via explicit confirmation. Then verify the
remaining bots continue responding. For a project with history, verify
archiving and restoring — do not invent financial activity or delete real
records to create a test.
