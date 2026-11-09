# Project Context — Telegram Membership SaaS

## Telegram 2026 — Native dates and time (current PR)

After Managed Bots (#102–#103, verified live), the next agreed technology
is native formatted dates. Render subscription/access expiry, payment history,
and expiry reminders using Telegram `<tg-time unix="..." format="d|dt">`
with readable text fallback. Use the common module
`src/telegram_membership_saas/telegram_dates.py`, and
[TELEGRAM_NATIVE_DATES.md](TELEGRAM_NATIVE_DATES.md) for exact scope
and staging checks. UTC business timestamps, project timezone, billing,
access checks, RU/EN and `compose.light100.yaml` remain unchanged.
Never merge or deploy a PR without explicit owner approval.

## Rich Messages — единый интерфейс ViMi Pay (ноябрь 2026)

Основной интерактивный UI платформы теперь использует Telegram Rich Messages:
центральный бот, все клиентские боты, панели владельца/админа, меню покупателей,
подписки, продажу файлов и статистику. Единая реализация:
`src/telegram_membership_saas/rich_messages.py`;
полный контракт: [RICH_MESSAGES_PLATFORM.md](RICH_MESSAGES_PLATFORM.md).

Общий переключатель `TMS_RICH_MESSAGES_ENABLED=true` (по умолчанию)
охватывает все боты, **без пер-ботных ограничений**. Установка false возвращает
классический интерфейс; финансовая логика не изменяется. Rich Messages можно
редактировать в исходном сообщении через `editMessageText(rich_message=...)`,
чтобы не плодить старые клавиатуры. Особые карточки тарифов и файлов
используют нативные таблицы. Сохранить RU/EN и дословный текст
платёжных инструкций, заданный администратором.


Этот файл — обязательная стартовая точка для любого нового чата, разработчика или Codex-сессии.

## Buyer preview — read-only

Кнопка «Смотреть гостем» в подключённом боте показывает **те же**
доступные к покупке тарифы, валюты, цены и совместимые способы оплаты,
что и каталог реального покупателя. Предпросмотр интерактивный,
включая инструкции оплаты, но **без создания заявок, платежей,
подписок и выдачи доступа**. Только владелец с действующими правами
может использовать preview callbacks. Названия, введённые владельцем,
не переводить автоматически. Для настоящей покупки нужен отдельный
покупательский аккаунт в том же боте.

## RU/EN и развёртывание staging

Перевод ViMi Pay на русский и английский разработан через PR #36–#40.
Он распространяется на центральный бот, подключённые боты, владельца
платформы и покупателей. Язык выбирается автоматически по Telegram
`language_code`, вручную через `/language`, хранится глобально на пользователя.
При уведомлениях учитывается язык получателя, не отправителя.

**Для запуска обновлённого кода нужны миграции 0021 и 0022** до
перезапуска центрального бота и клиентских воркеров. Инструкция:
[STAGING_LOCALIZATION_ROLLOUT.md](STAGING_LOCALIZATION_ROLLOUT.md).
Не объявлять RU/EN принятым без проверки реальных Telegram-экранов на
staging. Секреты и рабочие подписки не трогать.

## Что мы строим

Мы создаём международно расширяемую SaaS-платформу для управления платным доступом к Telegram-каналам и группам.

На первом этапе главный payment flow — полуавтоматический: пользователь платит владельцу напрямую по его инструкции, отправляет скриншот/чек в Telegram, администратор самостоятельно проверяет реальное поступление денег в банке и вручную подтверждает или отклоняет заявку. После подтверждения всё управление подпиской и доступом выполняется автоматически.

## Что НЕ является целью

Это не одноразовый single-channel bot и не копия UnionBot один-в-один. Мы строим modular SaaS core с возможностью позже добавить Telegram Stars, Stripe, YooKassa, PayPal, crypto providers, web dashboard, API, white-label bots, multi-resource plans и другие access providers.

## Этап E — неизменяемый учёт использования ViMi Pay

Миграция `0020_platform_usage_metering` создаёт защищённый
`platform_usage_events` без FK на удаляемые боты и проекты.
События пишутся PostgreSQL-триггерами в **той же транзакции**, что и
подключение/удаление бота, публикация тарифа, подтверждённый Payment,
регистрация покупателя и подписка. Учёт включается без дополнительного
сервиса или фонового опроса. Изменения/удаления событий запрещены runtime
через FORCE RLS. Доступ к агрегатам и истории только через аудируемую
SQL-функцию для подтверждённого владельца платформы.

Часть старой активности восстанавливается по реальным датам; исчезнувшие
до установки 0020 боты и неизвестные даты тарифов не выдумываются.
Клиентский оборот **не** равен доходам ViMi Pay; платный биллинг не включён.
Полный контракт: [PLATFORM_USAGE_METERING.md](PLATFORM_USAGE_METERING.md).

## Этап D — закрытая аналитика платформы

Канал управления ViMi Pay имеет отдельный mode `👑 Платформа` только для
выданного по Telegram numeric ID владельца. `TMS_PLATFORM_OWNER_TELEGRAM_ID`
в staging/production секреты не содержит, но не коммитить конкретный ID.
Alembic `0019_platform_dashboard` + однократная административная команда
`python -m telegram_membership_saas.platform_bootstrap` привязывают
операторское разрешение без доступа runtime к записи прав. Отчёт
`public.platform_report_snapshot` имеет фиксированную агрегированную
структуру, проверяет PostgreSQL ACL, аудирует каждый запрос и не выдаёт
персональных данных покупателей. Только подтверждённый клиентский оборот,
в разрезе валют; выручка SaaS ещё не реализована. См.
[PLATFORM_OWNER.md](PLATFORM_OWNER.md).

## ОБЯЗАТЕЛЬНО: ViMi Pay v2.0 (утверждено 04.11.2026)

Прочитать [VIMI_PAY_V2.md](VIMI_PAY_V2.md) **перед изменением кода**.
Это основной продуктовый контракт: центральный `@vimipay_bot` служит
для подключения, ротации и удаления ботов; ВСЯ настройка каналов, методов
оплаты, тарифов и заявок находится в собственном боте владельца.
Подключённый бот разделяет владельца/покупателя по серверным правам.
В центральном имеется отдельный закрытый Platform Owner режим глобальной
аналитики, а для платформенного владельца — переключатель в обычный
«Мои боты». Разделяем оборот владельцев и доход SaaS; закладываем usage
events, не вводя неутверждённый биллинг.

Разработка — вертикальными, законченными логическими частями с CI и
Codex exact-head; косметику не превращать в блокер, критические риски
платежей/прав доступа не пропускать. Интерфейс сразу лаконичный и
премиальный; никаких технических идентификаторов в пользовательском UX.

## Базовые продуктовые решения

- Один Owner может иметь несколько Projects.
- Project содержит BotInstance, Resources, Plans, Prices, Payment Methods, Customers, Subscriptions и Settings.
- BotInstance credentials are encrypted at rest using a dedicated server secret; bot identity is globally unique, linked to one Project. Registration and runtime readiness are distinct states. The central ViMi Pay bot is ONLY a token/connection registry for ordinary tenants (plus a restricted global Platform Owner dashboard). Each connected owner bot hosts BOTH project administration and buyer checkout. The multi-bot supervisor is already implemented; bot registration starts setup mode without a published catalog.
- Resource в MVP: `TELEGRAM_CHANNEL` или `TELEGRAM_GROUP`; модель должна быть расширяемой.
- У Plan нет основной валюты и основной цены.
- Все созданные пары `currency + price` равноправны.
- Кнопка «Валюта» управляет набором валют тарифа: добавить/удалить.
- Кнопка «Цена» показывает все существующие валюты тарифа и позволяет изменить цену любой из них.
- Новая валюта создаётся сразу с ценой.
- Удаление валюты удаляет её текущую цену.
- Currency и Payment Method не смешиваются. Например `USDT` — валюта, `USDT TRC20` — способ оплаты.
- Основной payment provider MVP: `ManualSettlement` (в UI: «Полуавтоматический»).
- Администратор проверяет деньги по своему банку; система не определяет подлинность чеков.
- Скриншот/чек не скачивается в наше хранилище, не архивируется, не анализируется OCR/AI и не превращается в отдельную доменную сущность.
- В новой заявке администратор видит пользователя, тариф, цену, валюту, способ оплаты и присланное Telegram-сообщение/файл, после чего нажимает `Подтвердить` или `Отклонить`.
- После подтверждения создаётся Payment и автоматически запускается subscription/access lifecycle.

## TimeZone

- Каждый новый Project по умолчанию получает `Europe/Moscow` (UTC+3).
- Владелец может изменить timezone проекта.
- Все timestamps хранятся в UTC.
- Отображение, календарные сроки тарифов, акции, статистика по дням и уведомления используют timezone проекта.

## Основная доменная цепочка

`PaymentRequest -> Payment -> Subscription -> EntitlementGrants -> EffectiveAccess -> ActualAccess`

Эти понятия запрещено объединять в одну таблицу или один boolean.

## Entitlement model

У пользователя может быть несколько независимых оснований доступа: PAYMENT, MANUAL, OWNER_SERVICE, PROMOCODE, TRIAL, REFERRAL, IMPORT, COMPENSATION.

Истечение одного основания не означает немедленное удаление пользователя. Сначала пересчитывается Effective Access. Доступ отзывается только если активных grants больше нет.

## Owner / Service Access

Владелец сети ботов не должен покупать собственные тарифы, отправлять подтверждение оплаты или вручную продлевать себе доступ в каждом проекте.

Нужно поддержать отдельный служебный постоянный доступ:

- один или несколько Telegram-аккаунтов владельца платформы/сети могут быть помечены как service accounts;
- такой аккаунт может иметь scope `ALL_OWNER_PROJECTS`, включая будущие проекты;
- служебный доступ создаётся напрямую как Entitlement Grant с source `OWNER_SERVICE`;
- для постоянного доступа `valid_until = NULL`;
- PaymentRequest, Payment и Subscription для такого доступа не создаются;
- service access не учитывается как продажа, выручка, paid subscriber или conversion;
- при необходимости обычному пользователю можно отдельно выдать MANUAL grant на один project/resource и сделать его временным или постоянным;
- management permissions и content access — разные системы: право управлять проектом не означает автоматический доступ к платному контенту, и наоборот.

Глобальный owner/service account — это платформенная настройка владельца сети, а не коммерческий Plan и не «нулевой тариф».

## Pricing

- PlanPrice должен поддерживать versioning/history.
- Изменение текущей цены не меняет старые PaymentRequests и Payments.
- PaymentRequest содержит immutable pricing snapshot: currency, regular amount, discount amount, final amount, promotion reference при наличии.
- Акция, закончившаяся после создания заявки, не должна менять сумму уже созданной заявки.
- Денежные значения не хранятся в float.

## Duration

Не считать месяц равным 30 дням. Использовать календарные единицы, например DAY/WEEK/MONTH/YEAR/LIFETIME с value + unit.

## Надёжность

Обязательны:

- idempotency;
- atomic approval;
- concurrency safety;
- transactional outbox;
- retry/backoff для внешних действий;
- reconciliation между desired access и фактическим Telegram state;
- audit log;
- backups/restore testing.

## Multi-tenancy и безопасность

- Project/Tenant — security boundary.
- Все tenant-owned данные должны быть изолированы.
- Предпочтительная база: shared PostgreSQL + tenant/project scope + Row Level Security.
- Нельзя полагаться только на `WHERE project_id`.
- Каждый callback и command повторно проверяет identity, membership в проекте, permissions, принадлежность объекта проекту и допустимость state transition.
- Roles в UI могут быть OWNER, ADMIN, PAYMENT_MANAGER, SUPPORT, VIEWER, но под капотом авторизация строится на permissions.
- Bot tokens и прочие секреты не хранятся в открытом виде и не коммитятся.

## Архитектурный стиль

Modular monolith.

Domain/Application core не зависит от aiogram/FastAPI UI details. Telegram — первый интерфейс и первый access adapter, а не вся бизнес-модель.

Границы модулей: identity, tenants/projects, bots, resources, plans/pricing/promotions, payment_methods/payment_requests/payments, subscriptions, entitlements, access, permissions/admins, notifications, audit, localization, workers.

## Предварительный стек

Python 3.12+, aiogram 3, FastAPI, PostgreSQL, SQLAlchemy 2, Alembic, Pydantic, pytest, Docker.

## Порядок разработки

Разработка ведётся маленькими PR. Не собирать огромные архитектурные и бизнес-изменения в одном PR.

Перед merge обязательно проверить exact HEAD, весь CI, review именно текущего HEAD, unresolved review threads = 0 и mergeability. Если HEAD изменился, старые CI/review считаются устаревшими и должны быть перепроверены.

Никогда не merge без явного разрешения владельца проекта.

После готовности значимой части продукта: deploy на отдельную staging VM, тестовый Telegram bot + тестовый закрытый канал/группа, реальные end-to-end сценарии. Production — только после стабильного staging.

## Public repository rule

Репозиторий публичный ради доступного GitHub Actions. Поэтому никакие секреты, реальные банковские реквизиты, реальные пользовательские данные, VM credentials, SSH keys, bot tokens и production `.env` не должны попадать в git ни при каких обстоятельствах.
## Network Profiles (optional owner-wide defaults)

See [NETWORK_PROFILES.md](NETWORK_PROFILES.md) for the approved initial network reuse implementation: a connected-bot admin can explicitly save one owner profile, including an optional choice to copy bank instructions. New central-registered bots created by the same Telegram owner receive isolated draft plans, active currency prices, and disabled manual payment methods, plus automatic first verified-channel linking. No purchases, subscriptions, Telegram credentials, historical financial rows or existing projects are copied or changed. The feature requires migration 0023 and must be tested on staging before production.


## Central connection management (owner-only)

See [CENTRAL_CONNECTION_MANAGEMENT.md](CENTRAL_CONNECTION_MANAGEMENT.md).
The central bot offers paginated per-project connection cards, owner-only
internal labels, protected bot retirement followed by permanent removal of
disconnected **history-free** projects, and a reversible archive for projects
with financial/access history. Do not hard-delete payments, subscription
history or platform usage records to clean up test connections. The connected
customer bot still owns all commercial plan/payment administration; the
central bot does not duplicate that UI. Existing customer supervisors and
the lightweight 100-bot Compose overlay remain unchanged.

## Owner self-service currencies

See [OWNER_CURRENCY_MANAGEMENT.md](OWNER_CURRENCY_MANAGEMENT.md). UAH/BYN
are primary selectable currencies; connected-bot project managers can add
additional existing ISO 4217 fiat or catalogued crypto codes and configure
tariff prices and manual payment methods. Project currency selections are
RLS scoped. The catalog metadata and payment histories stay immutable;
the central bot does not control tenant payment configuration. Migration
0024 is required before deploying new customer bot code.

## Buyer storefront visual polish

See [BUYER_PREMIUM_CURRENCY_UI.md](BUYER_PREMIUM_CURRENCY_UI.md). The buyer's price-currency selector now uses five compact flag-labeled buttons per row (RUB/USD/EUR/UAH/BYN first), an obvious green current selection, warm RU/EN plan descriptions, and removes redundant plan-name/duration repetition. The owner read-only buyer preview reuses the same UI and remains non-transactional. No checkout, database, VM, or manual-payment logic change.

## Owner wizard usability polish

See [OWNER_WIZARD_UX.md](OWNER_WIZARD_UX.md). New tariffs are duration-first with optional title, and period-only autogenerated names are hidden from buyer UI without changing database/payment schemas. RU/EN language changes render the same actual /menu immediately. All setup prompts now accept bot-scoped plain text without the persistent ForceReply banner; old matched replies still work, and /menu cancels stale wizard state. Keep 3 native commands per bot. Deploy central bot and customer-supervisor under compose.light100.yaml, without DB restart/migration.

## Connected-bot subscriber management (PR #55)

See [SUBSCRIBER_MANAGEMENT.md](SUBSCRIBER_MANAGEMENT.md). Project managers
may inspect paginated/searchable subscriber records, paid subscription terms,
confirmed payment history and desired vs actual Telegram access. Only the
verified project owner may grant/revoke *MANUAL* noncommercial access with
explicit confirmations. The original entitlement projection, transactional
outbox and Telegram worker remain the only access pipeline. A manual revoke
cannot cancel a paid or owner-service grant. No financial ledger or schema
migration changes. Central remains a connection registry only. All staging
changes require PR review, explicit merge authorization and deployment
approval; real customers must not be altered during acceptance tests.

## Owner statistics (read-only, PR #56)

See [OWNER_STATISTICS.md](OWNER_STATISTICS.md). The connected owner's bot offers
project-scoped 7/30/all-time statistics: original-currency approved Payments,
registered customers, active paying subscribers, separate manual grants and
pending manual reviews. Uses existing tenant RLS and no migrations. Platform
GMV and tenant sales are not SaaS revenue. This PR depends on #55 and must
not deploy before its own exact-head CI, review and approval.

## Approved owner notifications (PR #57 in progress)

On 2026-11-05 the project owner approved the full per-connected-bot
notification catalogue, UX and RU/EN contract. Authoritative implementation
specification: [OWNER_NOTIFICATIONS.md](OWNER_NOTIFICATIONS.md).
**Critical: immediately tell the owner when a buyer creates a payment
invoice**, even before any receipt/proof is submitted. This is not a confirmed
sale. Preserve existing receipt forwarding with original Approve/Reject
buttons and prevent duplicate noise. Other approved categories: approved or
rejected decisions, 7/3/1-day expiry and expired summaries, verified access
incidents/recovery, optional daily financial digest. Locale always belongs to
the recipient; statistics and subscribers must remain fully RU/EN.

First isolated implementation slice in PR #57 adds a durable per-project
invoice queue, notification settings and existing owner-bot routing; the
remaining producers are phased behind reviews, not falsely advertised as
already enabled. Database migration **0025_owner_notifications** must be
applied before deploying the modified customer-supervisor; do not deploy
a partial PR or restart PostgreSQL. Central bot, payment approval, access
grants/revocations, `compose.light100.yaml` and existing live channels
must be preserved. No merge without exact HEAD CI/Codex and owner approval.


## Channel continuity (PR #65)

When a channel is verified through Telegram and already belongs to a
different existing connection of the same project owner, a new project's
resource insertion is rejected by migration 0027. Restore/reuse the old
connection, then connect the new bot inside it; do not transplant payments
or reassign merchant identities. Concurrent channel claims are serialized.
A different Telegram owner is **not** automatically assigned another
owner's merchant history. See [MERCHANT_CONTINUITY.md](MERCHANT_CONTINUITY.md).


## PR #66 — merchant historical turnover (read-only)

Platform Owner in central @vimipay_bot has an audited Merchant ID report:
approved Payment UUIDs counted once, original exact amount and currency,
historic project periods retained after project/bot removal, per-business
pagination and RU/EN UI. Unknown old project attribution is explicitly
reported, not guessed. This is tenant GMV, not ViMi Pay revenue or bank
verification. Requires Alembic 0028 before central-bot rebuild and
platform owner database EXECUTE grant; existing customer bots unchanged.
Details: [MERCHANT_CONTINUITY.md](MERCHANT_CONTINUITY.md).


## PR #67 — protected commercial eligibility and optional quotas

Migration 0029 adds optional owner-wide bot caps (no default policy), unique
operator-controlled platform trial claims by Merchant ID or Telegram owner,
deduplicated canonical-payment commission reservations (no fees charged),
and platform-only cross-owner Chat ID review signals. Reuse of an already
commercially active channel remains protected even after the old project
was deleted. Different businesses and legitimate channel transfers are not
auto-merged. No real billing/trials/limits are turned on. See
[MERCHANT_CONTINUITY.md](MERCHANT_CONTINUITY.md). Deploy 0029 before any
updated bot runtime; protect the live `compose.light100.yaml` setup.


## PR #68 — platform-only manual channel review

The Platform Owner of central `@vimipay_bot` gets a read-only RU/EN
`🛡 Проверка каналов` list of PR67 cross-owner channel history signals.
A fixed audited SECURITY DEFINER report validates the real DB operator,
supports periods, pagination and timezone-aware labels. Signals do not
prove fraud and never trigger automatic blocks, migrations, or charges.
Migration **0030_platform_risk_review**, privileged platform bootstrap and
a **central-bot-only** rebuild are required. Customer supervisor stays
unchanged; preserve `compose.light100.yaml`. See
[MERCHANT_CONTINUITY.md](MERCHANT_CONTINUITY.md).


## PR #69 — payment evidence immutability (financial vulnerability hardening)

Migration **0031_financial_record_integrity** prevents altering/deleting
committed Payment rows and deleting/payment-price-snapshot rewriting of
PaymentRequest rows at the PostgreSQL boundary, including during a Project
cascade. Terminal decisions cannot be reopened. Ordinary review/approval,
bot reconnection, safe archive, buyer access and subscription processing
remain unchanged. No SaaS tariff, usage fee, trial, commission or platform
payment service is activated. No service rebuild: staging DB migration only,
preserving `compose.light100.yaml`. See [MERCHANT_CONTINUITY.md](MERCHANT_CONTINUITY.md).


## PR #70 — connected-bot broadcasts (stage 1)

Saved text or forwarded photo/video/document/GIF posts with Telegram entities,
preview, recipient confirmation, repeat send, rename, replace, library and
history. Audience is only users who actually pressed /start on the specific
bot ID, including non-buyers; unreachable users are learned from Telegram
send errors, not inferred from channel membership. Durable project-scoped
queue with RLS and 2 sends/sec per bot, pause/resume and unknown-outcome
handling; no paid Telegram broadcasting. Only connected owner bot admin UI;
no changes to payment confirmations or subscriptions. See
[BROADCASTS.md](BROADCASTS.md). Staging migration 0032 and supervisor-only
rebuild required after approved merge; preserve compose.light100.yaml.
