# Telegram UI / UX — premium MVP baseline

## Goal

The Telegram surface is the customer's first impression of the membership SaaS.
Clear progress, concise human-readable status, and intentional action emphasis
are part of product acceptance, not optional polish.

## Native Telegram UI

- Use Telegram `InlineKeyboardButton.style`: `primary` (blue) for plan selection/navigation,
  `success` (green) for purchase confirmation, approve, and joining a channel,
  `danger` (red) for destructive/cancel/reject actions.
- The Telegram client owns exact colors, geometry and theme; older clients can render
  buttons without color. Never promise pixel-perfect custom styling.
- Do not show raw private invite links in message text. Use a **green button**
  with `url=invite_link`. The invitation is temporary, protected by a join ticket and
  verified against the buyer's Telegram ID on admission.
- Plain text with limited intentional emoji and bold HTML headings is preferable to
  long automated text, overly decorative separators, or technical UUIDs in customer text.
- Escape any dynamic plan, username, instruction and method text before sending HTML.

## Buyer journey

1. `/start`: a branded title and concise subscription catalog, with primary buttons.
2. Selected plan: recognizable name, duration, price/currency, payment method and
   payment instructions, followed by the green **Я оплатил** control.
3. Receipt: buyer replies with a photo/document to a bot prompt. Until then no review.
4. Submitted: concise persistent acknowledgment; status remains pending review.
5. Approved: persistent **Подписка активирована** message showing plan and actual
   subscription period end in the project's timezone; does not claim that chat
   membership is complete yet.
6. Access worker: persistent invitation message with **Вступить в канал** green URL
   button. No raw link in body. Customer submits join request.
7. Join approved: separate **Вы в канале!** confirmation, after DB finalization.
8. Rejected: explicit rejection and direction to contact admin.

## Admin journey

1. Receipt media are copied directly in Telegram without storing file bytes/file IDs.
2. The review summary replies to the exact copied media message, preventing
   cross-association when many buyers submit simultaneously.
3. Green approve / red reject controls require permission validation and are idempotent.
4. After a successful review, remove the action keyboard on the reviewed message
   (best effort), and send a **persistent** admin confirmation with buyer, plan,
   amount, state and actual subscription end in project-local time.
5. A confirmed subscription is not yet the same as actual Telegram membership.
   Avoid saying the user has joined until the join request is finalized.
6. Repeated callback on an already processed request must not send duplicate
   success notifications.

## Reliability and safety

- User-facing notification failures must not roll back already committed
  payment approvals or successful channel joins.
- No new database migrations, banking information, receipt storage or payment
  verification are introduced by this UX slice.
- Avoid logging private invite links or real customer details.
- Verify bot type support for `style` with current aiogram and Telegram Bot API.
- Test formatting, expiration timezone, colors, receipt button/link, duplicate
  callback, and post-join confirmation.
- Keep all UI changes in a PR with CI, exact-head Codex review and explicit approval.
