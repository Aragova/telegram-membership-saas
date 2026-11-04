"""Safely detach customer bots after polling stops without erasing billing history.

Revision ID: 0016_bot_deletion_lifecycle
Revises: 0015_customer_runtime_supervisor
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_bot_deletion_lifecycle"
down_revision: str | None = "0015_customer_runtime_supervisor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION_NAME = "public.list_customer_bot_removals()"


def upgrade() -> None:
    op.drop_constraint(
        "ck_bot_instances_status", "bot_instances", type_="check"
    )
    op.create_check_constraint(
        "ck_bot_instances_status", "bot_instances",
        "status IN ('registered', 'disabled', 'deleting')",
    )
    # Deleting a BotInstance will automatically remove its runtime health.
    # No former poller can recreate an orphan health record after the bot is
    # gone: the FK rejects the stale INSERT.
    op.execute(
        """
        DELETE FROM public.customer_bot_runtime_health AS h
        WHERE NOT EXISTS (
            SELECT 1 FROM public.bot_instances AS b
            WHERE b.telegram_bot_id = h.telegram_bot_id
        )
        """
    )
    op.create_foreign_key(
        "fk_customer_bot_health_telegram_identity",
        "customer_bot_runtime_health", "bot_instances",
        ["telegram_bot_id"], ["telegram_bot_id"],
        ondelete="CASCADE",
    )
    op.execute(
        """
        CREATE FUNCTION public.list_customer_bot_removals()
        RETURNS TABLE(project_id uuid, telegram_bot_id bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT b.project_id, b.telegram_bot_id
            FROM public.bot_instances b
            WHERE b.status = 'deleting'
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION_NAME} FROM PUBLIC")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime') THEN
                GRANT EXECUTE ON FUNCTION {FUNCTION_NAME} TO membership_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Refuse downgrade until pending deletions have completed.
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS(SELECT 1 FROM public.bot_instances WHERE status = 'deleting') THEN
                RAISE EXCEPTION 'Complete pending bot deletions before downgrade';
            END IF;
        END $$"""
    )
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}")
    op.drop_constraint(
        "fk_customer_bot_health_telegram_identity",
        "customer_bot_runtime_health", type_="foreignkey",
    )
    op.drop_constraint(
        "ck_bot_instances_status", "bot_instances", type_="check",
    )
    op.create_check_constraint(
        "ck_bot_instances_status", "bot_instances",
        "status IN ('registered', 'disabled')",
    )
