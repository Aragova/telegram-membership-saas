"""Allow durable native membership departure observations in the outbox.

Revision ID: 0108_native_departure_outbox
Revises: 0107_monitor_heartbeat_bound
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0108_native_departure_outbox"
down_revision: str | None = "0107_monitor_heartbeat_bound"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_outbox_events_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_events_type", "outbox_events",
        "event_type IN ('access_recalculate', 'native_departure')",
    )


def downgrade() -> None:
    # Never silently discard queued membership evidence on rollback.
    op.drop_constraint("ck_outbox_events_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_events_type", "outbox_events",
        "event_type IN ('access_recalculate')",
    )
