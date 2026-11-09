# Telegram-native subscription and payment dates (2026)

## Contract

The presentation helper `src/telegram_membership_saas/telegram_dates.py`
emits Telegram Bot API `<tg-time unix="..." format="dt|d|r">fallback</tg-time>`
for **timezone-aware** Python datetimes. Unix seconds always represent the
original instant, independently of the recipient's project or client timezone.

- `dt`: localized short date and local clock time, used for subscription and
  access deadlines.
- `d`: localized short date, used for payment/invoice history.
- `r`: optional native relative rendering (not combined with other flags).
- The **inner text** remains a readable, escaped, project-timezone date/time
  for old Telegram clients. A client supporting native date entities renders
  the same instant using **the viewing user's locale and timezone**.
- No native tag for dates before Unix epoch or after now + 1098 days: use the
  readable fallback without causing Telegram parser errors.
- Lifetime/no expiry stays literal `бессрочно / lifetime`. Do not invent an
  expiry or convert `None` to `0`.
- Never HTML-escape a *finished native tag* again. Escape all user-controlled
  strings, including plan names and currencies, separately.

Official API:
https://core.telegram.org/bots/api#formatting-options
https://core.telegram.org/api/entities

## Visible changes

- Connected bot > buyer > My purchases > My subscriptions.
- Payment approval/activation messages to channel owners and customers.
- Owner subscriber cards: subscriptions, paid history, and grant deadlines;
  classic and Rich table rendering.
- Owner invoice-history Rich cards and classic invoice history.
- Owner daily paid-term expiration digest.

No other date fields, financial calculation, `Subscription.current_period_end`,
`EntitlementGrant.valid_until`, UTC storage, callback/action, billing
or access worker code is changed. No migrations or secrets.

## Staging acceptance (real Telegram)

1. View one **finite** test subscription in My subscriptions. It should show
   a tappable localized date; on a supported client date/time follows **the
   viewer's timezone** and offers Telegram's native date actions.
2. View the same customer as owner and compare Rich and classic card date
   against original UTC instant; use dates near midnight and, if possible, DST.
3. Approve one test payment and verify its deadline in buyer/owner messages
   without changing amount, plan, or access.
4. View payment/invoice history and an expiry notification if one is due.
5. Verify a lifetime subscription still says lifetime and has **no date tag**.
6. Verify RU and EN and a client without native formatted-date support.
7. Confirm no duplicate renewal, payment state change, or access grant change.

Keep staging's `compose.light100.yaml` and all existing connected bots.
