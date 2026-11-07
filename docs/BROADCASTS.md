# ViMi Pay — tenant bot broadcasts (stage 1)

## Scope and user experience

Only inside an administrator's own **connected bot**:
`Admin dashboard → 📨 Рассылки`.

- ✏️ Write a text post using Telegram's normal editor: emoji, URLs
  (`t.me/...`), rich text entities and links.
- ↪️ Forward an existing text/photo/video/document/animation message to
  the same bot. Copy its body, caption, UTF-16 Telegram entities and media
  `file_id` into an independently reusable template. Protected posts
  are rejected. Telegram photo/video albums (2–10 items) are supported as
  **one saved post** with a single optional caption and media in message order.
  Each album arrives as separate updates. After 1.5 seconds without a
  new element, the editor offers **Save album / Discard album** and shows
  the item count. It does NOT save partial albums automatically. Later
  elements invalidate previous confirmation buttons and trigger a fresh
  count prompt; the admin explicitly confirms the full group before it
  consumes the draft. An unconfirmed batch may need to be resent if the
  supervisor restarts. Documents, GIFs, text and single media remain
  supported exactly as before.
- Each saved post has a preview, **send-to-all confirmation**, replace
  content, rename and remove-from-library controls. Removing a saved post
  does not change past campaign snapshots or the delivery journal.
- The history lists all campaigns with status, recipients, successful sends,
  blocked/unavailable, failures, and **unknown** outcomes. Pause and resume
  are supported. No scheduled/paid broadcasts.
- Templates are tied to the original Telegram bot ID; media `file_id`
  cannot be copied blindly to another bot after a credential/bot swap.

## Audience semantics

Only a real private `/start` addressed to *this particular connected*
Telegram bot records a contact. All registered contacts, including people
who never purchased and formerly subscribed, are eligible; subscriptions
are unrelated to consent to contact a bot. Record de-duplication is keyed
by (project, bot ID, Telegram user ID). Future `/start` removes a blocked
flag. No attempt is made to enumerate subscribers of a channel or discover
Telegram blocks ahead of time; the Telegram Bot API has no such list.

The audience is **not retrospectively backfilled** from a buyer's past
purchase or channel membership: those events do not prove they started
the current bot. New /start registrations are available after deployment
of the connected-bot runtime. Campaign recipients are snapshotted at
confirmation, so fresh /start events join **subsequent campaigns**.

## Delivery and safety

- Explicit admin confirmation and actor/project authorization at every
  callback. Each confirmation button contains a random one-use identity,
  atomically stored with the campaign. Replaying that same Telegram
  callback returns its original campaign without re-enqueueing recipients
  even after completion; a new preview/confirmation creates a new identity.
  Central ViMi Pay has no broadcast controls, no shared tenants
  or cross-project audience reads.
- PostgreSQL FORCE RLS on all five broadcast tables, composite references
  between saved posts and campaigns/delivery rows. The normal per-project
  supervisor launches a sibling broadcast task, separately from paid
  access synchronization; default throttle: **2 messages/second per bot**,
  not Telegram Paid Broadcast.
- Durable queue survives restarts. A delivery is marked `sending` before
  the external API call. Telegram has no idempotency key for this send:
  an uncertain outcome after crash, network failure or timeout is marked
  `unknown`, **never silently retried**, preventing unwanted duplicates.
  The owner sees these counts clearly. Rate-limit responses (429) may be
  retried with retry-after, at most 3 attempts because Telegram rejected
  the request. The retry-after deadline is persisted on the campaign and
  pauses **all remaining recipients** of that bot, not just the rejected
  recipient, including after a worker restart. Other Telegram errors are recorded without revealing
  personal data or secrets in the UI.
- Telegram `Forbidden` or unavailable recipient marks only that bot's
  audience contact as `blocked`; a new /start reactivates the contact.
  This is an inferred delivery state, not an API-provided exact block list.
- Existing Telegram payment receipt handlers, plan editing, finance
  records, subscriber management, owner service/access actions, bot tokens,
  and central control remain unmodified.

## Staging rollout (after merge approval)

1. `git pull --ff-only origin main`; check exact merge SHA.
2. Migrate PostgreSQL to `0032_tenant_broadcasts` with normal
   `compose.yaml + compose.light100.yaml` Alembic flow.
3. Rebuild **customer-supervisor only** using `compose.light100.yaml`;
   no central bot code change is necessary for campaign controls.
4. Open a connected owner bot, create and preview a saved text post.
   Verify a real test account that pressed /start receives it only once.
   Also test a forwarded photo/video with caption, the saved library,
   pause/resume and delivery history.
5. Do not broadcast to real customers during staging acceptance without
   approval. Use dedicated test accounts.

No production changes, SaaS fees, new payment methods, file-store services,
S3, multimedia downloads or broad permission changes.

## Album enhancement (PR after stage 1)

- Migration `0033_broadcast_media_albums` adds optional JSON media-element lists to
  saved posts and immutable campaign snapshots. Existing media and delivery
  rows are unchanged; no data is removed. Downgrade is intentionally blocked
  while album records exist rather than deleting financial/delivery evidence.
- A caption and its Telegram entities are preserved, then sent with the first
  photo/video in the group using `sendMediaGroup`. Duplicate messages
  and unsupported/broken album formats are rejected as a single batch.
- The broadcast worker spaces recipient sends by the number of media elements
  (2 elements per second average, before Telegram retry-after cooldown).
  Delivery status remains per recipient, not per attachment. A Telegram
  group-send timeout can have an uncertain outcome: it is never retried
  automatically to avoid duplicate marketing posts.
- Verify on staging using test users before live broadcasts. Test mixed
  photo+video albums, rich caption, preview, save/edit, repeat and progress.
