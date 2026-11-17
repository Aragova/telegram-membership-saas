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

**Current GitHub stage status (17 Nov 2026):** PR #176 (Stage-3
ingress documentation) and PR #178 (independent FastAPI foundation)
have both been merged into main. Neither merge started an API process,
changed Nginx or updated the 51-bot fleet. The separate restricted
API-only deployment workflow was merged as PR #179; an operator still
needs to provision its dedicated VM credentials and runtime under a
separately approved release. Never use the existing bot `staging-deploy.yml` for the API.

The next independent Stage-4C preparation is documented in
[STAGE4_INGRESS_CUTOVER.md](STAGE4_INGRESS_CUTOVER.md): one exact-match
Nginx `/health` location proxy, and only after the API is demonstrably
healthy on loopback. The other public routes stay 404. No shared VM
write, Nginx cutover or automatic deploy is implied by this source code.

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
   active Nginx and expected external 503 on `/health`. Stage-3 PR #176 is already merged; its templates were not installed by merging.
2. The FastAPI foundation #178 has been reviewed and merged. Its GitHub
   merge did **not** deploy it: staging SSH workflow restarts only
   bot/supervisor services and must not be repurposed silently.
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

## Dedicated API-only deployment preparation (follow-up PR, not installed)

The repository contains a **new manual, main-only** workflow
`.github/workflows/api-staging-deploy.yml`, plus **examples** of two
root-owned forced-command scripts. Merely merging them does not create
VM users, access keys, sudo privileges, containers or public endpoints.

### VM operator prerequisites (separate approval and secure access)

1. Inspect current HTTPS `pay-api.usevimi.ru/health` (expect closed 503),
   the **existing** Nginx configuration, Certbot timer/hook, and current
   `127.0.0.1:18080` port ownership. Confirm disk, build memory, Docker
   Compose v2 with `config --format json`, Python 3 and `flock` are available.
   Do not touch other hosts' virtual-host files.
2. Using a trusted VM administrative session, provision a separate
   **`vimi-api-deploy`** account with password login disabled, no Docker
   group, no general sudo privileges and no access to existing bot secrets.
   The existing `vimi-deploy` account and bot deployment scripts stay intact.
3. Provision a separate **root-owned** Git checkout at
   `/opt/vimi-pay-api/repo` with *read-only* GitHub deploy-key access and
   pinned `github.com` SSH host keys. Do **not** reuse the bot checkout or
   copy its secrets. Ensure the whole checkout and parent are root-owned
   and unwritable to the API SSH user. Its `origin` must point to this
   repository. An empty or dirty checkout is a release blocker.
4. Copy the two reviewed scripts from the approved checkout into root-only
   installed files, using `install -o root -g root -m 0755`:
   - `ops/deploy/vimi-api-ssh-gate.sh.example` to
     `/usr/local/sbin/vimi-api-ssh-gate`
   - `ops/deploy/vimi-api-root-deploy.sh.example` to
     `/usr/local/sbin/vimi-api-root-deploy`

   Before installation, validate both with `bash -n` and inspect the
   actual content. The deploy scripts are **examples**, never auto-installed.
   Their installed files and parent directories must not be writable by
   `vimi-api-deploy`. Do not copy repo files blindly as root.
5. Create a distinct SSH key for this account; its authorized-key line
   must use the restricted forced command:

   ```text
   restrict,command="/usr/local/sbin/vimi-api-ssh-gate" ssh-ed25519 <API-DEPLOY-PUBLIC-KEY>
   ```

   The account's dedicated, root-owned sudoers rule must authorize
   **only the no-argument command**:

   ```text
   vimi-api-deploy ALL=(root) NOPASSWD: /usr/local/sbin/vimi-api-root-deploy ""
   ```

   The sudoers `""` explicitly disallows command-line arguments. Validate the
   sudoers file with `visudo -c`. The gate accepts only
   literal `preflight` or `deploy <exact-current-main-40hex-SHA>`.
   It passes the SHA to the root script over stdin. There is no interactive
   shell, general `sudo`, Docker group, password auth, SSH forwarding,
   access to Telegram tokens, or bot restart permission.
6. Create the GitHub **`api-staging`** environment, limited to
   deployment branch **main** with human approval if available.
   Configure **only in that environment**:
   - variable `PAY_API_STAGING_SSH_HOST` (operator-verified VM IPv4)
   - secret `PAY_API_STAGING_SSH_PRIVATE_KEY` (dedicated API SSH private key)
   - secret `PAY_API_STAGING_SSH_KNOWN_HOSTS` (pinned exact VM ED25519 host key)

   Never reuse the existing `staging` environment keys or copy private
   keys into code, logs or chat.

### Opt-in release sequence

1. After *separately approved* merge and VM setup, in GitHub Actions choose
   **ViMi Pay | API-only staging release**, branch **main**, operation
   `connection-test`. Expect `API-ONLY SSH CONNECTION TEST: SUCCESS`.
   This operation only validates the dedicated SSH gate.
2. For the separately approved rollout, choose `operation: deploy`.
   GitHub sends its immutable `GITHUB_SHA`; the root script requires
   `origin/main` to equal that SHA, verifies a clean root-owned checkout,
   and builds/starts **only** Compose project `vimi-pay-public` and
   service `pay-api`. The resolved Compose config is rejected unless
   it contains just `pay-api`, one loopback-published 18080->8000 port,
   no mounts/secrets/dependencies, non-root and read-only settings.
   A process lock serializes API deployments independently of bot releases.
3. Expect `PAY_API_DEPLOY_SUCCESS: <SHA> (loopback only; public ingress unchanged)`.
   Check the container's `healthy` state, local `GET /health` JSON
   `{"status":"ok"}`, and local `/payments` 404. Confirm
   `18080/tcp` is **not reachable outside the VM**. The failure trap
   attempts to restore *only the previous API service*, or stops the new
   API if it did not previously exist. If restoration fails, **stop and
   inspect**; do not restart Telegram bots.
4. External `https://pay-api.usevimi.ru/health` **must still return**
   `503 {"status":"api_not_deployed"}`. The public Nginx switch
   to a loopback health proxy is a separate approved change after this
   local release; default paths, payments and webhooks remain closed.

**No automatic deploy on PR/merge/push**, no reuse of staging bot SSH,
no firewall port-opening or Nginx change, and no hidden service restart.
If API staging credentials or VM prerequisites are absent, the new workflow
fails closed. An API-only deployment must not be interpreted as a public
financial/webhook launch.

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
