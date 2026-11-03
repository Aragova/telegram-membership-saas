# Project Context — Telegram Membership SaaS

Этот файл — обязательная стартовая точка для любого нового чата, разработчика или Codex-сессии.

## Что мы строим

Мы создаём международно расширяемую SaaS-платформу для управления платным доступом к Telegram-каналам и группам.

На первом этапе главный payment flow — полуавтоматический: пользователь платит владельцу напрямую по его инструкции, отправляет скриншот/чек в Telegram, администратор самостоятельно проверяет реальное поступление денег в банке и вручную подтверждает или отклоняет заявку. После подтверждения всё управление подпиской и доступом выполняется автоматически.

## Что НЕ является целью

Это не одноразовый single-channel bot и не копия UnionBot один-в-один. Мы строим modular SaaS core с возможностью позже добавить Telegram Stars, Stripe, YooKassa, PayPal, crypto providers, web dashboard, API, white-label bots, multi-resource plans и другие access providers.

## Базовые продуктовые решения

- Один Owner может иметь несколько Projects.
- Project содержит BotInstance, Resources, Plans, Prices, Payment Methods, Customers, Subscriptions и Settings.
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

У пользователя может быть несколько независимых оснований доступа: PAYMENT, MANUAL, PROMOCODE, TRIAL, REFERRAL, IMPORT, COMPENSATION.

Истечение одного основания не означает немедленное удаление пользователя. Сначала пересчитывается Effective Access. Доступ отзывается только если активных grants больше нет.

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