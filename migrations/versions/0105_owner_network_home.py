"""Owner-only, bounded-roundtrip network summary for the central ViMi Pay bot.

Revision ID: 0105_owner_network_home
Revises: 0104_saas_native_sales

Read-only: does not change invoices, balances, subscriptions, worker leases
or checkout. Amounts are native historical gross receipts, NOT net profit.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0105_owner_network_home"
down_revision: str | None = "0104_saas_native_sales"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
      CREATE FUNCTION public.platform_saas_owner_network_home()
      RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
      SET search_path = pg_catalog, public
      AS $fn$
      DECLARE
        actor uuid;
        now_at timestamptz := clock_timestamp();
        network jsonb;
        sales jsonb;
        invoices jsonb;
      BEGIN
        actor := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
        IF actor IS NULL OR NOT EXISTS(
          SELECT 1 FROM public.telegram_users t WHERE t.id = actor
        ) THEN
          RAISE EXCEPTION 'Authenticated ViMi Pay account required'
            USING ERRCODE = '42501';
        END IF;

        -- A current connected bot is not healthy merely because a token
        -- exists. Check the same heartbeat freshness/instance identity as
        -- bot_health.public_health_label; one set query regardless of fleet size.
        WITH connected AS (
          SELECT b.id, b.project_id, b.telegram_bot_id,
                 h.state, h.last_heartbeat,
                 h.telegram_bot_id AS heartbeat_bot_id
          FROM public.bot_instances b
          JOIN public.projects p ON p.id = b.project_id
          LEFT JOIN public.customer_bot_runtime_health h
            ON h.project_id = b.project_id
          WHERE p.owner_user_id = actor AND p.status = 'active'
            AND b.status = 'registered'
        ), classified AS (
          SELECT *,
            CASE
              WHEN heartbeat_bot_id = telegram_bot_id
                   AND state = 'running'
                   AND last_heartbeat >= now_at - interval '40 seconds'
                THEN 'healthy'
              WHEN heartbeat_bot_id = telegram_bot_id
                   AND state IN ('starting','queued','setup_needed','degraded')
                   AND last_heartbeat >= now_at - interval '40 seconds'
                THEN 'attention'
              ELSE 'down'
            END AS class
          FROM connected
        )
        SELECT jsonb_build_object(
          'connected', count(*),
          'healthy', count(*) FILTER (WHERE class = 'healthy'),
          'attention', count(*) FILTER (WHERE class = 'attention'),
          'down', count(*) FILTER (WHERE class = 'down')
        ) INTO network FROM classified;

        -- All historical transactions for THIS owner, including sales on a
        -- since-transferred project only during this owner's legal tenure.
        -- Original native amounts are grouped, never converted or summed
        -- across currencies. Source payment records avoid capture lag and
        -- count paid Stars/Crypto exactly once, including unvalued sales.
        WITH raw_sales AS (
          SELECT p.currency_code::text AS currency,
                 p.final_amount::numeric AS amount
          FROM public.payments p
          WHERE p.approved_at IS NOT NULL AND EXISTS(
            SELECT 1 FROM public.merchant_project_periods h
            WHERE h.project_id = p.project_id
              AND h.owner_user_id = actor
              AND h.started_at <= p.approved_at
              AND (h.ended_at IS NULL OR h.ended_at > p.approved_at)
          )
          UNION ALL
          SELECT f.currency_code::text, f.amount::numeric
          FROM public.file_sale_manual_orders f
          WHERE f.status = 'approved' AND f.paid_at IS NOT NULL
            AND EXISTS(
              SELECT 1 FROM public.merchant_project_periods h
              WHERE h.project_id = f.project_id
                AND h.owner_user_id = actor
                AND h.started_at <= f.paid_at
                AND (h.ended_at IS NULL OR h.ended_at > f.paid_at)
            )
          UNION ALL
          SELECT 'XTR'::text, f.stars_amount::numeric
          FROM public.file_sale_purchases f
          WHERE f.status = 'paid' AND f.telegram_charge_id IS NOT NULL
            AND EXISTS(
              SELECT 1 FROM public.merchant_project_periods h
              WHERE h.project_id = f.project_id
                AND h.owner_user_id = actor
                AND h.started_at <= f.paid_at
                AND (h.ended_at IS NULL OR h.ended_at > f.paid_at)
            )
          UNION ALL
          SELECT 'XTR'::text, s.stars_amount::numeric
          FROM public.subscription_star_orders s
          WHERE s.status = 'paid' AND s.telegram_charge_id IS NOT NULL
            AND EXISTS(
              SELECT 1 FROM public.merchant_project_periods h
              WHERE h.project_id = s.project_id
                AND h.owner_user_id = actor
                AND h.started_at <= s.paid_at
                AND (h.ended_at IS NULL OR h.ended_at > s.paid_at)
            )
          UNION ALL
          SELECT i.currency_code::text, i.amount::numeric
          FROM public.crypto_pay_invoices i
          JOIN public.crypto_pay_apps a ON a.id = i.app_id
            AND a.project_id = i.project_id
          WHERE i.status = 'paid' AND a.environment = 'mainnet'
            AND EXISTS(
              SELECT 1 FROM public.merchant_project_periods h
              WHERE h.project_id = i.project_id
                AND h.owner_user_id = actor
                AND h.started_at <= i.paid_at
                AND (h.ended_at IS NULL OR h.ended_at > i.paid_at)
            )
        ), groups AS (
          SELECT currency, sum(amount) AS gross, count(*) AS purchases
          FROM raw_sales GROUP BY currency HAVING sum(amount) > 0
        )
        SELECT coalesce(jsonb_agg(jsonb_build_object(
          'currency', currency, 'gross', gross::text,
          'purchases', purchases
        ) ORDER BY currency), '[]'::jsonb)
        INTO sales FROM groups;

        -- The owner remains liable for invoices from deleted or transferred
        -- bots. The nearest actual ISSUED, still-unpaid future payment wins.
        -- A merely expected next-month invoice is not invented as a due date.
        WITH pending AS (
          SELECT i.id, i.bot_instance_id, i.due_at,
                 public.platform_saas_v2_remaining(i.id) AS remaining
          FROM public.platform_saas_invoices i
          WHERE i.owner_user_id = actor AND i.status = 'issued'
        ), due AS (
          SELECT * FROM pending WHERE remaining > 0
        ), upcoming AS (
          SELECT * FROM due WHERE due_at > now_at
        ), nearest AS (
          SELECT * FROM upcoming ORDER BY due_at, id LIMIT 1
        )
        SELECT jsonb_build_object(
          'unpaid', (SELECT count(*) FROM due),
          'overdue', (SELECT count(*) FROM due WHERE due_at <= now_at),
          'overdue_rub', coalesce((
            SELECT sum(remaining) FROM due WHERE due_at <= now_at
          ),0)::text,
          'next', (
            SELECT jsonb_build_object(
              'invoice_id', n.id::text,
              'due_at', n.due_at,
              'remaining_rub', n.remaining::text,
              'username', b.username,
              'same_day_count', (
                SELECT count(*) FROM upcoming other
                WHERE (other.due_at AT TIME ZONE 'Europe/Moscow')::date =
                      (n.due_at AT TIME ZONE 'Europe/Moscow')::date
              )
            )
            FROM nearest n
            LEFT JOIN public.bot_instances b ON b.id = n.bot_instance_id
          )
        ) INTO invoices;

        RETURN jsonb_build_object(
          'snapshot_at', now_at,
          'network', network,
          'revenue', sales,
          'billing', invoices
        );
      END; $fn$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION public.platform_saas_owner_network_home()
      FROM PUBLIC
    """)
    op.execute("""
      DO $grants$
      BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
        THEN
          GRANT EXECUTE ON FUNCTION
            public.platform_saas_owner_network_home()
          TO membership_runtime;
        END IF;
      END $grants$
    """)


    # A next-invoice callback MUST resolve the current page at click time,
    # not assume the nearest due invoice is on descending statement page 0.
    op.execute("""
      CREATE FUNCTION public.platform_saas_owner_invoice_page(p_invoice uuid)
      RETURNS integer LANGUAGE plpgsql STABLE SECURITY DEFINER
      SET search_path = pg_catalog, public
      AS $fn$
      DECLARE
        actor uuid;
        due_at_value timestamptz;
        order_id uuid;
        offset_count bigint;
      BEGIN
        actor := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
        IF actor IS NULL OR p_invoice IS NULL THEN
          RAISE EXCEPTION 'Authenticated invoice owner required'
            USING ERRCODE='42501';
        END IF;
        SELECT i.due_at, i.id INTO due_at_value, order_id
        FROM public.platform_saas_invoices i
        WHERE i.id = p_invoice AND i.owner_user_id = actor
          AND i.status = 'issued';
        IF NOT FOUND THEN
          RAISE EXCEPTION 'Invoice does not belong to account'
            USING ERRCODE='42501';
        END IF;
        SELECT count(*) INTO offset_count
        FROM public.platform_saas_invoices i
        WHERE i.owner_user_id = actor AND i.status = 'issued'
          AND (i.due_at, i.id) > (due_at_value, order_id);
        RETURN floor(offset_count::numeric / 6)::integer;
      END; $fn$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
        public.platform_saas_owner_invoice_page(uuid)
      FROM PUBLIC
    """)
    op.execute("""
      DO $grants$
      BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
        THEN
          GRANT EXECUTE ON FUNCTION
            public.platform_saas_owner_invoice_page(uuid)
          TO membership_runtime;
        END IF;
      END $grants$
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_owner_invoice_page(uuid)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_owner_network_home()"
    )
