"""Durable operator-only bot incidents and delivery outbox.

Revision ID: 0042_platform_monitor_alerts
Revises: 0041_monitoring_details

No new bot supervisor, monitoring service, tenant alert or payment change.
The health probe reads existing Supervisor records behind the SAME platform
operator database role boundary as platform_report_snapshot().
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_platform_monitor_alerts"
down_revision: str | None = "0041_monitoring_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_monitor_incidents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("bot_instance_id", sa.Uuid(),
                  sa.ForeignKey("bot_instances.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("suspected_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_runtime_state", sa.String(24), nullable=False),
        sa.Column("last_error_code", sa.String(40), nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("healthy_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_platform_monitor_open_incident", "platform_monitor_incidents",
        ["bot_instance_id"], unique=True,
        postgresql_where=sa.text("recovered_at IS NULL"),
    )
    op.create_table(
        "platform_monitor_alert_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("incident_id", sa.Uuid(),
                  sa.ForeignKey("platform_monitor_incidents.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "incident_id", "kind",
            name="uq_platform_monitor_incident_alert_kind",
        ),
        sa.CheckConstraint(
            "kind IN ('problem', 'recovered')",
            name="ck_platform_monitor_alert_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'cancelled')",
            name="ck_platform_monitor_alert_status",
        ),
    )
    # Table policies must not rely on the runtime DB role having direct
    # SELECT rights on platform_operators. Reuse an explicit owner identity
    # check behind the same privileged database boundary as 0019.
    op.execute("""
        CREATE FUNCTION public.platform_monitor_is_operator()
        RETURNS boolean LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT EXISTS (
                SELECT 1 FROM public.telegram_users u
                JOIN public.platform_operators o
                  ON o.telegram_user_id = u.telegram_user_id
                WHERE u.id = NULLIF(
                    current_setting('app.current_user_id', true), ''
                )::uuid AND o.role = 'owner'
            )
        $function$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.platform_monitor_is_operator() FROM PUBLIC"
    )
    for table in ("platform_monitor_incidents", "platform_monitor_alert_outbox"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        # Runtime may access ONLY with the true, DB-bootstrapped platform
        # owner's user context. This matches other platform RLS boundaries.
        op.execute(f"""
            CREATE POLICY platform_owner_monitor_read ON public.{table}
            FOR SELECT USING (public.platform_monitor_is_operator())
        """)
        op.execute(f"""
            CREATE POLICY platform_owner_monitor_insert ON public.{table}
            FOR INSERT WITH CHECK (public.platform_monitor_is_operator())
        """)
        op.execute(f"""
            CREATE POLICY platform_owner_monitor_update ON public.{table}
            FOR UPDATE USING (public.platform_monitor_is_operator())
            WITH CHECK (public.platform_monitor_is_operator())
        """)
        # No DELETE policy: incident history and send dedup cannot be erased
        # through a runtime DB credential, even if it has broad table grants.

    # Single bounded health source: existing bot_instances and Supervisor
    # heartbeat records; no Telegram API calls and no raw errors or tokens.
    op.execute("""
        CREATE FUNCTION public.platform_monitor_health_snapshot()
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            actor uuid := NULLIF(
                current_setting('app.current_user_id', true), ''
            )::uuid;
            result jsonb;
        BEGIN
            IF actor IS NULL OR NOT EXISTS (
                SELECT 1 FROM public.telegram_users u
                JOIN public.platform_operators o
                  ON o.telegram_user_id = u.telegram_user_id
                WHERE u.id = actor AND o.role = 'owner'
            ) THEN
                RAISE EXCEPTION 'Platform access denied'
                    USING ERRCODE = '42501';
            END IF;
            INSERT INTO public.platform_report_audit(
                actor_user_id, report_kind, period_since
            ) VALUES (actor, 'monitor_health', now());

            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                'bot_instance_id', b.id::text,
                'username', b.username,
                'registered_at', b.created_at,
                'status', b.status,
                'runtime_state', CASE
                    WHEN h.state = 'running'
                     AND h.last_heartbeat < now() - interval '120 seconds'
                    THEN 'stale'
                    ELSE COALESCE(h.state, 'unknown')
                END,
                'last_heartbeat', h.last_heartbeat,
                'error_code', h.error_code
            ) ORDER BY b.id), '[]'::jsonb)
            INTO result
            FROM public.bot_instances b
            LEFT JOIN public.customer_bot_runtime_health h
              ON h.project_id = b.project_id
             AND h.telegram_bot_id = b.telegram_bot_id;

            RETURN result;
        END;
        $function$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.platform_monitor_health_snapshot() FROM PUBLIC"
    )
    # Existing staging has membership_runtime; CI creates it AFTER Alembic.
    op.execute("""
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime')
            THEN
                GRANT EXECUTE ON FUNCTION public.platform_monitor_health_snapshot()
                    TO membership_runtime;
                GRANT EXECUTE ON FUNCTION public.platform_monitor_is_operator()
                    TO membership_runtime;
                GRANT SELECT, INSERT, UPDATE
                    ON public.platform_monitor_incidents,
                       public.platform_monitor_alert_outbox
                    TO membership_runtime;
            END IF;
        END
        $grant$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.platform_monitor_health_snapshot()")
    for table in ("platform_monitor_alert_outbox", "platform_monitor_incidents"):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS public.platform_monitor_is_operator()")
