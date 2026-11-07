# ViMi Pay — Owner subscriber management (PR #55)

## Production-compatible scope

The connected owner's Telegram bot (never central `@vimipay_bot`) has a
**👥 Подписчики / Subscribers** section. This release uses existing tables
only — no migration, no change to checkout, payment approval, subscriptions,
Telegram worker, supervisor, bot tokens, or VM configuration.

### Directory

- Project-scoped, paginated customer list (8 per page), sorted by newest.
- Filters: **Все / Активные / Истекают / Истекли**. Active means there is
  an active paid subscription or an effective time-valid access entitlement.
  Expiring means a live paid subscription ends within 7 days.
  Expired means historical subscription/grant without any current paid or
  noncommercial entitlement. These are *database statuses*, not an unverified
  claim that Telegram API has already removed the member.
- Search by Telegram numeric ID or a registered `@username`.
  Telegram IDs are not treated as authentication secrets. Search is always
  scoped to the bot's own `ProjectCustomer` table, not global users.
- Cards show actual plan terms, up to 5 **confirmed historical payments per
  page**, current grants broken down by source (paid, manual, service, other)
  and Telegram desired vs recorded actual access. Historic payments remain
  available even after plan prices change or grants expire.
- All timestamps show the project's configured IANA timezone.

### Manual, noncommercial access

The **actual project owner** (not an ordinary channel collaborator) may
grant a separate entitlement for an existing customer of the bot to a
verified active channel or group, for 7 / 30 / 90 days or without an
expiry. The customer must already have opened the bot at least once:
Telegram generally cannot initiate a private conversation with a bot on
behalf of a never-seen user.

A second confirmation is mandatory. The write transaction acquires the
existing project checkout-retirement lock, checks ownership, the exact
project/customer/resource, active resource state and duplicate active manual
rights. It creates only:

- an `EntitlementGrant(source=MANUAL, valid_until=...)`;
- its normal `EffectiveAccess` recalculation and queued Telegram access
  work through the existing outbox/worker;
- an auditable owner decision.

**No `Payment`, `PaymentRequest` or `Subscription` is created, updated
or deleted.** There is no fake sale/revenue. An already active manual
entitlement on that resource cannot accidentally be duplicated; to replace
the duration, the owner first revokes that manual grant.

Revocation requires explicit confirmation and is restricted to
`source=MANUAL` and the same customer/project. Paid and `OWNER_SERVICE`
grants are never selected for this action. When another valid entitlement
remains, the access projection stays granted: a manual revoke never deletes
someone's paid Telegram access. The owner receives a status message;
Telegram physical access is completed asynchronously by the original worker,
not claimed successful at the moment of the database write.

### Security and operational behavior

- The project manager permission is required for read-only subscriber lists
  and histories. Only the verified project's owner may mutate manual access.
- Existing PostgreSQL tenant RLS continues to isolate all project reads and
  writes. Callback IDs carry compact UUIDs, but every mutation rechecks
  authorization, scope and current resource state under the transaction lock.
- The central connection registry does not expose subscriber callbacks.
  No new native Telegram `/menu` commands are introduced.
- No bulk cancellations or changes to paid subscriptions. Existing
  owner-obligation remediation remains the only deliberately exceptional
  workflow for early paid cancellations before safe bot retirement.
- No external API, extra polling service, paid VM tier, or long-lived
  per-customer database connection is required.
- Existing active bots can continue running during merge. **No staging or
  production deployment before explicit owner approval.**

## Acceptance

CI contains real PostgreSQL RLS checks on cross-project reads/writes,
search, filters, idempotent revoke, audit, resource validation, and simultaneous
paid+manual entitlements; UI checks verify compact callbacks, two-step
confirmation, role isolation, and backwards-compatible navigation.

On staging, open an existing customer card in a connected owner bot, confirm
payment history and actual vs desired access, create a *test* manual 7-day
grant in a nonfinancial test channel, then revoke that manual grant and
verify the real paid customer account remains unaffected. Do not manipulate
live paying customers merely to simulate a test.

### Additional safeguards verified by review

Manual grants recheck bot-retirement state under the existing checkout-retirement lock; once a bot is marked deleting, no new manual entitlement is created. Active access grants are paginated (8 per page), so a grant older than 30 newer paid or manual entitlements can still be found and revoked. Both cases are covered by dedicated PostgreSQL and UI regression tests.

## Stage 2 — buyer card / invoice navigation (PR after broadcasts)

- Reuse the existing `ProjectCustomer` card and `PaymentRequest` ledger.
  There is **no second customer database** and no new financial, payment
  approval, subscription or access-grant writes.
- The connected bot's existing subscriber card now shows counts of all buyer
  invoices and unpaid/review-pending invoices, plus one **Invoice history**
  action. History is paginated (8 per page) and includes unpaid, in-review,
  approved, rejected, cancelled and expired invoices, with status and the
  original request's frozen amount/currency.
- Each invoice in this buyer-scoped list opens the existing `own:invoice`
  detail. Detail displays request reference, current plan label, original
  duration and amount, payment method, creation time and status. **A new
  invoice is never represented as a confirmed payment.** From detail the
  admin can navigate back to the buyer card or invoice list.
- The existing Telegram manual-receipt review message receives a
  **Customer and invoice** shortcut below the original Approve/Reject
  actions. It is an informational shortcut only and never invokes approval.
  New-invoice notifications already link to the same screens.
- All callback reads are limited to this connected bot's project, require
  an authorized project manager, and are validated against `PaymentRequest`
  and `ProjectCustomer` in the existing tenant session with FORCE RLS.
  The central bot is not allowed to open these screens.
- Keep the manual access confirmation controls in the existing buyer card;
  do not add payment cancellation, subscription grants or refund actions to
  invoice detail. Actual payment review remains in the original safe path.
- UI RU/EN. No schema migration. Roll out the customer-supervisor only
  after exact-head checks, review, approval and staging tests.


## One-tap customer command in invoice alerts

- New **invoice created** notifications show the stable customer command directly
  next to the customer's username/first name, e.g. `👤 @buyer · /u3tz8ab`.
  Telegram recognizes slash commands as tappable bot commands; tapping sends it
  to that *same connected bot*. No search or intermediate invoice menu is needed.
- The duplicate **Subscriber** and **Invoice** inline buttons are removed from
  new invoice notifications. Their original customer-card/invoice-history
  pages remain available from the subscriber directory and card.
- For new invoices the compact `/u...` reference is reversible base36 of the
  Telegram numeric user ID. For *already queued* notifications missing that
  historical field, `/c...` is a canonical base32 UUID reference.
  Neither is a bearer credential or financial authorization.
- The command handler only accepts a canonical ID in a private chat and only
  in that connected project's bot. It verifies an actual project manager and
  resolves an exact `ProjectCustomer` under tenant RLS. A central-bot,
  unrelated-project, unauthorized or missing buyer lookup never reveals the
  customer. Manual and paid access controls are unchanged.
- No schema migration and no change to invoice, payment, subscription or access
  writes. Previously sent Telegram messages retain their old inline buttons;
  new notifications use the compact command format.
- Verify on staging with a **test invoice** from a test customer. Tap its
  `/u...` code from the owner's Telegram client; ensure it opens the card
  directly, with `🧾 История счетов` inside. Verify a non-manager cannot open
  it even if they know the command, and other project bots cannot resolve it.

## Buyer management completion — owner card actions

The previous read-only card was not a full admin console. Keep the one-tap
`/u...` buyer navigation; add the following real operations from the existing
card, all **owner-only** and within that connected bot's project:

- **Message customer** — use the existing bot-scoped 15-minute text wizard.
  Only plain text, emoji and links are sent to the exact `ProjectCustomer`
  Telegram ID; Telegram delivery errors are surfaced, without pretending
  a message arrived. No message, receipt, or personal file archive is created.
- **Ban / unban** — project-local `ProjectCustomer.is_banned`; a real
  PostgreSQL migration (0034). Ban and checkout share an advisory transaction
  lock to stop post-ban invoice races. The checkout service checks the ban
  before creating an invoice, and both the receipt prompt and receipt
  submission check it before forwarding evidence. The ban does not delete
  confirmed Payments, Subscriptions or Entitlement Grants. It does not
  forcibly kick a paying customer from their channel; owners can separately
  resolve a paid subscription with appropriate refund handling.
- **Activate plan** — select a published plan, then explicitly confirm.
  This produces atomic manual grants for all active resources referenced
  by the plan, with the plan's real calendar-period duration and timezone.
  The `manual_plan_id` field on `entitlement_grants` retains the plan
  provenance. This is a **complimentary entitlement**, not an approved
  payment or a fake Subscription. Multiple source grants share one activation
  UUID and are revoked as one unit. Duplicate active activation is rejected.
  A separate single-channel grant action remains available in the picker.
- **Revoke** — picker selects a particular active paid Subscription or
  complimentary grant. Early ending of a paid subscription reuses the
  pre-existing `end_owner_subscription` service and a strong confirmation,
  preserves financial history, and warns about refunds outside ViMi Pay.
  Free manual-plan activations and single-resource manual grants use their
  respective targeted revocation functions. Different paid grants or owner
  service rights remain unaffected.
- **Existing invoice history, payments, refresh, directory and short customer
  command** remain available, but are not duplicated across card screens.

Moderator access and a full ownership transfer are **separate protected
workflows**, not fake/inert card buttons. Moderator must define permissions
and revocation; transfer must verify both account owners and the BotFather
ownership boundary, preserve MerchantIdentity and all payment history,
and never transfer a bot through an unconfirmed buyer callback.

### Staging acceptance
1. Merge only after exact-head CI and Codex review.
2. Rebuild the app image with mounted migrations as in prior staging runs,
   apply `0034_buyer_management`, then update only `customer-supervisor`
   using `compose.light100.yaml`.
3. Use a test customer: ban/unban, attempt invoice and receipt when banned,
   send one private message, activate and revoke complimentary plan,
   test confirmation of a paid subscription **only with test data**.
4. Verify actual Payments/Subscriptions/Entitlement Grants are unchanged
   by a ban or message; project isolation and 100-bot light configuration
   remain intact.

## Two-way direct messaging (buyer ↔ owner)

The buyer card's **Write message** action now starts a real conversation,
not a one-way blind `send_message`. The owner composes the existing
text/emoji/Telegram-link message, and the customer receives it with a
**↩️ Ответить / Reply** inline button and visible reply instructions.
Customers may either use the button (ForceReply composer) or Telegram's
normal reply-to-message gesture on the admin message.

- A buyer's reply must be in their private chat, to a real message sent by
  the **same connected bot** with a recognized conversation prefix. Plain
  messages, payment receipts and slash commands are never intercepted.
- A project-scoped read resolves the buyer's real ProjectCustomer and owner.
  Only non-banned customers in the current active project can send a reply.
  The owner's chat receives `💬 Покупатель /u...` with sender identity,
  the submitted text, **✉️ Ответить** and **👤 Карточка**.
- The owner may reply using that button (the original `subscriber_message`
  flow) or by Telegram's native reply to this bot notification. Both routes
  re-check current project ownership and customer identity. A support-role
  or random user cannot replay a copied reference to send messages.
- Telegram itself stores the message conversation in its chats. No new
  message table, no migration, and no changes to payments, subscriptions,
  financial history or moderation decisions. No claims of delivery are made
  if Telegram refuses the send.
- Phase one accepts text, emoji and Telegram links up to 3500 characters.
  Media messages are deliberately not forwarded as text; richer media
  support and an optional opt-in conversation history can follow after testing.
- Staging acceptance: owner sends a test message, buyer taps Reply,
  owner receives buyer text with the correct /u reference, owner replies
  back, and buyer sees it. Check a blocked buyer and another connected bot
  cannot route a reply. Do not broadcast or touch real paid subscriptions.
