# ViMi Pay — opt-in 100-bot supervisor topology (v1, no checkout rewrite)

## Scope and guarantee

This is an **infrastructure capacity profile**, not a product-level ceiling.
An owner may continue registering separate projects/bots without a global
counter. The scale profile targets **up to 100 concurrently polling registered
bots**, subject to the actual VM and PostgreSQL capacity and an acceptance test
with real Telegram bots. It does NOT claim that one small VM can handle 100 busy
bots, or that 100 real bots have already been load-tested.

The current customer bot runtime, project isolation (PostgreSQL RLS), buyer
checkout, subscriptions, entitlement expiry, token encryption, financial ledger,
and per-project advisory lease remain unchanged.

## Why four shards of 32 (capacity 128), not one 100-bot process

A healthy bot holds one dedicated PostgreSQL advisory-lock connection for its
full polling lifetime. Increasing the old 10->100 on one process would consume
all default PostgreSQL connections and starve payments, expiry and other bots.
Instead, four supervisor replicas share an identical deterministic placement
algorithm (project-ID rendezvous preference with a bounded fallback), at most
32 workers per replica. 128 slots provide capacity for 100 projects even when
hash preferences are skewed. The placement is recomputed from the same
registered-project snapshot on every scan and is independent of discovery order.

A missing shard will leave its assigned projects offline. Running only the
first shard with shard_count=4 **does not** cover all 100 bots. All four shards
and the database must run together. Existing per-project advisory locks
serialize rolling transfers/restarts so two pollers do not simultaneously own
one project. A changed fleet or topology can temporarily move projects, so
change shard count during a maintenance window only.

All replicas still discover all registered targets. A target belongs to exactly
one shard at a time, and overflow beyond the total installation capacity is
marked queued by shard 0. A queued status can never replace a live lease
holder's health row. Bot retirement remains lock-safe across replicas.

## Required PostgreSQL budget

The **scale profile** adds a separate opt-in Compose overlay. It configures
PostgreSQL max_connections=240, four supervisors with max=32 each and a
fail-closed startup check before polling. Each supervisor can hold 32 dedicated
lease connections plus an operational pool of up to 15. A further 40 connection
slots are reserved for central/API/maintenance; at least three remain reserved
for PostgreSQL superuser use. Required budget: 4*(32+15)+40+3 = 231;
240 passes. A server with max_connections=100 will refuse the four-shard
configuration rather than launching a fleet that starves payments.

This **does not** establish that the VM has enough memory, CPU or Telegram
throughput. Check CPU, RAM, network and connection activity before deployment;
increase capacity or split infrastructure if needed. Keep storage and backups
verified. In real production, a separate managed PostgreSQL instance can be
configured with the same budget without running the Compose postgres service.

## Compatible default (no changes to existing staging)

With only `compose.yaml` the supervisor remains exactly the previous setup:
one shard (ID=0), ten workers by default, max 32 per process. No migrations or
new secrets are needed. Production and staging **do not** change until the
operator deliberately supplies `-f compose.scale100.yaml`.

The overlay uses four supervisor services:
`customer-supervisor` (shard 0), `customer-supervisor-1` (1),
`customer-supervisor-2` (2) and `customer-supervisor-3` (3).

## Staged deployment after merge authorization and resource preflight

1. Record exact merged SHA, back up PostgreSQL and encrypted-bot credentials
   (do not print tokens or keys), and verify the available VM memory/CPU.
2. During an agreed maintenance window, validate the **full** Compose config:

   ```bash
   docker compose -f compose.yaml -f compose.scale100.yaml \
     --profile supervisor --profile supervisor-scale config -q
   ```

3. Run the Postgres change deliberately (may restart PostgreSQL briefly):

   ```bash
   docker compose -f compose.yaml -f compose.scale100.yaml up -d postgres
   ```

   Confirm PostgreSQL is healthy and its configured max_connections is 240.
   Do not attempt to raise max_connections via an unrelated SQL UPDATE.
4. Start all four supervisors together:

   ```bash
   docker compose -f compose.yaml -f compose.scale100.yaml \
     --profile supervisor --profile supervisor-scale up -d --build \
     customer-supervisor customer-supervisor-1 \
     customer-supervisor-2 customer-supervisor-3
   ```

5. Verify all four workers run, all registered bots have a fresh *real*
   Telegram getUpdates heartbeat, and payments and expiration continue.
   Test bot registration, token rotation, shutdown/restart, owner admin,
   cross-tenant isolation, subscription expiry and no double polling.
   Then perform staged load testing at 10, 25, 50, 75 and 100 *real bots*.
   Monitor PostgreSQL active/idle sessions, CPU, RAM, restart loops, queue
   statuses, Telegram rate limits and buyer checkout latency.
   **Do not claim 100-bot production acceptance without these measurements.**

## Rollback

Keep PostgreSQL at the higher connection setting during immediate rollback
(it need not be restarted a second time). Stop and remove only shards 1–3,
then restore the old single-supervisor environment with plain `compose.yaml`
and restart shard 0. The project advisory lock still prevents double polling.
If over ten registered bots already exist, overflow will queue until the
capacity issue is addressed — rollback is an emergency safety mechanism,
not a 100-bot steady-state configuration.

## Next scaling boundary

1000+ busy bots will warrant measured database partitioning, more replicas,
and likely a webhook/queue architecture to remove a one-lease-connection-per-
polling-bot pattern. This PR deliberately **does not** rewrite the existing
access/runtime system or promise unlimited capacity on one PostgreSQL server.
