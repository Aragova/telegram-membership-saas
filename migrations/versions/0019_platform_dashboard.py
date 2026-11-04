"""Platform operator grants and narrowly authorized, audited aggregate reporting.

Revision ID: 0019_platform_dashboard
Revises: 0018_owner_setup_bot_scope

SECURITY DEFINER is confined to one fixed report without arbitrary tenant
filters or user-provided SQL. Runtime cannot grant itself operator privileges.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_platform_dashboard"
down_revision: str | None = "0018_owner_setup_bot_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_operators",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'support')",
            name="ck_platform_operators_role",
        ),
        sa.CheckConstraint(
            "telegram_user_id > 0", name="ck_platform_operators_telegram_id",
        ),
    )
    op.create_table(
        "platform_report_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "actor_user_id", sa.Uuid(), sa.ForeignKey("telegram_users.id"),
            nullable=False,
        ),
        sa.Column("report_kind", sa.String(24), nullable=False),
        sa.Column("period_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.execute("ALTER TABLE platform_operators ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_operators FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_report_audit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_report_audit FORCE ROW LEVEL SECURITY")
    # No direct tenant/runtime write grants by RLS, even if the runtime role
    # has broad table DML grants. The definer is exclusively the DB migration
    # owner (PostgreSQL superuser in CI).
    op.execute("""
        CREATE POLICY platform_operator_self_read ON platform_operators
        FOR SELECT USING (
            telegram_user_id = (
                SELECT u.telegram_user_id FROM telegram_users u
                WHERE u.id = NULLIF(
                    current_setting('app.current_user_id', true), ''
                )::uuid
            )
        )
    """)
    op.execute("""
        CREATE POLICY platform_report_audit_self_read ON platform_report_audit
        FOR SELECT USING (
            actor_user_id = NULLIF(
                current_setting('app.current_user_id', true), ''
            )::uuid
        )
    """)
    op.execute("""
    CREATE FUNCTION public.platform_report_snapshot(
        p_since timestamptz, p_page integer, p_page_size integer
    ) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $function$
    DECLARE
        actor uuid := NULLIF(
            current_setting('app.current_user_id', true), ''
        )::uuid;
        result jsonb;
    BEGIN
        IF actor IS NULL OR NOT EXISTS (
            SELECT 1
            FROM public.telegram_users u
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
            RAISE EXCEPTION 'Invalid platform report parameters'
                USING ERRCODE = '22023';
        END IF;

        INSERT INTO public.platform_report_audit(
            actor_user_id, report_kind, period_since
        ) VALUES (actor, 'snapshot', p_since);

        SELECT jsonb_build_object(
            'owners', (
                SELECT count(DISTINCT owner_user_id)
                FROM public.projects
            ),
            'projects', (SELECT count(*) FROM public.projects),
            'bots', (SELECT count(*) FROM public.bot_instances),
            'bots_active', (
                SELECT count(*) FROM public.bot_instances
                WHERE status = 'registered'
            ),
            'bots_disabled', (
                SELECT count(*) FROM public.bot_instances
                WHERE status = 'disabled'
            ),
            'bots_running', (
                SELECT count(*) FROM public.bot_instances b
                JOIN public.customer_bot_runtime_health h
                  ON h.project_id = b.project_id
                 AND h.telegram_bot_id = b.telegram_bot_id
                WHERE b.status = 'registered'
                  AND h.state = 'running'
                  AND h.last_heartbeat >= now() - interval '120 seconds'
            ),
            'bots_new_period', (
                SELECT count(*) FROM public.bot_instances
                WHERE created_at >= p_since
            ),
            'resources', (
                SELECT count(*) FROM public.resources WHERE status = 'active'
            ),
            'plans_published', (
                SELECT count(*) FROM public.plans WHERE status = 'active'
            ),
            'plans_draft', (
                SELECT count(*) FROM public.plans WHERE status = 'draft'
            ),
            'subscriptions_active', (
                SELECT count(*) FROM public.subscriptions
                WHERE status = 'active'
                  AND (
                      current_period_end IS NULL OR current_period_end > now()
                  )
            ),
            'subscriptions_new_period', (
                SELECT count(*) FROM public.subscriptions
                WHERE started_at >= p_since
            ),
            'requests_pending', (
                SELECT count(*) FROM public.payment_requests
                WHERE status = 'waiting_review'
            ),
            'payments_period', (
                SELECT count(*) FROM public.payments
                WHERE approved_at >= p_since
            ),
            'gmv_by_currency', (
                SELECT COALESCE(jsonb_agg(jsonb_build_object(
                    'currency', totals.currency_code,
                    'amount', totals.total::text,
                    'count', totals.count
                ) ORDER BY totals.currency_code), '[]'::jsonb)
                FROM (
                    SELECT currency_code, sum(final_amount) AS total,
                           count(*) AS count
                    FROM public.payments
                    WHERE approved_at >= p_since
                    GROUP BY currency_code
                ) totals
            ),
            'bot_pages', (
                SELECT ceil(count(*)::numeric / p_page_size)
                FROM public.bot_instances
            ),
            'bot_rows', (
                SELECT COALESCE(jsonb_agg(to_jsonb(bot_row)
                    ORDER BY bot_row.connected_at DESC), '[]'::jsonb)
                FROM (
                    SELECT b.project_id::text AS project_id,
                           b.username AS username,
                           b.status AS status,
                           p.name AS project_name,
                           b.created_at AS connected_at,
                           CASE
                             WHEN h.state = 'running' AND h.last_heartbeat
                               < now() - interval '120 seconds'
                             THEN 'stale'
                             ELSE COALESCE(h.state, 'unknown')
                           END AS runtime_state,
                           (SELECT count(*) FROM public.resources r
                            WHERE r.project_id = b.project_id
                              AND r.status = 'active') AS resources,
                           (SELECT count(*) FROM public.plans pl
                            WHERE pl.project_id = b.project_id
                              AND pl.status = 'active') AS plans,
                           (SELECT count(*) FROM public.subscriptions s
                            WHERE s.project_id = b.project_id
                              AND s.status = 'active'
                              AND (s.current_period_end IS NULL
                                   OR s.current_period_end > now())
                           ) AS subscriptions,
                           (SELECT count(*) FROM public.payments pay
                            WHERE pay.project_id = b.project_id
                              AND pay.approved_at >= p_since
                           ) AS payments_period,
                           (
                             SELECT COALESCE(jsonb_agg(
                                 jsonb_build_object(
                                   'currency', amounts.currency_code,
                                   'amount', amounts.total::text
                                 ) ORDER BY amounts.currency_code
                             ), '[]'::jsonb)
                             FROM (
                               SELECT pay.currency_code,
                                      sum(pay.final_amount) AS total
                               FROM public.payments pay
                               WHERE pay.project_id = b.project_id
                                 AND pay.approved_at >= p_since
                               GROUP BY pay.currency_code
                             ) amounts
                           ) AS gmv_by_currency
                    FROM public.bot_instances b
                    JOIN public.projects p ON p.id = b.project_id
                    LEFT JOIN public.customer_bot_runtime_health h
                           ON h.project_id = b.project_id
                          AND h.telegram_bot_id = b.telegram_bot_id
                    ORDER BY b.created_at DESC, b.id DESC
                    LIMIT p_page_size OFFSET p_page * p_page_size
                ) bot_row
            )
        ) INTO result;

        RETURN result;
    END;
    $function$
    """)
    # PostgreSQL grants function execution to PUBLIC by default.
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.platform_report_snapshot(timestamptz, integer, integer)
        FROM PUBLIC
    """)
    # Existing staging roles can be granted at migration time. CI creates its
    # runtime role *after* migrations and grants EXECUTE in the workflow.
    op.execute("""
        DO $grant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime')
            THEN
                GRANT EXECUTE ON FUNCTION
                public.platform_report_snapshot(timestamptz, integer, integer)
                TO membership_runtime;
            END IF;
        END
        $grant$
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION IF EXISTS
        public.platform_report_snapshot(timestamptz, integer, integer)
    """)
    op.execute("DROP POLICY IF EXISTS platform_report_audit_self_read ON platform_report_audit")
    op.execute("DROP POLICY IF EXISTS platform_operator_self_read ON platform_operators")
    op.execute("ALTER TABLE platform_report_audit NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_report_audit DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_operators NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform_operators DISABLE ROW LEVEL SECURITY")
    op.drop_table("platform_report_audit")
    op.drop_table("platform_operators")
