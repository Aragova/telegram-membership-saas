# Telegram owner/admin interface — implementation progress

The product scope is defined by [CONCEPT.md](CONCEPT.md) v1.4, sections 2 and 4.
The owner must eventually configure projects, bots, channels/groups, timezone,
plans, currency/price pairs and payment methods **without database/SSH commands**.
Do not replace that acceptance criterion with API-only or manually seeded staging.

## Owner/admin foundation (first UI slice)

This slice introduces a real, project-scoped admin entry point in the existing
one-bot / one-project Telegram runtime:

- `/admin` opens the owner/admin home.
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
