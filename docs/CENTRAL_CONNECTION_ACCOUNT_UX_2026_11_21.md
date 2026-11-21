# Central ViMi Pay: approved connection and account-bill UX (21 Nov 2026)

## Scope

This one UX slice changes the **central** @vimipay_bot, not the connected
merchant bots' checkout or platform financial calculation rules.

- **🔑 Update token:** Rich + classic RU/EN. Open @BotFather, choose the bot,
  obtain API Token, and send it **as a normal message in the current ViMi Pay
  chat**. The existing secure prompt binding, token format validation,
  incoming-message deletion, and credential encryption are reused.
- **✏️ Rename connection:** Rich + classic RU/EN. Owner enters a 2–120-character
  internal display label in this chat. This never changes Telegram @username.
- **🗑 Disconnect bot:** Rich + classic RU/EN with a live
  `count_retirement_blockers` owner-scoped snapshot. The red confirmation is
  visible only when no blockers are present. The server-side retirement
  service **rechecks** under its existing safety lock even after confirmation.
  Existing project configuration, payment history, and archival policy remain.
- **👥 Customers and access:** human-readable counts and grouped actions:
  open purchase requests, paid subscriptions, other access and pending access
  processing. Users see 12 entries per category page. Existing confirm
  actions, buyer notifications and safety warnings still run; there is no
  silent early termination of paid subscriptions, implicit refund, or
  fabricated successful Telegram access revocation. File purchases, pending
  Stars checkouts and active native paid links are also explained and owners
  are directed to their bot; these protections are never bypassed.
- **🧾 Account bill:** both approved variants (no debt / issued unpaid bills),
  green debt-free or warning state, finance summary, overdue count/amount,
  total payable amount, and nearest *actually issued future* payment if present.
  It uses the existing `merchant_saas_statements` and
  `read_owner_network_home` owner-scoped read-only reports. No newly invented
  deadline, invoice or settlement amount. Working open-nearest,
  all-issued-invoices (paginated), my-connections and main-menu buttons.
  Paid historical invoice access remains available.

## Presentation and navigation

Follow `docs/RICH_UI_STYLE_GUIDE.md`: larger semantic headings, bold action
titles, short italic explanations, clear arrows, true data, escaped external
names, full RU/EN and classic HTML fallback. No raw token, UUID, technical
database field or fake button. One contextual **⬅️ Back** on input prompts
rather than two competing exit controls. Existing generation-bound prompt
cancellation prevents old keyboards from canceling a newer input.

## Verification

Run the full CI and review exact PR HEAD. Cover RU/EN and HTML escaping,
empty and due account invoices, nearest due date, all categories of retirement
blockers, input binding, clean back navigation, protected disconnect,
12-row pagination, and existing owner authorization. Production-like tests
should use real test bots without mutating paid buyer data.

Do not merge or deploy without explicit owner approval.
