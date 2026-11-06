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
SECURITY DEFINER BEFORE INSERT/UPDATE resource guard. A transaction advisory
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
