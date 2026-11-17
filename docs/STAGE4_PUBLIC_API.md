# Stage 4: isolated ViMi Pay public FastAPI foundation

**Stage order**: Stage 3 prepared the `pay-api.usevimi.ru` HTTPS virtual
host and deliberately returns `503 {"status":"api_not_deployed"}`.
Stage 4 starts **after** the common `usevimi.ru` website work. This
release is a closed local API foundation, not Telegram webhook cutover or
public billing launch.

## Verified versus pending (17 Nov 2026)

The operator's earlier output (16 Nov, not a fresh automated probe)
shows `pay-api.usevimi.ru/health` responding with **503
`{"status":"api_not_deployed"}`**, Nginx syntax passing, listener 80/443
active, a valid certificate, Certbot renewal simulation passing and a
renewal timer installed. This confirms the intended *closed* edge at that
time, not a deployed public FastAPI process. Recheck the response and
localhost port ownership before an approved API deployment.

Two open PRs serve separate purposes:
- **#176**, Stage-3 infrastructure record, closed Nginx templates, and
  renewal/rollback checks. The live server was configured separately; this
  PR must **not** overwrite or automatically reapply those templates.
- **#178**, this Stage-4 independent FastAPI + loopback Compose foundation.
  Merging #178 alone does **not** start a container or change the live 503.

**Next action after approval of the foundations:** design and review an
independent API-only restricted deploy path (distinct SSH forced command,
host trust, permissions and Compose project from the 51-bot deploy). The
existing `staging-deploy.yml` forced command accepts only `deploy <SHA>`
for the **bot stack**, not an API deployment. Do not repurpose or expand
its privileged command without a separate scoped security review.
The API release must validate a clean checkout and current approved
`main`, ensure no public TCP listener on 18080/8000 and start only
`vimi-pay-public`. Observe local health before any separate Nginx
proxy change. No shared VM remote write has been performed by either PR.

## Safety boundaries

- Existing `usevimi.ru`, `api.usevimi.ru` (Autoposting) and
  `staging.usevimi.ru` (Social) must remain unchanged.
- Keep the existing 51 Telegram bots, customer supervisor, database and
  billing ledger untouched. Do not switch any bot from polling to webhook.
- `src/telegram_membership_saas/public_api.py` is an independent ASGI
  entrypoint. It exposes **only** unauthenticated `GET /health` with the
  non-sensitive response `{"status":"ok"}`, no environment field.
  OpenAPI, Swagger, ReDoc, payments, Telegram/webhook and business
  endpoints are **not available**. A 200 is process liveness only.
- `ops/public-api.compose.yaml` is a **standalone Compose project**
  and must NEVER be combined with `compose.yaml`: that file's default
  `app` publishes `8000:8000` on all network interfaces.
- The new service binds host **127.0.0.1:18080 only**. Do not permit
  public firewall access to `18080`, `8000`, or PostgreSQL.
  Run it separately from bot/customer-supervisor deployment.
- TLS termination is still Nginx on the separately provisioned
  `pay-api.usevimi.ru` vhost. The stage-3 default must remain
  **503 closed** until an independently approved ingress-change PR
  and operator validation switch only `/health` to proxy.

## Independent release gates

1. Confirm Stage 3 live state: isolated DNS and TLS, certbot renewal,
   active Nginx and expected external 503 on `/health`. The Stage 3
   documentation/templates PR #176 is separate and may not be merged.
2. Review and merge this code only with owner's explicit approval,
   CI green on exact head and clean Codex review. A GitHub merge does
   **not** deploy the API: the existing staging SSH workflow restarts
   only bot/supervisor services and must not be repurposed silently.
3. For a later explicitly approved deployment, use the separate project:
   `docker compose -p vimi-pay-public -f ops/public-api.compose.yaml
   config` then `docker compose ... up -d --build` through a
   reviewed, restricted GitHub Actions process. **No manual git pull or
   live Docker changes outside the approved deploy workflow.**
4. On the VM, first confirm only `127.0.0.1:18080` is listening.
   Loopback `curl -fsS http://127.0.0.1:18080/health` should return
   `{"status":"ok"}`. All other routes must be 404.
5. Then in a **separate** reviewed cutover, edit only the
   `pay-api.usevimi.ru` Nginx server to proxy the exact `/health`
   path, leaving default `location /` closed; verify syntax,
   reload via approved procedure and test external 200 health,
   404 for unrelated routes, 443/TLS, plus all other vhosts.
6. To roll back: restore only the isolated `/health` 503 rule
   and reload Nginx after validation; stop only the dedicated API
   Compose project if necessary. Do not restart the 51 bots.

## What this first stage-4 PR intentionally excludes

No authenticated admin/customer REST routes, webhook handlers,
financial mutations, public /docs, payment-provider callbacks,
database migrations, production promotion, auto-deploy, DNS changes,
port-opening firewall rules or bot polling changes. Exposing a payment
or Telegram endpoint requires **separate** authentication/signature
verification, idempotency, tenant scoping, rate/size/time limits and
rollback/E2E review.

## Verification

`pytest tests/test_stage4_public_api.py` checks the liveness
response, disabled docs/business routes, loopback-only Compose binding,
no bot dependencies, and backwards compatibility of the original
development API.
