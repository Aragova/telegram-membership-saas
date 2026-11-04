# ViMi Pay v1.5 — administrative owner journey acceptance

The goal is **one complete administrative journey before repeating a buyer E2E**.
This is not another payment prototype. The backend already supports checkout,
receipts, manual review and access enforcement; the missing part is owner
self-service.

## New central-bot screens

- /start and /menu open central ViMi Pay for every Telegram owner, including
  previously unknown accounts. A new actor can create a project immediately
  without SSH or SQL. Existing owners can select between their projects.
- Durable `OwnerSetupSession` persists the selected project, wizard step,
  payload and the **exact Telegram ForceReply prompt message ID**. Wizard steps
  expire after 15 minutes; mismatched/stale replies must not be consumed.
- Bot token: owner sends it as a private ForceReply. The message must be
  successfully deleted **before** getMe/encryption. The token is never stored
  in the session table; `BotInstance` retains only encrypted credentials.
- Channel/group: native Telegram chat picker or manual chat ID/@username.
  The connected **owner bot**, not the central bot, validates the real Telegram
  chat, verifies the applicant is its admin, and checks the customer bot can
  invite/ban users. Do not register an unverified chat.
- Tariff: draft plan → choose duration → attach one active channel/group →
  add a versioned currency/price via the existing pricing service.
- Payment method: choose active currency → name method → provide instruction
  template via the existing `ManualSettlement` service. No receipts/files
  are saved.
- Publish: only when the plan has an active linked resource, an active price
  and an active payment method supporting at least one priced currency.
- Timezone: Europe/Moscow default; owner can change it to an IANA zone.
- Dashboard reports **configuration state**, not false positive runtime health.
  Registering a BotInstance does not imply its long-polling worker is online.

## Existing product compatibility

Existing projects, subscriptions, payment decisions, access-grants and previous
buyer E2E remain untouched. The old admin router remains as a compatibility
layer, but the new central wizard precedes it for /start and /menu.
This is one consolidated owner-setup workstream, not a request to manually
retest payment after every edit.

## Already noticed copy defects — must stay fixed

- No raw UUID in human-facing bot connection prompts.
- No developer-facing word `runtime` in Telegram instructions.
- No claims that a bot is accepting sales merely because it is registered.
- No exposed bot tokens, hidden token excerpts or raw exception traces.
- Owner should never need `/admin` to enter the console.

## Remaining gate for a *true* hands-off complete product

The new owner config is usable from Telegram, but the existing Customer-Bot
Runtime only starts via an explicitly configured project/container profile.
A multi-project automatic startup/health controller (and operational recovery)
must be integrated **before claiming fully automated start of sales**.
The acceptance test is not complete until a new owner, with no VM access,
can configure and start their own customer bot and the full buyer checkout
is exercised once afterward. Do not offer manual .env/SQL instructions as
a substitute for this acceptance criterion.

## CI scope

- Real PostgreSQL/RLS integration test creates a new owner/project and runs
  channel verification, tariff/price, payment, publication and tenant isolation.
- Router tests cover /start and project selection, wizard prompt matching,
  deletion of bot tokens, stale replies, and checkout/admin isolation.
- Existing buyer tests continue running as regression checks.
- Merge only after complete CI, Codex exact-head review, no unresolved threads,
  and explicit user approval.
