# Project Context — Telegram Membership SaaS

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
