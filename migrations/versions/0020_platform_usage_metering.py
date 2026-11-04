"""Immutable, tenant-opaque usage events and audited platform usage reporting.

Revision ID: 0020_platform_usage_metering
Revises: 0019_platform_dashboard

All writes occur in original business transactions via tightly scoped triggers.
History is retained even when a BotInstance or Project is deleted.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_platform_usage_metering"
down_revision: str | None = "0019_platform_dashboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_usage_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        # No foreign keys: deleting a bot/project must not erase its history.
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(36), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("bot_username", sa.String(64), nullable=True),
        sa.Column("currency_code", sa.String(16), nullable=True),
        sa.Column("amount", sa.Numeric(38, 18), nullable=True),
        sa.Column("event_key", sa.String(120), nullable=True, unique=True),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'bot_connected','bot_removed','bot_disabled','bot_reenabled',"
            "'bot_deleting','plan_published','payment_approved',"
            "'subscription_started','subscription_renewed','customer_joined'"
            ")",
            name="ck_platform_usage_event_type",
        ),
        sa.CheckConstraint(
            "(event_type = 'payment_approved' AND currency_code IS NOT NULL "
            "AND amount > 0) OR "
            "(event_type <> 'payment_approved' AND currency_code IS NULL "
            "AND amount IS NULL)",
            name="ck_platform_usage_payment_snapshot",
        ),
    )
    op.create_index(
        "ix_platform_usage_events_type_time",
        "platform_usage_events", ["event_type", "occurred_at"],
    )
    op.create_index(
        "ix_platform_usage_events_project_time",
        "platform_usage_events", ["project_id", "occurred_at"],
    )
    op.create_index(
        "ix_platform_usage_events_time_id",
        "platform_usage_events", ["occurred_at", "id"],
    )
    op.execute("ALTER TABLE platform_usage_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_usage_events FORCE ROW LEVEL SECURITY")
    # No direct runtime SELECT/INSERT/UPDATE/DELETE policies. The migration
    # owner-owned trigger records events; platform report definer aggregates
    # them after the same explicit owner check as Stage D.

    op.execute("""
    CREATE FUNCTION public.platform_meter_event()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $meter$
    BEGIN
        IF TG_TABLE_NAME = 'bot_instances' THEN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id, bot_username,
                    event_key, occurred_at
                ) VALUES (
                    NEW.project_id, 'bot_connected', NEW.id, NEW.username,
                    'bot_connected:' || NEW.id::text, NEW.created_at
                ) ON CONFLICT (event_key) DO NOTHING;
            ELSIF TG_OP = 'DELETE' THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id, bot_username, event_key
                ) VALUES (
                    OLD.project_id, 'bot_removed', OLD.id, OLD.username,
                    'bot_removed:' || OLD.id::text
                ) ON CONFLICT (event_key) DO NOTHING;
                RETURN OLD;
            ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id, bot_username
                ) VALUES (
                    NEW.project_id,
                    CASE NEW.status
                      WHEN 'disabled' THEN 'bot_disabled'
                      WHEN 'deleting' THEN 'bot_deleting'
                      ELSE 'bot_reenabled'
                    END,
                    NEW.id, NEW.username
                );
            END IF;
            RETURN NEW;
        ELSIF TG_TABLE_NAME = 'plans' THEN
            IF TG_OP = 'INSERT' AND NEW.status = 'active' THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id, event_key
                ) VALUES (
                    NEW.project_id, 'plan_published', NEW.id,
                    'plan_published:' || NEW.id::text
                ) ON CONFLICT (event_key) DO NOTHING;
            ELSIF TG_OP = 'UPDATE' AND NEW.status = 'active'
               AND OLD.status IS DISTINCT FROM NEW.status THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id
                ) VALUES (NEW.project_id, 'plan_published', NEW.id);
            END IF;
            RETURN NEW;
        ELSIF TG_TABLE_NAME = 'payments' THEN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id, currency_code,
                    amount, event_key, occurred_at
                ) VALUES (
                    NEW.project_id, 'payment_approved', NEW.id,
                    NEW.currency_code, NEW.final_amount,
                    'payment_approved:' || NEW.id::text, NEW.approved_at
                ) ON CONFLICT (event_key) DO NOTHING;
            END IF;
            RETURN NEW;
        ELSIF TG_TABLE_NAME = 'subscriptions' THEN
            IF TG_OP = 'INSERT' AND NEW.status = 'active' THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id, event_key, occurred_at
                ) VALUES (
                    NEW.project_id, 'subscription_started', NEW.id,
                    'subscription_started:' || NEW.id::text, NEW.started_at
                ) ON CONFLICT (event_key) DO NOTHING;
            ELSIF TG_OP = 'UPDATE' AND NEW.status = 'active'
                AND (
                    NEW.current_period_start IS DISTINCT FROM OLD.current_period_start
                    OR OLD.status IS DISTINCT FROM NEW.status
                ) THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id
                ) VALUES (NEW.project_id, 'subscription_renewed', NEW.id);
            END IF;
            RETURN NEW;
        ELSIF TG_TABLE_NAME = 'project_customers' THEN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO public.platform_usage_events (
                    project_id, event_type, source_id, event_key, occurred_at
                ) VALUES (
                    NEW.project_id, 'customer_joined', NEW.id,
                    'customer_joined:' || NEW.id::text, NEW.created_at
                ) ON CONFLICT (event_key) DO NOTHING;
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'Unexpected usage event source: %', TG_TABLE_NAME;
    END;
    $meter$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.platform_meter_event() FROM PUBLIC")

    for table, operations in (
        ("bot_instances", "INSERT OR UPDATE OR DELETE"),
        ("plans", "INSERT OR UPDATE"),
        ("payments", "INSERT"),
        ("subscriptions", "INSERT OR UPDATE"),
        ("project_customers", "INSERT"),
    ):
        op.execute(
            "CREATE TRIGGER trg_meter_" + table
            + " AFTER " + operations + " ON public." + table
            + " FOR EACH ROW EXECUTE FUNCTION public.platform_meter_event()"
        )

    # Accurate timestamps for existing durable records. There is no attempt
    # to invent past deletions or publish/renewal dates not preserved in the DB.
    op.execute("""
        INSERT INTO public.platform_usage_events(
            project_id, event_type, source_id, bot_username,
            event_key, occurred_at
        )
        SELECT project_id, 'bot_connected', id, username,
               'bot_connected:' || id::text, created_at
        FROM public.bot_instances
        ON CONFLICT (event_key) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.platform_usage_events(
            project_id, event_type, source_id, currency_code,
            amount, event_key, occurred_at
        )
        SELECT project_id, 'payment_approved', id, currency_code,
               final_amount, 'payment_approved:' || id::text, approved_at
        FROM public.payments
        ON CONFLICT (event_key) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.platform_usage_events(
            project_id, event_type, source_id, event_key, occurred_at
        )
        SELECT project_id, 'customer_joined', id,
               'customer_joined:' || id::text, created_at
        FROM public.project_customers
        ON CONFLICT (event_key) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.platform_usage_events(
            project_id, event_type, source_id, event_key, occurred_at
        )
        SELECT project_id, 'subscription_started', id,
               'subscription_started:' || id::text, started_at
        FROM public.subscriptions
        WHERE status = 'active'
        ON CONFLICT (event_key) DO NOTHING
    """)

    op.execute("""
    CREATE FUNCTION public.platform_usage_snapshot(
        p_since timestamptz, p_page integer, p_page_size integer
    ) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $report$
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
        IF p_since IS NULL OR p_since > now()
           OR p_page IS NULL OR p_page < 0 OR p_page > 100000
           OR p_page_size IS NULL OR p_page_size < 1
           OR p_page_size > 20 THEN
            RAISE EXCEPTION 'Invalid usage report parameters'
                USING ERRCODE = '22023';
        END IF;
        INSERT INTO public.platform_report_audit(
            actor_user_id, report_kind, period_since
        ) VALUES (actor, 'usage', p_since);

        SELECT jsonb_build_object(
            'events_total', (
                SELECT count(*) FROM public.platform_usage_events
                WHERE occurred_at >= p_since
            ),
            'event_counts', (
                SELECT COALESCE(jsonb_object_agg(event_type, n), '{}'::jsonb)
                FROM (
                    SELECT event_type, count(*) AS n
                    FROM public.platform_usage_events
                    WHERE occurred_at >= p_since
                    GROUP BY event_type
                ) counts
            ),
            'payments_by_currency', (
                SELECT COALESCE(jsonb_agg(
                    jsonb_build_object(
                        'currency', currency_code,
                        'count', n, 'amount', total::text
                    ) ORDER BY currency_code
                ), '[]'::jsonb)
                FROM (
                    SELECT currency_code, count(*) AS n, sum(amount) AS total
                    FROM public.platform_usage_events
                    WHERE event_type = 'payment_approved'
                      AND occurred_at >= p_since
                    GROUP BY currency_code
                ) totals
            ),
            'bot_event_pages', (
                SELECT ceil(count(*)::numeric / p_page_size)
                FROM public.platform_usage_events
                WHERE event_type IN (
                    'bot_connected', 'bot_removed', 'bot_disabled',
                    'bot_reenabled', 'bot_deleting'
                ) AND occurred_at >= p_since
            ),
            'bot_events', (
                SELECT COALESCE(jsonb_agg(to_jsonb(history)
                    ORDER BY history.occurred_at DESC, history.id DESC),
                    '[]'::jsonb)
                FROM (
                    SELECT id, project_id::text, source_id::text AS bot_id,
                           event_type, bot_username, occurred_at
                    FROM public.platform_usage_events
                    WHERE event_type IN (
                        'bot_connected', 'bot_removed', 'bot_disabled',
                        'bot_reenabled', 'bot_deleting'
                    ) AND occurred_at >= p_since
                    ORDER BY occurred_at DESC, id DESC
                    LIMIT p_page_size OFFSET p_page * p_page_size
                ) history
            )
        ) INTO result;
        RETURN result;
    END;
    $report$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.platform_usage_snapshot(timestamptz, integer, integer)
        FROM PUBLIC
    """)
    op.execute("""
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime')
            THEN
                GRANT EXECUTE ON FUNCTION
                public.platform_usage_snapshot(timestamptz, integer, integer)
                TO membership_runtime;
            END IF;
        END
        $grant$
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS
        public.platform_usage_snapshot(timestamptz, integer, integer)
    """)
    for table in (
        "project_customers", "subscriptions", "payments",
        "plans", "bot_instances",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_meter_{table} ON public.{table}")
    op.execute("DROP FUNCTION IF EXISTS public.platform_meter_event()")
    op.execute("ALTER TABLE platform_usage_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_usage_events DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_platform_usage_events_time_id", table_name="platform_usage_events")
    op.drop_index("ix_platform_usage_events_project_time", table_name="platform_usage_events")
    op.drop_index("ix_platform_usage_events_type_time", table_name="platform_usage_events")
    op.drop_table("platform_usage_events")
