# ViMi Pay — optional plan names, automatic language menu, clean inputs

## Plan name is optional

New connected-bot plan setup starts with **duration**, not with a mandatory
title prompt. A separate "✏️ Добавить название (необязательно)" button lets
admins enter a custom name; otherwise the wizard proceeds directly to channel
and price. The database still holds a nonempty internal name based on the
subscription period (e.g. "1 месяц") for compatibility with existing
payment, history, profile-import and plan tables. The buyer-facing storefront
recognizes duration-only names across Russian and English and suppresses
the redundant 🎟️ plan title, leaving one clear 🗓️ duration and 💰 price.
True custom names still appear. When a draft duration is edited, its
generated name follows the new period; custom names remain untouched.
The actual payment checkout and the {plan} substitution in admin-defined
payment instructions use the buyer's localized period for unnamed plans,
never an old internal Russian label.

## Switching language

Selecting RU or EN updates native Telegram commands **and renders the
current real /menu screen automatically**, without requiring the user to type
/menu. The existing owner admin panel / central connections UI is reused.
Non-admin buyers see the real live subscription catalog after switching.
This is not a second independently-maintained menu.

## No sticky 'В ответ...' composer

The owner setup wizard used Telegram ForceReply on all forms; this forced
the reply-to-message composer to remain open in Telegram and overlap user
input. Plain private messages are now accepted as the next answer to the
active owner-only bot-scoped wizard step. Prompts explicitly remove stale
reply keyboard UI using ReplyKeyboardRemove. Explicit replies to the current
prompt still work. Replies to *stale* bot prompts are rejected; slash
commands never become data, and /menu cancels a pending step before opening
the correct menu. The same flows retain 15-minute TTL, row-level session
locking, project and admin permission checks, atomic consumption, rollback
on errors, and deletion of BotFather token messages.

This change does not modify checkout, manual-settlement confirmation, buyer
subscriptions, access control, VM resources, Telegram native menu command
count, or the project's database schema.

## Rollout

Review and merge only after exact-HEAD green CI, completed clean Codex
review, zero unresolved threads and explicit user approval. No DB migration
or backup required; update central `bot` and `customer-supervisor` via
`compose.light100.yaml` so menu/input UX is consistent in both bot types.
No restart of PostgreSQL, no production deploy.

Navigation callback middleware discards any active plain-text capture on exit, preserving generation-bound duration/use/back buttons. The buyer receipt flow still requires a reply to the receipt request to prevent proof being attached to the wrong purchase.
