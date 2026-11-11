"""Native Stars subscription checkout and shared durable grant identity.

Revision ID: 0057_subscription_stars
Revises: 0056_saas_star_refunds
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_subscription_stars"
down_revision: str | None = "0056_saas_star_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO public.currency_assets(
            code,asset_type,name,symbol,precision,status
        ) VALUES ('XTR','fiat','Telegram Stars','⭐',0,'active')
        ON CONFLICT(code) DO NOTHING
    """)
    op.create_table(
        "subscription_star_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("plan_price_id", sa.Uuid(), nullable=False),
        sa.Column("currency_code", sa.String(16), nullable=False,
                  server_default="XTR"),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("invoice_payload", sa.String(128), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("plan_name_snapshot", sa.String(120), nullable=False),
        sa.Column("duration_value", sa.Integer(), nullable=True),
        sa.Column("duration_unit", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="pending"),
        sa.Column("telegram_charge_id", sa.String(256), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("precheckout_approved_at",
                  sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id","project_id"],
            ["project_customers.id","project_customers.project_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id","project_id"], ["plans.id","plans.project_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_price_id","project_id","plan_id","currency_code"],
            ["plan_prices.id","plan_prices.project_id","plan_prices.plan_id",
             "plan_prices.currency_code"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id","project_id",
                            name="uq_subscription_star_order_project"),
        sa.UniqueConstraint("invoice_payload",
                            name="uq_subscription_star_payload"),
        sa.UniqueConstraint("telegram_bot_id","telegram_charge_id",
                            name="uq_subscription_star_bot_charge"),
        sa.CheckConstraint("currency_code='XTR'",
                           name="ck_subscription_star_currency"),
        sa.CheckConstraint("stars_amount BETWEEN 1 AND 250000",
                           name="ck_subscription_star_amount"),
        sa.CheckConstraint(
            "(status='pending' AND telegram_charge_id IS NULL AND paid_at IS NULL)"
            " OR (status='paid' AND telegram_charge_id IS NOT NULL "
            "AND paid_at IS NOT NULL)",
            name="ck_subscription_star_state",
        ),
        sa.CheckConstraint(
            "(duration_unit='lifetime' AND duration_value IS NULL) OR "
            "(duration_unit IN ('day','week','month','year') "
            "AND duration_value IS NOT NULL AND duration_value>0)",
            name="ck_subscription_star_duration",
        ),
    )
    op.create_index(
        "ix_subscription_star_order_customer",
        "subscription_star_orders", ["project_id","customer_id","created_at"],
    )
    op.create_index(
        "ix_subscription_star_order_paid", "subscription_star_orders",
        ["paid_at", "id"],
        postgresql_where=sa.text("status='paid'"),
    )
    op.execute("""
        ALTER TABLE public.subscription_star_orders ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.subscription_star_orders FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE POLICY subscription_star_orders_scope
        ON public.subscription_star_orders
        FOR ALL USING(
          project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        ) WITH CHECK(
          project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        )
    """)
    op.alter_column("subscriptions","last_payment_id",
                    existing_type=sa.Uuid(),nullable=True)
    op.add_column(
        "subscriptions",
        sa.Column("last_star_order_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscriptions_last_star_order_project",
        "subscriptions","subscription_star_orders",
        ["last_star_order_id","project_id"],["id","project_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_subscriptions_last_paid_source","subscriptions",
        "(last_payment_id IS NOT NULL) OR (last_star_order_id IS NOT NULL)",
    )
    op.drop_constraint("ck_entitlement_grants_source",
                       "entitlement_grants",type_="check")
    op.create_check_constraint(
        "ck_entitlement_grants_source","entitlement_grants",
        "source IN ('payment','stars_payment','manual','owner_service',"
        "'promocode','trial','referral','import','compensation')",
    )
    op.create_table(
        "subscription_star_refunds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("invoice_payload", sa.String(128), nullable=False),
        sa.Column("telegram_charge_id", sa.String(256), nullable=False),
        sa.Column("stars_amount", sa.Integer(), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.ForeignKeyConstraint(
            ["order_id", "project_id"],
            ["subscription_star_orders.id", "subscription_star_orders.project_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("order_id", name="uq_subscription_star_refund_order"),
        sa.UniqueConstraint(
            "telegram_bot_id", "telegram_charge_id",
            name="uq_subscription_star_refund_charge",
        ),
        sa.CheckConstraint("stars_amount BETWEEN 1 AND 250000",
                           name="ck_subscription_star_refund_amount"),
    )
    op.execute("""
        ALTER TABLE public.subscription_star_refunds ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.subscription_star_refunds FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE POLICY subscription_star_refunds_scope
        ON public.subscription_star_refunds FOR ALL
        USING (
            project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        ) WITH CHECK (
            project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        )
    """)
    op.execute("""
        CREATE FUNCTION public.guard_subscription_star_refund()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $f$
        DECLARE o record;
        BEGIN
          IF TG_OP IN ('UPDATE','DELETE') THEN
            RAISE EXCEPTION 'Stars refund evidence is immutable'
              USING ERRCODE='42501';
          END IF;
          SELECT * INTO o FROM public.subscription_star_orders
            WHERE id=NEW.order_id AND project_id=NEW.project_id FOR UPDATE;
          IF o.id IS NULL OR o.status<>'paid'
             OR o.telegram_bot_id<>NEW.telegram_bot_id
             OR o.telegram_user_id<>NEW.telegram_user_id
             OR o.invoice_payload<>NEW.invoice_payload
             OR o.telegram_charge_id IS DISTINCT FROM NEW.telegram_charge_id
             OR o.stars_amount<>NEW.stars_amount
             OR o.paid_at IS NULL OR NEW.refunded_at<o.paid_at
          THEN
            RAISE EXCEPTION 'Refund does not match paid Stars order'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
        $f$
    """)
    op.execute("""
        CREATE TRIGGER trg_subscription_star_refund_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.subscription_star_refunds
        FOR EACH ROW EXECUTE FUNCTION public.guard_subscription_star_refund()
    """)
    op.create_table(
        "platform_saas_star_subscription_credits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("original_sale_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_star_sales.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("refund_id", sa.Uuid(), sa.ForeignKey(
            "subscription_star_refunds.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("reward_credit_usd", sa.Numeric(38,18), nullable=False),
        sa.Column("commission_credit_usd", sa.Numeric(38,18), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "original_sale_id", name="uq_saas_star_sub_credit_source",
        ),
        sa.UniqueConstraint("refund_id", name="uq_saas_star_sub_credit_refund"),
        sa.CheckConstraint(
            "reward_credit_usd>0 "
            "AND commission_credit_usd=reward_credit_usd*0.05",
            name="ck_saas_star_sub_credit_math",
        ),
    )
    op.execute("""
        ALTER TABLE public.platform_saas_star_subscription_credits
        ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
        ALTER TABLE public.platform_saas_star_subscription_credits
        FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_star_subscription_credit_immutable
        BEFORE UPDATE OR DELETE ON public.platform_saas_star_subscription_credits
        FOR EACH ROW EXECUTE FUNCTION
        public.platform_saas_star_evidence_immutable()
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_capture_subscription_star_refunds(
          p_limit integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE r record; recorded integer:=0; waiting integer:=0;
                cutoff timestamptz;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'Invalid Stars refund limit' USING ERRCODE='22023';
          END IF;
          SELECT started_at INTO cutoff
          FROM public.platform_saas_star_capture_control WHERE id=1;
          FOR r IN
            SELECT f.*,s.id AS sale_id,s.owner_user_id,s.project_id AS sale_project,
                   s.reward_usd,s.commission_usd
            FROM public.subscription_star_refunds f
            JOIN public.subscription_star_orders o ON o.id=f.order_id
            JOIN public.platform_saas_star_sales s
              ON s.source_kind='subscription_stars' AND s.source_id=o.id
              AND s.telegram_bot_id=f.telegram_bot_id
              AND s.telegram_charge_id=f.telegram_charge_id
              AND s.stars_amount=f.stars_amount
            WHERE o.paid_at>=cutoff
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_saas_star_subscription_credits c
                WHERE c.refund_id=f.id
              )
            ORDER BY f.refunded_at,f.id LIMIT p_limit
          LOOP
            INSERT INTO public.platform_saas_star_subscription_credits(
                id,original_sale_id,refund_id,project_id,owner_user_id,
                reward_credit_usd,commission_credit_usd,refunded_at
            ) VALUES(
                gen_random_uuid(),r.sale_id,r.id,r.sale_project,r.owner_user_id,
                r.reward_usd,r.commission_usd,r.refunded_at
            ) ON CONFLICT(refund_id) DO NOTHING;
            IF FOUND THEN recorded:=recorded+1; END IF;
          END LOOP;
          SELECT count(*) INTO waiting
          FROM public.subscription_star_refunds f
          JOIN public.subscription_star_orders o ON o.id=f.order_id
          WHERE o.paid_at>=cutoff
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_subscription_credits c
              WHERE c.refund_id=f.id
            );
          RETURN jsonb_build_object(
            'recorded',recorded,'waiting_for_original_sale',waiting,
            'applied_to_invoices',false
          );
        END;
        $f$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.platform_saas_capture_subscription_star_refunds(integer)
        FROM PUBLIC
    """)
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT SELECT,INSERT ON public.subscription_star_refunds
              TO membership_runtime;
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_capture_subscription_star_refunds(integer)
              TO membership_runtime;
            REVOKE ALL ON public.platform_saas_star_subscription_credits
              FROM membership_runtime;
          END IF;
        END $grants$
    """)
    op.execute("""
        DO $grant$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT SELECT,INSERT,UPDATE ON public.subscription_star_orders
            TO membership_runtime;
          END IF;
        END $grant$
    """)
    # Reuse the existing 0046 hold/exemption rules for new Stars orders,
    # without creating a bypass for overdue tenants or harming existing sales.
    op.execute("""
        CREATE FUNCTION public.platform_saas_guard_new_star_subscription()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $guard$
        DECLARE liable_owner uuid; merchant uuid; bot_uuid uuid;
                gate text; actor_project uuid;
        BEGIN
          actor_project:=NULLIF(
            current_setting('app.current_project_id',true),''
          )::uuid;
          IF actor_project IS DISTINCT FROM NEW.project_id THEN RETURN NEW; END IF;
          SELECT p.owner_user_id,p.merchant_id INTO liable_owner,merchant
          FROM public.projects p WHERE p.id=NEW.project_id;
          IF liable_owner IS NULL THEN RETURN NEW; END IF;
          gate:=public.platform_saas_owner_hold_state(
            liable_owner,clock_timestamp()
          );
          IF gate<>'held' THEN RETURN NEW; END IF;
          SELECT b.id INTO bot_uuid FROM public.bot_instances b
          WHERE b.project_id=NEW.project_id
            AND b.telegram_bot_id=NEW.telegram_bot_id
            AND b.status='registered';
          IF bot_uuid IS NOT NULL AND
             public.platform_saas_exemption_state(
               NEW.project_id,bot_uuid,merchant,liable_owner,clock_timestamp()
             )='exempt' THEN
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'New ViMi Pay subscription checkout is on hold'
            USING ERRCODE='P6711';
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_hold_new_star_subscription
        BEFORE INSERT ON public.subscription_star_orders
        FOR EACH ROW EXECUTE FUNCTION
            public.platform_saas_guard_new_star_subscription()
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_capture_new_subscription_stars(
          p_limit integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE
          p record;
          history record;
          rate_day date;
          rate numeric;
          trial_start timestamptz;
          started timestamptz;
          exempt boolean;
          recorded integer:=0;
          unresolved integer:=0;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'Invalid subscription Stars capture batch'
              USING ERRCODE='22023';
          END IF;
          SELECT c.started_at INTO started
          FROM public.platform_saas_star_capture_control c WHERE c.id=1;
          IF started IS NULL THEN
            RAISE EXCEPTION 'Missing Stars capture cutoff'
              USING ERRCODE='42501';
          END IF;
          FOR p IN SELECT o.* FROM public.subscription_star_orders o
            WHERE o.status='paid' AND o.paid_at>=started
            AND o.telegram_charge_id IS NOT NULL
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_sales e
              WHERE e.source_kind='subscription_stars'
                AND e.source_id=o.id
            )
            AND (SELECT count(*) FROM public.merchant_project_periods h
                 WHERE h.project_id=o.project_id AND h.started_at<=o.paid_at
                 AND (h.ended_at IS NULL OR h.ended_at>o.paid_at))=1
            AND EXISTS(
              SELECT 1 FROM public.platform_saas_star_reward_rates r
              WHERE r.effective_on<=
                (o.paid_at AT TIME ZONE 'Europe/Moscow')::date
            )
            ORDER BY o.paid_at,o.id LIMIT p_limit
          LOOP
            SELECT merchant_id,owner_user_id INTO history
            FROM public.merchant_project_periods h
            WHERE h.project_id=p.project_id AND h.started_at<=p.paid_at
              AND (h.ended_at IS NULL OR h.ended_at>p.paid_at);
            SELECT effective_on,usd_per_star INTO rate_day,rate
            FROM public.platform_saas_star_reward_rates r
            WHERE r.effective_on<=
              (p.paid_at AT TIME ZONE 'Europe/Moscow')::date
            ORDER BY effective_on DESC LIMIT 1;
            SELECT EXISTS(
              SELECT 1 FROM public.telegram_users u
              JOIN public.platform_operators po
                ON po.telegram_user_id=u.telegram_user_id
              WHERE u.id=history.owner_user_id AND po.role='owner'
            ) INTO exempt;
            SELECT min(t.claimed_at) INTO trial_start
            FROM public.merchant_platform_trial_claims t
            WHERE t.owner_user_id=history.owner_user_id
              AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
            INSERT INTO public.platform_saas_star_sales(
                id,source_kind,source_id,project_id,merchant_id,
                owner_user_id,telegram_bot_id,telegram_charge_id,
                stars_amount,usd_per_star,reward_usd,commission_usd,
                rate_effective_on,paid_at,billing_eligible
            ) VALUES(
                gen_random_uuid(),'subscription_stars',p.id,p.project_id,
                history.merchant_id,history.owner_user_id,p.telegram_bot_id,
                p.telegram_charge_id,p.stars_amount,rate,
                p.stars_amount*rate,p.stars_amount*rate*0.05,
                rate_day,p.paid_at,
                NOT exempt AND trial_start IS NOT NULL
                AND p.paid_at>=trial_start+interval '72 hours'
                AND EXISTS(
                  SELECT 1 FROM public.platform_saas_billing_control b
                  WHERE b.id=1 AND b.enabled=true
                  AND b.effective_from IS NOT NULL
                  AND p.paid_at>=b.effective_from
                )
            ) ON CONFLICT (source_kind,source_id) DO NOTHING;
            IF FOUND THEN recorded:=recorded+1; END IF;
          END LOOP;
          SELECT count(*) INTO unresolved
          FROM public.subscription_star_orders o
          WHERE o.status='paid' AND o.paid_at>=started
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_sales s
              WHERE s.source_kind='subscription_stars' AND s.source_id=o.id
            );
          RETURN jsonb_build_object(
            'recorded',recorded,'unresolved',unresolved,
            'fee_issued',false
          );
        END;
        $fn$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
          public.platform_saas_capture_new_subscription_stars(integer)
          FROM PUBLIC
    """)
    op.execute("""
        DO $grant$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_capture_new_subscription_stars(integer)
            TO membership_runtime;
          END IF;
        END $grant$
    """)



    # Replace only the merchant read model: keep migration 0056 immutable.
    # Both kinds of Stars refunds are summed by the Moscow refund month and
    # *historical liable owner*, never the current owner of a transferred bot.
    op.execute("""
        CREATE OR REPLACE FUNCTION
        public.platform_saas_star_project_refund_summary(
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
          sub_count integer;
          file_count integer;
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
          FROM (
            SELECT f.stars_amount,f.reward_credit_usd,
                   f.commission_credit_usd
            FROM public.platform_saas_star_refund_credits f
            WHERE f.project_id=p_project AND f.owner_user_id=actor
              AND f.refunded_at>=period_start AND f.refunded_at<period_end
            UNION ALL
            SELECT o.stars_amount,s.reward_credit_usd,
                   s.commission_credit_usd
            FROM public.platform_saas_star_subscription_credits s
            JOIN public.subscription_star_refunds f ON f.id=s.refund_id
            JOIN public.subscription_star_orders o ON o.id=f.order_id
            WHERE s.project_id=p_project AND s.owner_user_id=actor
              AND s.refunded_at>=period_start AND s.refunded_at<period_end
          ) c;
          SELECT count(*) INTO file_count
          FROM public.file_sale_star_refunds f
          JOIN public.file_sale_purchases p ON p.id=f.purchase_id
          WHERE f.project_id=p_project
            AND f.refunded_at>=period_start AND f.refunded_at<period_end
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
          SELECT count(*) INTO sub_count
          FROM public.subscription_star_refunds f
          JOIN public.subscription_star_orders o ON o.id=f.order_id
          WHERE f.project_id=p_project
            AND f.refunded_at>=period_start AND f.refunded_at<period_end
            AND o.paid_at>=(
              SELECT started_at FROM public.platform_saas_star_capture_control
              WHERE id=1
            )
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_star_subscription_credits c
              WHERE c.refund_id=f.id
            )
            AND EXISTS(
              SELECT 1 FROM public.merchant_project_periods h
              WHERE h.project_id=f.project_id
                AND h.owner_user_id=actor
                AND h.started_at<=o.paid_at
                AND (h.ended_at IS NULL OR h.ended_at>o.paid_at)
            );
          pending:=file_count+sub_count;
          RETURN jsonb_build_object(
              'refunds',count_refunds,
              'refunded_stars',stars_refunded,
              'reward_credit_usd',reward::text,
              'commission_credit_usd',commission::text,
              'pending_refund_reconciliation',pending,
              'pending_subscription_refunds',sub_count,
              'pending_file_refunds',file_count,
              'applied_to_invoices',false
          );
        END;
        $f$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
        public.platform_saas_star_project_refund_summary(uuid,date)
        FROM PUBLIC
    """)
    op.execute("""
        DO $refund_summary$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT EXECUTE ON FUNCTION
            public.platform_saas_star_project_refund_summary(uuid,date)
            TO membership_runtime;
          END IF;
        END $refund_summary$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Refuse to remove immutable Stars subscription sales and paid grants."
    )
