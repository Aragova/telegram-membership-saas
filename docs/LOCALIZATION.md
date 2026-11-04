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
