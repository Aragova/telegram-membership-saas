"""Scope owner setup wizard state to the physical bot, preserving existing rows.

Revision ID: 0018_owner_setup_bot_scope
Revises: 0017_owner_obligation_audit
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_owner_setup_bot_scope"
down_revision: str | None = "0017_owner_obligation_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "owner_setup_sessions",
        sa.Column(
            "bot_scope_id", sa.BigInteger(), nullable=False, server_default="0",
        ),
    )
    op.drop_constraint(
        "owner_setup_sessions_pkey", "owner_setup_sessions", type_="primary",
    )
    op.create_primary_key(
        "owner_setup_sessions_pkey", "owner_setup_sessions",
        ["user_id", "bot_scope_id"],
    )


def downgrade() -> None:
    # Only allow rollback when no connected-bot wizard rows are present.
    # Do not silently erase unrelated in-progress owner configuration.
    conn = op.get_bind()
    if conn.execute(sa.text(
        "SELECT 1 FROM owner_setup_sessions WHERE bot_scope_id <> 0 LIMIT 1"
    )).first():
        raise RuntimeError(
            "Cannot downgrade: connected-bot owner wizard sessions exist"
        )
    op.drop_constraint(
        "owner_setup_sessions_pkey", "owner_setup_sessions", type_="primary",
    )
    op.create_primary_key(
        "owner_setup_sessions_pkey", "owner_setup_sessions", ["user_id"],
    )
    op.drop_column("owner_setup_sessions", "bot_scope_id")
