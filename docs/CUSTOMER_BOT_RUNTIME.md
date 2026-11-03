# Customer bot runtime — first branded buyer bot slice

Source of truth: [CONCEPT.md](CONCEPT.md) v1.5. Owner manages projects
through central ViMi Pay; customers must buy through the owner's BotInstance.

## What this slice runs

The `customer-bot` Docker profile starts a separate `tms-customer-bot`
polling process. It loads one registered `BotInstance` *inside the specified
project's system RLS scope*, decrypts the token inside the worker using the
existing Fernet key, verifies the bot's real Telegram ID via `getMe`,
checks absence of an existing webhook (does **not** delete foreign hooks), and
verifies that the bot has both invite and ban/restrict permissions in every
active Telegram channel/group in the project.

The customer Dispatcher has **only** purchase/payment and channel-join
handlers, not owner token management. Existing review/receipt/approval services
are reused in the same project-scoped transactions and the same Telegram bot
sends the resulting access invitations. Incoming join requests are restricted
to the worker's exact project, including when an invite is forged for another
project. The project access queue/revocation loop runs under this customer
bot's Telegram identity.

When the customer bot is deployed, central management continues in a separate
`bot` container with `TMS_TELEGRAM_CENTRAL_ACCESS_ENABLED=false`.
This disables **both** customer commerce and the old central access worker
and join-request listener, preventing two bots competing for the same
project's access queue. The owner management screens remain available. The
central `Заявки` button and old `/payments` command now direct the owner
to the customer bot's `/payments`; old central approve/reject inline
buttons display an explicit redirect instead of silently failing or sending
customer notifications through the wrong bot. No payment is changed by
these old buttons. The legacy combined central bot remains the default
until staging explicitly performs the cutover.

## Important limitations

This first slice handles **one registered customer bot per project**;
startup fails if the project has none or more than one registered bot.
The underlying access actions are project/resource-scoped and do not yet
identify the individual BotInstance, so multiple active bot workers per
project could send invitations/revocations with the wrong bot. This failure
is deliberate, not a hidden per-bot limit in the final product.

Each project deployment currently requires configuration of
`TMS_TELEGRAM_PROJECT_ID` and `TMS_CUSTOMER_TELEGRAM_BOT_ID`. The full
self-service multi-project scheduler, bot-specific access routing,
token rotation/restart/recovery and live health statuses remain mandatory
future steps. Registering a BotInstance does not imply that it's running.
Do not expose a customer's buy link as ready before a complete staging E2E.

## Staging checklist — *only after authorized merge*

1. Preserve current DB and .env backups; do not reveal any credentials.
2. Inspect the registered BotInstance ID and ensure precisely **one**
   registered customer bot belongs to the intended test project.
3. Add the customer bot as an admin of all project channels/groups with
   invitation and user-ban rights. Owner starts a private chat with the
   customer bot to receive receipt review messages.
4. Configure `TMS_CUSTOMER_TELEGRAM_BOT_ID` using numeric ID from the
   registered row, not the bot token; ensure Fernet key remains the same.
5. Set `TMS_TELEGRAM_CENTRAL_ACCESS_ENABLED=false`, rebuild/restart the
   central bot first. Verify that `/start` still opens the owner's menu.
6. Start `customer-bot` and check stable logs (no token dump) and
   buyer `/start` in the **owner's** bot, not central ViMi Pay.
7. Submit test proof, have the owner approve inside their customer bot,
   verify new subscription and invitation, auto-join and revocation.
8. Verify wrong-account and wrong-project join requests are denied.
9. If rollback needed, stop customer-bot **first**, restore central access
   worker flag, restart central bot. Never leave both project workers
   simultaneously active.

The owner-facing main menu should eventually have a real live/runtime
health view. A project with multiple bots must be supported by extending
access routing before removing the fail-closed limitation.
