# ViMi Pay SaaS Lava.top short checkout URLs

**Scope:** operator-authorized stage-A improvement only. Short URLs present the
*same* original Lava.top checkout more cleanly in Telegram. They do NOT create
another payment method, gateway order, finance ledger, provider key, webhook,
currency conversion, buyer purchase flow or settlement route.

## What changes

- Existing platform SaaS order UUIDv4 is encoded to an opaque 22-character
  Base64URL token: `https://pay-api.usevimi.ru/p/<token>`.
- Only the original central `@vimipay_bot` Lava payment button may use it,
  and only after `TMS_SAAS_SHORTLINKS_ENABLED=true` is specifically enabled
  in the central bot's Compose environment. Default is **off**.
- Isolated FastAPI handles **GET only**. The URL identifies an existing issued
  Lava order, not an invoice ID, payer ID or a merchant's checkout.
- One SQL SECURITY DEFINER lookup only returns the original provider URL when
  an unexpired original order remains payable for its original owner, has not
  been charged, has the exact unchanged remaining amount, and belongs to a
  retired or still-original-owner bot (not a transferred bot).
- Python validates HTTPS and the Lava.top domain again. Then it sends a
  no-store 302, a no-referrer policy, and no HTML containing provider tokens.
  No tracking pixels, analytics, search indexing or access logs for tokens.
- The destination browser **may still display the long Lava URL** after
  redirect. Telegram may still show an external-link warning. This change
  makes the preview and bot action compact; it cannot hide the provider's
  final address or bypass native Telegram security prompts.
- Original 30-minute gateway-order TTL still governs availability.
  Expired/paid/changed/cross-owned/invalid links fail closed with 404.
  A database outage fails with generic 503; an old direct Lava link continues
  to work with the feature disabled.

## Least privilege, staging prerequisites

Public API remains a **separate Docker Compose project** (127.0.0.1:18080),
separate API-only deployment, without Telegram bot/API or Lava credentials.

1. Apply reviewed migration `0095_saas_payment_shortlinks` through the
   existing controlled migration release. It creates a NOLOGIN group role
   `membership_shortlink_reader`, with schema USAGE and EXECUTE only on
   `platform_saas_lava_shortlink_target(uuid)`; PUBLIC execution is revoked.
   No table SELECT grants or finance mutation rights are given.
2. Using the authorized database operator, create a new unique LOGIN role
   (e.g. `vimi_shortlink_api`) with a **random private password**, NO SUPERUSER,
   NO CREATEROLE, NO BYPASSRLS and no other inherited roles. Grant only
   `membership_shortlink_reader`, CONNECT to the membership DB, and confirm
   it cannot SELECT gateway tables or EXECUTE payment settlement functions.
   Never reuse `membership_runtime`, `membership` or the bot DB password.
   Ensure the account is not a member of any pre-existing privileged role.
3. In the independently owned API deployment VM, root creates
   `/etc/vimi-pay-api/shortlinks.env` as **regular root-owned mode 0600**.
   Its only key is `TMS_SHORTLINK_DATABASE_URL`, using
   `postgresql+asyncpg://vimi_shortlink_api:<URL-encoded-private-password>@postgres:5432/membership`.
   Never paste the secret in chat, screenshots, GitHub, CI or logs.
   Compose interpolation reads this file via the root-owned API deploy script
   (which does NOT source/execute the file). No secret file is mounted in
   the container. Confirm the service sees this env var only.
4. Preflight confirms the external staging Docker network is exactly
   `telegram-membership-saas-staging_default`, and that `postgres` is
   reachable there. No Postgres port is published to the VM host or internet.
   The API joins only this existing network; this does not restart bots or
   alter other Docker networks. Operator must verify network topology before
   release. If it differs, stop instead of guessing/auto-connecting.
5. Only after API-only CI/review and explicit owner approval, use the
   `api-staging` manual workflow on merged main, and test local
   `/health` 200 and unknown routes 404. The API service must not be
   deployed by the central-bot staging workflow. Health alone is not a
   successful payment redirect test.
6. **Separately**, after an approved Nginx backup and `nginx -t`, install
   the example `ops/ingress/pay-api.lava-shortlink.location.nginx.example`
   as the sole additional `GET /p/<22 token>` route in the existing 443
   `pay-api.usevimi.ru` vhost. Register the example's named
   `limit_req_zone` in Nginx `http` context; this is required for
   rate limiting. Keep `/health` as-is, `location /` returning 404,
   and existing websites, ACME, SSL, API and Telegram routes unchanged.
   No wildcard proxy. Disable Nginx token access logs, while Uvicorn
   access logging is disabled in isolated API Compose.
7. Inspect a single **already issued and unpaid** original Lava gateway order
   with a short token through the API as an operator-only acceptance test.
   Expect HTTP 302 whose Location is the existing signed Lava checkout, with
   exactly the original amount. Test random/expired/paid tokens 404,
   `/payments` and `/webhooks/telegram` 404 and other ViMi sites unchanged.
   Do not perform a provider payment or create another invoice as a
   health check. Keep signed URLs and bearer tokens out of chat/screenshots.
8. **Only then** set `TMS_SAAS_SHORTLINKS_ENABLED=true` in the central
   bot's private staging `.env` and roll out the central bot through the
   separately approved bot staging workflow. Confirm `💳 Оплатить 150 ₽`
   leads through short `/p/` to the original amount, no duplicate order,
   and the existing independent provider GET settlement is unchanged.

## Rollback

1. Turn `TMS_SAAS_SHORTLINKS_ENABLED=false` back on in the central bot
   release environment and redeploy the approved bot version. New Lava buttons
   immediately revert to the original provider URLs.
2. Restore the backed-up Nginx vhost to remove only the scoped `/p/` route
   and its named request-limit zone, `nginx -t`, reload and verify normal
   sites/health endpoints.
3. Stop/revert the API-only deployment if needed. Previously paid orders and
   immutable original finance records remain untouched. Do not erase real
   checkout orders or revoke existing user entitlements.

**Out of scope:** USD/EUR payment currency selection, rates, $5 pricing,
web-cabinet login, provider webhooks, tenant purchases, automated refund
issuance. A 150 RUB invoice must not be silently repriced to the provider's
USD/EUR minimum 5; select and lock any alternate currency in a separate
reviewed financial PR with sale-time FX and exact provider settlement.
