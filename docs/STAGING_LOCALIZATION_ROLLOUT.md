# ViMi Pay staging — one-pass RU/EN rollout

Scope: **staging** only, on `fast-vm` in
`~/telegram-membership-saas-staging`. This procedure does not modify
production or customer billing. The connected staging bot may control a real
channel, so choose a brief maintenance window and do not interrupt pending
payments or approval operations intentionally.

## Preflight

- PRs #36–#40 and the final language-audit PR must be merged to `main`.
- Verify the merge commit exact SHA in GitHub and `git rev-parse HEAD` on VM.
- Check central `@vimipay_bot` and connected bot are healthy; do not re-add,
  disconnect, or replace bot credentials.
- Never paste tokens, full `.env`, or customer IDs into chats or CI logs.
- Existing backups, `TMS_PLATFORM_OWNER_TELEGRAM_ID`, and `Europe/Moscow`
  timezone settings are preserved.

## Database first, then containers

Run as **one logical maintenance step**, stop if any command fails:

```bash
cd ~/telegram-membership-saas-staging
set -e

mkdir -p ~/vimi-staging-backups
sudo docker compose exec -T postgres \
  pg_dump -U membership -d membership -Fc \
  > ~/vimi-staging-backups/pre-ru-en-$(date -u +%Y%m%d-%H%M%S).dump

git pull --ff-only origin main
git rev-parse HEAD

sudo docker compose run --rm --no-deps \
  -v "$PWD:/work:ro" -w /work -e PYTHONPATH=/work/src \
  --entrypoint alembic bot upgrade head

sudo docker compose exec -T postgres \
  psql -U membership -d membership -Atc \
  'SELECT version_num FROM alembic_version;'
```

Expected Alembic head is `0022_detected_user_language`. The two new
schema changes `0021_user_language` and `0022_detected_user_language`
only add nullable columns and language constraints to global
`telegram_users`; existing payment and subscription data are not rewritten.

**Do not restart bots if backup, git update or migrations failed.**
Check the migration result before the next step.

## Rebuild/recreate with the exact migrated code

```bash
cd ~/telegram-membership-saas-staging
set -e
sudo docker compose --profile telegram --profile supervisor \
  up -d --no-deps --build bot customer-supervisor

sudo docker compose ps
sudo docker compose exec -T bot python -c \
  'import os; print("OWNER:", "CONFIGURED" if os.getenv("TMS_PLATFORM_OWNER_TELEGRAM_ID") else "MISSING")'
git rev-parse --short HEAD
```

Expected `bot`, `customer-supervisor`, `postgres` up, postgres healthy,
OWNER=CONFIGURED, and Git HEAD matching GitHub merge commit. Check container
logs for polling conflicts/startup errors **without publishing secrets**.

## Acceptance (no new charge required)

1. Telegram account with Russian app language: open central `/start`,
   `/platform` if Platform Owner, connected owner `/start` and buyer `/plans`.
   Check all navigation, bot statuses, plans, payment options and warnings.
2. English-language account: open central and connected bot `/start`.
   Verify English automatically; switch with `/language` and restart both bots.
   Confirm that an explicit choice persists across bots.
3. Open the connected bot's **existing published plan** in English and follow
   checkout through the payment instructions **without transferring money**.
   Customer-authored plan names and owner-entered bank details must remain
   unchanged; currency and amounts must be accurate.
4. Confirm pending review actions, buyer/access notification wording, timezone
   display and Platform Owner aggregates match their Russian counterparts.
   Only use controlled test accounts for paid approval/revocation scenarios.
5. Verify removing a bot remains blocked by outstanding paid rights and does not
   touch customer subscriptions during locale testing. Do not actually delete
   the working bot as part of this acceptance.

## Recovery

If new containers fail to start, **leave the migrated database intact**:
the added columns are backward-compatible with the previous code. Restore
the previous *known-good code revision* on staging, rebuild the bot/supervisor
images and restart those services only; first inspect migrations and errors,
never downgrade/drop language columns automatically. Restore the pg_dump only
after evaluating ongoing buyer activity, otherwise recently approved payments
could be lost. Keep all rollout work scoped to staging.
