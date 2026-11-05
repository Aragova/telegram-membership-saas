# ViMi Pay — Connected-bot owner statistics (PR #56)

## Scope and safety

The project manager sees a read-only **📊 Статистика / Statistics** screen in
their own connected Telegram bot. The central `@vimipay_bot` does not expose
tenant sales data or these callbacks. This work depends on PR #55 for its
starting code; do not merge #56 before #55.

- Calendar-day period filters: current local day + previous six days,
  current local day + previous 29 days, or all recorded time. The boundary is
  midnight in the project's configured IANA timezone, stored/query-filtered
  in UTC; daylight-saving transitions are calendar-correct.
- Sales = actual rows in `payments` with `approved_at`, not unapproved
  requests, screenshots, currency exchange conversions or ViMi Pay revenue.
  Sum immutable `Payment.final_amount` by original `currency_code` in
  PostgreSQL Numeric/Decimal. Show confirmed-payment counts alongside totals.
- Current state (independent of sales period): distinct customers with
  effective active `subscriptions`, distinct paid customers expiring within
  the next seven days, active **manual grants** counted separately, and
  pending requests awaiting a bank-balance check.
- All registered project customers and newly registered customers in period
  are listed separately; they must never be mistaken for paying customers.
- Management authorization through the existing project manager permission,
  project-scoped RLS, and fixed BotInstance project context.
- Read-only aggregate SQL: no payment, access, subscription, outbox or owner
  service state is modified; no migrations, new workers or extra VM resources.

## Acceptance

- Connected-bot admin sees concise RU/EN screen and 7 / 30 / all period buttons.
- Central and unauthorised users cannot read tenant stats.
- PostgreSQL integration covers real approved sale, exact currency total,
  paid subscription, manually separate access, another project and outsider.
- Existing checkout and paid rights are unchanged.
- Review CI and Codex at exact current HEAD before any merge.
