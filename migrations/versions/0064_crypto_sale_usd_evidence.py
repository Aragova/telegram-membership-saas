"""Original Crypto Pay sale-date USD evidence and dormant 5% commission preview.

Revision ID: 0064_crypto_sale_usd_evidence
Revises: 0063_file_stars_authorized

No SaaS invoice, retroactive charges, automatic holds or buyer activation.
The provider's original payment-time USD asset valuation is append-only;
fiat sale-date conversion uses the existing immutable official CBR table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_crypto_sale_usd_evidence"
down_revision: str | None = "0063_file_stars_authorized"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crypto_pay_paid_usd_rates",
        sa.Column("invoice_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("asset_code", sa.String(16), nullable=False),
        sa.Column("usd_per_asset", sa.Numeric(38, 18), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_name", sa.String(40), nullable=False,
                  server_default="crypto_pay_paid_usd_rate"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.ForeignKeyConstraint(
            ["invoice_id", "project_id"],
            ["crypto_pay_invoices.id", "crypto_pay_invoices.project_id"],
            ondelete="RESTRICT", name="fk_crypto_paid_usd_invoice_project",
        ),
        sa.CheckConstraint(
            "usd_per_asset > 0 AND usd_per_asset <= 1000000000",
            name="ck_crypto_paid_usd_price",
        ),
        sa.CheckConstraint(
            "source_name='crypto_pay_paid_usd_rate'",
            name="ck_crypto_paid_usd_source",
        ),
    )
    op.create_table(
        "platform_saas_crypto_capture_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("id=1", name="ck_saas_crypto_capture_singleton"),
    )
    op.execute(
        "INSERT INTO public.platform_saas_crypto_capture_control(id) VALUES(1)"
    )
    # A durable owner-only scan cursor advances past absent quotations and
    # ambiguous attribution instead of letting the same oldest 200 invoices
    # block every newer, fully verified sale forever. Not a financial fact:
    # unlike the immutable evidence tables, this cursor MAY be updated.
    op.create_table(
        "platform_saas_crypto_scan_cursor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_paid_at", sa.DateTime(timezone=True)),
        sa.Column("last_invoice_id", sa.Uuid()),
        # Freeze each scan cycle's upper bound: continuously arriving
        # payments cannot postpone a retry of older unresolved invoices.
        sa.Column("max_paid_at", sa.DateTime(timezone=True)),
        sa.Column("max_invoice_id", sa.Uuid()),
        sa.CheckConstraint("id=1", name="ck_crypto_scan_singleton"),
        sa.CheckConstraint(
            "(last_paid_at IS NULL) = (last_invoice_id IS NULL)"
            " AND (max_paid_at IS NULL) = (max_invoice_id IS NULL)"
            " AND (last_paid_at IS NULL OR max_paid_at IS NOT NULL)",
            name="ck_crypto_scan_complete",
        ),
    )
    op.execute("INSERT INTO public.platform_saas_crypto_scan_cursor(id) VALUES(1)")
    op.create_table(
        "platform_saas_crypto_sales",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invoice_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("provider_invoice_id", sa.BigInteger(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), sa.ForeignKey(
            "merchant_identities.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("bot_instance_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("currency_code", sa.String(16), nullable=False),
        sa.Column("currency_type", sa.String(16), nullable=False),
        sa.Column("original_amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("usd_per_unit", sa.Numeric(38, 18), nullable=False),
        sa.Column("gross_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("commission_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("rate_source", sa.String(40), nullable=False),
        sa.Column("sale_day", sa.Date(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("billing_eligible", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.ForeignKeyConstraint(
            ["invoice_id", "project_id"],
            ["crypto_pay_invoices.id", "crypto_pay_invoices.project_id"],
            ondelete="RESTRICT", name="fk_saas_crypto_invoice_project",
        ),
        sa.ForeignKeyConstraint(
            ["app_id", "project_id"],
            ["crypto_pay_apps.id", "crypto_pay_apps.project_id"],
            ondelete="RESTRICT", name="fk_saas_crypto_app_project",
        ),
        sa.UniqueConstraint(
            "app_id", "provider_invoice_id", name="uq_saas_crypto_original_charge",
        ),
        sa.CheckConstraint(
            "source_kind IN ('file_crypto','subscription_crypto')",
            name="ck_saas_crypto_kind",
        ),
        sa.CheckConstraint(
            "currency_type IN ('fiat','crypto')",
            name="ck_saas_crypto_currency_type",
        ),
        sa.CheckConstraint(
            "rate_source IN ('fiat_usd','cbr_sale_day','crypto_pay_paid_usd_rate')",
            name="ck_saas_crypto_rate_source",
        ),
        sa.CheckConstraint(
            "original_amount>0 AND usd_per_unit>0 AND "
            "gross_usd=round(original_amount*usd_per_unit,18) AND "
            "commission_usd=round(gross_usd*0.05,18)",
            name="ck_saas_crypto_exact_fee",
        ),
    )
    op.create_index(
        "ix_saas_crypto_sales_owner",
        "platform_saas_crypto_sales", ["owner_user_id", "paid_at"],
    )
    op.create_index(
        "ix_saas_crypto_sales_project",
        "platform_saas_crypto_sales", ["project_id", "paid_at"],
    )
    for table in (
        "crypto_pay_paid_usd_rates",
        "platform_saas_crypto_capture_control",
        "platform_saas_crypto_scan_cursor",
        "platform_saas_crypto_sales",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")

    # Never loosen the protected transaction, invoice or subscription tables.
    op.execute("""
        CREATE FUNCTION public.platform_saas_crypto_evidence_immutable()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $f$
        BEGIN
          RAISE EXCEPTION 'Crypto Pay commercial evidence is immutable'
            USING ERRCODE='42501';
        END $f$
    """)
    for table in (
        "crypto_pay_paid_usd_rates",
        "platform_saas_crypto_capture_control",
        "platform_saas_crypto_sales",
    ):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION
                public.platform_saas_crypto_evidence_immutable()
        """)
    # Rate evidence is only inserted in the verified settlement transaction.
    # A different project, unpaid source or mismatched quote is rejected.
    op.execute("""
        CREATE FUNCTION public.platform_saas_crypto_rate_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        BEGIN
          IF NOT EXISTS(
            SELECT 1 FROM public.crypto_pay_invoices i
            WHERE i.id=NEW.invoice_id AND i.project_id=NEW.project_id
              AND i.currency_type='crypto'
              AND i.currency_code=NEW.asset_code
              AND i.status='paid' AND i.paid_at=NEW.paid_at
          ) THEN
            RAISE EXCEPTION 'Original paid crypto invoice mismatch'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $f$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_crypto_rate_origin
        BEFORE INSERT ON public.crypto_pay_paid_usd_rates
        FOR EACH ROW EXECUTE FUNCTION
            public.platform_saas_crypto_rate_guard()
    """)
    op.execute("""
        CREATE POLICY crypto_paid_rate_scope
        ON public.crypto_pay_paid_usd_rates
        FOR ALL USING (
          project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        ) WITH CHECK (
          project_id=NULLIF(current_setting('app.current_project_id',true),'')::uuid
        )
    """)
    op.execute("""
        DO $rev$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            REVOKE ALL ON public.platform_saas_crypto_capture_control,
                public.platform_saas_crypto_scan_cursor,
                public.platform_saas_crypto_sales
                FROM membership_runtime;
            GRANT SELECT,INSERT ON public.crypto_pay_paid_usd_rates
                TO membership_runtime;
          END IF;
        END $rev$
    """)

    # SECURITY DEFINER + checked platform operator; direct sale-table access
    # is not available to tenant/runtime sessions. Each immutable source
    # gets exactly one evidence row, even across rotation or replay.
    op.execute("""
        CREATE FUNCTION public.platform_saas_capture_crypto_sales(
            p_limit integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          original record;
          historical record;
          rate numeric;
          usd_rub numeric;
          asset_rate numeric;
          rate_provenance text;
          sale_date date;
          original_bot uuid;
          bot_count integer;
          found_periods integer;
          trial_started timestamptz;
          is_operator boolean;
          exemption text;
          eligible boolean;
          processed integer:=0;
          inserted integer:=0;
          unresolved integer:=0;
          cutoff timestamptz;
          scan_state record;
          last_seen_at timestamptz;
          last_seen_id uuid;
          max_at timestamptz;
          max_id uuid;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 THEN
            RAISE EXCEPTION 'Invalid Crypto Pay audit batch'
              USING ERRCODE='22023';
          END IF;
          SELECT started_at INTO cutoff
            FROM public.platform_saas_crypto_capture_control WHERE id=1;
          IF cutoff IS NULL THEN
            RAISE EXCEPTION 'Crypto Pay capture cutoff missing'
              USING ERRCODE='42501';
          END IF;
          SELECT last_paid_at,last_invoice_id,max_paid_at,max_invoice_id
            INTO scan_state
          FROM public.platform_saas_crypto_scan_cursor WHERE id=1 FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Crypto Pay audit scan cursor unavailable'
              USING ERRCODE='42501';
          END IF;
          max_at:=scan_state.max_paid_at;
          max_id:=scan_state.max_invoice_id;
          IF max_at IS NULL THEN
            -- Each pass owns a fixed snapshot horizon. New invoices may
            -- appear during or between batches; they belong to NEXT pass.
            SELECT i.paid_at,i.id INTO max_at,max_id
            FROM public.crypto_pay_invoices i
            JOIN public.crypto_pay_apps a
              ON a.id=i.app_id AND a.project_id=i.project_id
            WHERE i.status='paid' AND i.paid_at>=cutoff
              AND a.environment='mainnet'
            ORDER BY i.paid_at DESC,i.id DESC LIMIT 1;
            UPDATE public.platform_saas_crypto_scan_cursor
              SET max_paid_at=max_at,max_invoice_id=max_id WHERE id=1;
          END IF;
          FOR original IN
            SELECT i.id,i.project_id,i.app_id,i.provider_invoice_id,
                   i.telegram_bot_id,i.kind,i.currency_code,
                   i.currency_type,i.amount,i.paid_at
            FROM public.crypto_pay_invoices i
            JOIN public.crypto_pay_apps a ON a.id=i.app_id
                AND a.project_id=i.project_id
            WHERE i.status='paid' AND i.paid_at>=cutoff
              AND a.environment='mainnet'
              AND max_at IS NOT NULL
              AND (i.paid_at,i.id) <= (max_at,max_id)
              AND (scan_state.last_paid_at IS NULL OR
                   (i.paid_at,i.id)>(
                     scan_state.last_paid_at,scan_state.last_invoice_id
                   ))
              AND NOT EXISTS (
                SELECT 1 FROM public.platform_saas_crypto_sales s
                WHERE s.invoice_id=i.id
              )
            ORDER BY i.paid_at,i.id LIMIT p_limit
          LOOP
            processed:=processed+1;
            last_seen_at:=original.paid_at;
            last_seen_id:=original.id;
            sale_date:=(original.paid_at AT TIME ZONE 'Europe/Moscow')::date;
            rate:=NULL;
            rate_provenance:=NULL;
            IF original.currency_type='fiat' THEN
              IF original.currency_code='USD' THEN
                rate:=1; rate_provenance:='fiat_usd';
              ELSE
                SELECT rub_per_unit INTO usd_rub
                  FROM public.platform_saas_daily_fx_rates
                  WHERE rate_date=sale_date AND currency_code='USD'
                    AND source_name='CBR';
                IF original.currency_code='RUB' THEN
                  asset_rate:=1;
                ELSE
                  SELECT rub_per_unit INTO asset_rate
                    FROM public.platform_saas_daily_fx_rates
                   WHERE rate_date=sale_date
                     AND currency_code=original.currency_code
                     AND source_name='CBR';
                END IF;
                IF usd_rub IS NOT NULL AND asset_rate IS NOT NULL
                   AND usd_rub>0 AND asset_rate>0 THEN
                  rate:=asset_rate/usd_rub;
                  rate_provenance:='cbr_sale_day';
                END IF;
              END IF;
            ELSE
              SELECT e.usd_per_asset INTO rate
              FROM public.crypto_pay_paid_usd_rates e
              WHERE e.invoice_id=original.id
                AND e.project_id=original.project_id
                AND e.paid_at=original.paid_at
                AND e.asset_code=original.currency_code;
              rate_provenance:='crypto_pay_paid_usd_rate';
            END IF;
            IF rate IS NULL OR rate<=0 OR rate>1000000000 THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            -- The quote is stored in numeric(38,18): round it FIRST so
            -- both the stored gross and CHECK constraint use precisely
            -- the same immutable exchange-rate snapshot.
            rate:=round(rate,18);
            IF rate<=0 THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT count(*) INTO found_periods
              FROM public.merchant_project_periods mp
              WHERE mp.project_id=original.project_id
                AND mp.started_at<=original.paid_at
                AND (mp.ended_at IS NULL OR mp.ended_at>original.paid_at);
            IF found_periods<>1 THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            SELECT mp.merchant_id,mp.owner_user_id INTO historical
              FROM public.merchant_project_periods mp
              WHERE mp.project_id=original.project_id
                AND mp.started_at<=original.paid_at
                AND (mp.ended_at IS NULL OR mp.ended_at>original.paid_at);
            SELECT count(*),max(e.source_id::text)::uuid
              INTO bot_count,original_bot
              FROM public.platform_usage_events e
              WHERE e.project_id=original.project_id
                AND e.event_type='bot_connected'
                AND e.occurred_at<=original.paid_at
                AND NOT EXISTS(
                  SELECT 1 FROM public.platform_usage_events gone
                  WHERE gone.project_id=original.project_id
                    AND gone.event_type='bot_removed'
                    AND gone.source_id=e.source_id
                    AND gone.occurred_at<=original.paid_at
                );
            IF bot_count<>1 OR NOT EXISTS(
              SELECT 1 FROM public.bot_instances b
              WHERE b.id=original_bot AND b.project_id=original.project_id
                AND b.telegram_bot_id=original.telegram_bot_id
            ) THEN
              unresolved:=unresolved+1; CONTINUE;
            END IF;
            exemption:=public.platform_saas_exemption_state(
              original.project_id,original_bot,
              historical.merchant_id,historical.owner_user_id,original.paid_at
            );
            IF exemption NOT IN ('exempt','not_exempt') THEN
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
            eligible:=NOT is_operator
                AND exemption='not_exempt'
                AND trial_started IS NOT NULL
                AND original.paid_at>=trial_started+interval '72 hours'
                AND EXISTS(
                  SELECT 1 FROM public.platform_saas_billing_control control
                  WHERE control.id=1 AND control.enabled=true
                    AND control.effective_from IS NOT NULL
                    AND original.paid_at>=control.effective_from
                );
            INSERT INTO public.platform_saas_crypto_sales(
                id,invoice_id,project_id,app_id,provider_invoice_id,
                merchant_id,owner_user_id,bot_instance_id,telegram_bot_id,
                source_kind,currency_code,currency_type,original_amount,
                usd_per_unit,gross_usd,commission_usd,rate_source,
                sale_day,paid_at,billing_eligible
            ) VALUES (
                gen_random_uuid(),original.id,original.project_id,
                original.app_id,original.provider_invoice_id,
                historical.merchant_id,historical.owner_user_id,
                original_bot,original.telegram_bot_id,
                original.kind||'_crypto',original.currency_code,
                original.currency_type,original.amount,rate,
                round(original.amount*rate,18),
                round(round(original.amount*rate,18)*0.05,18),
                rate_provenance,sale_date,original.paid_at,eligible
            ) ON CONFLICT (invoice_id) DO NOTHING;
            IF FOUND THEN inserted:=inserted+1; END IF;
          END LOOP;
          -- Every pass completes after its fixed high-water mark, EVEN
          -- while new merchant payments keep arriving. A partial scan
          -- resumes from the last item; after exhausting the frozen
          -- horizon, old missing-FX invoices are eligible for retry.
          IF processed=0
             OR (last_seen_at,last_seen_id)>=(max_at,max_id) THEN
            UPDATE public.platform_saas_crypto_scan_cursor
               SET last_paid_at=NULL,last_invoice_id=NULL,
                   max_paid_at=NULL,max_invoice_id=NULL WHERE id=1;
          ELSE
            UPDATE public.platform_saas_crypto_scan_cursor
               SET last_paid_at=last_seen_at,last_invoice_id=last_seen_id
             WHERE id=1;
          END IF;
          RETURN jsonb_build_object(
              'examined',processed,'recorded',inserted,
              'unresolved',unresolved,
              'billing_activated',EXISTS(
                SELECT 1 FROM public.platform_saas_billing_control
                WHERE id=1 AND enabled=true
              ),
              'invoices_issued',false,
              'source','crypto_pay_getInvoices'
          );
        END $f$
    """)
    # Project owner read-only preview. Missing quotations do NOT count as
    # 0-value sales; unresolved rows are visible, never silently suppressed.
    op.execute("""
        CREATE FUNCTION public.platform_saas_crypto_project_summary(
            p_project uuid,p_month date
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          actor uuid;
          began timestamptz;
          finished timestamptz;
          sold integer;
          pending integer;
          gmv numeric;
          fee numeric;
          billable numeric;
          by_kind jsonb;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF actor IS NULL OR p_project IS NULL
             OR p_month IS NULL OR extract(day from p_month)<>1
             OR NOT EXISTS(
               SELECT 1 FROM public.projects
               WHERE id=p_project AND owner_user_id=actor
             ) THEN
            RAISE EXCEPTION 'Crypto Pay report requires project owner'
              USING ERRCODE='42501';
          END IF;
          began:=p_month::timestamp AT TIME ZONE 'Europe/Moscow';
          finished:=(p_month::timestamp+interval '1 month')
               AT TIME ZONE 'Europe/Moscow';
          SELECT count(*),coalesce(sum(gross_usd),0),
                 coalesce(sum(commission_usd),0),
                 coalesce(sum(commission_usd)
                    FILTER(WHERE billing_eligible),0)
          INTO sold,gmv,fee,billable
          FROM public.platform_saas_crypto_sales
          WHERE project_id=p_project AND owner_user_id=actor
            AND paid_at>=began AND paid_at<finished;
          SELECT coalesce(jsonb_agg(jsonb_build_object(
             'kind',source_kind,'currency',currency_code,
             'sales',sales,'gross_usd',gross::text
          ) ORDER BY source_kind,currency_code),'[]'::jsonb)
          INTO by_kind FROM (
            SELECT source_kind,currency_code,count(*) AS sales,
                   sum(gross_usd) AS gross
            FROM public.platform_saas_crypto_sales
            WHERE project_id=p_project AND owner_user_id=actor
              AND paid_at>=began AND paid_at<finished
            GROUP BY source_kind,currency_code
            ORDER BY source_kind,currency_code LIMIT 30
          ) grouped;
          SELECT count(*) INTO pending
          FROM public.crypto_pay_invoices i
          JOIN public.crypto_pay_apps a ON a.id=i.app_id
          WHERE i.project_id=p_project
            AND i.status='paid' AND a.environment='mainnet'
            AND i.paid_at>=began AND i.paid_at<finished
            AND i.paid_at>=(
                SELECT started_at
                FROM public.platform_saas_crypto_capture_control WHERE id=1
            )
            AND NOT EXISTS(
                SELECT 1 FROM public.platform_saas_crypto_sales s
                WHERE s.invoice_id=i.id
            )
            AND EXISTS (
                SELECT 1 FROM public.merchant_project_periods mp
                WHERE mp.project_id=i.project_id
                  AND mp.owner_user_id=actor
                  AND mp.started_at<=i.paid_at
                  AND (mp.ended_at IS NULL OR mp.ended_at>i.paid_at)
            );
          RETURN jsonb_build_object(
              'project_id',p_project,'month',p_month,'sold',sold,
              'gross_usd',gmv::text,
              'commission_usd',fee::text,
              'billable_commission_usd',billable::text,
              'pending_valuation',pending,
              'rows',by_kind,
              'preview_only',true,
              'invoices_issued',false
          );
        END $f$
    """)
    for signature in (
        "platform_saas_capture_crypto_sales(integer)",
        "platform_saas_crypto_project_summary(uuid,date)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"""
            DO $grants$
            BEGIN
              IF EXISTS(
                SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
              ) THEN
                GRANT EXECUTE ON FUNCTION public.{signature}
                TO membership_runtime;
              END IF;
            END $grants$
        """)


def downgrade() -> None:
    raise RuntimeError(
        "Cannot discard original Crypto Pay paid USD quotations or commission evidence."
    )
