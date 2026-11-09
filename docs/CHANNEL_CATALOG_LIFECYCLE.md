# ViMi Pay — channel management and paid-only removal blockers

## Admin UI

Connected bot → **📢 Каналы** (central bot remains owner-only registry):

- Visible channel/group list (one button per active resource), plus "Add channel",
  "Add group" and "Add by ID" with existing Telegram-verification flow.
- Channel card: live Telegram member count via `getChatMemberCount`; unique
  paid buyers; currently active paid subscriptions; number of active tariffs;
  all-time confirmed payments; pending invoices (informational); GMV grouped
  by original currency, no fake conversion or manual grants counted as sales.
- No member list or private buyer details returned in this admin card.
- Empty bot/insufficient Telegram permissions yields "Unavailable" for live
  member count, never an invented zero.
- Removal requires a fresh button confirmation and server-side project/RLS
  check. Other owners cannot use a copied callback.

## Lifecycle and immutable financial records

Migration **0040_catalog_soft_removal** adds `removed_at` to:
`resources`, `plans`, `file_sale_folders`, `file_sale_products`.

**Only real confirmed purchases block deletion.**

- Channel: any real `Payment` linked to a `PlanResource`, or a
  payment-derived grant on the resource blocks removing the channel, even
  after an old subscription expires. `PaymentRequest` created/submitted
  but not approved is **not** a deletion blocker.
- Channel without confirmed purchases: hide it from the admin list and
  archive/tombstone its linked plans, so no new checkouts are created.
  **Resource stays active internally** because an earlier submitted invoice
  might later be confirmed after funds are verified; access work must still
  fulfill such a payment. Existing owner/service/manual grants are not
  silently revoked. The actual Telegram chat is not deleted and ViMi Pay
  cannot change its permissions here. Re-adding the same Telegram channel
  through the verified onboarding flow restores its visibility, **not**
  its old tariffs' sales state.
- Tariff: confirmed payment/subscription history blocks removal. A tariff
  referenced only by unpaid invoices is tombstoned and archived; its prices,
  original price snapshots and immutable `PaymentRequest` rows survive.
  No invoice history means it can be hard-deleted as before.
- File product: paid Stars purchase or approved manual order blocks removal,
  including if media delivery failed; prior customers must retain access
  to paid attachments and redelivery. If only nonpaid orders exist, the
  product is archived and hidden, retaining original assets and order
  snapshots for eventual receipt review/settlement. If no orders exist,
  it can be physically deleted.
- Folder: empty folder may be physically deleted; nonempty with no real
  paid purchases is hidden with its products. Any paid product blocks removal
  of the folder; the owner may still manually archive files to stop new sales.
- The lists and soft quota exclude tombstones, so old unpaid invoices never
  consume a permanent visible slot.

**Never delete** payment requests, confirmed payments, Stars charges, manual
orders or immutable financial snapshots as a shortcut. Rejected/cancelled
manual file orders remain immutable by the 0038 PostgreSQL trigger. The
database retains invoice-level records for dispute/audit purposes.

An open manual bank-transfer receipt can be an **unverified real transfer**:
removing an unpaid product does NOT automatically reject that review. Owners
must still check their bank and approve/reject the preserved submitted
invoice. For existing Stars invoices, a payment confirmation may arrive after
catalog removal; assets and purchase IDs remain available.

## Staging rollout (requires explicit owner merge approval)

1. Apply database migration **0040 before new bot runtime**. Preserve
   `compose.light100.yaml` and all existing subscriptions. No destructive
   backfill or user-data reset.
2. Deploy updated central bot and customer-supervisor only after successful
   migration. Keep production unchanged.
3. Open an attached bot → Channels. Verify list, live participant count, buyer
   counts, per-currency historical totals and "Add channel" flow.
4. With an empty test channel and unpaid invoice, remove channel; ensure it
   disappears, its new offers stop and the unpaid invoice remains auditable.
   Verify an existing submitted receipt still can be reviewed and, if paid,
   access can be fulfilled.
5. A channel with **one approved payment** must reject removal, even if the
   subscription is expired. The same applies to any paid file in a folder.
6. Create a folder + file with an unpaid/cancelled Stars or manual order.
   Remove and verify it is hidden, no new checkout is possible, and history
   plus private file IDs remain. A paid file must still be redownloadable.
7. Test another administrator/tenant, old callback, multi-bot folder and
   concurrent payment confirmation. The database guard is the last line of
   defense for immutable orders.
