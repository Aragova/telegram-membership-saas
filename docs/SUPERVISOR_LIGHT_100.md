# ViMi Pay — lightweight 100-bot supervisor profile (opt-in)

## Product objective

Keep **100 low-traffic customer bots** reachable on the current 4-vCPU / ~4-GB
VM without requiring a new VM plan, a PostgreSQL restart or a global bot
registration cap. This is a targeted engineering capacity objective, **not**
a claim that 100 live Telegram bots have already been benchmarked.

## What changes (only in the optional profile)

- Existing `customer-supervisor` process handles up to 100 bots; no extra
  Docker supervisor replicas or dedicated per-bot processes are needed.
- The opt-in `SharedProjectLeaseManager` uses **one PostgreSQL session** to
  hold up to 100 different session-scoped advisory locks, rather than 100
  persistent database sessions. Lock **keys are unchanged**, so legacy
  supervisors and standalone pollers still cannot poll the same project.
- SQL on this one connection is serialized. Every project-specific statement
  reapplies `app.current_project_id` under the same mutex to preserve RLS.
  Connection loss and backend PID replacement cause a fail-closed shutdown.
  Health checks use balanced reentrant locks to avoid accumulating lock counts.
- Worker startup is spread over batches of 5 per 10-second discovery scan.
- For low traffic, access/outbox worker idle checks are every **5 seconds**
  instead of every 1 second. This may delay access/revocation by up to the
  configured idle interval, but **never** changes payment approval state or
  eliminates required entitlement reconciliation. Existing subscriptions
  continue to be processed while the catalogue is inactive.
- `max_connections` stays at the server's existing 100. An opt-in startup
  guard reserves the shared lease connection, bounded operational pool, and
  40 spare connections for the central bot and other applications. It checks
  `SHOW max_connections` before starting the customer fleet.

No financial-model, access-contract, RLS migration, token-storage, admin UI,
or buyer flow changes. The original `compose.yaml`, default 10-bot cap and
`per_project` lease mode are **unchanged**.

## Opt-in profile

File: `compose.light100.yaml`. Validate only (safe, no restart):

```bash
docker compose -f compose.yaml -f compose.light100.yaml \
  --profile supervisor config -q
```

Do **not** combine `compose.light100.yaml` with the earlier
`compose.scale100.yaml` profile. That earlier option runs four supervisor
replicas and increases PostgreSQL max_connections to 240; it is deliberately
not recommended for the current 4-GB VM.

## Before any activation on the existing staging VM

1. Verify exact deployed SHA and preserve database backups plus Fernet key.
   Never print tokens or keys.
2. Check CPU/RAM, current PostgreSQL connections and host-wide Docker memory.
   Avoid running other experiments simultaneously.
3. Confirm that **one** customer supervisor is active; no standalone legacy
   customer-bot is polling the same project. Existing project locks protect
   the transition even during a rolling restart.
4. Deploy `customer-supervisor` with only the lightweight overlay (not the
   older scale100 overlay). PostgreSQL is **not** restarted.
5. Inspect liveness, Telegram getUpdates, and real buyer admin/checkout flows
   on existing bots. Monitor subscription expiry, access revocation, no
   duplicate polling, and RLS. Roll back to `compose.yaml` if anything fails.
6. Increase test-bot count gradually (10, 25, 50, 75, 100). Measure RAM,
   CPU, connection count, 95th-percentile checkout latency, access latency,
   queue count and failure/retry patterns before claiming production readiness.

The 100-bot tests in CI simulate dispatcher admission and use a real
PostgreSQL instance for 100 advisory locks on one connection, including
cross-project tenant scoping. **These tests are not a 100-bot Telegram load
test and do not establish that one VM can handle 100 busy channels.**

## Fallback and rollback

Use the original `compose.yaml` to recreate only `customer-supervisor`.
Default settings restore legacy 10-bot admission and one PostgreSQL session
per active bot. No migration rollback or PostgreSQL restart is involved.
If more than 10 bots are already registered, some will be queued again,
so only use rollback for an actual incident.

The shared connection is a single failure domain for its project leases.
If it is lost, all owned bots fail closed. The supervisor drains the
pollers and exits with an error so Docker's existing `restart: unless-stopped`
policy can start a fresh process and reacquire locks automatically. This is
safer than two project pollers simultaneously issuing access actions. Do not disable the project advisory lock or fake health
heartbeats to sidestep this.
