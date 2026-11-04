# ViMi Pay — Premium Telegram UX and navigation contract

Status: adopted UX direction; rollout in coherent slices. The initial inventory
was performed against `main` after PR #42. Staging requires acceptance, not
just green unit tests.

## Core principles

- **No dead ends:** Every screen must expose a next useful action, a back action
  when it has a parent, or a persistent Telegram command that returns to the
  role's home screen. Never instruct users to retype `/start` as the only exit.
- **Button-first and calm:** Prefer compact inline keyboards over piles of
  free-form text; label actions with clear verbs; at most two short choices on
  one row. Edit the current screen when it is safe; retain immutable receipts,
  payment confirmations and audit notifications as separate messages.
- **Native Telegram command menu**: use `setMyCommands` and private-chat
  scopes, with RU/EN descriptions and role-aware menus. This is the bottom-left
  Telegram **Menu**, not a persistent ReplyKeyboard that obscures the text
  input. Telegram's own bot guidelines: https://core.telegram.org/bots/guidelines
  and https://core.telegram.org/bots/api#setmycommands.
- **True role boundaries:** central Platform Owner, central registrar,
  connected tenant owner and buyer have different entry points. Every action
  must still check authorization; menus are only a convenience, not a policy.
- **Locales:** command *identifiers* and callback payloads are stable and
  language-agnostic. Visible labels follow the user's explicit RU/EN choice,
  otherwise their Telegram language. Owner-created plan names and payment
  instructions must remain unchanged.
- **Data safety:** navigating away from an unsubmitted checkout or a ForceReply
  receipt does **not** cancel an existing payment request or remove paid access.
  An explicit user-confirmed cancellation and the existing transaction service
  are the only ways to change payment state. No automatic refunds.
- **Old keyboards:** validate IDs/ownership again on tap. If a button is stale,
  show a friendly message with a route to the current section. Never accept
  arbitrary callback IDs as permissions or mutate financial state on back.
- **No automatic support identity disclosure:** project owner Telegram IDs and
  usernames are not exposed to purchasers by default. Support contact can be
  explicitly configured later; until then, explain how to reach the relevant
  channel administration without inventing a contact.

## Navigation audit: 2026-11-04

| Role | Current flow / dead end | Required UX | Priority |
| --- | --- | --- | --- |
| Buyer | `/start` and `/plans` only; native menu missing | `/menu`, catalog, account, help, language shortcuts | P0 |
| Buyer | Catalog has currency and plan selection, offer has Back | Add Home/Account access; preserve selected currency and page | P1 |
| Buyer | `buy:` creates waiting_confirmation and shows only paid/cancel | Home/Account without financial cancellation; resume receipt | P0 |
| Buyer | Receipt ForceReply expects attachment; no explicit exit | `/menu`/account, clear exit instructions, preserve pending request | P0 |
| Buyer | Receipt submitted or rejected/approved notification is plain text | Return to Account or Plans; show request/subscription state | P0 |
| Buyer | No `/subscriptions` handler | Read-only account, active/expired access and payment-request states | P0 |
| Buyer | No `/support` route | Help entry, no fake support link; future explicit contact setting | P1 |
| Tenant owner | `/start` dashboard, but native menu absent | `/menu`, plans, payment reviews, help, language, cancel | P0 |
| Tenant owner | ForceReply token/channel/price/method/timezone | Inline Cancel + safe flow reset, Back to parent after each outcome | P0 |
| Tenant owner | Duration selection, resource assignment | Back and Cancel, invalidate stale wizard callbacks | P0 |
| Tenant owner | Chat picker opens ReplyKeyboard and waits | Clean keyboard on Cancel/finish/error; return to channels | P0 |
| Tenant owner | Help text has no keyboard | Back to dashboard + relevant direct setup buttons | P1 |
| Tenant owner | Review empty/success texts sometimes have no route | Reviews ↔ admin dashboard; no re-approval on stale cards | P1 |
| Central registrar | Connected bot list/connection/obligations may require /start | Native /menu,/bots,/help,/language; safe registry navigation | P0 |
| Platform Owner | Report sections mostly have Back; no native menu | /platform, /bots, /menu, /help; compact report navigation | P1 |
| Both bots | Selecting language advises typing /start | Return to previously selected role home / dashboard | P1 |
| All | Old callbacks show only alert | Refresh/current-section button where safe | P2 |

## User-visible command menus (no fake commands)

- Customer: `menu`, `subscriptions`, `support`, `language`, `cancel`.
- Tenant administrator: `menu`, `plans`, `payments`, `help`,
  `language`, `cancel`.
- Central registrar: `menu`, `bots`, `help`, `language`.
- Platform Owner: `platform`, `menu`, `bots`, `help`, `language`.

The command menu can use a `BotCommandScopeChat` override per user so an
English-speaking admin sees English descriptions even if their Telegram app
language remains Russian. Public scope defaults to English with Russian
language-specific descriptions. A revoked owner's formerly visible command
must still fail server-side authorization.

## Acceptance

1. With an English buyer, open the native Menu and independently enter
   catalog, My subscriptions/purchases, Support and Language.
2. Start a *controlled* checkout and leave it via Menu without cancelling
   money. Reopen the pending request and attach the receipt only if a real
   transfer was made; verify state and no duplicate request created.
3. Confirm catalog → offer → payment method → instruction has Back to its
   parent and a route home. A tap on an old keyboard is safe.
4. With tenant owner, native Menu shows admin sections and wizard cancel
   invalidates any old ForceReply. Connected bot owner cannot use buyer
   purchases by default until acting as an authorized buyer (roles remain
   separated).
5. Central Platform Owner can switch platform vs personal mode without
   accidentally inheriting project privileges.
6. RU/EN switch updates *both* message labels and native command descriptions.
7. Run both permission/tenant-isolation tests and regression of
   approval/rejection, access invitations and bot removal. Do not alter
   production before staging acceptance.

## Delivery slices

- **UX-A:** Native scoped RU/EN command menus, customer account and
  navigation in purchase/receipt/notification flows; central entry shortcuts.
- **UX-B:** Tenant admin wizard Cancel/Back and chat-picker cleanup,
  consistent admin menu and help/empty-state navigation.
- **UX-C:** Central/platform report and registry navigation, stale callback
  refresh, explicit customer support configuration and final polish.
- **UX acceptance:** E2E buyer and owner paths through staging, then promote
  only after permission/finance regressions pass.
