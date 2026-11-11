## Monthly official FX and customer-chosen bill currency (2026-11-11)

User-approved rule: **one official monthly exchange-rate snapshot** for the
entire platform, never 80/90 example values hardcoded, never daily conversion.
Supervisor shard 0 requests the Central Bank of Russia official XML for the
first Moscow calendar day of the month (including denomination, e.g. ten
hryvnias), stores immutable RUB-per-unit USD/EUR/UAH/BYN rates together with
the source publication date, and retries safely if a source is unavailable.
If the provider omits a currency or a rate is stale/invalid, invoicing
**fails closed**. Existing rates are never overwritten or silently changed.
No new bot-specific schedulers, no added FX button in platform settings.

Inside a connected bot's main Admin menu, the existing **🧾 Счёт / Bill**
button shows confirmed turnover by native currency, Stars separately, and
the preview of 5% commission plus next-month RUB 150 bot service fee. After
**💱 Choose bill currency**, the owner selects **RUB, USD, EUR, UAH, BYN**.
One read-only owner-scoped DB quote converts the same RUB reference total
into the selected currency using the stored accounting-month quote
(amount/one-currency-unit), preserving two-decimal rounding and publishing
source metadata. There are not five separate invoices; the chosen currency
must become part of **one** immutable invoice/settlement snapshot in the
future commercial activation workflow. A quote is not a payable invoice,
and it cannot be paid before a real invoice is issued and verified by
the ViMi Pay platform owner.

Telegram Stars (XTR) are counted as separate confirmed turnover. They are
**not** fiat and are not converted via CBR. They require an independently
verified Stars reward-value source, withdrawal/refund/chargeback reconciliation
and a fixed-rate payout snapshot. Until then, any Stars-inclusive quote is
explicitly incomplete: no final payable total or invented exchange rate.
This PR does not issue a live consolidated invoice, enable billing/holds/
trials, send payment instructions, or interrupt any customer bot. Preserve
ViMi Pay's own My Bots fee exemption without Telegram creator checks.
Follow-up: Stars reconciliation, correction/credit ledger, durable
single-invoice currency selection and payment confirmation.

## Permanent ViMi Pay owner rule (2026-11-11, supersedes PR #119-#121)

**A bot/channel added via the platform owner's "👤 Мои боты" belongs to
the ViMi Pay owner account and is SaaS-exempt. Period.** The authoritative
server-side criterion is the project's `owner_user_id` (including its
historical `merchant_project_periods`) matching the database-provisioned
`platform_operators.role='owner'` account, with the bot belonging to that
project. No Telegram `creator` verification, screenshots, exemption approval,
extra buttons, preflight lists or separate UI workstreams. Never rely on
callback data or a self-declared username for billing identity.

Owner-account bots automatically skip RUB 150 monthly SaaS invoices,
5% platform commissions, merchant trial quotas, complimentary grants quota
and SaaS commercial holds. If ownership is transferred, future
billing depends on the recipient's actual project owner; prior owner periods
remain historically attributed. Other customers are unchanged and billable
under separately activated commercial policy. Billing, trial/quotas and
holds remain OFF until explicitly approved. There is only the existing
"🧾 Счета → 💳 Реквизиты" billing UI, no "Проверка исключений" button.
Migration `0050_my_bots_exempt` applies the rule in the
central DB function, grant quota and hold calculation and removes unused
audit functions; it creates no new invoices.

This is the controlling rule for subsequent PRs. Earlier paragraphs
describing Telegram creator-based exemption verification are historical,
not the current specification. Preserve RU/EN, native Rich where useful,
`compose.light100.yaml` and all active customer access.

## PR #117: merchant invoices, owner bank instructions and notices (2026-11-10)

Migration `0047_saas_owner_payment_ui` adds a single operator-controlled payment-instructions record, plus a durable, idempotent platform-invoice notification queue. These are **separate from merchant-to-customer payment methods**; no customer-facing checkout details, payment cards, or subscription access rights are changed.

- Platform owner opens existing **👑 Platform → 💼 Finances → 🧾 Invoices → 💳 Payment details** to enter, view, replace, or explicitly clear bank instructions. Every operation checks the configured platform Telegram owner and the privileged database `platform_operators` role. Sensitive bank instructions are never printed into server logs. They are HTML-escaped for Telegram output. The bank screen stays short; no exemption-audit or creator-check button is needed.
- Connected-bot administrator opens **⚙️ Settings → 🧾 ViMi Pay invoices**, with historical owner-only account invoices, due dates, RUB totals, receipt state, and published bank instructions. The read-only SECURITY DEFINER report is limited to the authenticated historical liable Telegram owner, not merely a moderator or a new owner after bot transfer. No cross-merchant invoice rows or direct financial-table grants.
- The existing central bot alert loop now schedules and sends invoice-issued / payment-due / seven-day-overdue Telegram reminders to the liable account (not through arbitrary customer bots). Outbox row is unique by invoice and kind, with skip-locked leasing, bounded retries, and suppressed pending messages after an operator-confirmed receipt. The messages contain the exact statement ID and payment instructions only when they have been configured.
- Notifications are **inactive** if platform SaaS billing is OFF or no bank instructions are published. Existing invoice/hold/trial switches remain OFF by default. A newly deployed migration will create no real bills and send no commercial notifications until an explicit, separately approved rollout.
- Bank transfer or a customer's `I paid` action NEVER marks a platform bill paid. The platform owner alone confirms actual received funds from their bank; the existing receipt function automatically releases any overdue hold.
- The platform statement history and notification delivery records are never deleted or repurposed as merchant-to-customer payment history. Message retry does not change ledger values or Telegram paid access.

**Activation remains a separate operator-controlled decision:** this PR deliberately has no toggle to begin automatic billing, charge accounts or freeze commercial activity. Complete prelaunch validation of payment instructions, FX for multi-currency commission, owner exemptions and dispute/refund adjustments before authorizing the existing dormant controls.

## PR #116 — opt-in SaaS overdue grace and new-commerce holds (2026-11-10)

Migration 0046 adds a *third OFF-by-default* control, `platform_saas_hold_control`, with seven days' grace. Only unpaid, issued platform invoices issued on/after the separately activated hold rollout date can pause **new** subscription checkout, manual/Stars file orders, bot connection and channel additions. Debt is owner-wide across Merchant/project IDs; it does NOT follow a transferred project to another owner. The platform-owner account and its own projects are automatically SaaS-exempt. Existing paid buyer entitlements and file redelivery, pending manual approval, customer subscriptions, customer-supervisor operation and existing bots remain untouched. Platform owner manual bank receipt confirmation automatically lifts the hold. Status is in the existing Finance -> Invoices panel, not a duplicate dashboard. RU/EN exceptions. Neither policy is activated or merged without owner authorization. See docs/PLATFORM_COMMERCIAL_BILLING.md.

PR #115 final hardening: trial slots count even BotInstance rows currently status=deleting until physical removal, so retirement/recovery cannot bypass one-bot policy. Grandfather eligibility for pre-rollout ownership transfer uses historical *overlap* between the transferred bot's connected lifetime and recipient's owner period, not its original bot_connected time. New PostgreSQL integration tests protect both scenarios. Still OFF by default.\n\nPR #115 hardening: Trial checks also cover the existing dual-consent ownership transfer path; a recipient cannot gain an extra trial bot or skip owner enrollment by changing project ownership. Bot created_at is not trusted for eligibility. Manual entitlement UPDATE cannot reactivate a revoked grant, change buyer/resource/source or extend validity for enrolled owners; ordinary revocation and paid-entitlement servicing remain unaffected. Policy stays OFF.\n\n## PR #115 — 72-hour once-per-owner trial and one shared complimentary-access quota (2026-11-10)

The new migration 0045 adds an **independent OFF-by-default** access policy for future new owners, a once-per-Telegram-owner trial claim on first successful bot registration, and a 3-per-channel monthly combined quota on non-paid manual access/paid-plan complimentary activations. Enforcement occurs on PostgreSQL bot/entitlement INSERT for concurrency safety and bypass resistance, rather than UI-only. Free grants are immutable audit events by original Telegram owner and channel ID, surviving reconnection, project or Merchant ID changes by that owner; one activation to N channels counts once **per channel**. Moderators cannot issue free grants or expand limits. Legacy owners with bots before rollout are grandfathered. ViMi Pay platform-owner projects are quota-exempt using the actual project-owner identity; admin privileges on a different customer's project do not change ownership. The broader SaaS billing control remains OFF, and no customer payment or existing access changes. See docs/PLATFORM_COMMERCIAL_BILLING.md.

## PR #114 — 2026-11-10: platform SaaS invoices (OFF by default)

Owner-only SECURITY DEFINER SQL operations for prepaid 150 RUB per bot, monthly 5% commission on approved subscriptions/manual file orders (explicit FX), single owner finance screen to review invoice status, two-step manual bank-receipt confirmation. Database control enabled=false by default, no paid invoices or blocks until an approved launch. Requires provisioned 72-hour owner trial for ordinary merchants; ViMi Pay owner-account projects are exempt by database ownership. See docs/PLATFORM_COMMERCIAL_BILLING.md for safety and next stages.

## PR #113 — dormant ViMi Pay platform invoice persistence (2026-11-10)

Migration 0043 introduces strict platform-only invoice, paid-source lines, immutable manually confirmed receipts and verified own-channel exemptions. It does NOT generate invoices, activate charging, alter any customer grants/payment access, or add Telegram buttons. New tables have FORCE RLS and no runtime CRUD permission. Only real approved Payment/FileManualOrder snapshots are eligible for the 5% ledger, with global source dedupe; a fixed RUB 150 line applies to an individual bot service period. The latest rule is owner-account project ownership (migration 0050); the older separate Telegram creator verification requirement is superseded. See docs/PLATFORM_COMMERCIAL_BILLING.md for the staged rollout and acceptance gates. No merge/deploy without exact-head CI/Codex review and explicit owner permission.

# ViMi Pay commercial SaaS billing direction — 2026-11-09

Approved business direction: one 72-hour trial per verified owner for one bot; RUB 150 prepaid for each bot's next monthly service period, plus 5% on persisted approved subscription/file purchase price snapshots, billed by Moscow calendar month; payment to platform owner by manually confirmed instructions. Overdue debts restrict only NEW commerce after grace, while all existing customer entitlements, payment reviews, access revocations and history continue operating. Complimentary access through manual grants or free plan activation needs a durable combined monthly quota, no moderator override, and separately controlled OWNER_SERVICE accounts. Billing, quotas and holds are NOT live; the first PR only adds the approved contract and a pure tested commission calculator. See docs/PLATFORM_COMMERCIAL_BILLING.md for the policy, safety boundaries and staged implementation order.

# Project Context — Telegram Membership SaaS

## Platform monitoring alert delivery — PR #111

Only **central** `@vimipay_bot → 👑 Platform → 🖥 Monitoring` owns
global health and incident reporting. A small task runs inside the
existing central bot process (no second Supervisor or service), reads
persisted supervisor heartbeat through a new narrow operator-only audited
SQL function and stores durable incident/outbox records under platform
owner RLS. Alert after 10 minutes of persistent non-running (20 minutes
queued/starting), recover only after two minutes of good heartbeats.
No alerts for owner-disabled, deleting or setup-needed connections.
At most one problem and recovery event per incident; Telegram send/DB
ack cannot guarantee exactly-once under uncertain transport failures.
All notifications link to existing `plat:bots:30:0` (no new buttons).

`0042_platform_monitor_alerts` migration must apply before central bot
restart. Rich bot card now shows one useful status, last signal, and
explanation only for problems; hides repeated project name.
No tenant payment, subscriber, access or resource operations touched.
Do not merge/deploy without explicit owner approval and exact-head
CI/Codex checks.

## Platform monitoring diagnostics — PR #110

Continue ONLY central `@vimipay_bot → 👑 Платформа → 🖥 Мониторинг`.
Do not introduce another menu, scanner or monitoring worker.
Existing audited `platform_report_snapshot` gains two safe per-bot
fields (`last_heartbeat` and `error_code`) in Alembic
`0041_monitoring_details`, unchanged authorization, finance and grants.
One bot card shows recent heartbeat and reason mapped from allowlisted
Supervisor error codes; never expose raw errors or secrets.
Recent restarts (5-minute display grace) and queued/starting workers are
**not confirmed outages**. No new automatic alerts in this slice; future
incident tracking must distinguish persistent failures and dedupe
recovery signals in the same monitoring section.
Deploy staging DB migration before central-bot code and preserve
`compose.light100.yaml`. Merge only after owner permission and exact-head
CI/Codex review.

## Single platform-wide monitoring entry (agreed rule)

All operator monitoring belongs in **one location**: central
`@vimipay_bot` → `👑 Платформа` → `🖥 Мониторинг`.
The first monitoring PR **relabels/reuses** existing `🤖 Все боты` and
its authorized `plat:bots`/`plat:detail` read-only reports. It moves
runtime rollups out of the central business overview; no new button, new
Supervisor or duplicate health computation elsewhere. RU/EN and native Rich
status table; classify non-running registered bots as "no confirmed running
signal", NOT as definitively broken. Existing per-bot owner readiness indicators
and necessary tenant access alerts stay where they are because these are
separate buyer obligations, not a second global operator console.
Next incident/alert/post-deploy checks must link back into this one
monitoring section, with no top-level duplicate "Problems"/"Status" menu.

Details: [PLATFORM_OWNER.md](PLATFORM_OWNER.md).

## Staging GitHub Actions deployment (manual, pending PR)

A restricted, manually triggered `staging-deploy.yml` deploy workflow is
proposed. It uses GitHub Environment `staging` (branch `main` only),
environment-scoped SSH private key / pinned host key and public host IP.
The SSH principal `vimi-deploy` has a forced command and may invoke a single
root-owned staging deploy script; it cannot open a terminal or access Docker
directly. An explicit `connection-test` operation is the default and does not
touch VM data. `deploy` checks exact `main` SHA on the VM, then builds,
migrates and restarts the central bot/customer supervisor using
`compose.light100.yaml`, retaining PostgreSQL. No auto deploy on merge.
Read [STAGING_GITHUB_ACTIONS.md](STAGING_GITHUB_ACTIONS.md).

**Do not merge this PR without explicit owner approval.** The GitHub connector
can create/review workflows and fetch logs but may not offer a button-run
action; tell the user how to click Run workflow manually.

## Channel cards / paid-only catalog deletion (current PR)

A practical admin upgrade takes priority over experimental Telegram Welcome
and Ephemeral Messages; #105 was closed unmerged. Connected-bot Channels now
gets per-channel Telegram member counts + confirmed payment/subscriber
statistics, and add/remove navigation.

Deletion of channels/tariffs/folders/files is blocked by **confirmed
purchases** only, not by merely created, submitted, rejected or cancelled
unpaid invoices. Do not destroy immutable financial snapshots to accomplish
this: migration **0040_catalog_soft_removal** provides project-scoped
`removed_at` tombstones. Catalog entries with unpaid invoice history disappear
from new sales while their orders, media, receipt-review workflow and future
legitimate redelivery survive. A paid channel/file remains protected. Existing
Telegram channels are not physically deleted; previously paid subscriptions
and manual/service access must not be interrupted.

See [CHANNEL_CATALOG_LIFECYCLE.md](CHANNEL_CATALOG_LIFECYCLE.md). Migration
0040 is mandatory **before** the central/customer bot rebuild and the
existing `compose.light100.yaml` overlay must be preserved. No merge/deploy
without explicit owner approval.

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

**Обязательное постоянное правило с 2026-11-10:** любой новый или
изменённый интерактивный Telegram-экран во всех режимах ViMi Pay должен
получать осмысленное нативное Rich-оформление (для сводок — таблица метрик,
для карточек — заголовок/структурированные блоки), а не только обычный
HTML с жирным заголовком. Проверять RU/EN, клавиатуры, безопасный fallback
и визуальный вид на staging в том же PR. Не дублировать меню, кнопки,
панели и рендереры, не менять бизнес-логику ради оформления. Исключения —
поля ввода/ForceReply, чеки, пользовательский контент, технические
неинтерактивные сообщения. Полное правило в
[RICH_MESSAGES_PLATFORM.md](RICH_MESSAGES_PLATFORM.md).

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
