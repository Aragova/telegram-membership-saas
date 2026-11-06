# ViMi Pay — PR #64: Merchant ID continuity foundation

## Non-disruptive migration

- One immutable Merchant ID is created for **each pre-existing project**.
  Even projects owned by the same Telegram user are NOT silently merged:
  one person can legitimately operate independent businesses.
- New projects receive their own Merchant ID in the **same transaction** as
  project creation, via database triggers. A failed registration rolls back
  the identity and the project together.
- Original `payments`, `payment_requests`, `subscriptions`, `resources`,
  `bot_instances`, pricing, tokens and grants stay untouched.
- Historical merchant/project periods have no cascading project foreign key;
  the merchant survives bot removal and any later deliberate project purge.
- Telegram numeric Chat IDs are recorded as **unverified observations**, not
  proof of business ownership. The same Chat ID in multiple projects does not
  merge their finances or expose one owner's data to another.
- Tables `merchant_identities`, `merchant_project_periods` and
  `merchant_channel_periods` use FORCE RLS and have no permissive tenant
  policies. The project creation/resource observation triggers are narrowly
  scoped SECURITY DEFINER functions with fixed schema search paths.
- No owner API yet permits changing an established Merchant ID or project
  owner; both changes are rejected by a database guard until a verified
  controlled transfer workflow is implemented.
- No new bot UI or runtime dependencies; no accounting, limits, charges,
  subscriptions or payment confirmation logic changes in this PR.

## Rollout

After PR review, merge approval and a tested staging update, run migration
`0026_merchant_continuity_foundation` using the **migration** DB role.
Keep `compose.light100.yaml` and the current live bot fleet as-is.
Compare counts: merchant identities == existing project count on initial
backfill; each project has exactly one active period; duplicated Chat IDs
remain distinct *observations* across merchants. Do not update production.

## Next increments

- PR #65: risk-aware existing-business continuation when registering a new
  bot/project, verifying original ownership. Never infer business authority
  merely from shared Telegram admins or Chat IDs.
- PR #66: aggregate merchant-level historical turnover by original currency
  and count each unique Payment once.
- PR #67: merchant-level trial/limit/charge continuity and antifraud reviews.
  No unverifiable claims about direct bank transfers.

Security caveat: Merchant ID at this stage is a **persistent data foundation**,
not yet a complete defense against intentionally creating a fresh project.


## PR #65 — same-owner channel continuity enforcement

After the owner and connected bot both pass the existing live Telegram
administrator checks, an active channel cannot be added to a *different*
existing project of that same real owner if that Telegram numeric Chat ID was
previously linked to another still-existing project. Historical observations
remain protective even when the old resource association is deleted.

The owner sees a short RU/EN message directing them to @vimipay_bot →
My connections, to restore an archived original connection if needed and
attach a replacement bot **to that original project**. Payments,
subscriptions, access grants, owner/merchant IDs and existing bot credentials
are never copied or reassigned.

Enforcement is in PostgreSQL migration 0027, under a narrowly scoped
SECURITY DEFINER BEFORE INSERT/UPDATE resource guard. It validates the invoker's
selected tenant and owner/admin identity **before reading privileged history**, so
foreign project UUIDs cannot probe another tenant's channel observations.
A transaction advisory
lock by numeric Telegram Chat ID closes concurrent registrations. The
runtime role cannot read private historical merchant data and no old-owner
project existence is disclosed to a different Telegram owner.

Removing an entirely history-free old project permits subsequent reuse,
since the original project no longer exists. A genuinely transferred channel
registered by a *different* Telegram owner is never automatically grouped
with its previous owner; a separately verified transfer workflow is future
work. Trial/billing continuity for deliberately switched accounts or
different channel IDs is addressed separately in PR #67.

Rollout: migrate 0027 before restarting the new connected-bot supervisor;
central bot and existing database rows require no destructive changes.


## PR #66 — merchant-level historical financial reporting

An owner-only, audited PostgreSQL function `platform_merchant_finances`
aggregates original `payment_approved` events from the immutable usage
ledger, grouping by permanent Merchant ID and **original currency**. Each
unique Payment UUID is counted once, regardless of duplicated metering
observations. Decimal amounts remain exact: no FX conversion.

Attribution uses `merchant_project_periods` and the actual event timestamp,
not the Telegram bot ID, username or Chat ID. It remains valid when a bot
is removed or an old project is deleted. Rows that predate available
merchant history are reported separately as **unattributable**, never
silently assigned to the newest project.

The central Platform Owner screen exposes a private RU/EN
`💼 Финансы бизнесов` button with period selection and pagination.
This data is not accessible to channel owners or buyers and cannot approve,
modify or generate payments. The displayed amounts are admin-confirmed
client turnover, **not independently verified bank deposits or ViMi Pay
SaaS revenue**.

Rollout: migration `0028_merchant_financial_report` before rebuilding the
central bot. The migration grants runtime EXECUTE only on the fixed report
for the existing runtime database role; `platform_bootstrap` may be rerun
idempotently. No changes to payments, subscriptions, resources or live
bot supervisors. Retain `compose.light100.yaml`.


## PR #67 — commercial safety without activating paid billing

Migration `0029_merchant_commercial_guards` creates four protected
platform-only ledgers and new database guards. All four tables have FORCE
RLS, no tenant policies and explicitly REVOKE default runtime table grants.

- `platform_owner_bot_limits`: optional maximum **connected** bots per
  original Telegram owner across **all** their distinct projects and
  Merchant IDs. A missing row means unlimited (current default). Once an
  operator explicitly configures a cap, a DB BEFORE INSERT trigger serializes
  concurrent registrations using an owner-scoped transaction advisory lock
  and blocks excess connections with SQLSTATE `P6701`. Disabled bots
  remain connected and consume slots; deleting the bot releases its slot.
  Updating an already-connected bot's token does not consume another slot.
  There is no forced suspension of existing bots when a cap is lowered.
- `merchant_platform_trial_claims`: protected lifetime history of future
  **SaaS merchant** trial claims, completely separate from subscriber trial
  entitlements. The configured trial policy chooses `owner` (one claim per
  Telegram account across all independent businesses) or `merchant`
  (one per Merchant ID). Scoped unique constraints enforce atomic,
  concurrency-safe one-time use; SQL triggers reject mismatched owner IDs
  and any UPDATE/DELETE. **There is no automatic trial offer or claim**.
- `merchant_commission_reservations`: future billing **idempotency
  reservations**, not charges. One canonical immutable Payment UUID may
  be reserved once, after its event identity and historical Merchant ID are
  verified by the database. No commission percentages, fees, invoices,
  debits, subscriptions, collection or platform revenue are created. The
  table starts empty and has no tenant-facing API.
- `merchant_risk_observations`: append-only platform-only signals when a
  historical Telegram Chat ID appears under a *different* owner. These
  observations do not prove fraud or ownership; legitimate transfers are
  **never automatically blocked or merged**. Operator review is necessary
  before any trust/identity decisions. No private data is shown to tenants.

The existing channel-history guard now also rejects a same-owner attempt
to reuse a channel from a **purged** original project if immutable approved
payments or prior SaaS trial claims/reservations prove commercial use.
History-free purged projects remain reusable. This does not assume two
businesses run by one Telegram account are the same business. The error
directs the owner to platform support rather than a no-longer-existing
project.

**Limitations**: A fresh Telegram account with changed channel identity
cannot reliably be correlated to a previous legal person without a separately
verified transfer/identity process. Manually approved customer payments are
not bank-verified; no fee can be calculated reliably from them without
an approved platform policy and reconciliation. Neither trials nor limits
are activated by this migration. Later operator interfaces must require
explicit policy approval, auditable changes and rollout checks.

Staging rollout: apply Alembic 0029 **before** rebuilding the central bot
and the connected-bot supervisor; preserve `compose.light100.yaml`.
Do not touch production or deploy/merge before explicit approval.
