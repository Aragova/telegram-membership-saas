# ViMi Pay — RU/EN localization contract and release status

## User-facing language

ViMi Pay supports Russian and English in **both** central
`@vimipay_bot` and connected customer/owner bots. Telegram supplies the
optional `User.language_code` on incoming updates: `ru*` selects Russian;
other languages and absent hints default to English on incoming updates.
The explicit `/language` setting overrides automatic selection, is saved
per global Telegram user, and applies across all bots.

The last normalized Telegram hint (`detected_language`) is stored so
background notifications can use the **recipient's** own language:
`preferred_language` → `detected_language` → historical RU fallback.
Owner, reviewer and buyer events must never share mutable language state.
Bot callback payloads, commands, UUIDs and state transitions are
language-independent; changing UI language must not change authorization,
tenant isolation, prices, payment approval or entitlements.

Store owner-entered channel names, plans, bank details and payment instructions
verbatim. Do not translate customer-provided text or infer location/timezone
from language. The Bot API does **not** disclose a user's IANA timezone.
Default project timezone is `Europe/Moscow`, editable by the owner; database
instants remain UTC. BotFather localized bot descriptions may follow separately.

## Implemented slices

| PR | Scope |
| --- | --- |
| #36 | Preference persistence, Telegram auto-selection, shared `/language`, initial buyer catalog |
| #37 | Platform Owner reports, history, bot registry and central onboarding |
| #38 | Connected owner channels, tariffs, payments, reviews and configuration |
| #39 | Buyer checkout, receipts, approvals, invitations and recipient language hints |
| #40 | Central bot removal, paid obligations, legacy administration and owner-initiated notices |
| Final audit | Owner-service error boundaries and old central command redirects; rollout guide |

The RU/EN rollout is **not accepted solely by merged code**; staging must
apply the schema changes, rebuild both types of runtimes and pass the
Telegram-language E2E checks below.

## Schema compatibility

- `0021_user_language`: optional `telegram_users.preferred_language`
  (`ru`/`en`), set only by the user.
- `0022_detected_user_language`: optional
  `telegram_users.detected_language`, updated from incoming Telegram hints.
- Existing payment requests, subscriptions, grants, financial figures,
  and project timezone are not rewritten by these migrations.
- Apply **both** before restarting any upgraded bot. Do not downgrade
  columns just to roll back the code.

## Deployment and acceptance

See [STAGING_LOCALIZATION_ROLLOUT.md](STAGING_LOCALIZATION_ROLLOUT.md)
for staging backup, exact HEAD, Alembic, Docker Compose restart, acceptance
and recovery commands. Validate at least Russian and English Telegram
accounts in central/connected bot flows and paid-review notifications,
including recipient-language isolation and old Russian ForceReply receipts.

Critical defects affecting money, access, security, locale mixing or English
navigation block release. Cosmetic wording may be scheduled separately.
