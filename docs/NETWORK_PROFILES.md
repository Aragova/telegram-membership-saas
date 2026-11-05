# ViMi Pay — Network Profiles v1

## Business scenario

One Telegram owner manages multiple independent private channels, each served
by its own subscription bot. The owner configures plans and manual payment
instructions on the first bot, opens **📁 Профиль сети** inside that bot's
admin panel, and explicitly saves a template. The next **new bot connected
via central @vimipay_bot** automatically receives the template.

**Existing connected bots are never changed by this feature.** The profile
must be opted into; nothing is copied before the owner confirms it. One
default profile per owner is supported in v1. Re-saving creates a new profile
version used only for future bots. Pause/resume auto-copy is available in
the same card. A future version may add multiple named profiles and bulk
updates; it is NOT implemented by this change.

## Copy contract

Included from the owner's source project:
- Draft and published plans (not archived), including names, durations,
  sort order and **currently active** per-currency prices; retired historical
  price versions are not copied.
- Manual payment methods, their compatible currencies and names.
- Payment instructions/bank details **only when the owner explicitly chooses
  the "С реквизитами" confirmation**. Otherwise methods get safe placeholder
  instructions that must be edited before enabling.

Every target receives fresh project-scoped UUIDs:
- Each plan is a **draft** without a channel (PlanResource).
- Each payment method is **disabled**.
- No new project has sale-ready offerings until its owner verifies its own
  private channel, checks payment details, enables methods and publishes plans.
- On verification of the **first active channel** in a profile-imported
  project, previously unbound draft plans are linked to that channel.
  The project row is locked before inserting a verified channel, so even two
  concurrent first-channel verifications cannot bind one plan to both.
  Project admins may verify a channel and bind drafts via a narrow application
  marker RLS policy, but cannot read or change the owner's private profile.
  No existing plan/resource binding changes on subsequent channel connections.

Explicitly excluded: bot credentials and tokens, Telegram chat/resource IDs,
project members, consumers, payment requests, payments, subscriptions,
entitlement grants, outbox, financial reports and analytics.

The source project and its subscribers are never changed. Copies never share
live Plan or PaymentMethod identity with the source.

## Identity, ownership and isolation

The central registrar already authenticates Telegram users by stable numeric
Telegram user ID. Every new bot project is created under owner_user_id. It
selects the owner's opt-in profile and applies an immutable JSON snapshot
in the **same project registration transaction**. The project bot token is
never stored in the template.

Profiles and applications have PostgreSQL FORCE RLS. The profile is visible
only to the owning TelegramUser. Saving a source project additionally requires
Project.owner_user_id == actor_id, not merely an admin/member role.
Applications are owner- and target-project-scoped. A connected bot's owner UI
may manage the owner's profile; the central bot may not edit tariffs, methods,
or profile configuration.

The current v1 schema stores one editable owner profile with a monotonically
increasing version and an application record per new project. Each application
is unique by target project_id; retries of the import service are idempotent.
No automatic refresh or changes to existing projects when the profile changes.

## Failure behavior

Profile data is read from the source as a snapshot, and every new target
configuration is recreated through existing price precision / manual payment
validators inside a nested transaction. If an unsupported currency or corrupted
template makes import fail, only the imported configuration is rolled back:
the independently verified new bot registration succeeds, application status
becomes `failed`, and central displays a warning. Users can configure that
bot manually. There is no silent partial import.

If the owner pauses auto-copy, newly connected bots follow the original
empty setup. Token rotation or reconnection to an existing project does not
reapply the profile; copying occurs only in `register_new_owner_bot`.

## UI and operational concerns

No additional Telegram native Menu commands. The single owner-dashboard button
**📁 Профиль сети** opens a compact profile card. Refreshing the profile
replaces the snapshot and increments its version; opt-in always requires
explicit choice about banking instructions. Existing buyers are unaffected.

Database migration `0023_owner_network_profiles` is required **before**
deploying new customer/central bots. Staging must be backed up before migration.
No production change, no hidden table deletion or checkout handler rewrite.
Owner-only RLS migration tests, registration, snapshot content, disabled status,
idempotence, pause, versioning and channel binding run in GitHub CI with real
PostgreSQL before merge. A staging Telegram E2E must then verify the two-bot
path; never create fake real-world tokens or real sales to simulate this.

## Not implemented in v1

Mass updates across existing 50+ bots, multiple source profiles, automatic
publishing, copying subscriptions or payments, secret provider API credentials,
and blanket bot/channel administrator permission copies. None should be
implied by the initial version.
