# ViMi Pay — RU/EN localization contract

## How language is chosen

All Telegram control-plane, owner/admin and buyer UI is to support Russian and English.
On each incoming Telegram update, use the caller's optional `from_user.language_code`
(ISO/IETF language tag): explicit `ru` → Russian; everything else or missing → English.
A user's explicit choice via `/language` or the `🌐 Language` button overrides
Telegram auto-detection and applies in both the central bot and connected bots.
Store this on the global TelegramUser identity, **not** on a Project or checkout.
No buyer can change another user's preference through a callback.
Keep callback payloads/commands stable and language-independent.

Use a shared `tr(ru, en)` helper at the presentation layer. All UI, including
prompts, moderation, receipts, subscription statuses, validation errors, admin
screens, platform reports and buyer purchasing, needs translation; never
translate user-authored plan names, bank details, chat names or payment instructions
without owner's explicit action. Keep the underlying money, access and RLS
operations identical in every language. BotFather descriptions/commands may be
localized with Telegram's language-specific APIs in a later operational step.

## Timezone and location

Telegram Bot API does **not** send a trustworthy geographical location or
the user's IANA timezone in ordinary `User` objects. `language_code=en`
does not tell whether someone is in London, Amsterdam or New York.
Never infer timezone from language or silently rewrite a channel's timezone.
A new Project starts at `Europe/Moscow` and the authorized channel owner
can edit timezone in the connected bot via its existing timezone settings.
All database instants remain UTC. If a future Mini App obtains a browser
timezone, explicitly ask before applying it to a project.

## Deployment and acceptance

The first migration adds a nullable `preferred_language` column to
`telegram_users` without rewriting any existing preferences or projects.
Apply Alembic before restarting upgraded bots. Verify English and Russian
users separately; switch language, restart and switch between central and
connected bots, ensure language stays selected. Verify buyer receipts and
moderation have the recipient's own language and no cross-user state leaks.
Do not assume migration and locale infrastructure alone translate every screen.


## Delivery sequence

- Stage 1 (PR #36): Telegram language detection, persistent per-user preference,
  `/language`, bilingual buyer catalog / duration / payment choice.
- Stage 2 (this stage): bilingual Platform Owner reporting, bot timeline,
  connected bot list, central welcome and initial token registration.
  All business and authorization decisions remain server-side.
- Next stages: complete central removal/obligation flow, connected owner
  setup, buyer payment confirmation and notifications, error states and
  end-to-end language consistency.

Until those stages pass, the product must not claim that *all* UI text is
already translated.


- Stage 3 (this stage): connected owner setup screens for channels and groups,
  published/draft plans and pricing, payment method lists and details,
  manual review inbox, preview, contextual setup prompts, IANA timezone choices,
  readiness checks and confirmation notices.
- Stage 4: finish central retirement/obligations workflow and buyer-facing
  payment and receipt notifications with per-recipient locale resolution.

Existing user-entered channel/plan names and bank payment instructions are
never translated. Test both languages for callbacks and admin-only authorization.


- Stage 4 (PR #39): buyer checkout, receipt prompt/retry, cancellation,
  approval/rejection, role-specific outgoing payment decisions, access invitations
  and join-request approval notices. The receipt reply recognizer accepts both
  legacy Russian and new English headers, but only in verified bot-authored
  private ForceReply prompts. Callback data and payment state transitions remain
  language-independent.
- New migration \`0022_detected_user_language\` stores only the normalized RU/EN
  language last seen on an incoming Telegram update, without overriding a user's
  explicit \`preferred_language\`. Outgoing notifications choose explicit language
  first, then the last observed hint, then Russian as the backward-compatible
  fallback. No geographic location or timezone can be derived from this hint.
- Migrations \`0021\` and \`0022\` are required on staging before deploying these
  revisions; deploy both central and all connected bot processes together.


- Stage 5 (PR #40): owner-only bot removal, outstanding payment/subscription
  obligations, action confirmations and the customer notifications that accompany
  legitimate owner decisions, plus legacy central owner/admin routes. The
  retirement/payment/audit code remains identical in RU and EN. Existing
  Russian ForceReply bot-token prompts remain valid alongside English ones.
  Buyer notice language is resolved for the buyer independently of the owner.
- The existing explicit manual language choice and stored Telegram language hint
  are shared across central and customer bots. Changing display language
  does not change money, entitlements, project timezone, transaction IDs,
  or any user-authored payment instructions.
