"""Immutable Telegram Stars refund evidence linked to file sales and SaaS credits.

Revision ID: 0056_saas_star_refunds
Revises: 0055_saas_stars_evidence

Telegram refunds create separate audit events and negative SaaS commission
adjustments, never erase a buyer's paid purchase or retroactively rebill.
Subscription Stars charging remains a separate feature until safe entitlement
issuance is implemented; never accept funds without granting purchased access.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_saas_star_refunds"
down_revision: str | None = "0055_saas_stars_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_sale_star_refunds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey(
            "projects.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("purchase_id", sa.Uuid(), sa.ForeignKey(
            "file_sale_purchases.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_charge_id", sa.String(256), nullable=False),
        sa.Column("invoice_payload", sa.String(128), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("clock_timestamp()"),
        ),
        sa.UniqueConstraint(
            "purchase_id", name="uq_file_star_refund_purchase",
        ),
        sa.UniqueConstraint(
            "telegram_bot_id", "telegram_charge_id",
            name="uq_file_star_refund_bot_charge",
        ),
        sa.CheckConstraint(
            "stars_amount BETWEEN 1 AND 250000",
            name="ck_file_star_refund_amount",
        ),
        sa.CheckConstraint(
            "length(telegram_charge_id)>0",
            name="ck_file_star_refund_charge_id",
        ),
    )
    op.create_index(
        "ix_file_star_refund_paid_at",
        "file_sale_star_refunds", ["refunded_at", "id"],
    )
    op.execute(
        "ALTER TABLE public.file_sale_star_refunds ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.file_sale_star_refunds FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
        CREATE POLICY file_sale_star_refunds_scope
        ON public.file_sale_star_refunds
        FOR ALL USING (
            project_id = NULLIF(
                current_setting('app.current_project_id',true),''
            )::uuid
        )
        WITH CHECK (
            project_id = NULLIF(
                current_setting('app.current_project_id',true),''
            )::uuid
        )
    """)
    # Live staging already has membership_runtime before Alembic runs.
    # The connected bot only needs append/read access; financial rollup
    # tables remain completely inaccessible outside owner-only RPCs.
    op.execute("""
        DO $tenant_grant$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles
                    WHERE rolname='membership_runtime') THEN
            GRANT SELECT,INSERT ON public.file_sale_star_refunds
              TO membership_runtime;
          END IF;
        END $tenant_grant$
    """)
    # Verify every inserted Telegram service event against the immutable
    # original paid order, not just application-supplied model fields.
    op.execute("""
        CREATE FUNCTION public.guard_file_star_refund()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $f$
        DECLARE purchase record;
        BEGIN
          IF TG_OP IN ('UPDATE','DELETE') THEN
            RAISE EXCEPTION 'Stars refund audit entries are immutable'
              USING ERRCODE='42501';
          END IF;
          SELECT * INTO purchase FROM public.file_sale_purchases
          WHERE id=NEW.purchase_id FOR UPDATE;
          IF purchase.id IS NULL OR purchase.status<>'paid'
             OR purchase.project_id<>NEW.project_id
             OR purchase.telegram_bot_id<>NEW.telegram_bot_id
             OR purchase.telegram_user_id<>NEW.telegram_user_id
             OR purchase.telegram_charge_id IS DISTINCT FROM NEW.telegram_charge_id
             OR purchase.invoice_payload<>NEW.invoice_payload
             OR purchase.stars_amount<>NEW.stars_amount
             OR purchase.paid_at IS NULL
             OR NEW.refunded_at<purchase.paid_at
          THEN
            RAISE EXCEPTION 'Refund does not match verified Stars payment'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
        $f$
    """)
    op.execute("""
        CREATE TRIGGER trg_file_star_refund_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.file_sale_star_refunds
        FOR EACH ROW EXECUTE FUNCTION public.guard_file_star_refund()
    """)
    op.create_table(
        "platform_saas_star_refund_credits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("original_sale_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_star_sales.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("file_refund_id", sa.Uuid(), sa.ForeignKey(
            "file_sale_star_refunds.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("reward_credit_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("commission_credit_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("clock_timestamp()"),
        ),
        sa.UniqueConstraint(
            "original_sale_id", name="uq_saas_star_credit_sale",
        ),
        sa.UniqueConstraint(
            "file_refund_id", name="uq_saas_star_credit_source",
        ),
        sa.CheckConstraint(
            "stars_amount>0 AND reward_credit_usd>0 "
            "AND commission_credit_usd=reward_credit_usd*0.05",
            name="ck_saas_star_refund_credit_exact",
        ),
    )
    op.create_index(
        "ix_saas_star_refund_credit_owner",
        "platform_saas_star_refund_credits",
        ["owner_user_id", "refunded_at"],
    )
    op.execute(
        "ALTER TABLE public.platform_saas_star_refund_credits ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_star_refund_credits FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
        DO $role$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            REVOKE ALL ON public.platform_saas_star_refund_credits
              FROM membership_runtime;
          END IF;
        END $role$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_star_credit_immutable
        BEFORE UPDATE OR DELETE ON public.platform_saas_star_refund_credits
        FOR EACH ROW EXECUTE FUNCTION
          public.platform_saas_star_evidence_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_capture_star_refunds(
            p_limit integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          r record;
          original record;
          inserted integer:=0;
          waiting integer:=0;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'Invalid Stars refund batch'
              USING ERRCODE='22023';
          END IF;
          FOR r IN
            SELECT f.* FROM public.file_sale_star_refunds f
            JOIN public.platform_saas_star_sales s
              ON s.source_kind='file_stars' AND s.source_id=f.purchase_id
              AND s.telegram_bot_id=f.telegram_bot_id
              AND s.telegram_charge_id=f.telegram_charge_id
              AND s.stars_amount=f.stars_amount
            WHERE NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_refund_credits c
              WHERE c.file_refund_id=f.id
            )
            ORDER BY f.refunded_at,f.id LIMIT p_limit
          LOOP
            SELECT * INTO original
            FROM public.platform_saas_star_sales s
            WHERE s.source_kind='file_stars'
              AND s.source_id=r.purchase_id
              AND s.telegram_bot_id=r.telegram_bot_id
              AND s.telegram_charge_id=r.telegram_charge_id
              AND s.stars_amount=r.stars_amount;
            IF original.id IS NULL THEN CONTINUE; END IF;
            INSERT INTO public.platform_saas_star_refund_credits(
                id,original_sale_id,file_refund_id,project_id,
                owner_user_id,stars_amount,reward_credit_usd,
                commission_credit_usd,refunded_at
            ) VALUES(
                gen_random_uuid(),original.id,r.id,original.project_id,
                original.owner_user_id,r.stars_amount,original.reward_usd,
                original.commission_usd,r.refunded_at
            ) ON CONFLICT (file_refund_id) DO NOTHING;
            IF FOUND THEN inserted:=inserted+1; END IF;
          END LOOP;
          SELECT count(*) INTO waiting
          FROM public.file_sale_star_refunds f
          JOIN public.file_sale_purchases p ON p.id=f.purchase_id
          WHERE p.paid_at>=(
              SELECT started_at
              FROM public.platform_saas_star_capture_control WHERE id=1
            )
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_refund_credits c
              WHERE c.file_refund_id=f.id
            );
          RETURN jsonb_build_object(
            'recorded',inserted,
            'waiting_for_original_sale',waiting,
            'applied_to_invoices',false
          );
        END;
        $f$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_star_project_refund_summary(
            p_project uuid, p_month date
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          actor uuid;
          period_start timestamptz;
          period_end timestamptz;
          count_refunds integer;
          stars_refunded bigint;
          reward numeric;
          commission numeric;
          pending integer;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF p_project IS NULL OR p_month IS NULL
             OR extract(day from p_month)<>1 OR actor IS NULL
             OR NOT EXISTS(
               SELECT 1 FROM public.projects
               WHERE id=p_project AND owner_user_id=actor
             ) THEN
            RAISE EXCEPTION 'Stars refund report requires project owner'
              USING ERRCODE='42501';
          END IF;
          period_start:=p_month::timestamp AT TIME ZONE 'Europe/Moscow';
          period_end:=(p_month::timestamp+interval '1 month')
              AT TIME ZONE 'Europe/Moscow';
          SELECT count(*),coalesce(sum(c.stars_amount),0),
                 coalesce(sum(c.reward_credit_usd),0),
                 coalesce(sum(c.commission_credit_usd),0)
          INTO count_refunds,stars_refunded,reward,commission
          FROM public.platform_saas_star_refund_credits c
          WHERE c.project_id=p_project AND c.owner_user_id=actor
            AND c.refunded_at>=period_start AND c.refunded_at<period_end;
          SELECT count(*) INTO pending
          FROM public.file_sale_star_refunds f
          JOIN public.file_sale_purchases p ON p.id=f.purchase_id
          WHERE f.project_id=p_project
            AND f.refunded_at>=period_start AND f.refunded_at<period_end
            -- Pre-0055 paid purchases are deliberately non-billable.
            -- Their verified refund evidence remains immutable but cannot
            -- wait for a positive SaaS sale that must never be backfilled.
            AND p.paid_at>=(
              SELECT started_at FROM public.platform_saas_star_capture_control
              WHERE id=1
            )
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_refund_credits c
              WHERE c.file_refund_id=f.id
            )
            AND EXISTS(
              SELECT 1 FROM public.merchant_project_periods h
              WHERE h.project_id=f.project_id
                AND h.owner_user_id=actor
                AND h.started_at<=p.paid_at
                AND (h.ended_at IS NULL OR h.ended_at>p.paid_at)
            );
          RETURN jsonb_build_object(
              'refunds',count_refunds,
              'refunded_stars',stars_refunded,
              'reward_credit_usd',reward::text,
              'commission_credit_usd',commission::text,
              'pending_refund_reconciliation',pending,
              'applied_to_invoices',false
          );
        END;
        $f$
    """)
    for signature in (
        "platform_saas_capture_star_refunds(integer)",
        "platform_saas_star_project_refund_summary(uuid,date)",
    ):
        op.execute("REVOKE ALL ON FUNCTION public." + signature + " FROM PUBLIC")
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT EXECUTE ON FUNCTION
                public.platform_saas_capture_star_refunds(integer),
                public.platform_saas_star_project_refund_summary(uuid,date)
            TO membership_runtime;
          END IF;
        END $grants$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Refuse rollback of immutable Telegram payment refund evidence."
    )
