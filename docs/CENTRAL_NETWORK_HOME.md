# ViMi Pay central owner home

The **central** @vimipay_bot has one deterministic account-level entrypoint.
Tenant admin bots keep their independent project dashboards and command menus.

## Native Telegram menu

- \`/start\` — \`🏠 Главное меню / Main menu\`.
- \`/bots\` — \`🤖 Мои подключения / My connections\` (always list, regardless of selected project).
- \`/billing\` — \`🧾 Счёт аккаунта / Account bill\` (all historical liabilities, including disconnected and transferred bots).
- \`/menu\` and \`/help\` remain accepted but hidden for existing links.
- First use **before language selection** retains the original RU/EN language welcome. After selection, \`/start\` always shows the account home, even with zero connected bots or an archived-only account.
- A bot's own bill remains **🧾 Счёт бота** in that bot's connection management.

## Three home sections

**📊 ВАША СЕТЬ / YOUR NETWORK:** connected registered bot count, healthy,
attention, and down/error. Health is derived from the **same <=40-second
heartbeat and bot identity matching contract** as the existing public bot
health service. A registered token alone is not proof the bot is running.
A healthy runtime does not promise that tariffs and payments have been set up.

**💰 ВЫРУЧКА ВСЕЙ СЕТИ / NETWORK SALES:** all-time, *confirmed gross* native
amounts during the account owner's historical project ownership, grouped by
currency. Includes manual subscriptions/files, bot-mediated Telegram Stars
subscriptions/files, and paid mainnet Crypto Pay invoices with or without USD
valuation/capture. Every confirmed purchase is counted **once**, regardless
of commission capture. Zero native currency totals are omitted, with no FX
conversion or cross-currency addition. Gross amounts are **not** profit or
withdrawable balances and do not deduct returns, fees or refunds. Purchases
made directly within Telegram channels without bot-visible evidence cannot
be inferred by the Telegram Bot API and must not be fabricated.

**🧾 РАСЧЁТЫ С ViMi Pay / ViMi Pay BILLING:** real issued unpaid invoices
across the owner's entire historical account liability, a separate overdue
count/amount, and the earliest *future* issued unpaid invoice. Shows its
original Moscow-local due date, remaining RUB amount, and @bot username.
If there are multiple invoices due on that same Moscow calendar date,
shows \`+ ещё N / + another N\`. An inline button opens that **exact issued
invoice** (owner authorization rechecked in the existing invoice route).
Deleted bots have a neutral "Previously disconnected bot" label and still
link to their retained invoice. No fictional upcoming date is shown for
unissued future charges or unexpired trials.

## Implementation and fail-safe behavior

- New read-only \`platform_saas_owner_network_home()\` SECURITY DEFINER RPC,
  one owner identity from \`app.current_user_id\` and **one DB roundtrip** for
  the entire network. It never writes billing or sales records.
- The application role has only \`EXECUTE\` rights to the protected report,
  not unrestricted finance table read privileges.
- A lost/incomplete reporting snapshot shows an explicit warning and
  preserves all normal navigation. It does **not** show false zero sales,
  zero debts or green healthy counts.
- RU/EN Rich and classic HTML have the same financial meaning. Bot labels
  are HTML-escaped. No UUIDs or tokens are shown in messages.
- Inline keyboard: green My connections / Add bot; blue Account bill /
  language; direct green external support link to
  [@vimi_support_bot](https://t.me/vimi_support_bot). For an actual nearest
  invoice, one blue direct-invoice button is added.
- No inert **Settings** button is included: central bot currently has no
  independent account settings page; bot-specific settings belong inside
  each connected bot. Do not add duplicate or placeholder navigation.
- The standalone platform owner dashboard remains reachable through the
  existing **👑 Панель платформы** link.
- No automatic deployment. Review and explicit owner authorization are
  mandatory before merge, and the staging deployment workflow remains
  manual-only.
