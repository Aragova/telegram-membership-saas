# Priority audit: access and payment boundaries

Baseline: main `5190fa5`, audit of PR #237, 778 mypy findings in 104 files.
This is a scoped risk review, not a blanket fix of type annotations.

## P1 — confirmed unsafe data path

- Telegram access worker persisted five raw exception strings in DB/result
  paths. Telegram transport exceptions can contain tokens or private URLs.
  Fixed codes replace raw strings without changing the failure policy.
  Qodo review requested diagnostic context; warning logs now include only
  action UUID, fixed code and exception class, with no traceback.
  Existing owner incident detection relies on non-null error, not text.
  No historical data rewrite or credential rotation: actual past exposure
  has not been established. Review old data separately with redacted queries.

## P2 — boundary hardening

- Crypto Pay: malformed URL/port now raises a safe domain error; createInvoice
  rejects nonpositive invoice IDs, consistent with recovery. Non-ASCII
  signatures return false instead of causing compare_digest TypeError.
  Provider amount/timestamp parse errors no longer chain raw input exceptions.
- Lava.top: malformed provider amounts use a bounded LavaTopError instead of
  ValueError; malformed URL/port and UUID errors do not expose parser causes.
  Local input validation still returns ValueError. Frozen amount/currency,
  allowed ranges and provider authentication unchanged.
- File Stars payment ingress: explicitly requires matching private-chat sender,
  successful_payment and file payload; consistent with subscription ingress.
  The router already filters file payments; this is defense in depth.

## Deliberately unchanged

- SQL constraints for paid Crypto Pay dates/targets and native Stars links
  (migrations 0059/0060/0095) establish non-null invariants.
- Historical Stars termination branch only has termination times when a
  subscription exists (owner_end_times). Do not change ledger semantics.
- Supervisor shared lease handle is verified by PostgreSQL RLS tests.
- Decimal finite checks and retry/backoff algorithms remain unchanged.
- No menu changes, migration, price conversion, fee adjustment, provider
  calls, mass type suppression or bulk refactoring in this PR.

Scope is not a complete audit of all 778 findings or all exception/logging
paths. Lower-priority type cleanup and additional independently verified
issues remain separate work. Tests use synthetic secrets and offline
provider responses; PostgreSQL integration runs in existing isolated CI.
