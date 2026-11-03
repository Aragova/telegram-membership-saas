# Telegram owner/admin interface — implementation progress

The product scope is defined by [CONCEPT.md](CONCEPT.md) v1.5, sections 2 and 4.
The owner must eventually configure projects, bots, channels/groups, timezone,
plans, currency/price pairs and payment methods **without database/SSH commands**.
Do not replace that acceptance criterion with API-only or manually seeded staging.

## Owner/admin foundation (first UI slice)

This slice introduces a real, project-scoped admin entry point in the existing
one-bot / one-project Telegram runtime:

- `/start` opens the owner’s registered bots immediately; `/menu` opens the project overview. The legacy `/admin` command remains only as a compatibility alias. The central management bot never routes `/start` to the buyer catalog; nonowners get a clear onboarding-in-progress message. The existing `/plans` buyer command remains only for staging backwards compatibility until a customer-bot runtime is deployed.
- View current channels/groups, plans and payment methods using **live project DB data**.
- Open a live payment-review queue from the **Заявки** button; existing
  `/payments` still works.
- **Посмотреть как клиент** opens the real customer catalogue.
- **Настройки → Часовой пояс** changes the actual `Project.timezone`.
  The chooser offers common zones and `/timezone Region/City` accepts any valid
  installed IANA timezone. New projects still default to `Europe/Moscow`.
- A project owner or assigned project admin may use this initial menu. All
  buttons and commands re-check the Telegram actor against the project's
  existing ProjectMember and Permission model. Unknown buyers, unauthorized
  roles and unrelated projects cannot browse/edit settings.
- All project-owned administration reads/writes happen inside an authenticated
  `tenant_session_scope`, with PostgreSQL RLS, not an unrestricted maintenance
  session. Telegram identity is resolved globally before entering tenant scope.
- Admin navigation is private-chat only and does not change customer `/start`.
- No new schema, credentials, bot token storage or banking integrations.

## Customer bot registration — next slice (PR #19)

- Owner selects **Мои боты → Подключить** from the existing admin menu.
- BotFather token is accepted only as a private reply to a prompt from this bot.
  The incoming token message must be successfully deleted by Telegram **before**
  any credential verification or storage. A stray token is deleted, not processed.
- Telegram `getMe` verifies the token and resolves the immutable Telegram bot ID.
- `BotInstance` stores identity, project, display metadata, state
  `registered` and **Fernet-encrypted** credentials. No plaintext tokens, token
  hashes, URLs or file copies are stored. One Telegram bot ID may belong to
  only one project globally. Reconnection inside the same project rotates
  the encrypted token. Other projects cannot silently take over.
- Only the project owner may connect or replace a bot (ordinary admins cannot
  manage powerful bot credentials). Tenant-scoped transactions and PostgreSQL
  RLS remain mandatory; permissions are rechecked before and after `getMe`.
- A server-side encryption key `TMS_BOT_TOKEN_ENCRYPTION_KEY` is mandatory
  for registration; the key must be provisioned separately from the public repo.
  **Never send bot tokens or encryption keys in chat with developers or commit
  them to git.** Store keys in restricted environment/secret management and
  preserve them in operational backups. Rotation needs a separate tested plan.
  The staging Docker bot service receives this variable from the VM environment;
  do not share `docker compose config` output after provisioning it because
  that output may include secrets. Production should migrate to Docker/KMS
  secrets with restricted access.
- Telegram private chats are **not end-to-end encrypted**. Deletion reduces
  exposure, but does not guarantee erasure from every client or Telegram
  infrastructure. If a bot token may be exposed, rotate it in BotFather.
  A secure HTTPS onboarding form can be added before production.
- `registered` explicitly **does not mean running**. This PR has no customer
  bot dispatcher/webhook; that is the next mandatory slice. Never show a
  customer-facing buy link until routing and Telegram E2E pass.

## Not implemented by this first slice

The **read-only** channel, plan, and payment-method panels are not complete
management screens. Adding/editing resources, plans, prices, currencies and
payment methods, attaching a custom bot, creating new projects through Telegram,
and subscriber/statistics management remain **mandatory pending features in
CONCEPT.md**, not optional extras.

The current runtime still binds `TMS_TELEGRAM_PROJECT_ID` to one project and
`TMS_TELEGRAM_BOT_TOKEN` to one bot process. A later onboarding slice must
remove the need for manual staging SQL and VM configuration for every new owner
without weakening project isolation or secret handling.

## Real staging acceptance (after owner approves merge)

1. Owner opens `/admin`, sees only the configured project and real data.
2. Owner changes timezone using buttons and a custom IANA zone; dates in
   existing customer/admin notifications use the new project zone.
3. Unauthorized buyer cannot open the admin menu, forge a timezone callback,
   or use `/timezone`.
4. Owner opens `Заявки` to get the real pending review controls and can view
   the catalog as a customer.
5. No customer purchases, existing access, payments or subscriptions are changed.

Next slices must implement the original configuration order from CONCEPT.md:
create project / connect bot, connect channels and groups, create tariff
and duration, add currency/price pairs, attach manual payment methods,
publish, then fully test a new owner's setup and a fresh buyer purchase.
