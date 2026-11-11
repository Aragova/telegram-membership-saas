"""Immutable settlement quotes, operator-confirmed FX receipts and refund credits.

Revision ID: 0053_saas_settlement_credits
Revises: 0052_sale_day_fx

Everything is dormant unless a real issued platform invoice already exists.
No billing, trial, quotas, notification, or hold switch is enabled.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_saas_settlement_credits"
down_revision: str | None = "0052_sale_day_fx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The invoice itself remains an immutable RUB ledger. A merchant's
    # payment-currency choice is sealed exactly once in its own immutable row.
    op.create_table(
        "platform_saas_currency_settlements",
        sa.Column("invoice_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_invoices.id", ondelete="RESTRICT"
        ), primary_key=True),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("quote_date", sa.Date(), nullable=False),
        sa.Column("source_date", sa.Date(), nullable=True),
        sa.Column("source_name", sa.String(12), nullable=False),
        sa.Column("rub_per_unit", sa.Numeric(38, 18), nullable=False),
        sa.Column("total_rub", sa.Numeric(38, 2), nullable=False),
        sa.Column("amount_currency", sa.Numeric(38, 2), nullable=False),
        sa.Column("chosen_by", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("chosen_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "currency_code IN ('RUB','USD','EUR','UAH','BYN')",
            name="ck_saas_settlement_currency",
        ),
        sa.CheckConstraint(
            "rub_per_unit>0 AND rub_per_unit<=1000000000 "
            "AND total_rub>0 AND amount_currency>0 "
            "AND amount_currency=round(total_rub/rub_per_unit,2)",
            name="ck_saas_settlement_amount",
        ),
        sa.CheckConstraint(
            "(currency_code='RUB' AND source_name='RUB' "
            "AND rub_per_unit=1 AND source_date IS NULL) OR "
            "(currency_code<>'RUB' AND source_name='CBR' "
            "AND source_date IS NOT NULL AND source_date<=quote_date "
            "AND source_date>=quote_date-interval '14 days')",
            name="ck_saas_settlement_source",
        ),
    )
    op.create_table(
        "platform_saas_currency_receipts",
        sa.Column("invoice_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_currency_settlements.invoice_id",
            ondelete="RESTRICT",
        ), primary_key=True),
        sa.Column("receipt_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_receipts.id", ondelete="RESTRICT"
        ), unique=True, nullable=False),
        sa.Column("received_currency", sa.String(3), nullable=False),
        sa.Column("received_amount", sa.Numeric(38, 2), nullable=False),
        sa.Column("confirmed_by", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("reference", sa.String(160), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "received_amount>0", name="ck_saas_currency_receipt_amount",
        ),
    )
    # Refunds are compensating records, NEVER updates/deletes of sale lines.
    # Application of these credits against future bills is a separate
    # approval gate; recording a credit cannot silently clear SaaS debt.
    op.create_table(
        "platform_saas_refund_credits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invoice_line_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_invoice_lines.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("refund_reference", sa.String(160), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("credit_rub", sa.Numeric(38, 2), nullable=False),
        sa.Column("recorded_by", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("invoice_line_id", "refund_reference",
                            name="uq_saas_refund_credit_reference"),
        sa.CheckConstraint(
            "credit_rub>0 AND reason IN ('refund','chargeback','correction')",
            name="ck_saas_credit_positive",
        ),
        sa.CheckConstraint(
            "length(btrim(refund_reference)) BETWEEN 8 AND 160",
            name="ck_saas_credit_reference",
        ),
    )
    op.create_index(
        "ix_saas_credit_line", "platform_saas_refund_credits",
        ["invoice_line_id"],
    )
    for table in (
        "platform_saas_currency_settlements",
        "platform_saas_currency_receipts",
        "platform_saas_refund_credits",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $permissions$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            REVOKE ALL ON public.platform_saas_currency_settlements,
                public.platform_saas_currency_receipts,
                public.platform_saas_refund_credits
                FROM membership_runtime;
          END IF;
        END $permissions$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_credit_entry_immutable()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        BEGIN
          RAISE EXCEPTION 'Platform financial record is immutable'
            USING ERRCODE='42501';
        END;
        $fn$
    """)
    for table in (
        "platform_saas_currency_settlements",
        "platform_saas_currency_receipts",
        "platform_saas_refund_credits",
    ):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON public.{table}
            FOR EACH ROW EXECUTE FUNCTION
              public.platform_saas_credit_entry_immutable()
        """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_select_invoice_currency(
            p_invoice uuid, p_currency text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE
          actor uuid;
          invoice record;
          existing record;
          quote_day date;
          quote_rate numeric;
          cbr_source date;
          payable numeric;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF p_invoice IS NULL OR p_currency IS NULL
             OR p_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
          THEN
            RAISE EXCEPTION 'Invalid currency selection'
              USING ERRCODE='22023';
          END IF;
          SELECT id,owner_user_id,status,total_rub INTO invoice
          FROM public.platform_saas_invoices
          WHERE id=p_invoice FOR UPDATE;
          IF invoice.id IS NULL OR invoice.status<>'issued'
             OR actor IS DISTINCT FROM invoice.owner_user_id THEN
            RAISE EXCEPTION 'Only liable owner can select issued invoice currency'
              USING ERRCODE='42501';
          END IF;
          SELECT * INTO existing
          FROM public.platform_saas_currency_settlements
          WHERE invoice_id=p_invoice;
          IF FOUND THEN
            IF existing.currency_code<>p_currency THEN
              RAISE EXCEPTION 'Settlement currency is already immutable'
                USING ERRCODE='23505';
            END IF;
          ELSE
            IF EXISTS (SELECT 1 FROM public.platform_saas_receipts
                       WHERE invoice_id=p_invoice) THEN
              RAISE EXCEPTION 'Cannot quote a paid invoice'
                USING ERRCODE='42501';
            END IF;
            quote_day:=(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date;
            IF p_currency='RUB' THEN
              quote_rate:=1;
              cbr_source:=NULL;
            ELSE
              SELECT rub_per_unit,source_date INTO quote_rate,cbr_source
              FROM public.platform_saas_daily_fx_rates
              WHERE rate_date=quote_day AND currency_code=p_currency
                AND source_name='CBR'
                AND source_date<=rate_date
                AND source_date>=rate_date-interval '14 days';
              IF quote_rate IS NULL THEN
                RAISE EXCEPTION 'Missing official quote-day CBR rate'
                  USING ERRCODE='22023';
              END IF;
            END IF;
            payable:=round(invoice.total_rub/quote_rate,2);
            IF payable<=0 THEN
              RAISE EXCEPTION 'Unrepresentable settlement amount'
                USING ERRCODE='22023';
            END IF;
            INSERT INTO public.platform_saas_currency_settlements(
              invoice_id,currency_code,quote_date,source_date,source_name,
              rub_per_unit,total_rub,amount_currency,chosen_by
            ) VALUES(
              p_invoice,p_currency,quote_day,cbr_source,
              CASE WHEN p_currency='RUB' THEN 'RUB' ELSE 'CBR' END,
              quote_rate,invoice.total_rub,payable,actor
            );
          END IF;
          RETURN (SELECT jsonb_build_object(
            'invoice_id',s.invoice_id,'currency',s.currency_code,
            'quote_day',s.quote_date,'source_day',s.source_date,
            'source',s.source_name,'rub_per_unit',s.rub_per_unit,
            'total_rub',s.total_rub,'amount',s.amount_currency,
            'locked',true
          ) FROM public.platform_saas_currency_settlements s
          WHERE s.invoice_id=p_invoice);
        END;
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_invoice_currency_snapshot(
            p_invoice uuid
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE actor uuid; liable uuid;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          SELECT owner_user_id INTO liable
          FROM public.platform_saas_invoices WHERE id=p_invoice
            AND status='issued';
          IF actor IS NULL OR liable IS NULL OR (
            actor<>liable AND NOT EXISTS (
              SELECT 1 FROM public.telegram_users u
              JOIN public.platform_operators po
                ON po.telegram_user_id=u.telegram_user_id
              WHERE u.id=actor AND po.role='owner'
            )
          ) THEN
            RAISE EXCEPTION 'Invoice not visible'
              USING ERRCODE='42501';
          END IF;
          RETURN coalesce((
            SELECT jsonb_build_object(
              'invoice_id',s.invoice_id,'currency',s.currency_code,
              'quote_day',s.quote_date,'source_day',s.source_date,
              'source',s.source_name,'rub_per_unit',s.rub_per_unit,
              'total_rub',s.total_rub,'amount',s.amount_currency,
              'locked',true,
              'paid',EXISTS(SELECT 1 FROM public.platform_saas_receipts r
                            WHERE r.invoice_id=s.invoice_id)
            )
            FROM public.platform_saas_currency_settlements s
            WHERE s.invoice_id=p_invoice
          ), jsonb_build_object('invoice_id',p_invoice,'locked',false));
        END;
        $fn$
    """)
    # The old RUB-only operator confirmation must not silently mark a
    # frozen foreign-currency quote paid at a different amount.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.platform_saas_confirm_receipt(
            p_invoice uuid, p_reference text
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE actor uuid; record_id uuid; entry record;
        BEGIN
          actor:=public.platform_saas_require_owner();
          IF p_invoice IS NULL OR p_reference IS NOT NULL
             AND (length(p_reference)>160 OR
                  length(btrim(p_reference))=0) THEN
            RAISE EXCEPTION 'Invalid payment reference'
              USING ERRCODE='22023';
          END IF;
          SELECT id,total_rub,status INTO entry
          FROM public.platform_saas_invoices
          WHERE id=p_invoice FOR UPDATE;
          IF entry.id IS NULL OR entry.status<>'issued' THEN
            RAISE EXCEPTION 'Issued invoice required'
              USING ERRCODE='42501';
          END IF;
          IF EXISTS (
            SELECT 1 FROM public.platform_saas_currency_settlements
            WHERE invoice_id=p_invoice AND currency_code<>'RUB'
          ) THEN
            RAISE EXCEPTION 'Foreign settlement needs currency receipt'
              USING ERRCODE='42501';
          END IF;
          SELECT id INTO record_id FROM public.platform_saas_receipts
          WHERE invoice_id=p_invoice;
          IF record_id IS NOT NULL THEN RETURN record_id; END IF;
          record_id:=gen_random_uuid();
          INSERT INTO public.platform_saas_receipts(
            id,invoice_id,confirmed_by,amount_rub,reference
          ) VALUES(
            record_id,p_invoice,actor,entry.total_rub,p_reference
          );
          RETURN record_id;
        END;
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_confirm_currency_receipt(
            p_invoice uuid, p_currency text, p_received numeric,
            p_reference text
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE actor uuid; s record; old_receipt uuid; existing record;
        BEGIN
          actor:=public.platform_saas_require_owner();
          IF p_invoice IS NULL OR p_currency IS NULL
             OR p_received IS NULL OR p_reference IS NOT NULL AND (
               length(p_reference)>160 OR length(btrim(p_reference))=0
             ) THEN
            RAISE EXCEPTION 'Invalid confirmed settlement'
              USING ERRCODE='22023';
          END IF;
          PERFORM 1 FROM public.platform_saas_invoices
          WHERE id=p_invoice AND status='issued' FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Issued invoice required'
              USING ERRCODE='42501';
          END IF;
          SELECT * INTO s FROM public.platform_saas_currency_settlements
          WHERE invoice_id=p_invoice;
          IF s.invoice_id IS NULL OR s.currency_code<>p_currency
             OR s.amount_currency IS DISTINCT FROM p_received THEN
            RAISE EXCEPTION 'Currency receipt does not match frozen quote'
              USING ERRCODE='23514';
          END IF;
          SELECT * INTO existing
            FROM public.platform_saas_currency_receipts
           WHERE invoice_id=p_invoice;
          IF existing.invoice_id IS NOT NULL THEN
            IF existing.received_currency<>p_currency
               OR existing.received_amount<>p_received
               OR existing.reference IS DISTINCT FROM p_reference THEN
              RAISE EXCEPTION 'Conflicting receipt confirmation'
                USING ERRCODE='23505';
            END IF;
            RETURN existing.receipt_id;
          END IF;
          IF EXISTS (SELECT 1 FROM public.platform_saas_receipts
                     WHERE invoice_id=p_invoice) THEN
            RAISE EXCEPTION 'Invoice already settled by another method'
              USING ERRCODE='23505';
          END IF;
          old_receipt:=gen_random_uuid();
          INSERT INTO public.platform_saas_receipts(
            id,invoice_id,confirmed_by,amount_rub,reference
          ) VALUES(old_receipt,p_invoice,actor,s.total_rub,p_reference);
          INSERT INTO public.platform_saas_currency_receipts(
            invoice_id,receipt_id,received_currency,received_amount,
            confirmed_by,reference
          ) VALUES(p_invoice,old_receipt,p_currency,p_received,actor,p_reference);
          RETURN old_receipt;
        END;
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_record_refund_credit(
            p_line uuid, p_reference text, p_credit numeric, p_reason text
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE actor uuid; original record; previous record;
                used_credit numeric; credit_id uuid;
        BEGIN
          actor:=public.platform_saas_require_owner();
          IF p_line IS NULL OR p_reference IS NULL
             OR length(btrim(p_reference)) NOT BETWEEN 8 AND 160
             OR p_credit IS NULL OR p_credit<=0
             OR p_credit<>round(p_credit,2)
             OR p_reason NOT IN ('refund','chargeback','correction')
             OR p_reason IS NULL THEN
            RAISE EXCEPTION 'Invalid audited refund credit'
              USING ERRCODE='22023';
          END IF;
          -- Lock the parent line, not the credit row, so two concurrent
          -- refunds cannot each spend the same unused charge balance.
          SELECT l.id,l.charge_rub,l.source_kind,i.status
          INTO original FROM public.platform_saas_invoice_lines l
          JOIN public.platform_saas_invoices i ON i.id=l.invoice_id
          WHERE l.id=p_line FOR UPDATE OF l;
          IF original.id IS NULL OR original.status<>'issued'
             OR original.source_kind NOT IN ('subscription','file_manual') THEN
            RAISE EXCEPTION 'Creditable issued sale line required'
              USING ERRCODE='42501';
          END IF;
          SELECT * INTO previous FROM public.platform_saas_refund_credits
          WHERE invoice_line_id=p_line
            AND refund_reference=btrim(p_reference);
          IF previous.id IS NOT NULL THEN
            IF previous.credit_rub<>p_credit OR previous.reason<>p_reason THEN
              RAISE EXCEPTION 'Refund reference already has different terms'
                USING ERRCODE='23505';
            END IF;
            RETURN previous.id;
          END IF;
          SELECT coalesce(sum(credit_rub),0) INTO used_credit
          FROM public.platform_saas_refund_credits
          WHERE invoice_line_id=p_line;
          IF used_credit+p_credit>original.charge_rub THEN
            RAISE EXCEPTION 'Refund exceeds original charged commission'
              USING ERRCODE='23514';
          END IF;
          credit_id:=gen_random_uuid();
          INSERT INTO public.platform_saas_refund_credits(
            id,invoice_line_id,refund_reference,credit_rub,reason,recorded_by
          ) VALUES(
            credit_id,p_line,btrim(p_reference),p_credit,p_reason,actor
          );
          RETURN credit_id;
        END;
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_refund_credit_report(
            p_owner uuid
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE actor uuid; result jsonb;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF p_owner IS NULL OR actor IS NULL OR (
            actor<>p_owner AND NOT EXISTS(
              SELECT 1 FROM public.telegram_users u
              JOIN public.platform_operators o
                ON o.telegram_user_id=u.telegram_user_id
              WHERE u.id=actor AND o.role='owner'
            )
          ) THEN
            RAISE EXCEPTION 'Credit history is owner only'
              USING ERRCODE='42501';
          END IF;
          SELECT jsonb_build_object(
            'owner_id',p_owner,
            'total_credited_rub',coalesce(sum(c.credit_rub),0),
            'credit_count',count(*),
            'application_status','pending_future_statement'
          ) INTO result
          FROM public.platform_saas_refund_credits c
          JOIN public.platform_saas_invoice_lines l
            ON l.id=c.invoice_line_id
          JOIN public.platform_saas_invoices i ON i.id=l.invoice_id
          WHERE i.owner_user_id=p_owner;
          RETURN result;
        END;
        $fn$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_credit_entry_immutable() FROM PUBLIC"
    )
    for signature in (
        "platform_saas_select_invoice_currency(uuid,text)",
        "platform_saas_invoice_currency_snapshot(uuid)",
        "platform_saas_confirm_currency_receipt(uuid,text,numeric,text)",
        "platform_saas_record_refund_credit(uuid,text,numeric,text)",
        "platform_saas_refund_credit_report(uuid)",
    ):
        op.execute("REVOKE ALL ON FUNCTION public." + signature + " FROM PUBLIC")
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
          THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_select_invoice_currency(uuid,text),
              public.platform_saas_invoice_currency_snapshot(uuid),
              public.platform_saas_confirm_currency_receipt(uuid,text,numeric,text),
              public.platform_saas_record_refund_credit(uuid,text,numeric,text),
              public.platform_saas_refund_credit_report(uuid)
              TO membership_runtime;
          END IF;
        END $grants$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Refuse rollback of immutable financial settlement/credit evidence."
    )
