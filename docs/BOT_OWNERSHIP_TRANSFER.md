# Safe transfer of one connected bot between Telegram accounts

## Scope and irreversible boundary

A connected ViMi Pay Project is the unit of management ownership.
An owner with 20 projects transfers **one** project/bot only.
Nothing is copied or merged between unrelated projects or merchants.

Telegram BotFather ownership is separate and cannot be verified by Telegram's
Bot API. ViMi Pay requires a recipient-controlled **new bot token** that
`getMe` proves belongs to the same numeric bot ID, plus verification that
the recipient is an administrator in every active attached channel. The UI
explicitly tells both people to perform the BotFather ownership change
outside ViMi Pay first. These proofs do not certify legal ownership.

## Flow

1. Previous owner in their connected bot: **Settings → Transfer bot**.
   Enter the recipient's numeric Telegram ID. Recipient must have interacted
   with the bot (opened `/start`). Creates a 48-hour pending invitation.
2. Recipient: `/transfer` in **that bot**, examines the specific invitation,
   and confirms the transfer after performing the external BotFather transfer.
   They submit a fresh bot token. The bot immediately deletes the Telegram
   message; failure to delete means **no acceptance**, and the token must
   be rotated in BotFather.
3. Telegram `getMe` must return the exact old numeric bot identity and
   `getChatMember` must confirm recipient is an administrator of each
   active attached channel. The token is Fernet-encrypted while the request
   awaits final approval, never returned in UI/callbacks.
4. Previous owner: `/transfer` → Accepted → **Finalize** → explicit
   **final confirmation**. Finalization is atomic under project checkout
   lock, project row lock and transaction. Preconditions: current owner
   unchanged, correct bot ID, unexpired accepted request, no unfinished
   manual payment requests and no active owner-service grants.
5. Atomic effects: disable **all old active payment methods** so new buyers
   cannot pay the former owner's bank accounts; replace encrypted bot token;
   remove any now-redundant target `ProjectMember` role; change only the
   project's `owner_user_id`. A database SECURITY DEFINER history trigger
   closes the previous merchant-owner period, opens a new period on the
   **same permanent MerchantIdentity**, and marks the invitation completed,
   clearing the pending token. No PaymentRequest, Payment, Subscription,
   buyer, grant, entitlement or resource row is deleted or reassigned.
6. The existing supervisor tracks `BotInstance.updated_at`, therefore
   changing the encrypted token stops the incumbent polling task and starts
   the same bot's new-token poller automatically. The recipient opens their
   bot administrator panel, reviews payment instructions, and re-enables
   only methods that point to their own payee details.
7. Old owner loses project-specific management access. Other connected
   projects remain unchanged.

## Security boundaries

- Participant-only FORCE RLS for requests; SQL trigger controls immutable
  parties, status transitions and the exact actors. Arbitrary SQL updating
  `projects.owner_user_id` remains rejected, including after recipient
  acceptance unless the new token was installed, payees were disabled,
  unfinished payment requests resolved and no service grants remain.
- Merchant history is immutable. A separate historical owner period is
  created without changing `merchant_id` or any older settled invoices.
- The DB trigger enforces a recipient's explicitly configured connected-bot
  quota, if any; there is **no default limit**.
- Old owner can cancel or recipient can decline while invitation is open.
  One open invitation per project; expired invitations are invalid and
  automatically cleared upon a new owner attempt.
- No automatic refunds or backdating. Existing paid subscriptions remain
  in force; the new owner assumes servicing obligations. Unfinished requests
  must be resolved before transfer, not silently moved to another payee.
- The feature does not transfer Telegram BotFather ownership by itself.
  Anyone who lacks BotFather control should not hand over a fresh token.

## Staging acceptance (test accounts only)

- Create recipient account and add it as channel admin before acceptance;
  perform the external BotFather transfer for a **test bot** only.
- Owner creates invite; third-party actor cannot view/accept/finalize.
  Recipient submits a wrong bot token (rejected), then correct new token;
  owner explicitly finalizes.
- Verify owner changes only for that bot; new `/menu` owner panel and
  former owner's loss of access. Confirm supervisor restarts token version.
- Verify payment methods all disabled, previous financial records and active
  subscriptions unchanged. Manually re-enable test new-owner payee method.
- Verify expired/cancelled requests cannot complete, role change is
  race-safe, and historical merchant periods are preserved.

Migration: `0036_bot_ownership_transfer`.
Compose: `compose.yaml` plus `compose.light100.yaml`, apply Alembic
via the workspace-mounted app image before restarting customer-supervisor.
Never touch production.
