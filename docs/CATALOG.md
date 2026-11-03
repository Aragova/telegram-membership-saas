# Catalog: Resources and Plans

This document defines the first commercial catalog boundary.

## Resource

A Resource is an access target. MVP types are:

- `TELEGRAM_CHANNEL`
- `TELEGRAM_GROUP`

The domain uses the generic name `Resource`; it is not modeled as a `telegram_chat` table. `external_id` is the adapter-side identifier used by the current Telegram access adapter.

Every Resource belongs to exactly one Project and is protected by project-scoped PostgreSQL RLS.

## Plan

A Plan is a commercial offer definition. In this slice it contains only:

- project ownership;
- name;
- calendar duration;
- lifecycle status;
- presentation order.

Pricing is intentionally not part of this PR. Currency/PlanPrice versioning will be added in its own slice.

Plan lifecycle statuses:

- `DRAFT`
- `ACTIVE`
- `ARCHIVED`

New Plans default to `DRAFT` so an incomplete offer cannot accidentally become sellable.

## Duration

Duration is represented by `duration_value + duration_unit`.

Supported units:

- DAY
- WEEK
- MONTH
- YEAR
- LIFETIME

`LIFETIME` requires `duration_value = NULL`. All other units require a positive integer value. A month is never modeled as 30 fixed days.

## Plan ↔ Resource

Plans and Resources are many-to-many through `plan_resources` from the beginning. This supports future bundles without redesigning the catalog.

`plan_resources` stores `project_id` and uses composite foreign keys `(id, project_id)` to both parent tables. This is deliberate defense in depth: even if an application bug or guessed UUID attempts to connect a Plan from Project A to a Resource from Project B, PostgreSQL rejects the relation.

## Tenant isolation

`resources`, `plans`, and `plan_resources` all:

- carry `project_id`;
- require an active project scope;
- have PostgreSQL RLS enabled and forced;
- reject rows outside `app.current_project_id`.

RLS is not a replacement for permissions. Application authorization still checks `resource.view/resource.manage` and `plan.view/plan.edit`.

## Owner / Service access

Owner/service access is not a Plan. When Entitlements are implemented, `OWNER_SERVICE` will grant access directly to Resources and may cover current/future Resources without creating PaymentRequest, Payment or Subscription.

This keeps commercial catalog data and non-commercial service access separate.
