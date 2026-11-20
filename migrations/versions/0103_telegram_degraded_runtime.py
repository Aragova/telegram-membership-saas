"""Keep transient Telegram outages separate from permanent worker failures.

Revision ID: 0103_telegram_degraded_runtime
Revises: 0102_saas_currency_onboarding

No changes to invoices, subscriptions, tokens, access grants or worker leases.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0103_telegram_degraded_runtime"
down_revision: str | None = "0102_saas_currency_onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.customer_bot_runtime_health
        DROP CONSTRAINT ck_customer_bot_runtime_health_state
    """)
    op.execute("""
        ALTER TABLE public.customer_bot_runtime_health
        ADD CONSTRAINT ck_customer_bot_runtime_health_state
        CHECK (state IN (
            'setup_needed', 'queued', 'starting', 'running',
            'degraded', 'stopped', 'error'
        ))
    """)


def downgrade() -> None:
    # Never disguise Telegram unavailability as a healthy poller.
    op.execute("""
        UPDATE public.customer_bot_runtime_health
        SET state='error'
        WHERE state='degraded'
    """)
    op.execute("""
        ALTER TABLE public.customer_bot_runtime_health
        DROP CONSTRAINT ck_customer_bot_runtime_health_state
    """)
    op.execute("""
        ALTER TABLE public.customer_bot_runtime_health
        ADD CONSTRAINT ck_customer_bot_runtime_health_state
        CHECK (state IN (
            'setup_needed', 'queued', 'starting', 'running', 'stopped', 'error'
        ))
    """)
