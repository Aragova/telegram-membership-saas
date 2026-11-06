# ViMi Pay — tenant bot broadcasts (stage 1)

## Scope and user experience

Only inside an administrator's own **connected bot**:
`Admin dashboard → 📨 Рассылки`.

- ✏️ Write a text post using Telegram's normal editor: emoji, URLs
  (`t.me/...`), rich text entities and links.
- ↪️ Forward an existing text/photo/video/document/animation message to
  the same bot. Copy its body, caption, UTF-16 Telegram entities and media
  `file_id` into an independently reusable template. A protected message
  is rejected. One message per template; a multi-message media album is
  explicitly rejected instead of silently dropping files.
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
  callback. Central ViMi Pay has no broadcast controls, no shared tenants
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
