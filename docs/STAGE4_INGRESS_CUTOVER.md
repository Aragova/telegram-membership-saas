# Stage 4C: optional public health-only ingress for ViMi Pay

**Status: source-controlled preparation, NOT enabled on the VM.** Stage 3
continues to respond to HTTPS `GET /health` with the intentionally closed
`503 {"status":"api_not_deployed"}` until an explicit public cutover.

## Preconditions

1. PR #176 (Stage-3 infrastructure) and #178 (independent FastAPI) were
   merged on 17 November 2026. The separate manual-only API deployment
   mechanism is being reviewed in PR #179. Its merge cannot provision VM
   credentials or start containers on its own.
2. Using approved read-only VM checks, identify the **actual** existing
   `pay-api.usevimi.ru` 443/TLS Nginx server and exact `location = /health`.
   Confirm Nginx, certificate, renewal hook and existing other hostnames still
   work. The live vhost may differ from the repository's sample. Stop if
   there are ambiguous hostnames/locations or unexpected configuration.
3. Under a *separately approved* `api-staging` Actions deployment, verify
   `GET http://127.0.0.1:18080/health` returns JSON `{"status":"ok"}`,
   the API container is healthy, local non-health paths return 404 and the
   port is bound to loopback only. External HTTPS `/health` must still return
   the Stage-3 503. **Never use the 51-bot staging workflow** for this API.
4. Confirm firewall and listener isolation: neither 18080 nor application
   port 8000 may be publicly reachable. No payment/webhook or DB integration
   is permitted in this release. If any gate fails, keep the ingress closed.

## Proposed narrowly scoped operator change

The reviewed file
`ops/ingress/pay-api.health-only.location.nginx.example` is **only an
Nginx location snippet**, not a full virtual host configuration. For an
approved cutover, an authorized operator backs up the **actual** TLS vhost,
then replaces precisely the existing Stage-3 `location = /health` 503 stanza
within the `server_name pay-api.usevimi.ru` 443/TLS block with the snippet.

Preserve without modification:

- The existing 80/443 listeners, TLS certificate paths, ACME challenges and
  renewal hooks, default redirects, access logs and other vhosts.
- The existing HTTPS default `location / { return 404; }`. **Do not** add
  a wildcard proxy location or a second HTTPS vhost.
- All Telegram polling, customer bots, SaaS billing, databases, secrets,
  GCP/REG.RU DNS, firewall rules and other ViMi hostnames.

This PR installs **nothing** on the VM and has no Nginx deployment workflow.
The cutover needs a separate owner-approved operator action. Before a reload,
run `sudo nginx -t`; if validation fails, restore the saved original without
reloading. After a scoped reload, run read-only smoke checks, including the
preexisting `usevimi.ru`, `api.usevimi.ru` and `staging.usevimi.ru`.

The exact-match URI proxies only to the independent API loopback
`127.0.0.1:18080/health`; the Nginx method gate allows GET (and implicitly
HEAD), denying other methods. The API response proves *process liveness*,
not authorization, financial correctness or any webhook readiness.

## Acceptance

From a trusted workstation, after the approved cutover, run the versioned
read-only script:

`bash ops/ingress/pay-api-health-smoke.sh`

It must verify trusted HTTPS and exact public `{"status":"ok"}` on `/health`,
plus HTTP 404 for `/`, `/payments`, `/subscriptions`,
`/webhooks/telegram`, `/docs`, `/openapi.json` and `/health/`.
Also independently check the original website and other ViMi services.
If any negative-path check unexpectedly succeeds, this release is **not**
acceptable.

## Rollback

On any unexpected route, TLS regression, wrong host, failing API health
or other vhost impact, restore **only** the backup of the original
`location = /health` returning Stage-3 503 in this TLS vhost.
Validate with `nginx -t` before the operator-approved reload, then recheck
503/404 externally and the unaffected other hosts. Do not restart the 51
bots or touch financial data. The isolated API can remain loopback-only for
diagnosis, or be stopped by its separate approved API release process.

## Deliberately out of scope

No public payments, customer REST, Telegram webhooks, provider callbacks,
DB migration or extra ports. Opening any other route requires separate
security review for signatures/authentication, tenant scoping, deduplication,
rate/size/time limits, logs and rollback. Never infer commercial launch
readiness from a successful health endpoint.
