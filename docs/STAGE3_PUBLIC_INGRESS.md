# Stage 3: isolated public DNS and HTTPS for ViMi Pay

**Scope:** `pay-api.usevimi.ru` is a reserved future API/webhook hostname.
Stage 3A/B creates ONLY an isolated domain and closed HTTPS boundary.
**Not a production API launch or webhook cutover.** No existing buyer flows,
Telegram polling, tenant billing, files, PostgreSQL or Compose are changed.

## Current evidence and unknowns (16 Nov 2026)

- The operator previously reserved `34.13.188.95` for GCE `fast-vm`,
  zone `europe-west4-b` (recorded in stage-2/launch-readiness history).
  **Its current attachment and static reservation have NOT been rechecked.**
- Existing `usevimi.ru` hosts the separate ViMi Autoposting website;
  `api.usevimi.ru` and `staging.usevimi.ru` have prior uses.
  **Do not alter these records, virtual hosts, or sites.**
- `src/telegram_membership_saas/api.py` exposes only an application
  `/health`. The default Compose `app` service publishes `8000:8000` on
  all host interfaces if started. **Do not start this service on the public
  VM using the default Compose configuration.** The bot/supervisor do not
  need a public API listener today.
- No authenticated Google Cloud, REG.RU, VM shell or DNS-zone access is
  available to the GitHub PR. These templates do not assert live state.

## Gate 1: read-only preflight, before ANY changes

Use an authorized GCP operator console or terminal. Do not paste credentials,
SSH keys, tokens, full Nginx configuration or environment files into chats.

```bash
gcloud compute instances describe fast-vm --zone=europe-west4-b \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)'
gcloud compute addresses list \
  --filter='address=34.13.188.95' \
  --format='table(name,address,status,region)'
gcloud compute firewall-rules list \
  --format='table(name,direction,allowed,disabled)'
```

Require the VM's *current* external IPv4 to equal the reserved static address
and its reservation to be `IN_USE` by the intended VM. If not, STOP.
Never replace/reassign the VM address merely to make a documentation example
match. Reserve/promote its CURRENT ephemeral IP instead only if needed and
after assessing the existing hostname and SSH deployment dependencies.
Google Cloud's "promote ephemeral to static" keeps the same address.

Confirm which provider is authoritative for DNS; REG.RU being the registrar
does **not** prove REG.RU DNS hosts the zone. From an administrative workstation:

```bash
dig +noall +answer usevimi.ru NS
dig +noall +answer usevimi.ru A
dig +noall +answer usevimi.ru AAAA
dig +noall +answer api.usevimi.ru A
dig +noall +answer staging.usevimi.ru A
dig +noall +answer pay-api.usevimi.ru A
dig +noall +answer pay-api.usevimi.ru AAAA
dig +noall +answer pay-api.usevimi.ru CNAME
```

Record these BEFORE values privately. If `pay-api` has any existing
A/AAAA/CNAME, STOP until its purpose/ownership is verified. Do not change
NS, `@`, `www`, `api`, `staging`, or other hosts. No AAAA until IPv6 is
intentionally configured and reachable.

On the target VM, **read-only** check the webserver and listeners:

```bash
sudo ss -lntp
sudo systemctl status nginx --no-pager
sudo nginx -t
sudo ls -l /etc/nginx/sites-enabled/
```

Check for a conflicting `server_name pay-api.usevimi.ru` without sharing
the complete Nginx dump. Confirm whether another web server already owns
80/443. Nginx must be present and functioning or select an explicitly
reviewed equivalent. Do not stop existing web services to free a port.

## Gate 2: DNS, only after Gate 1 proves the target

In the **actual authoritative DNS panel** add just:

| Type | Hostname | Value | TTL |
| --- | --- | --- | --- |
| A | pay-api.usevimi.ru | the **verified current static VM IPv4** | 300 sec |

If the DNS panel uses a relative host label in the `usevimi.ru` zone,
enter `pay-api` rather than the whole FQDN. Do not create another A record
in parallel with an existing value. Verify externally that the new A record
resolves to the expected address; do not interpret cached DNS as definitive.

## Gate 3: isolated HTTP-01 challenge, no application yet

Only on a VM where Nginx already works, back up the current Nginx site
configuration, then use
`ops/ingress/pay-api.http.nginx.conf.example` as a *NEW* isolated vhost.
Do **not** replace default sites or existing ViMi vhosts.

```bash
sudo install -d -m 0755 /var/www/vimi-pay-acme/.well-known/acme-challenge
# Copy reviewed template into a NEW sites-available entry, enable ONLY it,
# then check syntax before reloading:
sudo nginx -t && sudo systemctl reload nginx
```

Verify an HTTP challenge file under the webroot is reachable via
`http://pay-api.usevimi.ru/.well-known/acme-challenge/<temporary-file>`.
The template responds 404 for all other paths, intentionally. Remove the
temporary challenge file after checking. If port 80 is not publicly reachable
or hostnames overlap, STOP and investigate rather than disabling services.

Use Certbot in **certonly webroot** mode to avoid editing unrelated vhosts:

```bash
sudo certbot certonly --webroot -w /var/www/vimi-pay-acme \
  -d pay-api.usevimi.ru
```

Enter the required contact details via the operator's secure console.
Do NOT use `certbot --standalone` if it requires shutting down the live
web server. Do not enable HTTPS before the certificate/key paths exist.

## Gate 4: HTTPS-only closed ingress

Use `ops/ingress/pay-api.https.nginx.conf.example` in place of the new
HTTP-only vhost, retaining all pre-existing vhosts unchanged.
First `sudo nginx -t`, then `sudo systemctl reload nginx`.
Verify **from outside** the VM:

```bash
curl -I http://pay-api.usevimi.ru/
curl -i https://pay-api.usevimi.ru/health
curl -I https://pay-api.usevimi.ru/
sudo certbot renew --cert-name pay-api.usevimi.ru --dry-run
```

**Certificate renewal reload is required for the webroot-only method.**
Before enabling automated renewals, inspect existing
`/etc/letsencrypt/renewal-hooks/deploy/` to avoid duplicate hooks.
If no existing reviewed hook reloads Nginx for this certificate, install
`ops/ingress/vimi-pay-certbot-deploy-hook.sh.example` as a new,
root-owned `0755` executable named
`/etc/letsencrypt/renewal-hooks/deploy/vimi-pay-nginx-reload`.
The hook checks `RENEWED_LINEAGE` for precisely
`/etc/letsencrypt/live/pay-api.usevimi.ru`, validates the whole Nginx
configuration with `nginx -t`, then reloads the service. It ignores
unrelated certificates. Use `sudo certbot certificates` to verify the
actual lineage matches; do not assume a suffixed/renamed certificate has
the default path. Never replace an existing hook indiscriminately.

```bash
sudo certbot renew --cert-name pay-api.usevimi.ru \
  --dry-run --run-deploy-hooks
```

Certbot's plain `--dry-run` does **not** invoke deploy hooks; the explicit
`--run-deploy-hooks` checks the installed hook after a successful staged
renewal, using the current live certificate rather than replacing it with
a test certificate. Separately confirm the enabled Certbot renewal timer
(or cron) and observe at least one scheduled run. No expiry/renewal SLA is
claimed from a one-time dry run.

Expected: HTTP redirects to HTTPS, HTTPS cert matches `pay-api.usevimi.ru`,
`/health` is **503 api_not_deployed** and `/` is 404.
This proves a secured empty ingress, NOT application readiness.
**Do not** publish `8000/tcp`, PostgreSQL `5432/tcp`, or the bot polling
process. If firewall changes are necessary, restrict them to the intended
HTTP/HTTPS ingress after verifying existing ingress rules.

## Gate 5: rollback, validation, later API release

- If the **new** vhost breaks Nginx validation, don't reload. Keep the
  last working configuration.
- If it must be rolled back after enablement, disable **only**
  `pay-api.usevimi.ru`'s new vhost and restore the previous known-good
  version, then `nginx -t && systemctl reload nginx`. Don't remove
  other vhosts/certs or edit existing DNS as a side effect.
- Keep the DNS A record if future launch still needs it; if DNS rollback is
  required, restore *only* the recorded `pay-api` record, never the zone.
- Independently verify `usevimi.ru`, `api.usevimi.ru` and
  `staging.usevimi.ru` still behave as before.
- Document observed URL, cert issuer/expiry and server response codes
  without tokens, IP inventories, user data or bank details.

**Next separate reviewed release (NOT in this stage):** configure an
opt-in API container bound **only to 127.0.0.1** on a dedicated safe host port,
without accidentally retaining Compose's existing `8000:8000` publish;
enable only authenticated/scoped API routes and verified webhook endpoints,
request-size/timeout/rate limits, safe proxy headers and secret validation.
Do not switch Telegram polling to webhook without durable update deduplication,
per-bot cutover and rollback tests. Production remains separate from staging.

## Evidence sources

- `docs/LAUNCH_READINESS_2026_11_15.md` / `docs/STAGING_GITHUB_ACTIONS.md`
- `docs/BACKUP_RECOVERY.md` (stage-2 static-IP and backup history)
- `compose.yaml`, `src/telegram_membership_saas/api.py`
- Google Cloud docs: https://cloud.google.com/vpc/docs/reserve-static-external-ip-address
- Certbot webroot instructions: https://eff-certbot.readthedocs.io/en/stable/using.html
