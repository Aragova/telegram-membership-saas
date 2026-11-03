# Tenant isolation

The application uses layered tenant isolation. Application authorization remains mandatory, while PostgreSQL Row-Level Security (RLS) is a second boundary against cross-project data leaks.

## Database roles

RLS is only meaningful if the runtime connection cannot bypass it. The project therefore separates two database identities:

- **migration/owner role** — owns schema objects and is used only by Alembic;
- **runtime role** — used by the application, is non-superuser, and must have `NOBYPASSRLS`.

`TMS_DATABASE_URL` is the runtime connection. `TMS_MIGRATION_DATABASE_URL` is optional and, when set, is used by Alembic instead of the runtime connection.

Staging and production must never run the application with a superuser, schema-owner, or `BYPASSRLS` role.

## Context

Every tenant-aware transaction binds:

- `app.current_user_id`;
- `app.current_project_id` when a project has been selected.

The settings are transaction-local (`set_config(..., true)`), so they disappear at transaction end and cannot leak through the connection pool.

## Project discovery

A user-only context can see projects where the current user is either:

- the explicit project owner; or
- present in `project_members`.

This allows the application to show the project picker before a project has been selected.

## Project scope activation

The application must not trust an arbitrary project ID. `tenant_session_scope()` binds the current user first, verifies that the requested project is visible under user-scoped RLS, and only then activates `app.current_project_id`.

If the project is not accessible, `TenantAccessDenied` is raised before project-scoped work begins.

## RLS tables

RLS is enabled and forced for:

- `projects`;
- `project_members`.

Future project-owned tables must receive project-scoped RLS policies in the same schema slice that introduces the table.

`telegram_users` is intentionally global identity data and is not tenant-owned.

## Authorization is separate

RLS answers: **may this tenant reach this row?**

Permissions answer: **may this actor perform this action?**

A viewer may be able to read a project row through RLS while the application permission policy still denies `project.manage`, `payment.approve`, and other privileged operations.

## CI requirement

CI starts a real PostgreSQL service, applies Alembic migrations as the database owner, then creates a separate non-superuser runtime role and runs the application tests through that runtime role.

The integration test proves that:

1. owner A can see project A;
2. owner A cannot see project B;
3. owner A cannot activate project B scope;
4. after owner B adds A as a project member, A can see and activate project B;
5. the application runtime role is genuinely subject to RLS.

This integration test is a required merge gate for future tenant-sensitive schema changes.

## Local Docker

Local Compose mirrors the same model. PostgreSQL initializes a `membership_runtime` role through `docker/postgres/init-runtime.sql`; Alembic uses the owner connection while the application uses the runtime connection.

If an old local PostgreSQL volume was created before this split, recreate that development volume so the init script can create the runtime role. This concerns local development only; production migrations must never delete persistent data to apply role changes.
