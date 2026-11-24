"""Align the audited platform running count with heartbeat freshness.

Revision ID: 0107_monitor_heartbeat_bound
Revises: 0106_saas_setup_hold

Only the existing report function's running count changes.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0107_monitor_heartbeat_bound"
down_revision: str | None = "0106_saas_setup_hold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "AND h.last_heartbeat >= now() - interval '120 seconds'"
_NEW = _OLD + "\n                  AND h.last_heartbeat <= now() + interval '60 seconds'"


def _replace_predicate(before: str, after: str) -> None:
    # Fetch the installed definition, preserving all unrelated report logic,
    # SECURITY DEFINER, fixed search_path, owner and EXECUTE grants.
    # Fail closed if the expected historical predicate is absent/ambiguous.
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef("
        "'public.platform_report_snapshot(timestamptz,integer,integer)'::regprocedure)"
    ).scalar_one()
    if definition.count(before) != 1:
        raise RuntimeError("Unexpected platform report heartbeat predicate")
    connection.exec_driver_sql(definition.replace(before, after))


def upgrade() -> None:
    _replace_predicate(_OLD, _NEW)


def downgrade() -> None:
    _replace_predicate(_NEW, _OLD)
