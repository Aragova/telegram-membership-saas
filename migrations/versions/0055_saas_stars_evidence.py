"""Versioned Telegram Stars reward valuation and read-only sales evidence.

Revision ID: 0055_saas_stars_evidence
Revises: 0054_saas_billing_views

This records ONLY new genuine existing paid-file Stars purchases for reporting.
No fee invoices, holds, receipt operations, trial enforcement or activation.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_saas_stars_evidence"
down_revision: str | None = "0054_saas_billing_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saas_star_reward_rates",
        sa.Column("effective_on", sa.Date(), primary_key=True),
        sa.Column("usd_per_star", sa.Numeric(18, 9), nullable=False),
        sa.Column("source_url", sa.String(250), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "usd_per_star > 0 AND usd_per_star < 1",
            name="ck_saas_star_reward_rate",
        ),
        sa.CheckConstraint(
            "length(source_url) BETWEEN 16 AND 250",
            name="ck_saas_star_source",
        ),
    )
    # Cutoff ensures pre-existing purchases are NOT backfilled, even
    # after commercial switches are eventually enabled.
    op.create_table(
        "platform_saas_star_capture_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint("id=1", name="ck_saas_star_capture_singleton"),
    )
    op.create_table(
        "platform_saas_star_sales",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_kind", sa.String(18), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), sa.ForeignKey(
            "merchant_identities.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_charge_id", sa.String(256), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("usd_per_star", sa.Numeric(18, 9), nullable=False),
        sa.Column("reward_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("commission_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("rate_effective_on", sa.Date(), sa.ForeignKey(
            "platform_saas_star_reward_rates.effective_on", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("billing_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.UniqueConstraint(
            "source_kind", "source_id",
            name="uq_saas_star_paid_source",
        ),
        sa.UniqueConstraint(
            "telegram_bot_id", "telegram_charge_id",
            name="uq_saas_star_charge_per_bot",
        ),
        sa.CheckConstraint(
            "source_kind IN ('file_stars','subscription_stars')",
            name="ck_saas_star_source_kind",
        ),
        sa.CheckConstraint(
            "stars_amount>0 AND usd_per_star>0 "
            "AND reward_usd=stars_amount*usd_per_star "
            "AND commission_usd=reward_usd*0.05",
            name="ck_saas_star_exact_commission",
        ),
    )
    op.create_index(
        "ix_file_purchases_paid_stars_capture",
        "file_sale_purchases", ["paid_at", "id"],
        postgresql_where=sa.text(
            "status='paid' AND telegram_charge_id IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_saas_star_sales_owner", "platform_saas_star_sales",
        ["owner_user_id", "paid_at"],
    )
    op.create_index(
        "ix_saas_star_sales_project", "platform_saas_star_sales",
        ["project_id", "paid_at"],
    )
    op.execute(
        "INSERT INTO public.platform_saas_star_reward_rates "
        "(effective_on,usd_per_star,source_url) "
        "VALUES('2026-11-11',0.013,"
        "'https://telegram.org/tos/bot-developers')"
    )
    op.execute(
        "INSERT INTO public.platform_saas_star_capture_control(id) VALUES(1)"
    )
    for table in (
        "platform_saas_star_reward_rates",
        "platform_saas_star_capture_control",
        "platform_saas_star_sales",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $rev$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            REVOKE ALL ON
              public.platform_saas_star_reward_rates,
              public.platform_saas_star_capture_control,
              public.platform_saas_star_sales
            FROM membership_runtime;
          END IF;
        END $rev$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_star_evidence_immutable()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        BEGIN
          RAISE EXCEPTION 'Stars reward evidence is immutable'
            USING ERRCODE='42501';
        END;
        $f$
    """)
    for table in (
        "platform_saas_star_reward_rates",
        "platform_saas_star_sales",
    ):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION
                public.platform_saas_star_evidence_immutable()
        """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_publish_star_reward_rate(
            p_effective date, p_usd numeric, p_url text
        ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE existing record;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_effective IS NULL OR p_usd IS NULL OR p_usd<=0
             OR p_usd>=1 OR p_usd<>round(p_usd,9)
             OR p_url IS NULL OR length(p_url) NOT BETWEEN 16 AND 250
             OR p_url NOT LIKE 'https://telegram.org/%'
             OR p_effective<
                (clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date THEN
            RAISE EXCEPTION 'Invalid official future Telegram reward rate'
              USING ERRCODE='22023';
          END IF;
          SELECT * INTO existing
          FROM public.platform_saas_star_reward_rates
          WHERE effective_on=p_effective;
          IF FOUND THEN
            IF existing.usd_per_star<>p_usd
               OR existing.source_url<>p_url THEN
              RAISE EXCEPTION 'Official Star reward rate date already sealed'
                USING ERRCODE='23505';
            END IF;
            RETURN false;
          END IF;
          INSERT INTO public.platform_saas_star_reward_rates(
              effective_on,usd_per_star,source_url
          ) VALUES(p_effective,p_usd,p_url);
          RETURN true;
        END;
        $f$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_capture_new_file_stars(
            p_limit integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          p record;
          historical record;
          count_periods integer;
          rate_day date;
          rate numeric;
          control record;
          trial_started timestamptz;
          is_operator boolean;
          inserted integer:=0;
          unresolved integer:=0;
          examined integer:=0;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'Invalid Stars capture batch'
              USING ERRCODE='22023';
          END IF;
          SELECT started_at INTO control
          FROM public.platform_saas_star_capture_control WHERE id=1;
          IF control.started_at IS NULL THEN
            RAISE EXCEPTION 'Stars capture cutoff unavailable'
              USING ERRCODE='42501';
          END IF;
          FOR p IN
            SELECT f.id,f.project_id,f.telegram_bot_id,
                   f.telegram_charge_id,f.stars_amount,f.paid_at
            FROM public.file_sale_purchases f
            WHERE f.status='paid' AND f.paid_at>=control.started_at
              AND f.telegram_charge_id IS NOT NULL
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_saas_star_sales e
                WHERE e.source_kind='file_stars' AND e.source_id=f.id
              )
              -- Invalid old owner periods/rates must not indefinitely
              -- occupy the first 200 rows and starve newer paid buyers.
              AND (SELECT count(*)
                   FROM public.merchant_project_periods h
                   WHERE h.project_id=f.project_id
                     AND h.started_at<=f.paid_at
                     AND (h.ended_at IS NULL OR h.ended_at>f.paid_at))=1
              AND EXISTS(
                SELECT 1 FROM public.platform_saas_star_reward_rates q
                WHERE q.effective_on<=
                   (f.paid_at AT TIME ZONE 'Europe/Moscow')::date
              )
            ORDER BY f.paid_at,f.id LIMIT p_limit
          LOOP
            examined:=examined+1;
            SELECT count(*) INTO count_periods
            FROM public.merchant_project_periods h
            WHERE h.project_id=p.project_id
              AND h.started_at<=p.paid_at
              AND (h.ended_at IS NULL OR h.ended_at>p.paid_at);
            IF count_periods<>1 THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT h.merchant_id,h.owner_user_id INTO historical
            FROM public.merchant_project_periods h
            WHERE h.project_id=p.project_id
              AND h.started_at<=p.paid_at
              AND (h.ended_at IS NULL OR h.ended_at>p.paid_at);
            rate_day:=(p.paid_at AT TIME ZONE 'Europe/Moscow')::date;
            SELECT effective_on,usd_per_star INTO rate_day,rate
            FROM public.platform_saas_star_reward_rates
            WHERE effective_on<=(p.paid_at AT TIME ZONE 'Europe/Moscow')::date
            ORDER BY effective_on DESC LIMIT 1;
            IF rate IS NULL THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT EXISTS(
                SELECT 1 FROM public.telegram_users u
                JOIN public.platform_operators o
                  ON o.telegram_user_id=u.telegram_user_id
                WHERE u.id=historical.owner_user_id AND o.role='owner'
            ) INTO is_operator;
            SELECT min(t.claimed_at) INTO trial_started
            FROM public.merchant_platform_trial_claims t
            WHERE t.owner_user_id=historical.owner_user_id
              AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
            INSERT INTO public.platform_saas_star_sales(
                id,source_kind,source_id,project_id,merchant_id,
                owner_user_id,telegram_bot_id,telegram_charge_id,
                stars_amount,usd_per_star,reward_usd,commission_usd,
                rate_effective_on,paid_at,billing_eligible
            ) VALUES (
                gen_random_uuid(),'file_stars',p.id,p.project_id,
                historical.merchant_id,historical.owner_user_id,
                p.telegram_bot_id,p.telegram_charge_id,p.stars_amount,
                rate,p.stars_amount*rate,
                p.stars_amount*rate*0.05,rate_day,p.paid_at,
                NOT is_operator
                AND trial_started IS NOT NULL
                AND p.paid_at>=trial_started+interval '72 hours'
                AND EXISTS(
                   SELECT 1 FROM public.platform_saas_billing_control b
                   WHERE b.id=1 AND b.enabled=true
                     AND b.effective_from IS NOT NULL
                     AND p.paid_at>=b.effective_from
                )
            ) ON CONFLICT (source_kind,source_id) DO NOTHING;
            IF FOUND THEN inserted:=inserted+1; END IF;
          END LOOP;
          -- Expose stalled historical attribution/rate cases separately
          -- without making paid buyer access depend on billing evidence.
          SELECT count(*) INTO unresolved
          FROM public.file_sale_purchases f
          WHERE f.status='paid' AND f.paid_at>=control.started_at
            AND f.telegram_charge_id IS NOT NULL
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_sales e
              WHERE e.source_kind='file_stars' AND e.source_id=f.id
            )
            AND (
              (SELECT count(*) FROM public.merchant_project_periods h
                WHERE h.project_id=f.project_id
                AND h.started_at<=f.paid_at
                AND (h.ended_at IS NULL OR h.ended_at>f.paid_at))<>1
              OR NOT EXISTS (
                SELECT 1 FROM public.platform_saas_star_reward_rates q
                WHERE q.effective_on<=
                  (f.paid_at AT TIME ZONE 'Europe/Moscow')::date
              )
            );
          RETURN jsonb_build_object(
              'examined',examined,'recorded',inserted,
              'unresolved',unresolved,'billing_activated',EXISTS(
                SELECT 1 FROM public.platform_saas_billing_control
                WHERE id=1 AND enabled=true
              )
          );
        END;
        $f$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_star_project_summary(
            p_project uuid, p_month date
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          actor uuid;
          started timestamptz;
          finished timestamptz;
          rows jsonb;
          unpriced integer;
          total_stars bigint;
          reward numeric;
          commission numeric;
          count_sales integer;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF p_project IS NULL OR p_month IS NULL
             OR extract(day from p_month)<>1 OR NOT EXISTS(
                SELECT 1 FROM public.projects p
                WHERE p.id=p_project AND p.owner_user_id=actor
             ) THEN
            RAISE EXCEPTION 'Stars report requires current project owner'
              USING ERRCODE='42501';
          END IF;
          started:=p_month::timestamp AT TIME ZONE 'Europe/Moscow';
          finished:=(p_month::timestamp+interval '1 month')
             AT TIME ZONE 'Europe/Moscow';
          SELECT count(*),coalesce(sum(stars_amount),0),
                 coalesce(sum(reward_usd),0),coalesce(sum(commission_usd),0)
            INTO count_sales,total_stars,reward,commission
          FROM public.platform_saas_star_sales
          WHERE project_id=p_project AND paid_at>=started
            AND paid_at<finished AND owner_user_id=actor;
          SELECT coalesce(jsonb_agg(jsonb_build_object(
               'source',source_kind,'price_stars',stars_amount,
               'sold',sold,'total_stars',stars_amount*sold
             ) ORDER BY source_kind,stars_amount),'[]'::jsonb)
          INTO rows FROM (
            SELECT source_kind,stars_amount,count(*)::integer AS sold
            FROM public.platform_saas_star_sales
            WHERE project_id=p_project AND paid_at>=started
              AND paid_at<finished AND owner_user_id=actor
            GROUP BY source_kind,stars_amount ORDER BY stars_amount LIMIT 30
          ) grouped;
          SELECT count(*) INTO unpriced
          FROM public.file_sale_purchases f
          WHERE f.project_id=p_project AND f.status='paid'
            AND f.paid_at>=started AND f.paid_at<finished
            AND f.paid_at>=(
              SELECT started_at FROM public.platform_saas_star_capture_control
              WHERE id=1
            )
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_sales s
              WHERE s.source_kind='file_stars' AND s.source_id=f.id
            );
          RETURN jsonb_build_object(
            'project_id',p_project,'month',p_month,
            'sold',count_sales,'stars',total_stars,
            'reward_usd',reward::text,
            'commission_usd',commission::text,
            'pending_reconciliation',unpriced,'rows',rows,
            'preview_only',true
          );
        END;
        $f$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_star_evidence_immutable() FROM PUBLIC"
    )
    for signature in (
        "platform_saas_publish_star_reward_rate(date,numeric,text)",
        "platform_saas_capture_new_file_stars(integer)",
        "platform_saas_star_project_summary(uuid,date)",
    ):
        op.execute("REVOKE ALL ON FUNCTION public." + signature + " FROM PUBLIC")
    op.execute("""
        DO $grant$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_publish_star_reward_rate(date,numeric,text),
              public.platform_saas_capture_new_file_stars(integer),
              public.platform_saas_star_project_summary(uuid,date)
              TO membership_runtime;
          END IF;
        END $grant$
    """)


def downgrade() -> None:
    # Financial snapshots are irreversible; never drop genuine paid history.
    raise RuntimeError(
        "Refuse to roll back Telegram Stars financial evidence and rates."
    )
