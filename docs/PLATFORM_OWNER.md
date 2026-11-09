# ViMi Pay v2 — platform owner control plane (Stage D)

**Approved behavior:** [VIMI_PAY_V2.md](VIMI_PAY_V2.md).

## Единый центр мониторинга (этап 1)

**Единственная точка входа:** центральный `@vimipay_bot` →
`👑 Панель платформы` → **`🖥 Мониторинг`**.

Это **замена существующей кнопки `🤖 Все боты`**, а не добавление
второго меню. Внутри остаётся прежний постраничный список всех подключений,
кнопки отдельных ботов и их агрегированные карточки: без дублирования
списков, платежей, пользователей или прав доступа. Старые `plat:bots` /
`plat:detail` callbacks остаются валидными.

Мониторинг показывает данные уже существующего
`public.platform_report_snapshot`, включая статусы Supervisor:
подключено, работает с подтверждённым heartbeat (окно 120 с),
нет подтверждения работы, явно отключено. **Нет подтверждения работы**
не приравнивается к сбою: это может быть настройка, запуск, очередь,
потеря heartbeat или временная недоступность. Никаких выдуманных
причин ошибок, показателей, last-seen дат или SLA. В карточках ботов
по-прежнему только имеющиеся runtime_state и коммерческие агрегаты.
Количество отключённых не считается ошибками.

Главное меню платформы сохраняет бизнес-цифры, но удаляет дублируемые
операционные индикаторы: они теперь только в «Мониторинге».
В клиентских/админских ботах не возникает новых глобальных кнопок.
Старый индикатор здоровья конкретного собственного бота остаётся
принадлежащим этому боту; существующие персональные уведомления
о доступе — часть обязательств перед покупателем, а не вторая
операторская панель.

Следующие этапы **только внутри того же раздела**: выявление
подтверждённых проблем на всей сети и причины из уже сохранённых
сигналов, уведомления владельцу платформы с возвратом в этот раздел,
проверка готовности после GitHub Actions. Никогда не создавать отдельный
пункт «Ошибки», «Состояние», «Здоровье», дублирующую панель
«Все боты» или второго Supervisor. Отдельная глобальная
сводка причин потребует защищённого аудируемого SQL-отчёта;
данный этап её не имитирует и не делает миграцию.

## Who sees this

Only the **configured platform owner** may access the additional
`👑 Платформа` mode in central `@vimipay_bot`. All other users continue to
see only the ordinary bot-connection registry. A platform owner can select
`🤖 Мои боты` and connect/administer personal bots like any other tenant,
then use `👑 Панель платформы` to return to global reporting.

Platform ownership **never** grants permission to review or approve another
tenant's payments, join private channels, or change tenant settings.
Connected owner bots do not mount the platform routes.

## One-time staging enablement (only after an approved merge)

1. Privately find the exact numeric Telegram user ID of the real platform
   owner. **Never use a username for authorization.** Telegram numeric IDs are
   not API tokens, but avoid posting them or app credentials in issues/chats.
2. On the staging server, set in its existing `.env`:
   `TMS_PLATFORM_OWNER_TELEGRAM_ID=<numeric user ID>`.
   Optional: `TMS_PLATFORM_TIMEZONE=Europe/Moscow` (IANA).
   Ensure a **distinct, privileged** `TMS_MIGRATION_DATABASE_URL` is already
   configured; it must not be the runtime DB role.
3. Apply Alembic `0019_platform_dashboard` as the migration DB owner.
   No existing payments, subscriptions, projects or bot connections are
   overwritten by the new migration.
4. With the same secure environment, run:
   `python -m telegram_membership_saas.platform_bootstrap`.
   This idempotently grants the configured numeric owner ID access in
   `platform_operators` and grants the runtime database role permission
   to call **only** the fixed aggregate report function. The CLI refuses
   silent transfer to a different platform owner. Never log DB secrets.
5. Restart the central bot only (existing bot access work remains unchanged)
   and privately open `/start` in `@vimipay_bot` from the owner account.
   Regular customers must not see platform controls.
6. Test a forged `plat:` callback from another account, per-bot reporting,
   time filters, return to `Мои боты`, and owner-bot buyer/admin workflows.

If `TMS_PLATFORM_OWNER_TELEGRAM_ID` is missing, the control-plane UI is not
mounted. If operator DB grants are absent, no global report is returned.

## What is collected and how it is interpreted

The report returns **aggregate, read-only data**, not personal buyer records.
Per-period values (Today in the configured zone / 7 / 30 / 365 days / All):
new connections and subscriptions, owner-approved payments, and
sum of approved payment amounts grouped by the original currency.
Current snapshots: connected/registered/disabled bots, truly-running bots
(fresh heartbeat), resources, published/draft tariffs, active subscriptions
and pending review requests.

Per-bot cards show username, registration/runtime state, counts of resources,
published tariffs, active subscriptions, confirmed payments and tenant
approved turnover by currency. No customer identity, bank details, receipt,
credential, or mutable payment controls are disclosed.

The value called **client turnover / tenant GMV** is the total of manually
approved Payment records, not independently bank-verified receipts. It must
never be mislabeled as the revenue of ViMi Pay. Platform SaaS revenue and
billing remain *not yet launched*, not a fictional zero or FX-converted total.

Existing bot rows vanish when a connection is safely deleted; complete
historical deleted-bot counts and billable usage metrics belong to the
subsequent append-only usage/metering stage (E). Do not claim the current
snapshot can report historical deletions.

## Immutable activity history — Stage E

The Platform Owner interface adds `📊 Использование и история`.
Usage events are recorded transactionally by database triggers, retain deleted
bot identity, and are reported by a second audited PostgreSQL function.
See [PLATFORM_USAGE_METERING.md](PLATFORM_USAGE_METERING.md).

After migration 0020, rerun the existing privileged `platform_bootstrap`
to grant runtime EXECUTE on the new fixed read-only report function.
There are **no platform billing charges** in Stage E.

## Database authorization and audit

Alembic 0019 introduces `platform_operators` (owner/admin/support schema,
only the owner role currently allowed to report),
`platform_report_audit`, and the fixed
`public.platform_report_snapshot(since,page,page_size)` function.

- Runtime DML on operator grants is denied by PostgreSQL FORCE RLS. Operators
  can only be provisioned using the dedicated migration database credential.
- Reports use a fixed parameterized SQL function with `SECURITY DEFINER`
  that **first checks** the server-bound actor against the operator grants.
  Calls are audit-logged before generating aggregates.
- The Telegram handler independently checks the actual `from_user.id`
  against `TMS_PLATFORM_OWNER_TELEGRAM_ID`. Neither client callbacks nor
  tenant claims can select another Telegram identity or access raw SQL.
- SQL inputs are only a constrained timestamp and bounded report page;
  the response is a fixed schema with counts/sums and pseudonym-free bot
  metadata. There is no arbitrary query, unrestricted tenant-admin API, or
  global mutation endpoint exposed through this interface.
- Existing per-project PostgreSQL tenant RLS, commercial transactions and
  owner/admin roles are not changed by this stage.

Full billing, platform operator delegation, anomaly alerts and usage events
are explicitly reserved for later work.


## Merchant financial continuity (PR #66)

A new operator-only `💼 Финансы бизнесов` section in the central bot
reports immutable approved payments per permanent Merchant ID and currency,
with a clear distinction between channel-admin confirmations and verified
bank receipts. History remains linked when a project is removed, using
its historical merchant/project periods. No reports are exposed in
connected customer bots. The fixed PostgreSQL function
`platform_merchant_finances` checks the DB operator ACL and audits each
invocation; grant it with the privileged platform bootstrap after migration
0028. This is not SaaS billing.


## PR #67 — future SaaS commercial protection

The staging-only schema extends merchant continuity with an opt-in bot
connection quota per verified Telegram owner, protected one-time merchant
or owner platform trial claims, and idempotent original-payment commission
reservations. All price, trial and limit *policies* are intentionally absent
by default: ViMi Pay does not grant trials, bill commissions, or restrict
currently connected owners automatically. A separate operator-approved
billing product and trustworthy payment reconciliation are required before
charges. Cross-owner Chat ID reuse produces protected **review signals**,
not fraud verdicts. See [MERCHANT_CONTINUITY.md](MERCHANT_CONTINUITY.md).


## PR #68 — private risk observation panel

`🛡 Проверка каналов` shows observations of Telegram Chat IDs reused
under a *different* account, as identified by PR67. It presents the channel
ID, project/channel labels and local timestamp with paginated period
filters, solely to the separately authorized Platform Owner. A
`platform_merchant_risk_snapshot` SECURITY DEFINER function validates
DB operator permissions and audits each read. Normal tenants cannot query
raw `merchant_risk_observations`. These are manual review signals, not
fraud determinations: lawful ownership transfers remain supported. No
automatic sanctions or billing. Apply migration 0030, privileged platform
bootstrap and a central-only rebuild on staging after approved merge.
