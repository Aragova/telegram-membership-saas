# Telegram MVP commerce flow

This slice turns the existing payment/access domain into a usable Telegram MVP.

## Runtime model

The current MVP binds one bot process to one Project:

- `TMS_TELEGRAM_BOT_TOKEN` — runtime secret; never commit a real token.
- `TMS_TELEGRAM_PROJECT_ID` — Project UUID handled by this bot process.
- `TMS_TELEGRAM_WORKER_POLL_SECONDS` — idle polling interval for internal access work.

The domain remains multi-project. The one-process/one-project binding is only the current
Telegram runtime deployment shape; BotInstance/multi-bot routing can be added later without
changing PaymentRequest, Payment, Subscription, EntitlementGrant or AccessState.

## Customer flow

1. `/start` or `/plans`.
2. The bot lists every active PlanPrice × compatible active PaymentMethod pair.
3. The customer chooses a price/payment method.
4. A `PaymentRequest` is created through the existing domain service, freezing price and
   duration snapshots.
5. The bot renders the configured manual payment instruction.
6. The customer presses **Я оплатил** and receives a ForceReply prompt linked to that request.
7. The customer **must reply to that prompt** with a Telegram photo or a PDF/JPG/PNG/WEBP document.
   Plain text, missing proof or a reply to someone else's message never submits the request.
8. On accepted proof, the request moves to `waiting_review`. The bot copies the media
   directly from buyer chat to owner/admin/payment managers inside Telegram, followed by
   approve/reject controls. Media bytes and Telegram file IDs are **not stored** in our DB,
   object storage or logs. The admin checks actual funds, not the image's authenticity.
9. Approval uses the existing atomic approval service:
   `Payment → Subscription → EntitlementGrant → ACCESS_RECALCULATE`.
10. The project runtime consumes outbox/access work and the Telegram access layer sends a
    protected join-request link when needed.

Rejecting a request creates no Payment and no Subscription.

## Admin flow

Authorized reviewers receive payment-review messages automatically, together with the proof
copied through Telegram. `/payments` lists pending requests again if a notification was missed,
but the bot does not retain receipt media for later replay. If Telegram delivery fails, the
buyer is informed to reply with the same photo/document again. A review without the attachment
should not be approved until the admin independently verifies actual funds.

Permissions are enforced by the domain permission model, not by trusting callback data. A buyer
cannot approve their own request merely by forging an inline callback.

## Runtime loop

The bot process also runs the project worker loop. Each iteration can:

- schedule due entitlement boundaries;
- expire stale Telegram join tickets;
- consume one `ACCESS_RECALCULATE` outbox event;
- execute one Telegram GRANT/REVOKE action.

OWNER_SERVICE access is reconciled periodically so newly activated Telegram resources also get
the project owner's permanent service entitlement without a commercial Payment or Subscription.

External Telegram calls remain outside database transactions.

## Local/staging launch

With PostgreSQL running and environment variables supplied:

```bash
docker compose --profile telegram up --build
```

The `bot` service runs `tms-bot`; the API service remains separate.

## Intentionally not included

- receipt/screenshot storage, OCR, AI receipt analysis or fraud scoring;
- automatic payment providers;
- promotions/referrals;
- web admin panel;
- multi-bot credential vault/routing.

The next milestone after this slice is a real staging E2E with a separate bot, database,
Telegram channel/group, owner account and buyer account.
