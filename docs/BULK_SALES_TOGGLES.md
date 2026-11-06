# Connected owner bot: compact bulk sales switches (PR #63)

## UX
In the CONNECTED bot only:
- `📦 Тарифы`: one contextual button: `⏸️ Выключить все тарифы`
  when any plan is active, otherwise `▶️ Включить все тарифы` when
  archived plans exist.
- `💳 Оплата`: one contextual button `⏸️ Выключить все оплаты`
  or `▶️ Включить все оплаты`. The control counts every payment
  method in the project, not merely the currently displayed 8-item page.
- Button switches the whole *existing* set only after explicit confirmation.
  RU/EN, owner/project permission checks and selected-project reference.
  Stale Telegram callbacks are rejected with a hash of complete state.
- For a mixed set (some active and some paused), the one button chooses
  disable-all first. The next screen switches to enable-all.

## Business invariants
- Disable plans: `active → archived` to prevent new purchases.
- Enable plans: `archived → active`, **only if every affected plan**
  has an active resource and a price with an active compatible payment
  method. Otherwise the entire switch fails without partial changes.
- Unfinished `draft` plans are **never** auto-published or modified.
- Disable payment methods: `active → disabled`; enable: `disabled → active`
  after rejecting missing/placeholder bank instructions.
- Do not cancel or rewrite existing `PaymentRequest`, `Payment`,
  `Subscription`, `EntitlementGrant`, `AccessAction`, or subscriber access.
  The toggles only affect NEW purchases, not outstanding manual bank
  confirmations or current paid subscriptions.
- Changes use the existing per-project checkout/retirement transaction
  advisory lock, the project manager authorization check and row locks.
  Commit all affected status changes in one transaction.
- No schema migration, no change to platform-owner statistics or financial
  event definitions, no change to compose.light100.yaml.
- Only the connected `customer-supervisor` runtime needs a staging rebuild
  after merge. Never touch production or restart PostgreSQL.

## Caveats
- The controls modify current statuses, not a persistent global sales gate.
  Creating a brand-new tariff later still requires its existing publish
  workflow; no future status is silently changed by this bulk action.
- If previously archived plans should remain archived on resume, the owner
  should restore selectively using existing per-plan controls instead.
