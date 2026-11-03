# Tenant isolation

This project uses layered tenant isolation. Application authorization remains mandatory, and PostgreSQL Row-Level Security (RLS) acts as a second boundary against cross-project data leaks.

## Context

Every tenant-aware transaction binds:

- `app.current_user_id`
- `app.current_project_id` when a project has been selected

The settings are transaction-local (`set_config(..., true)`), so they disappear at transaction end and cannot leak through the connection pool.

## Project discovery

A user-only context can see projects where the current user is either:

- the explicit project owner; or
- present in `project_members`.

This allows the application to show the project picker without first knowing a project ID.

## Project scope activation

The application must never trust an arbitrary project ID. `tenant_session_scope()` binds the current user first, then verifies that the requested project is visible under RLS, and only then activates `app.current_project_id`.

If the project is not accessible, `TenantAccessDenied` is raised before project-scoped work begins.

## RLS tables

RLS is enabled and forced for:

- `projects`
- `project_members`

Future project-owned tables must receive project-scoped RLS policies in the same migration that introduces the table.

`telegram_users` is intentionally global identity data and is not a tenant-owned table.

## Authorization is still separate

RLS answers: **may this tenant reach this row?**

Permissions answer: **may this actor perform this action?**

For example, RLS can expose a project row to a viewer, while the application permission policy still denies `project.manage` or `payment.approve`.

## CI requirement

CI starts a real PostgreSQL service, applies Alembic migrations, and runs integration tests proving that:

1. owner A cannot see project B;
2. owner A cannot activate project B scope;
3. after owner B adds A as a project member, A can see and activate B;
4. the same runtime database role is still subject to policies because `FORCE ROW LEVEL SECURITY` is enabled.

This test is a required merge gate for future tenant-sensitive schema changes.
