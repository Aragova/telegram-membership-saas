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
- **ViMi Pay platform owner's own channels and bots: lifetime SaaS fee exemption.** All channels/bots **actually owned by the ViMi Pay platform owner** are excluded from the RUB 150 bot prepayment, 5% sales commission, SaaS invoice creation, SaaS trial consumption and debt-related commercial holds. Complimentary-access anti-abuse limits for paying merchants are inapplicable to this verified platform-owned scope. An exempt project must not acquire a fictitious zero-valued debt or a bill merely for bookkeeping; normal customer sales, access and their own financial analytics continue.
- **Exemption identity and scope:** verify server-side using the DB-bootstrapped `platform_operators.role='owner'` identity matched to the **real project owner** and verified channel ownership. Being an admin/moderator of a third-party channel, having the platform operator role in a shared project, copying a bot token, or setting an input/callback flag does **not** qualify. A normal customer and any moderators remain billable. For the current one-bot/one-channel model, exempt only the individually owned bot/channel; never spread exemption to another merchant or tenant. Mixed-owned projects must be reviewed rather than silently exempting the entire bot.
- **Auditable changes:** exemption is derived from authorized ownership records and persists across token rotation or bot replacement **only while ownership remains verified**. A legitimate transfer to a third party ends future exemption at the verified effective time. No retroactive charges for the platform-owner period, and no erasure of obligations incurred by another owner before a transfer. A second personal account or service channel may be exempted only after explicit operator-only identity linking and verified ownership, with a dated audit record. Never expose exemption toggle to tenant administrators.


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

- ViMi Pay's own verified channels/bots never receive SaaS invoices or fee/commission charges, consume trial, or become blocked for SaaS debt; another channel where the operator is only admin is still billed. Transferring a channel changes exemption only prospectively.\n- One verified owner cannot claim a second trial through a new bot/project/merchant.
- Two concurrent free grants cannot bypass quota; staff cannot issue free paid plans; OWNER_SERVICE remains privileged.
- One approved Payment or approved FileManualOrder generates one billable event, by frozen price, and remains one event after retries, deletions and reconnections.
- FX and ruble rounding explicit; missing rate blocks invoice creation; refunds are compensating entries.
- A copied or fake proof cannot mark platform invoice paid; only authorized operator can confirm.
- Overdue account cannot start **new** checkout but old purchases and entitlements continue to completion and expiry.
- No runtime effect until new billing policy is deliberately enabled; preserve compose.light100.yaml and existing staging consumers.
