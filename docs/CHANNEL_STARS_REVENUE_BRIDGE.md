# Channel ⭐ Telegram Stars: actual balance and withdrawal availability

## Purpose / source of truth

This feature reads **actual** Telegram channel revenue via the official
user-MTProto method `payments.getStarsRevenueStats(peer=channel)`:

- `status.current_balance`: current channel Stars still held by Telegram;
- `status.available_balance`: amount available for withdrawal;
- `status.withdrawal_enabled`: whether Telegram currently permits withdrawal;
- `status.next_withdrawal_at`: UNIX timestamp of the next permitted withdrawal;
- observation time is shown in the channel/project timezone.

The channel's stars are **not** the bot's Stars. The bot balance continues to
come from `getMyStarBalance`. An unavailable channel response is reported as
unavailable, **never zero** or 'withdrawal available'. The reader never
initiates a withdrawal or opens Fragment.

Authoritative sources:
- https://core.telegram.org/method/payments.getStarsRevenueStats
- https://core.telegram.org/constructor/starsRevenueStatus
- https://core.telegram.org/api/stars
- https://core.telegram.org/constructor/starsAmount

Telegram explicitly labels the first method **Only users can use this
method**, unlike Bot API's `getMyStarBalance`.

## Important limit: account consent and session privileges

**This is a first, one-owner, explicit allowlist integration only.** The
Telegram login session created by MTProto grants broad/full account powers
at the protocol layer, even though the sidecar implementation only performs
read RPCs. No technical "read-only Telegram session" exists. Do not ask
customers for Telegram passwords, codes, 2FA or exported sessions in the
ViMi Pay bot, web cabinet, chat or support messages.

The operator alone must provision and protect the session for a **test
account they own**. The session is never sent to ViMi Pay's ordinary bot
process or stored in the shared PostgreSQL database. A compromise of the
isolated sidecar's session still endangers that Telegram account.

Before enabling for other merchants, build a separate, consent-based,
security-reviewed credential handling architecture. **Do not onboard third
party Telegram user sessions through this first-stage owner-only overlay.**

## Architectural boundary

```
Owner's trusted workstation (one-time local QR login)
    | privately managed authorized Telegram user session file
    v
channel-finance  [separate, opt-in container; no host ports]
    | payments.getStarsRevenueStats (USER MTProto, non-mutating)
    | owner identity check + Telegram channel creator check
    | explicit allowed Telegram channel numeric IDs
    | verified 45-second snapshot cache
    v
private HTTP inside Docker + 32-byte+ bearer token
    v
customer-supervisor (shared access token only, NO user session/API hash)
    | project-owner auth + Bot API creator check + owner-ID match
    | reject mismatched/stale/malformed snapshots (>120 seconds)
    v
existing RU/EN Rich main admin panel
```

No public API route, login form, database migration, new Telegram menu or
new customer button. No calculation from subscriber count/plan price.
Native channel revenue is **not** automatically included in the 5% SaaS
commission ledger: verified financial reconciliation remains a separate
billing feature.

## Secure opt-in rollout for ONE channel owned by the operator

**Do NOT install the overlay through the ordinary staging SSH deploy.** It
is a separate, explicitly approved infrastructure change, initially for one
own test channel. Nothing in this PR starts it automatically.

1. On a trusted local workstation (not in a chat), obtain your own Telegram
   developer `api_id` / `api_hash` from `my.telegram.org`. Install local
   dependencies using `pip install '.[channel-finance]'`.
2. Save API hash in a local file with restrictive permissions. Never paste
   it into ChatGPT, GitHub, Telegram messages or a public shell log.
3. Create the user session **locally**, scanning a QR with your own
   Telegram app. Example (placeholders, *not* real account identifiers):

   ```bash
   python ops/channel_finance_local_login.py \
     --api-id YOUR_API_ID \
     --api-hash-file /private/api_hash \
     --owner-id YOUR_TELEGRAM_NUMERIC_ID \
     --out-file /private/channel_finance.session
   ```

   The helper checks the authenticated Telegram user ID before writing the
   session file with mode `0600`; it never prints the session string or
   sends the code/2FA to the bot. It may ask for 2FA **in the local
   terminal only**, never in ViMi Pay. Treat the resulting session as an
   account-wide, highly privileged key.
4. Create a separate random bearer token of at least 32 bytes, store in
   another private file. Transfer the API hash/session/token to an
   operator-controlled Docker host using an independently authenticated
   secret-transfer mechanism. Keep files outside the repository, .env,
   screenshots, builds, backups and chat histories.
   **Docker Compose file-backed secrets retain host UID/GID and permissions**.
   The locally created session is mode 0600 and would be unreadable by a
   different non-root container user. Do NOT use mode 0444 or world-readable
   session secrets. Instead, on the trusted Docker host, after separate
   deployment approval, provision the three files with the root-only script:

   ```bash
   sudo bash ops/channel_finance_prepare_secrets.sh /private/api_hash /private/channel_finance.session /private/access_key /srv/vimi-channel-finance-secrets
   ```

   It installs three host files as UID/GID **25073**, mode **0400**,
   into a root-owned mode-0700 directory. The isolated reader runs as
   dedicated UID 25073 and can read its mounts. Secrets remain unreadable
   to other unprivileged host accounts.
5. Set operator-controlled environment configuration:

   ```dotenv
   TMS_CHANNEL_FINANCE_API_ID=YOUR_API_ID
   TMS_CHANNEL_FINANCE_OWNER_ID=YOUR_TELEGRAM_NUMERIC_ID
   TMS_CHANNEL_FINANCE_ALLOWED_CHAT_IDS=-100YOUR_PRIVATE_CHANNEL_ID
   CHANNEL_FINANCE_API_HASH_FILE=/srv/vimi-channel-finance-secrets/channel_finance_api_hash
   CHANNEL_FINANCE_SESSION_FILE=/srv/vimi-channel-finance-secrets/channel_finance_session
   CHANNEL_FINANCE_ACCESS_KEY_FILE=/srv/vimi-channel-finance-secrets/channel_finance_access_key
   ```

   These are names/placeholders only. Do not commit a real values file.
   The reader checks the signed-in user identity and the Telegram
   `ChannelParticipantCreator` response *before* any financial read.
6. After a separate operator approval and permission review, use the
   optional `compose.channel-finance.yaml` overlay alongside
   `compose.yaml` and the same optional supervisor profile already
   used by the fleet. **Do not replace the normal deployment setup or
   restart the fleet just to configure this integration.** The sidecar
   exposes no published TCP port. The supervisor receives only the
   internal endpoint and the bearer token file.
7. Verify on the operator-owned private channel: nonzero balance, zero
   balance, amount eligible for withdrawal, waiting timestamp and
   expected Telegram-owner identity. Compare with native Telegram
   Monetization screen. Then test revoked Telegram access and disabled
   overlay; it must fail closed.

To disconnect, stop the sidecar, remove the overlay, revoke that user session
in Telegram **Settings → Devices**, and destroy the session file and bearer
token. No deletion of past channel sales is necessary because this feature
stores **no** new sales or revenue ledger.

## Correct status semantics

- `withdrawal_enabled=true` + positive available Stars + no future
  `next_withdrawal_at`: **Allowed by Telegram**, not a promise of
  successful Fragment settlement (minimum payout, region, 2FA age and
  session requirements may apply).
- Future `next_withdrawal_at`: **Waiting**, show exact local date/time.
- `withdrawal_enabled=false` without future unlock: **Unavailable**.
- Zero available Stars while flag is enabled: **No withdrawable Stars**.
- Network/API auth error, missing permissions, deleted creator, wrong owner,
  stale timestamps, an unconfigured sidecar: **Unavailable / not verified**,
  never a fabricated 0 or success badge.

Snapshot is verified at query time and can be cached 45 seconds in the
isolated reader; the bot refuses observations older than 120 seconds.

## Security guardrails

- Explicit private channel allowlist and user numeric ID.
- Telegram creator verification with `channels.getParticipant`; no
  relying on project membership or a username alone.
- The bot separately checks the project owner under RLS and compares the
  creator Telegram user ID supplied by Bot API.
- Authorization to the private bridge with a constant-time key comparison.
- Secrets read only from separately mounted `/run/secrets/` files.
- No public port and no separate user login flow in Telegram bots.
- Error responses omit Telegram session/key/exception details.
- Request timeout and bounded data size; stale or mismatched data rejected.
- No withdrawal operations and no writable finance storage.
- Requires a dedicated security review before involving customer accounts.

## Deferred design

Multi-tenant merchant onboarding, credential custody, per-owner revocation,
secure remote consent, independent audit, and channel transaction-based
commission reconciliation are **not** part of this first-stage integration.
A single account session must never be assumed to cover other channel
owners' channels.
