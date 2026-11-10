"""ViMi Pay bank instructions, owner statements and durable billing notices.

Revision ID: 0047_saas_owner_payment_ui
Revises: 0046_saas_commercial_holds

All new tables are FORCE RLS, regular tenants have no direct access.
Billing, access policy and commercial hold controls remain OFF by default.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_saas_owner_payment_ui"
down_revision: str | None = "0046_saas_commercial_holds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saas_payment_details",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instructions", sa.String(900), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("id=1", name="ck_saas_details_singleton"),
        sa.CheckConstraint(
            "instructions IS NULL OR length(btrim(instructions)) BETWEEN 8 AND 900",
            name="ck_saas_details_length",
        ),
    )
    op.execute(
        "INSERT INTO public.platform_saas_payment_details "
        "(id,instructions,updated_by) VALUES(1,NULL,NULL)"
    )
    op.create_table(
        "platform_saas_notice_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "invoice_id", sa.Uuid(),
            sa.ForeignKey("platform_saas_invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False,
                  server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "invoice_id", "kind", name="uq_saas_notice_invoice_kind",
        ),
        sa.CheckConstraint(
            "kind IN ('issued','due','overdue')",
            name="ck_saas_notice_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','cancelled','failed')",
            name="ck_saas_notice_status",
        ),
        sa.CheckConstraint("attempts>=0", name="ck_saas_notice_attempts"),
    )
    op.create_index(
        "ix_saas_notice_pending",
        "platform_saas_notice_outbox", ["status", "available_at"],
    )
    for table in ("platform_saas_payment_details", "platform_saas_notice_outbox"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $revoke$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            REVOKE ALL ON public.platform_saas_payment_details,
                          public.platform_saas_notice_outbox
              FROM membership_runtime;
          END IF;
        END $revoke$
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_payment_details_read()
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE bank text;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          SELECT instructions INTO bank
            FROM public.platform_saas_payment_details WHERE id=1;
          RETURN jsonb_build_object(
            'configured',bank IS NOT NULL, 'instructions',bank,
            'billing_enabled',(SELECT enabled
              FROM public.platform_saas_billing_control WHERE id=1),
            'trial_enabled',(SELECT enabled
              FROM public.platform_saas_access_policy WHERE id=1),
            'holds_enabled',(SELECT enabled
              FROM public.platform_saas_hold_control WHERE id=1)
          );
        END;
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_payment_details_set(p_text text)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE actor uuid;
        BEGIN
          actor := public.platform_saas_require_owner();
          IF p_text IS NOT NULL AND (
               length(btrim(p_text))<8 OR length(p_text)>900
          ) THEN
            RAISE EXCEPTION 'Invalid platform bank instructions'
              USING ERRCODE='22023';
          END IF;
          UPDATE public.platform_saas_payment_details
            SET instructions=NULLIF(btrim(p_text),''),
                updated_at=clock_timestamp(),updated_by=actor
          WHERE id=1;
          RETURN FOUND;
        END;
        $fn$
    """)
    # Only the real historical liable owner, not a tenant admin/moderator
    # and not the latest project owner, may see an issued statement.
    # Never return statements from another owner or invisible invoice drafts.
    op.execute("""
        CREATE FUNCTION public.platform_saas_owner_invoice_report(
            p_page integer, p_size integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          actor uuid;
          bank text;
          result jsonb;
        BEGIN
          actor:=NULLIF(
            current_setting('app.current_user_id',true),''
          )::uuid;
          IF actor IS NULL OR NOT EXISTS(
            SELECT 1 FROM public.telegram_users u WHERE u.id=actor
          ) THEN
            RAISE EXCEPTION 'Authenticated owner required'
              USING ERRCODE='42501';
          END IF;
          IF p_page IS NULL OR p_page<0 OR p_page>100000
             OR p_size IS NULL OR p_size<1 OR p_size>8 THEN
            RAISE EXCEPTION 'Invalid statement page' USING ERRCODE='22023';
          END IF;
          SELECT instructions INTO bank
            FROM public.platform_saas_payment_details WHERE id=1;
          SELECT jsonb_build_object(
            'total',(SELECT count(*) FROM public.platform_saas_invoices i
                     WHERE i.owner_user_id=actor AND i.status='issued'),
            'unpaid',(SELECT count(*) FROM public.platform_saas_invoices i
                      WHERE i.owner_user_id=actor AND i.status='issued'
                        AND NOT EXISTS(
                          SELECT 1 FROM public.platform_saas_receipts r
                          WHERE r.invoice_id=i.id
                        )),
            'outstanding_rub',(
                SELECT coalesce(sum(i.total_rub),0)::text
                FROM public.platform_saas_invoices i
                WHERE i.owner_user_id=actor AND i.status='issued'
                  AND NOT EXISTS(
                    SELECT 1 FROM public.platform_saas_receipts r
                    WHERE r.invoice_id=i.id
                  )
            ),
            'details_configured',bank IS NOT NULL,
            'instructions',CASE WHEN EXISTS(
                SELECT 1 FROM public.platform_saas_invoices visible
                WHERE visible.owner_user_id=actor AND visible.status='issued'
            ) THEN bank ELSE NULL END,
            'invoices',coalesce((
              SELECT jsonb_agg(jsonb_build_object(
                  'id',f.id::text,
                  'kind',f.kind,
                  'amount_rub',f.total_rub::text,
                  'period_start',f.period_start,
                  'period_end',f.period_end,
                  'due_at',f.due_at,
                  'issued_at',f.issued_at,
                  'status',CASE WHEN receipt.invoice_id IS NOT NULL
                                THEN 'paid' ELSE 'unpaid' END
              ) ORDER BY f.due_at DESC,f.id DESC)
              FROM (
                SELECT i.* FROM public.platform_saas_invoices i
                WHERE i.owner_user_id=actor AND i.status='issued'
                ORDER BY i.due_at DESC,i.id DESC
                LIMIT p_size OFFSET p_page*p_size
              ) f
              LEFT JOIN public.platform_saas_receipts receipt
                ON receipt.invoice_id=f.id
            ),'[]'::jsonb)
          ) INTO result;
          RETURN result;
        END;
        $fn$
    """)

    # Outbox producer runs only in the central operator runtime. Never
    # queues statements if billing is inactive or bank instructions missing.
    # Actual Telegram delivery happens AFTER the transaction commits.
    op.execute("""
        CREATE FUNCTION public.platform_saas_notice_schedule()
        RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          started timestamptz;
          now_at timestamptz := clock_timestamp();
          notice_count integer := 0;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          SELECT effective_from INTO started
          FROM public.platform_saas_billing_control
          WHERE id=1 AND enabled=true;
          IF started IS NULL OR started>now_at
             OR NOT EXISTS(
               SELECT 1 FROM public.platform_saas_payment_details
               WHERE id=1 AND instructions IS NOT NULL
             ) THEN RETURN 0; END IF;
          PERFORM pg_advisory_xact_lock(2200471001);
          INSERT INTO public.platform_saas_notice_outbox(
              id,invoice_id,kind,available_at
          )
          SELECT gen_random_uuid(),i.id,k.kind,now_at
          FROM public.platform_saas_invoices i
          CROSS JOIN LATERAL (
            VALUES
              ('issued'::text,i.issued_at),
              ('due'::text,greatest(i.due_at,i.issued_at)+interval '1 day'),
              ('overdue'::text,greatest(i.due_at,i.issued_at)+interval '7 days')
          ) AS k(kind,notify_at)
          WHERE i.status='issued' AND i.issued_at>=started
            AND k.notify_at<=now_at
            -- If an invoice has gone unattended for weeks, do not blast
            -- issued + due + overdue messages in the same poll cycle.
            -- Enqueue only the latest relevant reminder stage.
            AND (
              (k.kind='issued' AND now_at<
                greatest(i.due_at,i.issued_at)+interval '1 day')
              OR (k.kind='due' AND now_at>=
                greatest(i.due_at,i.issued_at)+interval '1 day'
                AND now_at<greatest(i.due_at,i.issued_at)+interval '7 days')
              OR (k.kind='overdue' AND now_at>=
                greatest(i.due_at,i.issued_at)+interval '7 days')
            )
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_receipts r
              WHERE r.invoice_id=i.id
            )
          ON CONFLICT (invoice_id,kind) DO NOTHING;
          GET DIAGNOSTICS notice_count = ROW_COUNT;
          UPDATE public.platform_saas_notice_outbox n
            SET status='cancelled'
          FROM public.platform_saas_invoices i
          WHERE n.invoice_id=i.id AND n.status='pending'
            AND (
              i.status<>'issued' OR EXISTS(
                SELECT 1 FROM public.platform_saas_receipts r
                WHERE r.invoice_id=i.id
              )
              -- Prevent a paused delivery from replaying an obsolete
              -- invoice stage together with a newer due/overdue stage.
              OR (n.kind='issued' AND now_at >=
                  greatest(i.due_at,i.issued_at)+interval '1 day')
              OR (n.kind='due' AND now_at >=
                  greatest(i.due_at,i.issued_at)+interval '7 days')
            );
          RETURN notice_count;
        END;
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_notice_claim()
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          item record;
          bank text;
          now_at timestamptz := clock_timestamp();
        BEGIN
          PERFORM public.platform_saas_require_owner();
          SELECT instructions INTO bank
          FROM public.platform_saas_payment_details WHERE id=1;
          IF bank IS NULL OR NOT EXISTS(
            SELECT 1 FROM public.platform_saas_billing_control
            WHERE id=1 AND enabled=true AND effective_from<=now_at
          ) THEN RETURN NULL; END IF;
          SELECT n.id,n.invoice_id,n.kind,n.attempts,i.total_rub,
                 i.due_at,i.issued_at,i.status,i.owner_user_id,
                 u.telegram_user_id
          INTO item
          FROM public.platform_saas_notice_outbox n
          JOIN public.platform_saas_invoices i ON i.id=n.invoice_id
          JOIN public.telegram_users u ON u.id=i.owner_user_id
          WHERE n.available_at<=now_at
            AND (n.status='pending' OR
                 (n.status='sending' AND
                  n.claimed_at<now_at-interval '5 minutes'))
          ORDER BY n.available_at,n.id
          FOR UPDATE OF n SKIP LOCKED LIMIT 1;
          IF NOT FOUND THEN RETURN NULL; END IF;
          IF item.status<>'issued' OR EXISTS(
            SELECT 1 FROM public.platform_saas_receipts r
            WHERE r.invoice_id=item.invoice_id
          )
          OR (item.kind='issued' AND now_at>=
              greatest(item.due_at,item.issued_at)+interval '1 day')
          OR (item.kind='due' AND now_at>=
              greatest(item.due_at,item.issued_at)+interval '7 days')
          THEN
            UPDATE public.platform_saas_notice_outbox
              SET status='cancelled' WHERE id=item.id;
            RETURN NULL;
          END IF;
          UPDATE public.platform_saas_notice_outbox
             SET status='sending',attempts=attempts+1,claimed_at=now_at
          WHERE id=item.id;
          RETURN jsonb_build_object(
            'id',item.id::text,'invoice_id',item.invoice_id::text,
            'kind',item.kind,'attempt',item.attempts+1,
            'recipient',item.telegram_user_id,
            'amount_rub',item.total_rub::text,
            'due_at',item.due_at,'instructions',bank
          );
        END;
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_notice_finish(
            p_notice uuid,p_attempt integer,p_delivered boolean
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE item record;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_notice IS NULL OR p_attempt IS NULL OR p_delivered IS NULL
          THEN RAISE EXCEPTION 'Invalid notice state' USING ERRCODE='22023';
          END IF;
          SELECT id,attempts INTO item
          FROM public.platform_saas_notice_outbox
          WHERE id=p_notice AND status='sending' AND attempts=p_attempt
          FOR UPDATE;
          IF NOT FOUND THEN RETURN false; END IF;
          IF p_delivered THEN
            UPDATE public.platform_saas_notice_outbox
              SET status='sent',sent_at=clock_timestamp(),claimed_at=NULL
            WHERE id=p_notice;
          ELSIF item.attempts>=5 THEN
            UPDATE public.platform_saas_notice_outbox
              SET status='failed',claimed_at=NULL WHERE id=p_notice;
          ELSE
            UPDATE public.platform_saas_notice_outbox
              SET status='pending',claimed_at=NULL,
                  available_at=clock_timestamp()
                    + make_interval(secs=LEAST(
                        3600,(30*power(2,LEAST(item.attempts,6)))::integer
                    ))
            WHERE id=p_notice;
          END IF;
          RETURN true;
        END;
        $fn$
    """)

    functions=(
        "platform_saas_payment_details_read()",
        "platform_saas_payment_details_set(text)",
        "platform_saas_owner_invoice_report(integer,integer)",
        "platform_saas_notice_schedule()",
        "platform_saas_notice_claim()",
        "platform_saas_notice_finish(uuid,integer,boolean)",
    )
    for fn in functions:
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn} FROM PUBLIC")
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_payment_details_read(),
              public.platform_saas_payment_details_set(text),
              public.platform_saas_owner_invoice_report(integer,integer),
              public.platform_saas_notice_schedule(),
              public.platform_saas_notice_claim(),
              public.platform_saas_notice_finish(uuid,integer,boolean)
            TO membership_runtime;
          END IF;
        END $grants$
    """)


def downgrade() -> None:
    op.execute("""
        DO $safe$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM public.platform_saas_notice_outbox
          ) THEN
            RAISE EXCEPTION 'Cannot roll back SaaS notification history';
          END IF;
        END $safe$
    """)
    for fn in (
        "platform_saas_notice_finish(uuid,integer,boolean)",
        "platform_saas_notice_claim()",
        "platform_saas_notice_schedule()",
        "platform_saas_owner_invoice_report(integer,integer)",
        "platform_saas_payment_details_set(text)",
        "platform_saas_payment_details_read()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{fn}")
    op.drop_table("platform_saas_notice_outbox")
    op.drop_table("platform_saas_payment_details")
