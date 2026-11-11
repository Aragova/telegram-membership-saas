# ViMi Pay — approved commercial concept v1 (2026-11-09)

Status: product direction agreed; implementation phased, **all billing and restrictions DISABLED until separately approved rollout**. This document supplements VIMI_PAY_V2.md and MERCHANT_CONTINUITY.md.

## 1. Commercial offer

- **Trial:** 72 consecutive hours for one connected bot, claimed **once per verified Telegram owner**, including across their projects and historical Merchant IDs. Starts when their first bot is successfully connected, not when the user first opens /start. Trial never resets on deletion, reconnection, token rotation or changing a project.
- **Base fee:** RUB 150 per connected bot, prepaid for its next monthly service period. First period starts at trial expiry; following monthly periods renew on the corresponding calendar date (clamped for short months), unless explicitly cancelled. This separates bot-service anniversaries from commission accounting months, without short-month overcharges. No charge for simply opening the central bot.
- **Sales commission:** 5% of **approved and persisted purchases**, separately for each native-currency sale, including subscriptions and digital-file orders. The administrator pressing **Approve** is the billing event. Unpaid, cancelled or rejected invoices do not count. The pricing snapshot frozen on the purchase is authoritative, never a subsequently edited tariff.
- **Commission period:** Moscow calendar month, [first day 00:00, next month first day 00:00); statement generated after close and due within 7 days. Distinguish service prepayment and accrued commission; one owner can have several bots and merchant businesses, and an invoice must show the scope of each line.
- **Platform settlement:** buyer/merchant owner receives ViMi Pay's configured payment instructions, submits confirmation, then **the platform owner manually confirms actual receipt**. Never treat clicking "I paid" or uploading a receipt as confirmed platform revenue. Bank instructions and secrets must remain outside git. No automatic deductions or unapproved collection.
- **Currencies:** preserve original sale amount and currency. For a RUB invoice use an explicitly published, versioned month-specific conversion rule, immutable rate snapshot on each charge, a deterministic rounding policy and reconciliation. No floating point money, no implicit exchange rates or cross-currency addition. If a rate is missing, stop invoicing and show an operator exception, not zero commission.
- **Refunds:** only authenticated, audited refund/credit adjustments. Do not change the original payment or charge; a negative compensating entry refers to the original source and period. Duplicate sales/refunds may never be charged twice.
- **Existing customers:** no retroactive trial claims, debt or sales charges; commercial activation must have a separately approved start date and explicit owner communication.
- **ViMi Pay's own bots are always exempt.** A bot/channel connected through the platform owner's **«👤 Мои боты»** is exempt from RUB 150 SaaS fees, the 5% ViMi Pay commission, the trial/complimentary quota and SaaS debt holds. No separate Telegram creator verification or proof collection is required.
- **Identity comes from the database:** `projects.owner_user_id` and the historical `merchant_project_periods` owner must match the database-provisioned `platform_operators.role='owner'` account, and the bot must belong to that project. Membership as an administrator/moderator in another customer's project never establishes ownership. Callback data, token usernames, and Telegram group permissions never decide platform billing exemptions.
- **Transfers are prospective:** a transferred project stops being owner-exempt once it belongs to a non-platform account; previous owner periods must not be billed retroactively. Existing customer entitlements and financial history remain unchanged. No admin-facing toggle, extra “verify” screen or manual owner exemption record is necessary for operator-owned projects.


## 2. Protecting the paid event boundary

A paid subscription is recorded through the existing Payment approval flow, using Payment.id and Payment.final_amount/currency_code/approved_at. A manually paid file is recorded through file_sale_manual_orders when its state becomes approved, using its id and snapshotted amount/currency_code/paid_at. For other already-supported file providers, adapters must identify a **verified** successful purchase and its canonical unique source ID before inclusion. The same successful checkout may appear in more than one subsystem: enforce globally idempotent billing source keys.

A native amount must be > 0. Do not allow tariff or file sale edits, duplicate approvals, renames, deleting bots, switching merchant connections, or later price changes to erase or replay accrued fees. Customer gross is **not** ViMi Pay's revenue; only platform receipts confirmed by the platform owner are platform revenue.

The guarantee is limited to transactions **processed and approved inside ViMi Pay**. It cannot prove undisclosed direct-bank transfers or trades completed outside the platform.

## 3. Complimentary access anti-abuse

Manual no-payment grants and complimentary activations of paid plans consume **one combined quota of 3 activations per original channel per Moscow calendar month**, keyed by durable Merchant ID and channel identity across bot replacements and projects. The quota counts committed issue events, including later revoked or expired ones; revoke/reissue never restores a slot. Two simultaneous grants must not exceed quota (database serialization and uniqueness).

Only the verified project owner can use this allowance; ordinary admins/moderators cannot create complimentary grants, enlarge the quota, or impersonate the owner. Grant-by-duration and activate-existing-plan must share one enforcement point; prevent bypass through alternate handlers, direct service calls, scheduled tasks or renaming plans. No zero-priced commercial checkout. Any genuine exceptional compensation needs platform operator review, a reason, separate auditable authorization, and must not masquerade as a paid purchase.

Existing OWNER_SERVICE grants for verified, separately configured owner/service accounts remain outside the quota; this is a narrowly authorized role, not a general free-membership mechanism. It cannot be used for an arbitrary buyer without independently verified eligibility. No commissions on noncommercial entitlements.

## 4. Payment states and debt policy

- **trial:** one eligible bot works for 72h, with normal checkout; no backdating of commission if the chosen trial policy waives fees during trial.
- **awaiting_prepayment:** service-period invoice generated; do not start a new commercial selling period until the operator confirms receipt. Existing paid rights remain serviced.
- **active:** service fee paid, invoiceable sales permitted.
- **commission_due:** monthly statement issued; grace period of 7 days, selling remains active if prepaid service is current.
- **commercial_hold:** unpaid overdue obligations. Stop **new checkout creation, new complimentary grants, new product sale publication and new commercial channels/bot additions**. Keep central connection management, owner support, historical invoices and payment submission available.
- **restored:** after the platform owner confirms overdue receipts, remove the hold idempotently.

Do NOT turn off Telegram polling, delete resources, cancel completed orders, retract paid entitlements, stop expiration/revocation/reconciliation, or drop requests initiated before a hold. A customer who paid before hold must still receive their product/access; an admin must still be able to review those pre-hold requests. Failures in billing should fail closed only for new commercial actions and fail safe for existing customer obligations.

Debt survives bot deletion/reconnection and persists against durable Merchant ID and verified owner accounts. Channel reattachment under the same debtor must not clear debt. Legitimate cross-owner transfer needs an operator-reviewed appeal, not automatic assumption that the new owner owes another person's bills. Deleting a bot never erases invoices or charge sources.

## 5. Placement and separation

- Ordinary customer of ViMi Pay: **one** billing entry under the existing central-bot connection/account surface, showing service period, bot fees, commission, invoices, outstanding balance, instructions and "I paid". Do not spread payment controls among connected tenant bots.
- Platform owner: billing income, debt, receipts, disputes, and temporary policy overrides under **existing 👑 Platform**, isolated by platform-owner authorization; never display another merchant's finance to an ordinary owner.
- Connected bot remains the tenant admin/buyer tool; no parallel billing or monitoring panel in it.
- RU/EN, current Rich UI and classic fallback. No duplicated menus or buttons.

## 6. Safe implementation slices

1. **Contract + deterministic commission computation** (this PR): explicit policies, tested source identity, snapshot, RUB conversion and rounding; no migration, side effects, restrictions, invoices or live charges.
2. **Financial persistence + platform-owner exemption:** implement operator-verified, project/channel-scoped owner exemptions **before** issuing any billable entries; migration for billing statements, line items, payment confirmations and immutable adjustments; strict PostgreSQL RLS/privileges, unique source keys for subscriptions **and files**, Merchant ID attribution, atomic invoice generation, operator-only receipt confirmation, reliable audit. Integrate both approved-sale sources, including any verified file/Stars paths, without changing customer payment flows.
3. **Trial + grant quota:** reuse owner-scoped trial-claim table, implement durable shared complimentary-grant counters and owner-only exceptions. Enforce in all service paths with PostgreSQL concurrency tests. No retroactive quota to existing users.
4. **Central bot UI + manual settlement:** single bilingual billing entry, invoice details, bank instructions from secure configuration, confirmation upload, platform-owner review and receipt ledger.
5. **Commercial hold + operational rollout:** guarded new checkout/attachment paths, overdue notices, grace, safe restoration and paid-obligation continuance; staging E2E and explicit owner activation. Do not silently activate for existing customers.

## 7. Acceptance gates

- ViMi Pay owner-account My Bots never receive SaaS invoices or fee/commission charges, consume trial or become blocked for SaaS debt; another customer's bot where the operator is only admin is still billable. Transferring a channel changes exemption only prospectively.\n- One verified owner cannot claim a second trial through a new bot/project/merchant.
- Two concurrent free grants cannot bypass quota; staff cannot issue free paid plans; OWNER_SERVICE remains privileged.
- One approved Payment or approved FileManualOrder generates one billable event, by frozen price, and remains one event after retries, deletions and reconnections.
- FX and ruble rounding explicit; missing rate blocks invoice creation; refunds are compensating entries.
- A copied or fake proof cannot mark platform invoice paid; only authorized operator can confirm.
- Overdue account cannot start **new** checkout but old purchases and entitlements continue to completion and expiry.
- No runtime effect until new billing policy is deliberately enabled; preserve compose.light100.yaml and existing staging consumers.

## 7a. Simplified owner exemption (2026-11-11)

Migration `0050_platform_owner_my_bots_exempt` replaces the earlier
creator-proof checks with the project's genuine owner account. It removes
the unused read-only exemption/preflight RPCs. The central **Finance →
Invoices** panel retains only invoices and payment instructions. No extra
preflight buttons, bot-by-bot checking, creator verification, additional
settings or duplicated panels.

Before commercial activation, still verify payment instructions, supported
currency conversion policy, refunds/disputes and staged billing OFF-mode.
No retroactive invoices, no interruptions to existing subscribers, and no
automatic enabling of billing/holds/grants quotas.

## 8. Dormant invoice ledger — PR #113

Migration `0043_platform_saas_invoices` adds four private, non-tenant-editable tables:

- `platform_saas_owner_exemptions`: each verified personal channel/bot is an explicit, operator-only record. The DB ensures that the recorded project, bot and channel actually belong to the DB-authorized platform owner identity; evidence `telegram_creator_verified` must come from an independently performed Telegram *creator/owner*, not admin-only, check. No automatic exemption by merely having an admin role. Ended exemption periods remain in history.
- `platform_saas_invoices`: durable merchant/owner attribution, separate prepaid bot-service and month-end commission periods, fixed RUB total, stable invoice key and immutable issued snapshot. A draft cannot become issued until its lines sum to its exact amount; paid invoices cannot be voided.
- Each invoice line records the original **historical bot UUID**, resolved from the immutable connection/removal events at purchase approval time. Ambiguous multi-bot sale attribution stops invoice creation for operator review, rather than inheriting an exemption from an old bot. Commission month reservations are separate for each liable owner when Merchant ID ownership changes mid-month.
- Owner exemption is time-bound by `verified_at` and `ended_at`, and applies only to the verified bot/channel identity **at the effective time of the service period or sale**. An expired exemption never makes old exempt purchases billable; a replacement bot never inherits the old UUID's exemption. Mixed/ambiguous channel histories require manual review before charging.
- `platform_saas_invoice_lines`: each source ties to a genuine original Payment or approved FileManualOrder with its exact amount, currency, approved timestamp and Merchant ID period. No other purchase or fake manual grant can be billed. One global unique sale key prevents cross-invoice double charges. Bot service lines are exactly 150 RUB and unique per bot/service-start.
- `platform_saas_receipts`: one full payment confirmation per issued invoice, immutable afterward, available only through a privileged operator record. Submission of a receipt or an ordinary owner click does NOT mark the invoice paid.

The four tables use FORCE RLS and explicit runtime privilege revocation. There is no tenant SQL policy or billing/payment UI yet. New operator billing commands will need their own audited, narrow APIs in a separate reviewed PR. After migration **all tables start empty**: no historical charges, no automatic invoices, no enabled quotas or trial, and no customer restriction.

Supported commission sources in this first persistence slice are manually approved subscriptions and manually approved file orders. Stars file purchases require a separately reviewed exchange-rate policy and billing adapter before they can be charged; do not silently count their amount as RUB.

Integration tests use rollback-only privileged transactions to verify owner exemptions, rejection of unapproved sales, immutability, duplicate invoices, and refusal of payment confirmation by a tenant administrator. The only deployment change is applying the Alembic migration. No connected customer service needs a new UI or routing.

**Required before launch:** verified exemption provisioning, invoice issuance and FX policy, SaaS payment review UI, positive end-to-end merchant commission tests, grace logic and safe commercial holds. Do not activate paid billing from this migration alone.

## 9. Owner-only billing operations — PR #114

Migration `0044_saas_billing_operations` adds a billing master control that is **OFF by default**, owner-authenticated invoice-issuing functions, bank receipt confirmation and a platform-only invoice report. The 0043 ledgers remain FORCE RLS, invisible to tenant roles.

- **150 RUB service fee:** payable in advance per active bot, once per non-overlapping service month, after an explicitly claimed 72-hour owner trial. The platform owner's own channels are excluded only after a creator-verified, bot-and-channel-scoped exemption is recorded. Platform owner status alone is not ownership proof: admin-only or ambiguous projects are blocked from invoicing pending explicit review, rather than silently waived or charged.
- **5% monthly commission:** on approved subscriptions and manually approved files only. Original amount/currency/owner/bot at approval time is the source of truth. Only fully closed Moscow months are eligible, and the explicit month-specific Decimal conversion rates are snapshotted in individual invoice lines. Missing FX, source identity or valid trial claim prevents invoicing. Approval-time owner transfers and bot replacement histories are checked.
- **Manual settlement:** only the provisioned platform owner can mark an issued invoice paid, after independently verifying the bank receipt. The confirmed receipt is immutable. No customer's 'I paid' click can grant platform service.
- **Central owner UI:** the existing 👑 Platform → 💼 Finances section gains one invoice view; no new platform menu, no customer menu pollution or duplicate reporting. RU/EN, with a two-step confirmation before recording money received.
- **No live charging:** the configuration table remains disabled, there are no scheduled bill runners, no automatic payouts, no debts/holds and no changes to tenant checkout or existing entitlements.

Still needed before commercial launch: explicitly owner-verified trial starts, free-grant quotas, refund adjustments, payment instructions configured outside git, invoice delivery to each merchant and separate owner-approved activation. This stage alone must never be treated as a production billing launch.

## 10. Trial and complimentary-access enforcement — PR #115

Migration `0045_saas_trial_grant_quota` introduces a separate **`platform_saas_access_policy.enabled=false`**, never activating it automatically on deployment. The 0044 billing master switch is unchanged (still off).

- At successful first `bot_instances` insert **after an explicitly authorized rollout**, a transaction-scoped PostgreSQL lock and existing `merchant_platform_trial_claims` owner unique index ensure that each newly onboarded Telegram owner has at most one 72-hour trial. Changing the bot's token, making a new project/Merchant ID or removing a trial bot never resets the original claim. A second concurrently connected bot is rejected during those 72 hours. At the end of the trial the future paid billing/hold gate will control further connections (not enabled in this PR).
- **Existing owners** with pre-rollout connected bots (including deleted bot history) remain grandfathered. Claims or charges are never backdated. Platform operators do not consume personal trial claims; their admin-only third-party channels do not automatically inherit a free-grant exemption.
- An owner under the new commercial trial has a combined **three complimentary activations per original Telegram channel per Moscow calendar month**, shared across `grant_manual_access` and `activate_manual_plan`. Multi-channel plan grants consume one slot **per original channel**. This is enforced in a DB trigger on all `entitlement_grants.source='manual'` inserts, not only UI callbacks. Each committed grant has an immutable event record indexed by owner identity and original channel chat ID, surviving reconnection and Merchant ID rotation. Revoke/reissue does not restore quota.
- Only the actual project owner, with an authenticated user/project context, may issue commercial free grants; ordinary admins/moderators cannot issue grants or increase quota. The original owner/service-entitlement source remains untouched. Verified ViMi Pay creator-owned channels are excluded from the quota only when the exact project/bot/channel creator proof is recorded; platform role or channel administrator rights alone are not proof.
- Trial and quota flags have NO tenant write privilege. With access policy OFF, all existing handlers remain compatible and unchanged. No new charging, no blocking, no copying of grants into Payments, and no revocation or expiry changes.

**Implementation gate:** the platform owner must separately approve and perform commercial onboarding after the complete trial UI, subscriber billing instructions, payment reconciliation, appeals and commercial-hold safety are tested. Do not flip the feature flag just because migration 0045 is present.

### Hardened alternate-path protections (PR #115)

- An incoming verified ownership transfer also enrolls a first-time recipient, or refuses a second connected bot during an active 72-hour trial. The recipient trial claim uses an owner-origin MerchantIdentity, not the sender's frozen original project Merchant ID. No transfer changes historical merchant ownership, and the original two-party consent checks still apply.
- New bot enrollment depends on a trusted DB-clock transaction and durable prior ownership history, not a runtime-provided `created_at`. The inserted bot itself never makes a backdated usage event count as a pre-rollout legacy connection.
- For eligible owners, manual entitlement core identity, buyer, channel, validity and source fields are immutable after creation, including after revocation. Ordinary revocation is still allowed and paid-entitlement updates remain unchanged. This closes the UPDATE/reactivation bypass of an INSERT-only quota.

### Further Codex review fixes: deletion recovery and transferred historical owners

During the active 72-hour trial, *all* other bot rows owned by the account reserve its one slot, including `deleting` bots. Only actual row removal releases it; restoring a retiring bot can no longer create two concurrently connected trial bots. A pre-rollout recipient of an already-connected bot counts as an established owner if their authenticated ownership period overlapped the bot's actual connection lifetime before commercial rollout. This remains true even though the initial `bot_connected` event occurred under a different owner. These checks are applied in both new-bot and ownership-transfer entry points.

- Incoming ownership transfers count **every persisted bot across all projects that the recipient now owns**, including registered, disabled and deleting rows inside the incoming project. An active 72-hour trial cannot accept an incoming project containing two bot rows. A dedicated PostgreSQL test verifies that a registered-plus-disabled two-bot project is rejected before either bot or trial ownership changes.

- A first trial claim cannot be created by a second bot if a connected, disabled or deleting bot already exists without a claim (for example, if the policy was enabled late relative to `effective_from` or temporarily switched off). The owner lock protects the check and claim together, so late activation cannot issue two bot slots.
- When a legacy project is transferred away and later returns to its original pre-rollout owner, that owner's *earlier* merchant ownership period for that very project remains eligible for grandfathering. The just-created receiving-owner period does not qualify because its start is after the cutoff.

- Hardening of the grandfather evidence: future `bot_instances.created_at` values are generated by PostgreSQL's trusted `clock_timestamp()` on insertion and immutable thereafter, including before commercial flags are switched on. The older `platform_usage_events.bot_connected.occurred_at` derived from this timestamp can no longer be forged to an earlier rollout date. Existing bot rows and their historical records are not rewritten. PostgreSQL regression explicitly attempts to backdate a connection while the policy is OFF and proves a later second bot is still rejected.

- A **missing** trial claim by itself is not proof that an owner is grandfathered. If a bot was connected after the commercial cutoff while trial enforcement was temporarily disabled, this owner still consumes the monthly complimentary quota, even without a trial claim. A shared privileged historic-ownership function checks genuine pre-rollout connection/owner-period overlap for exceptions in both new grant inserts and manual-grant UPDATE protection. A regression verifies exactly three allowed grants, a refused fourth, and forbidden reassignment.

## 11. Overdue grace and restricted *new* commerce — PR #116

Migration `0046_saas_commercial_holds` is **OFF by default**, via `platform_saas_hold_control.enabled=false` with a default **7-day grace period**. Both this control and the already dormant `platform_saas_billing_control` must be manually activated and effective. No recurring job or bot callback can activate the switch.

- A merchant owner's state is derived from immutable `issued` invoices linked to their owner UUID. Only **invoices issued at or after the hold rollout cutoff**, still lacking a manually verified receipt, may affect status. Void invoices, already-paid invoices, invoices billed to former owners, and older grandfathered invoices do not count. For each such invoice, grace begins no sooner than its issue date, due date and the hold rollout timestamp. `held` begins after the configured grace window, `grace` before that, `clear` when none remain, and `disabled` when policy flags are OFF.
- The hold applies to that Telegram owner **across Merchant IDs and all current projects**, so moving to a second project cannot evade an unpaid platform invoice. Debt does not transfer to a new owner of a channel; the statement remains tied to its original liable owner.
- PostgreSQL `BEFORE INSERT` triggers prevent only new **subscription payment requests**, **manual-file checkouts**, **Stars file checkouts**, **bot registrations** and **new channel/group resources** for a held owner. Existing pending payment/manual-file orders can still be reviewed or cancelled; approval and delivery of previously accepted payments, ongoing subscriptions, expired access reconciliation, bought-file redelivery and already-connected bots are **never blocked**. Updates/deletes are not affected.
- Verified personal ViMi Pay creator-owned channels can keep creating checkouts for that **exact verified bot** (with historical single-channel check). Platform owner/admin role without Telegram creator verification does not waive a hold. A new bot or channel cannot inherit an old exemption.
- After the platform owner **independently confirms bank receipt** using the existing `platform_saas_confirm_receipt` operation, the next checkout/connection check sees the account as clear automatically. No customer-paid receipts, unverified screenshots or role toggles release holds.
- One compact set of **Commercial holds, Grace days, Owners held, Owners in grace** statistics appears inside existing `👑 Platform → 💼 Finances → 🧾 Invoices`, with no new menus. Customers get a localized RU/EN explanation without exposing debt amounts, financial information or project IDs.
- Default rollout behavior remains unchanged for all current operating bots. Activation, grace configuration and invoice issuance remain controlled outside normal tenant UI. **Do not flip either policy ON merely because this migration is deployed.**

Tests cover OFF-by-default, grace to overdue transition, owner-wide hold scope, no retrospective charge/hold, PostgreSQL checkout/bot/channel restrictions, safety of connected bots and immediate restoration following an owner-confirmed receipt. Multi-currency commission and refund settlement require separate review; none of this implements unilateral withdrawal from merchants.

## PR #117: merchant invoices, owner bank instructions and notices (2026-11-10)

Migration `0047_saas_owner_payment_ui` adds a single operator-controlled payment-instructions record, plus a durable, idempotent platform-invoice notification queue. These are **separate from merchant-to-customer payment methods**; no customer-facing checkout details, payment cards, or subscription access rights are changed.

- Platform owner opens existing **👑 Platform → 💼 Finances → 🧾 Invoices → 💳 Payment details** to enter, view, replace, or explicitly clear bank instructions. Every operation checks the configured platform Telegram owner and the privileged database `platform_operators` role. Sensitive bank instructions are never printed into server logs. They are HTML-escaped for Telegram output. A compact readiness summary shows the three commercial switches, without automatically enabling any.
- Connected-bot administrator opens **⚙️ Settings → 🧾 ViMi Pay invoices**, with historical owner-only account invoices, due dates, RUB totals, receipt state, and published bank instructions. The read-only SECURITY DEFINER report is limited to the authenticated historical liable Telegram owner, not merely a moderator or a new owner after bot transfer. No cross-merchant invoice rows or direct financial-table grants.
- The existing central bot alert loop now schedules and sends invoice-issued / payment-due / seven-day-overdue Telegram reminders to the liable account (not through arbitrary customer bots). Outbox row is unique by invoice and kind, with skip-locked leasing, bounded retries, and suppressed pending messages after an operator-confirmed receipt. The messages contain the exact statement ID and payment instructions only when they have been configured.
- Notifications are **inactive** if platform SaaS billing is OFF or no bank instructions are published. Existing invoice/hold/trial switches remain OFF by default. A newly deployed migration will create no real bills and send no commercial notifications until an explicit, separately approved rollout.
- Bank transfer or a customer's `I paid` action NEVER marks a platform bill paid. The platform owner alone confirms actual received funds from their bank; the existing receipt function automatically releases any overdue hold.
- The platform statement history and notification delivery records are never deleted or repurposed as merchant-to-customer payment history. Message retry does not change ledger values or Telegram paid access.

**Activation remains a separate operator-controlled decision:** this PR deliberately has no toggle to begin automatic billing, charge accounts or freeze commercial activity. Complete prelaunch validation of payment instructions, FX for multi-currency commission, owner exemptions and dispute/refund adjustments before authorizing the existing dormant controls.
