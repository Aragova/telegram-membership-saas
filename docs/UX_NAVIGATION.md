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

## Existing plan management — required contract

The plan card is the single admin location for prices **and** plan lifecycle;
there is no separate hidden settings menu. An owner can:

- Rename any existing plan (2–120 chars). A rename changes the label also
  shown on historical purchase screens; amounts, dates and access stay intact.
- Change duration and linked channel **on drafts only**. Changing terms or
  entitlements of an already sold plan silently is unsafe; for a different
  active offering, archive the old plan and create a new one.
- Edit prices by currency via the existing **versioned** price service.
- Archive an active plan with confirmation: it disappears from new buyer
  checkouts and preview; existing requests, payments and entitlements
  remain, and may still be reviewed and fulfilled.
- Restore an archived plan after the same readiness checks as publishing
  (active resource, currency price, matching enabled payment method).
- Permanently delete only a plan with **no historical** payment requests,
  payments or subscriptions, after a separate confirmation. This may
  include an unpublished or active-but-never-bought test plan. Existing price
  and resource links cascade with deletion; FK restrictions are a final
  safety barrier against missing checks.
- Plan buttons revalidate project membership and plan status on action.
  A stale confirmation must not delete a plan that changed state.

Keep these actions inside `Plans → Plan card`; the native Telegram Menu
remains **exactly three commands**, even for administrators. All text and
controls are RU/EN except owner-entered plan names.

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
| Tenant owner | `/start` dashboard, but native menu absent | Only three native shortcuts: main menu, my subscriptions, support; all other actions stay inline | P0 |
| Tenant owner | ForceReply token/channel/price/method/timezone | Inline Cancel + safe flow reset, Back to parent after each outcome | P0 |
| Tenant owner | Duration selection, resource assignment | Back and Cancel, invalidate stale wizard callbacks | P0 |
| Tenant owner | Chat picker opens ReplyKeyboard and waits | Clean keyboard on Cancel/finish/error; return to channels | P0 |
| Tenant owner | Help text has no keyboard | Back to dashboard + relevant direct setup buttons | P1 |
| Tenant owner | Review empty/success texts sometimes have no route | Reviews ↔ admin dashboard; no re-approval on stale cards | P1 |
| Central registrar | Connected bot list/connection/obligations may require /start | Native /menu,/bots,/help,/language; safe registry navigation | P0 |
| Platform Owner | Report sections mostly have Back; no native menu | /platform, /bots, /menu, /help; compact report navigation | P1 |
| Both bots | Selecting language advises typing /start | Return to previously selected role home / dashboard | P1 |
| All | Old callbacks show only alert | Refresh/current-section button where safe | P2 |

## Telegram native menu: exactly three commands (UX decision)

Do not copy the full inline dashboard into the Telegram command Menu.
After user feedback, the **connected bot** has exactly these shortcuts for
both buyers and tenant owners, in RU/EN:

- `/menu` — Главная / Main menu. Buyers see catalog; owners see admin panel.
- `/subscription` — Мои подписки / My subscriptions. Opens the real
  buyer account, including historical payment requests (not a fake screen).
- `/support` — Поддержка / Support. Buyers see membership support;
  owners see the project setup/help screen.

For the **central bot**, which has no buyer subscriptions, use exactly three
working shortcuts: `/menu`, `/bots`, `/help`. Platform reporting remains
accessible from the central screen or `/platform` typed manually, not
duplicated in Telegram Menu.

All other commands (`/start`, `/plans`, `/payments`, `/language`,
`/cancel`, `/platform`, etc.) **continue to work** when entered manually;
important actions remain on the inline admin/buyer screens. The menu is a
compact navigation aid, not a complete sitemap. Do not add command rows
just because a screen or command exists.

Existing per-chat Telegram command scopes are refreshed upon the next
`/start` or `/menu`, and after explicit language selection; users may
need to reopen Telegram Menu to see the changed set.


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

## Wizard navigation after PR #44

Keep the native Telegram command Menu at exactly **three shortcuts**. Back,
retry, cancel and other contextual actions belong inside the relevant screen,
not as more top-level menu commands.

For tenant administrators, navigating the *unsaved* plan wizard now has
generation-bound inline Back actions:

- Name entered → duration picker → Back to Plans (discard only unsaved input).
- Duration selected with multiple eligible channels → resource picker →
  Back to Duration (preserve the name, regenerate callback token).
- A stale duration/resource Back button **cannot cancel or replace** a newer
  wizard. Only exact step, project and generation are accepted.
- Currency picker Back leads to Payment methods instead of the dashboard;
  timezone selection Back leads to the admin panel.
- If no more currencies can be attached, Payment method offers a direct
  return to its card rather than a terminal message.
- Invalid channel picker results remove the temporary reply keyboard and offer
  a return to Channels.
- ForceReply cannot combine an inline keyboard with its reply field. Its text
  includes `/back` (return to parent section) and `/cancel` (exit to home)
  **without adding either to the native command Menu**. The pending state is
  invalidated atomically on the previous bot's scope before navigation.
- If validating a ForceReply input fails, show one contextual Back-to-section
  button. Prices return to the same plan card; no invalid input is saved.

These are display/state navigation changes; existing prices, methods,
subscriptions, access and payment requests must remain unchanged. Bot tokens
received in other-than-token prompts must still be deleted as before.

## Delivery slices

- **UX-A:** Native scoped RU/EN command menus, customer account and
  navigation in purchase/receipt/notification flows; central entry shortcuts.
- **UX-B (in progress):** Tenant admin wizard Back/Cancel and chat-picker cleanup,
  contextual retry navigation and safe generation-bound back actions.
- **UX-C:** Central/platform report and registry navigation, stale callback
  refresh, explicit customer support configuration and final polish.
- **UX acceptance:** E2E buyer and owner paths through staging, then promote
  only after permission/finance regressions pass.

## Navigation cleanup — approved after PR #57 staging acceptance (2026-11-05)

The owner explicitly requested removal of redundant buttons from the current
working customer bot. This section supersedes older audit proposals to put
"My purchases" shortcuts on every buyer screen:

- **Connected owner main dashboard:** Channels | Payments, Plans | Requests,
  Subscribers | Statistics, View as guest, **⚙️ Settings**, and **green**
  Language / Язык. No separate Notifications, Readiness, TimeZone, Network
  profile or Help entries on the dashboard.
- **⚙️ Settings:** Notifications; Readiness | TimeZone; Network profile;
  Help; Back to admin dashboard. Child screens return to Settings. Existing
  setting actions and database roles remain unchanged.
- **Buyer catalog / main menu:** Currency and buyable plans, plus one green
  Language / Язык control. If no plans exist, keep only Language. Read-only
  account history remains available via the native Telegram Menu
  `/subscription` (and existing legacy `account:` callbacks).
- **Offer / payment-method selection:** Only applicable currency, payment
  choices and **⬅️ All plans** back action. Remove My purchases and Language.
- **Created invoice:** Keep green I have paid, red Cancel request and
  Browse plans. Remove My purchases. **Never cancel on ordinary navigation.**
- **Explicit invoice cancellation:** Commit the cancellation via existing
  payment service, then show buyer home/catalog with a localized success
  notice in the **same catalog message**. Do not redirect to purchase history.
- Preserve callbacks on already-sent older messages so existing users can
  still navigate safely; do not introduce migrations or update live payments,
  subscriptions, entitlements or review mechanics.
- All newly introduced text/actions are localized in RU and EN; Telegram
  clients that don't support button styling still show the language label.
- Native Telegram Menu continues to have **exactly three** commands:
  `/menu`, `/subscription`, `/support`. Don't duplicate those commands
  as buttons throughout the purchase journey.
