## 2026-11-14 — Bank review completion and customer notification (PR #163)

The central operator receives the real proof push as implemented in #162.
After approving/rejecting that original proof, show an existing-section
Rich outcome with functional Review receipts / Invoices navigation; /cancel
also returns to a navigable Rich result without changing funds.

A new AFTER INSERT financial decision trigger records one durable
recipient notification per reviewed attempt (migration 0087). The existing
central monitor claims and sends it from the ORIGINAL customer-facing bot
to the same verified owner chat that submitted the proof. Reject: include
actual reason and explain invoice remains unpaid. Approve: report actual
bank verification. RU/EN. No new menus, duplicate buttons or runtime
supervisors. Retry transport failures independently of the bank ledger;
older decisions are deliberately not re-sent on rollout. Account billing
holds, trial and live switches are unchanged.

---

## 2026-11-14 — Owner-network debt enforcement and 12h proof review (PR #162)

**This later owner-approved enforcement decision supersedes the PR #161
"only the overdue bot" hold rule**, while preserving PR #161's bot-specific
invoice creation, anniversary, commission, full payment and exemption logic.
Every account remains financially liable for its own invoices. If any real
issued, due, unpaid ViMi Pay service or historical commission bill exists,
all nonexempt bots of that debtor are subject to a new-commerce hold and
eligible new bot registration is blocked, not just the billed bot. Access
for already paid buyers, support, Telegram polling, refund evidence, access
expiry and bank review continue without interruption. Owned platform bots
retain the existing exemption.

One submitted original bank proof starts a **maximum 12-hour**, automatic,
pending-review protection for that invoice. This clock is anchored to the
earliest immutable proof timestamp on that invoice, NOT last upload or last
payment attempt. A second fake PDF never extends it. Rejected or allocated
earlier proof disables the temporary protection, even if another proof is
submitted. If any due invoice is no longer protected and still unpaid, the
whole debtor's nonexempt network becomes held. An operator's confirmed full
receipt removes the debt and allows the normal coverage/renewal state to
resume; an operator's rejection never marks the bill paid.

At submission, after 6h and after 11h the CENTRAL @vimipay_bot sends normal
RU/EN private bank-proof alerts *only while no decision was taken*. Each
opens exactly the already-existing proof queue, whose existing buttons
approve/reject after real bank verification. No new pages or direct fake
approval buttons. Pending proof reminders that fall after the 12h window are
cancelled. The one operator review workflow keeps the actual source media
read-only and private. None of these notifications approves a payment,
creates a bill or changes the configured live billing switch.

Read-only merchant bill adds upcoming date, issued due timestamp, first-proof
protection deadline, and a clear held/paid/pending status with no technical
UUID in review list. Central owner screen distinguishes *issued before due*
from an unissued projected upcoming bill and shows the real Moscow deadline.

---


## 2026-11-14 — APPROVED billing contract for PR #161 (supersedes all older proposals)

**One connected bot = one real issued monthly ViMi Pay invoice.** No owner-wide
invoice total, no cross-bot debt, no choice between 'service' and 'commission'
in an admin's bill. Existing archived/issued historical invoices, payment
attempts, receipts, verified refund credits and operator audits remain
immutable for reconciliation. Central bot retains historical finance archive,
but current invoice is opened in the corresponding connected bot.

**Clock and price.** Keep the one-owner / one-bot 72-hour introductory trial.
At trial end, the bot receives its **first** issued service invoice: 150 RUB
or independently-priced 3 USD, with **no sales commission**. Its first
successful paid activation fixes its service-billing anniversary. On each next
anniversary the one invoice contains (a) prepaid next month's service fee,
(b) 5% of that same bot's *confirmed subscriptions* and (c) 5% of its
*confirmed files* for the **immediately previous completed service interval**.
Service periods run anniversary-to-anniversary, not Moscow's 1st-to-1st.
The exact day/time is retained with a short-month clamp (31 Jan → 28 Feb →
31 Mar). When ownership is transferred, the bot's *actually paid*
`effective_end` (not an expired printed service invoice end) governs the
recipient's earliest new bill and cross-owner overlap checks, preserving the
old owner's purchased coverage without transferring the old owner's debt.
The original owner's unpaid issued service invoice blocks completing the
ownership transfer; once settled, unused paid coverage follows the bot.
A bank transfer/proof from a previous owner that arrives after ownership
has already changed is held for explicit operator refund reconciliation:
the old owner's original invoice and actual bank evidence remain auditable,
but cannot silently activate service over a recipient's paid interval.
Verified original sale refunds may settle historical owner balances without
creating new service coverage on a bot owned by somebody else.
Ordinary on-time renewal verification preserves the established anniversary
anchor (Jan 31 → Feb 28 → Mar 31), while first activation and genuinely
expired long-frozen restarts use actual verification as the new anchor. Source confirmation, ownership at sale, billing activation, creator
exemptions, sale-day CBR FX, genuine provider reward valuations and verified
refund-credit evidence remain authoritative. No provisional month-to-date
commission is added to an already issued invoice.

**Presentation.** In the connected admin bot render exactly one RU/EN Rich
'🧾 Счёт ViMi Pay' showing service, subscription gross/count/5%, file
gross/count/5%, total charged, confirmed actual bank payments, optionally
nonzero verified refund credits, and amount due. Gross is shown by native
currency, never sum RUB/USD/⭐ Stars into a false mixed amount. The **first
post-trial invoice displays service only**. The only invoice navigation
buttons are '💳 Оплатить' (when unpaid) and '🏠 Главное меню'.

**Settlement.** No 25%/50% buttons, no partial new platform bank payment
attempts. A real payment request must cover the whole currently outstanding
invoice. Choose currency/method from the central operator catalog, use an
immutable bank/FX snapshot and receipt proof, and confirm only after funds
are actually verified by the platform operator. Historical partially
allocated funds remain in the immutable ledger; they are not discarded.
A verified reversal remains an audited credit, never a fictitious bank
payment. On USD-priced service, **confirmed bank payments** reduce the
outstanding native $3 fraction at the original booking rate before covering
commission. **Verified subscription/file refund credits** remain separate
RUB ledger adjustments, never impersonate bank cash, and can be carried
forward to a later unpaid invoice under the existing audited credit policy.
The payment quote is capped by the actual remaining RUB debt: credits cannot
produce a negative amount or cause a second charge. The original $3 price,
issued RUB ledger total and payment snapshots remain immutable.

**Hold.** No 7-day commission due-date delay and no extra 7-day grace.
When the trial or prepaid anniversary expires without the bot's full
settlement, immediately block only that bot's *new* paid checkout/publication
and unjustified free grants. Do not stop Telegram polling, bot support,
already paid subscriptions/files, reviews/delivery of already accepted
orders, access expiry/cleanup or the ability to pay. Do not hold a different
bot of the same owner. Resume new commerce after real full settlement. Direct complimentary
`entitlement_grants.source='manual'` INSERTs are also blocked while that
bot is held, preventing free-premium bypass; existing paid entitlements and
previously accepted purchases remain accessible.

**Historical compatibility.** Old ledger 'commission' invoices remain
readable and auditable as historical exceptions, but the live reconciler no
longer creates new merchant-wide calendar-month commission invoices. Do not
delete or overwrite old migration files. Changes are forward-only in
0083/0084 on this unmerged PR. Commercial activation, real customer funds
and staging rollout are **not** implicit in a merge.

---
## 2026-11-14 — Merchant billing UI cleanup (PR #160)

Approved merchant navigation is one concise chain:
`connected bot admin home → 🧾 Счёт → invoice → 💳 Оплатить →
currency + central methods → amount → real bank proof prompt`.

The 🧾 Счёт entry screen now lists only real issued invoice buttons labeled
by **purpose and amount** (for example, `🧾 Обслуживание ViMi Pay · 150 ₽`)
and one `🏠 Главное меню` button. The empty state has only Main menu.
Pagination appears only when there are more invoices. The invoice itself
shows purpose, payable amount, due date, status, remaining due and only
nonzero applied credits or historic adjustments. One Pay button opens the
existing central-authoritative method catalog; one Back returns to invoices.
No UUIDs, unset-payment-currency rows, zero-value audit entries, trial
preview, aggregate sales table, extra Pay on the list, Refresh or return
to unrelated Settings in the invoice entry UX.

The currency selector now uses human RU/EN currency labels and operator
method names; a method named simply `RUB` is rendered as `Оплата по
реквизитам / Payment instructions`, not `RUB · RUB`. Currency and
amount navigation **edits the current payment card in place**, with a
fallback send only for an old/uneditable Telegram message. Full, 50% and
25% partial payment options keep existing immutable allocation/quote code.
Only after confirming the amount do we emit a distinct native HTML bank
proof prompt (required for reply-to-photo/PDF handling).

Historical FX lock and financial audits stay in the database; no change to
invoices, payment methods, CBR valuations, RLS, subscription access, billing
activation or `compose.light100.yaml`. Prior Telegram callback links
remain valid for existing posted messages.

---

## 2026-11-14 — PR #156: honest refresh feedback in the central proof queue

The existing `🔄 Обновить / Refresh` button in
`👑 Финансы → Счета ViMi Pay → 🧾 Проверка чеков` already executes a
new, operator-authorized database query for each callback. If the
pending items are unchanged, however, Telegram responds
`message is not modified`, which the screen editor appropriately
silences, making the refresh appear inert.

The same existing queue now shows the platform-local time of its latest
database check and gives a quiet (non-alert) `✅ Список проверен / List checked`
callback acknowledgement after every successful query, including rapid
repeated taps within one second. It edits the existing card in place; there
is no duplicate card, no new debug button, no cache and no financial
mutation. The list still immediately reflects actual new, approved or
rejected pending evidence returned by PostgreSQL.

---

## 2026-11-14 — PR #155: fix replies to already-issued Rich payment prompts

Live staging demonstrated that PDF replies to ViMi Pay's invoice instructions
produced neither the required merchant acknowledgement nor any record in
the central review queue, even after prioritizing receipt routing in #154.
Root cause: the receipt parser used `reply_to_message.text`, but
`sendRichMessage` produces a Telegram `Message.rich_message.blocks` and
normally no `text` field. Telegram displays the reply preview even when
the Bot API reply message has no ordinary text.

Existing Rich invoice prompts are parsed using **only the original bot's
authenticated reply message** and a bounded read of the first paragraph or
section heading's RichText nodes (including inline-code invoice UUID).
The existing database RPC still verifies invoice ownership/attempt state.

Future `🧾 ViMi Pay · <UUID>` receipt prompts use Telegram's **native HTML**
message so user reply references always contain `Message.text` regardless
of the platform-wide Rich toggle. This only affects a receipt reply target:
the currency selector, invoice list, all other screens and buttons keep Rich.
No test-only buttons, new SQL schema, receipt content storage, billing
activation or changes to historical payment ledger. The former Rich prompt
is still supported, so existing unconfirmed invoices don't require a new
payment attempt.

---

## 2026-11-13 — PR #154: platform invoice receipt ingress priority

The live RU staging flow can create an account invoice and send a PDF as a
Telegram reply, but the sender sometimes receives no receipt acknowledgement
and the operator's existing queue remains empty. The SaaS receipt callback
previously lived **inside** the general owner router, which the customer
dispatcher registered **after** the file-sales/editor routers. A pending file
editor could consume the PDF before the invoice receipt handler.

A single dedicated `vimi-saas-proof-ingress` router is now registered before
all tenant file/broadcast/editor routers and before central-owner forms.
It matches only private photo/document replies to the original receiving bot's
`🧾 ViMi Pay · <UUID>` prompt (both UUID formats), regardless of prior
file editor state or supervisor restarts. The old late owner receipt handler
was **removed** to avoid duplicated callbacks. On save, the payer gets
a visible confirmation; failed/invalid saves yield a visible error.
The original bot's push to platform operator remains best-effort because
a connected bot may not have a /start chat with the platform owner.
The central pending queue and `📎 Чек` from PR #153 remain canonical.

This does **not** confirm bank funds, touch invoice balances, enable
commercial holds, add buttons, store file bytes or modify compose/light100.

---
## 2026-11-13 — Operator proof viewer for the existing invoice queue (PR #153)

The existing central `👑 Финансы → ⏳ Проверка платежей` now includes
`📎 Чек / Receipt` on each **pending** submission. This is an operational
bank-review action, not a test or duplicate checkout button.

Telegram media `file_id` is scoped to the bot that received the user's
proof. The central bot must **not** attempt to send that `file_id` itself.
Instead the operator-authorized, read-only SQL lookup finds the actual
registered source bot's project without exposing its credential; an existing
project-scoped token read decrypts the source credential in memory. The
original bot fetches the media, and the **central** bot immediately uploads
the bytes to the configured platform operator's chat. Media is not persisted
to the database or server filesystem; existing immutable proof metadata and
confirmed/rejected payment allocations are unchanged. The viewer is limited
to still-pending attempts and fails closed for removed/disabled bots, invalid
credentials, oversized media or unavailable Telegram files. A proof originally
submitted to the central bot (which has no customer BotInstance) is instead
downloaded with that already-authenticated central bot, without requiring
a tenant project or encryption key. No automatic bank approval follows
receipt viewing.

This does not grant a token to customer-supervisor, add a scheduler, change
`compose.light100.yaml`, or enable SaaS billing/holds. For old pending proofs
with an unavailable original bot, the operator must request resubmission.

---

## 2026-11-13 — First-live-rollout correction (PR #148)

The observed network has 29 platform-owner bots (exempt), 11 + 12 bots on
two previously connected test accounts, and one new bot connected on
2026-11-13. The initial rollout generated 24 distinct prepayment invoices
(not 24 duplicate invoices): 23 accounts' existing bots had their one-time
owner trials backdated by up to 72h, making them immediately billable. The
genuinely new owner's invoice due on 2026-11-16 is an intentionally
**upcoming** advance prepayment at the end of their real 72-hour trial.

**Safe correction:** For accounts with a genuinely connected bot at the FIRST
commercial enrollment, preserve their original immutable first-trial claim
but give a one-time 72h transition *from that first rollout effective time*.
Reactivations and new bots do not reset this period. Service prepayments for
these legacy accounts may only issue when the transition actually ends.
New accounts retain the existing advance prepayment/72h deadline behavior.
The same billable-after decision gates historical 5% sales commission,
Stars/Crypto eligibility, owner account trial display and direct service
invoice issuance. Operator-owned bots remain exempt.

Migration 0077 performs a narrow, audited clean-up: only originally issued
prepayment invoices that fall strictly BEFORE this new effective threshold
are marked VOID, only if they have NO legacy receipt, new bank attempt,
payment proof/confirmed allocation, currency settlement, refund allocation
or credit claim. Original invoice, invoice-line and trial evidence is retained.
Any invoice with money or a payment attempt is skipped for manual financial
review, not automatically deleted/voided. Real buyer subscriptions and
previously paid access remain unchanged. The new account's November 16
invoice is not eligible for the void.

Activation is a **separate operator decision**: never enable billing during
deployment, and while repairing keep commercial billing/holds OFF. Install
only after exact-head CI and financial review. Avoid new test-only buttons,
repayment scripts, or extra supervisors.

---

## 2026-11-13 — PR #145 release hardening (not activated)

The single PR #145 keeps commercial billing in Draft/OFF while its financial
review items are addressed. Service invoicing now rotates persistently through
the connected bot inventory rather than repeatedly selecting the oldest 100;
commission reconciliation also cycles through every unissued closed Moscow
month (including empty/deferred periods) without starving older unresolved
source evidence. Each new owner's first service period starts after connection,
trial, rollout, and actual ownership start; an outgoing owner's unpaid debt must
not prevent the incoming owner from starting service after an existing period
ends. Owners connected during the five-minute activation delay are enrolled
without resetting an existing trial. Previously enrolled owners retain their
complimentary-grant quotas even after OFF/ON cycling.

The USD $3 profile is a native service obligation. Its issue-day RUB value is
an accounting snapshot, not a fixed later payment quote. A new payment attempt
converts the unpaid native USD fraction at that attempt's verified rates, with
its RUB-book allocation retained as immutable historical evidence. Regular
150 RUB profiles and manual customer prices are unchanged.

**Verified refund application in the same PR (migrations 0073–0074):**
- The append-only activation audit establishes historical ON/OFF windows. A
  STOP/ARM cycle keeps old earned but deferred fees and unpaid hold eligibility,
  while purchases made during OFF intervals remain in buyer history but cannot
  accrue 5% platform commission. Stars/Crypto late evidence is valued by its
  original paid date and live interval.
- Only original-source provider-verified Stars file/subscription refunds and
  Crypto Pay merchant transfer credits linked to a positive issued commission
  line can create an immutable claim. The original sale-day official USD→RUB
  rate is reused; no new FX quote or synthetic provider refund is manufactured.
  Each provider evidence ID is unique, and partial refund credit totals are
  capped by the exact original commission line.
- A separate immutable allocation links each credit to unpaid SaaS invoice
  amounts. Original issuer totals and receipts are never rewritten; credit
  after full payment is held for the owner's next unpaid bill. The canonical
  remaining function now subtracts verified credit allocations and existing
  receipts, and the bank-approval/legacy receipt guards reject over-collection.
- Existing merchant/owner invoices, overdue restrictions and notices read the
  same remaining liability. The RU/EN Rich issued-invoice card shows applied
  Stars/Crypto refund credits and the live remaining RUB reference balance;
  separate historical manual refund notes remain visibly unapplied.
- Each sweep runs in existing customer-supervisor reconciliation, plus before
  a payer starts a new v2 attempt. No new buyer commands, test buttons,
  standalone refund action or per-bot scheduler.

Live commercial switches remain OFF until exact-HEAD database CI and Codex
review are clean and the platform owner approves staging rollout.

---

## 2026-11-12 — Single integrated commercial billing release PR

The existing connected-bot `🧾 Счёт` will replace duplicative FX and
Stars preview cards with one immutable-source read-only month summary across
approved manual subscriptions/files, verified native Stars sales, verified
Crypto Pay sales, and separate refund evidence. Sale-day CBR RUB conversion
is required; an unresolved rate remains visible, never zero or fabricated.
The 72-hour one-owner trial and activation cutoff determine fee eligibility.
This migration is strictly read-only until the SAME PR also supplies monthly
invoice source issuance, partial bank attempts/review/allocations, effective
holds and full E2E. DO NOT turn on SaaS billing from the preview alone.

---

## 2026-11-12 — Crypto Pay #141A refund evidence (not live billing)

The official @CryptoBot Crypto Pay API has no direct refundInvoice endpoint.
An explicitly labelled, completed original-merchant `getTransfers`
payout to the same buyer and same crypto asset can be recorded as an
immutable partial/full commission credit, using the **original paid sale**
USD rate, with caps/idempotency. Its preview is **unapplied**: no
invoice offsets, paid-status changes, holds, or buyer re-entitlements.
Only crypto-denominated invoices with verified transferred asset are
supported. Crypto Pay buyer activation and commercial billing remain OFF
until fiat paid-asset refunds and monthly invoice integration are reviewed.

---

## PR #129 — Native Stars refunds without subscription-access regressions (2026-11-11)

Migration `0056_saas_star_refunds` adds an immutable, tenant-scoped record
of a verified **Telegram `refunded_payment` event** tied to an original
paid-file charge ID, connected bot, original invoice payload, buyer,
project and exact Stars amount. A database trigger independently checks
that all fields match an original **paid** purchase; duplicate Telegram
updates are idempotent. The file editor cannot swallow refund service
messages. The original paid purchase and previously granted file delivery
history are preserved; there is no automatic revocation, double refund,
invoice mutation or access blocking.

Existing shard-zero supervisor processes the original paid sale and any
matching refund into **separate immutable** central financial events. The
return uses the **original developer-reward USD-per-Star rate** sealed on
the sale (not a future/current rate): refund credit equals the original
USD reward and 5% original commission. Historical liable owner, bot and
project remain fixed even after transfers. Monthly sale and monthly
refund statistics are distinct events by Moscow sale/refund date. Their
auditable counts and unapplied credit are displayed within the existing
RU/EN Rich **🧾 Счёт**, with HTML fallback. Underlying refund credits
do **not** silently reduce an issued bill, clear a commercial hold or
refund a customer twice.

A refund event received after a sale but before its financial capture
waits until the original paid source is reconciled, so no unjustified
negative commission entry is created. Source evidence may be delayed by
Telegram update delivery or a database outage. **A subsequent stage must
poll each connected bot's authenticated `getStarTransactions` ledger**
to reconcile missed refunds/chargebacks and independent app-store
reversals before live commercial billing. Never rely solely on Telegram
service messages to assert all refunds are complete.

### Next product gate: Stars subscription checkout

Current subscription purchases use the original manual approval →
Payment → Subscription → Entitlement pathway. Enabling a Stars payment
button without an atomic verified **Stars charge → Subscription →
Entitlement** transaction would take money without providing access.
Implement native XTR plan-pricing, authenticated Bot API pre-checkout,
durable first/renewal successful_payment, retry recovery, unique charge
source, cancellation/expiry, refunds, native RU/EN buttons and real
membership outbox in a separate full checkout slice. Do not insert
inert payment methods or dead Stars buttons merely to pretend the
feature exists. The 0055 financial source_kind `subscription_stars`
is reserved until that **verified delivery** source is implemented.
No live commission is issued from these preliminary events.

**Rollout controls:** existing SaaS billing/holds/trial switches remain
OFF, no fee backfill for old buyers, no production activation, preserve
`compose.light100.yaml`. No merge without exact-HEAD CI, Codex,
zero open review threads and explicit owner permission.


# ViMi Pay · Commercial Billing v2 — approved product contract (2026-11-11)

**Status: approved for phased development, NOT approved for commercial activation.**
All existing `platform_saas_billing_control`, trial/quota and hold switches stay OFF.
This document supersedes earlier conceptual proposals wherever they conflict.
Only verified historical invoices/receipts remain immutable; never retrobill
transactions before an explicitly approved rollout.

## 1. Roles and surfaces: strictly two Telegram bots

| Connected admin bot · **🧾 Счёт / 🧾 Billing** | Central bot · **👑 Финансы / 👑 Finances** |
|---|---|
| 72-hour trial, service activation/renewal, sales stats, merchant liabilities and history | Aggregate all merchant balances, pending proofs, issued invoices, receipts, disputes and controls |
| Single **💳 Оплатить / 💳 Pay** path, currency, method, full/partial amount, receipt submission | One authoritative catalog **💳 Реквизиты ViMi Pay** configured here; no duplicate catalog for each bot |
| Scopes sales by that connected bot; account-wide balance may be displayed only for its verified historical liable owner | Platform operator alone confirms/rejects bank receipts; accepted proof alone cannot mark funds received |

Compact native Rich tables with escaped HTML fallback; RU/EN everywhere.
No unrelated FX rate tables and no duplicate billing actions in Settings.
Price/currency selected for the **billing profile**, not for Telegram UI language.
Changing language must never change contracted service price.

## 2. Current commercial lifecycle (PR #161)

- Owner trial: 72 hours, starts on eligible first connection. At expiry,
  create the bot's first service-only invoice; no 5% for trial sales.
- Bot service price: 150 ₽ or native $3 according to the assigned profile.
  Later invoices include this next-period service plus 5% of subscription and
  file sales from the **preceding bot service month**.
- Anniversary date/time stays fixed per bot. At short months clamp to the
  last day; do not drift permanently to the 28th.
- Each bot has its own collectible invoice and unpaid/paid state, regardless
  of how many bots one administrator controls.
- Freeze only the unpaid bot at the due instant. No separate 7-day due delay
  or additional grace; never revoke purchased customer entitlements.
- Revenue is attributed to the bot active at actual sale time. Preserve the
  existing immutable sale IDs, verified Stars/Crypto evidence, daily CBR rates,
  exemptions and refund credits.

## 3. Connected bot 🧾 Счёт Rich UI

**First period:** service fee, total, confirmed payments, amount due.
**Second and later:** service fee + subscription sales (gross/native currency,
count, 5%) + files (gross/native currency, count, 5%), total, confirmed
payments, optional verified refund credits, amount due. Only one Pay button
and Main menu. RU/EN Rich with classic fallback, no technical IDs or FX
audit rows shown.

During trial or before an issued invoice exists, show a short no-invoice state
and Main menu, never a fake invoice or payment button.

## 4. One full payment, no percentage chooser

1. Admin presses Pay on *this bot's real issued invoice*.
2. Show central operator-approved currency and bank-method selector.
3. Method selection makes a full remaining-balance payment attempt, retaining
   immutable bank details and official payment-day FX snapshot.
4. Admin transfers, replies with photo/PDF proof; central operator checks
   actual receipt and confirms/rejects it in the existing proof queue.
5. Confirmed whole payment restores this bot's new paid commerce. Historical
   partial records and refund credits remain immutable, but there are no
   **new** 25%/50% controls, percentage callbacks or intentionally partial
   bank attempts. One bot's balance cannot unlock another bot.

## 5. Foreign exchange and Stars — mandatory

**Fiat sales**: preserve immutable native price, currency, date of confirmed
payment and CBR RUB conversion effective on that Moscow sale day (including
the last official source day for weekends/holidays). Account statements may
group native amounts; platform commission base is RUB after each sale-date
conversion. Unsupported or missing quotes block fee issuance, NOT delivery
of an already paid purchase.

**Telegram Stars (XTR)**: counted for BOTH verified subscription payments
and successful paid-file purchases, never at the buyer's regional top-up
price or 0.02 USD advertising credit. Telegram's official developer reward
terms currently assign **0.013 USD per earned Star** (verified 2026-11-11,
https://telegram.org/tos/bot-developers, section 6.2.4). Telegram states
this value may change. Keep a **versioned, operator-audited official
USD-per-Star reward rate** and snapshot rate and provenance per purchase.
Never silently recalculate historical sold events.

Example: 2,000 Stars * 0.013 USD/Star = 26.00 USD reward-equivalent;
5% commission = **1.30 USD**. Store exact Decimal precision and round only
after period aggregation, not per tiny Star purchase. For account RUB
ledger, convert the USD reward-equivalent at the official CBR USD→RUB
rate on the STAR SALE date. Settlement into an admin's selected invoice
currency is a **separate payment-request-date** conversion. For rate gaps,
report that commission is not ready; never invent 0 or reuse a monthly
rate. Capture Stars sale refund/chargeback as unique, source-linked
negative adjustment, without deleting the paid purchase or paid
entitlement. Verify Telegram's `successful_payment` identity,
`telegram_payment_charge_id`, and `refunded_payment` or authenticated
`getStarTransactions` ledger before any fee is billable. Telegram may
withhold rewards for up to 21 days; pending settlement is identified.

Historical recorded Star file purchases remain available. Only sales
after separately approved commercial rollout are eligible for monetization.
Subscription Stars checkout is a distinct product feature until developed;
do not claim it already exists.

**Security**: immutable event identity, idempotency, historical owner/bot
attribution, no double billing between manual/subscription/file channels,
refund reference uniqueness, held/removed bot financial history preserved.

## 6. Central bot 👑 Финансы

Top-level grid: **🧾 Счета ViMi Pay**, **💳 Реквизиты ViMi Pay**,
**⏳ Проверка платежей**, **📊 Финансовая сводка**.
Use existing central platform permissions only. Configurable methods
and currencies model existing tenant manual methods but are stored in a
**separate platform-owned catalog**; no tenant access and no duplication
of those methods. Each method has currency, label, instructions, active
flag, created/updated metadata and historical snapshots of what was shown.
One catalog feeds every connected bot, never account-local copies.

Invoice/reconciliation history includes service fees, approved sales,
Stars equivalent, refunds, receipts, allocations, remaining liability,
trial status and due dates. Strict RU/EN and Rich/fallback. Platform-owner
My Bots are exempt from fee, commission, trial and holds by real owner
identity in DB; transfers change only future liability.

## 7. Implementation and cleanup gates

1. Record this canonical contract and implement **read-only Stars financial
   evidence/pricing** from real Telegram-paid file purchases and an
   authenticated Stars reward rate policy, while billing remains OFF.
   Remove **FX preview table/button/handlers** from tenant invoice screen,
   replacing them with the confirmed invoice path. Retain internal CBR
   importer and immutable FX journal and SQL compatibility for historical
   records; never drop deployed financial evidence.
2. Add real Stars subscription sale source and refund reconciliation,
   source-linked event snapshots and account report aggregation. No fee
   activation until idempotent sale source, owned bot attribution, FX
   availability and refunds are covered.
3. Replace one-payment-per-invoice UI and 0053 lock-for-whole-invoice
   interaction with **partial payment attempts + ledger allocations** and
   customer proof upload, central review, per-currency payment method catalog.
   Clean up superseded callbacks and unused read-only previews in same PR.
4. Polish state-driven admin/account and platform-owner finance Rich
   screens, trial welcome/restore notices, grouping by price+currency,
   and cross-bot obligations. Full RU/EN and fallback.
5. Staging end-to-end from a fresh eligible account and one connected bot
   through buyer Stars/file purchase, source ledger, first/monthly prepay,
   partial commission payment, refund, operator approval, hold, restoration.
   Audit all real current customers/exempt owner accounts.
6. Only after explicit owner permission: enable billing at a future
   activation timestamp, never retroactively. Holds/quotas are separate
   opt-ins. Verify exact-head CI, Codex and 0 review threads for every PR.
   Keep `compose.light100.yaml`; production unchanged.
