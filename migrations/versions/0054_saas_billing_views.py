"""Read-only issued-invoice previews and commission-credit history for Rich UI.

Revision ID: 0054_saas_billing_views
Revises: 0053_saas_settlement_credits

No billing activation, currency selection, receipts or invoice mutation.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0054_saas_billing_views"
down_revision: str | None = "0053_saas_settlement_credits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.platform_saas_preview_invoice_currency(
            p_invoice uuid, p_currency text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE
          actor uuid;
          bill record;
          quote_day date;
          quote_rate numeric;
          source_day date;
          amount numeric;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF p_invoice IS NULL OR p_currency IS NULL
             OR p_currency NOT IN ('RUB','USD','EUR','UAH','BYN') THEN
            RAISE EXCEPTION 'Invalid requested settlement currency'
              USING ERRCODE='22023';
          END IF;
          SELECT id,owner_user_id,total_rub,status INTO bill
          FROM public.platform_saas_invoices WHERE id=p_invoice;
          IF bill.id IS NULL OR actor IS DISTINCT FROM bill.owner_user_id
             OR bill.status<>'issued' THEN
            RAISE EXCEPTION 'Invoice not visible to liable owner'
              USING ERRCODE='42501';
          END IF;
          IF EXISTS (
             SELECT 1 FROM public.platform_saas_currency_settlements
             WHERE invoice_id=p_invoice
          ) OR EXISTS (
             SELECT 1 FROM public.platform_saas_receipts
             WHERE invoice_id=p_invoice
          ) THEN
            RAISE EXCEPTION 'Invoice currency already settled'
              USING ERRCODE='42501';
          END IF;
          quote_day:=(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date;
          IF p_currency='RUB' THEN
            quote_rate:=1;
          ELSE
            SELECT rub_per_unit,source_date INTO quote_rate,source_day
            FROM public.platform_saas_daily_fx_rates
            WHERE rate_date=quote_day AND currency_code=p_currency
              AND source_name='CBR'
              AND source_date<=rate_date
              AND source_date>=rate_date-interval '14 days';
          END IF;
          IF quote_rate IS NOT NULL THEN
            amount:=round(bill.total_rub/quote_rate,2);
            IF amount<=0 THEN
              RAISE EXCEPTION 'Unrepresentable quote'
                USING ERRCODE='22023';
            END IF;
          END IF;
          RETURN jsonb_build_object(
            'invoice_id',p_invoice,'currency',p_currency,
            'total_rub',bill.total_rub,'quote_day',quote_day,
            'source_day',source_day,
            'source',CASE WHEN p_currency='RUB' THEN 'RUB' ELSE 'CBR' END,
            'rub_per_unit',quote_rate,'amount',amount,
            'quote_ready',quote_rate IS NOT NULL,
            'preview_only',true
          );
        END;
        $fn$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_invoice_credit_history(
            p_invoice uuid
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE
          actor uuid;
          bill record;
          total numeric;
          count_entries integer;
          items jsonb;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          SELECT id,owner_user_id,status INTO bill
          FROM public.platform_saas_invoices WHERE id=p_invoice;
          IF bill.id IS NULL OR bill.status<>'issued' OR actor IS NULL
             OR (actor<>bill.owner_user_id AND NOT EXISTS (
                SELECT 1 FROM public.telegram_users u
                JOIN public.platform_operators po
                  ON po.telegram_user_id=u.telegram_user_id
                WHERE u.id=actor AND po.role='owner'
             )) THEN
            RAISE EXCEPTION 'Invoice credit history not visible'
              USING ERRCODE='42501';
          END IF;
          SELECT coalesce(sum(c.credit_rub),0),count(*) INTO total,count_entries
          FROM public.platform_saas_refund_credits c
          JOIN public.platform_saas_invoice_lines l
            ON l.id=c.invoice_line_id WHERE l.invoice_id=p_invoice;
          SELECT coalesce(jsonb_agg(jsonb_build_object(
              'reason',c.reason,'reference',c.refund_reference,
              'credit_rub',c.credit_rub,'at',c.recorded_at,
              'source_kind',l.source_kind
          ) ORDER BY c.recorded_at DESC,c.id DESC),'[]'::jsonb)
          INTO items FROM (
            SELECT c.* FROM public.platform_saas_refund_credits c
            JOIN public.platform_saas_invoice_lines l
              ON l.id=c.invoice_line_id
            WHERE l.invoice_id=p_invoice
            ORDER BY c.recorded_at DESC,c.id DESC LIMIT 8
          ) c JOIN public.platform_saas_invoice_lines l
              ON l.id=c.invoice_line_id;
          RETURN jsonb_build_object(
            'invoice_id',p_invoice,
            'total_credited_rub',total,'credit_count',count_entries,
            'entries',items,'application_status','pending_future_statement'
          );
        END;
        $fn$
    """)
    # The displayed pre-confirmation quote must match the value frozen
    # atomically under the invoice row lock. No rate from a later Moscow day
    # or newly imported CBR quote may silently replace the confirmed amount.
    op.execute("""
        CREATE FUNCTION public.platform_saas_lock_invoice_currency_expected(
            p_invoice uuid, p_currency text, p_day date,
            p_rate numeric, p_amount numeric
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $fn$
        DECLARE
          owner_id uuid;
          actor uuid;
          report jsonb;
          result jsonb;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF p_invoice IS NULL OR p_day IS NULL
             OR p_rate IS NULL OR p_amount IS NULL THEN
            RAISE EXCEPTION 'Missing expected settlement quote'
              USING ERRCODE='22023';
          END IF;
          SELECT owner_user_id INTO owner_id
          FROM public.platform_saas_invoices
          WHERE id=p_invoice AND status='issued' FOR UPDATE;
          IF owner_id IS NULL OR actor IS DISTINCT FROM owner_id THEN
            RAISE EXCEPTION 'Invoice not visible to liable owner'
              USING ERRCODE='42501';
          END IF;
          report:=public.platform_saas_preview_invoice_currency(
              p_invoice,p_currency
          );
          IF NOT coalesce((report->>'quote_ready')::boolean,false)
             OR (report->>'quote_day')::date IS DISTINCT FROM p_day
             OR (report->>'rub_per_unit')::numeric IS DISTINCT FROM p_rate
             OR (report->>'amount')::numeric IS DISTINCT FROM p_amount THEN
            RAISE EXCEPTION 'Displayed FX terms changed, review again'
              USING ERRCODE='22023';
          END IF;
          result:=public.platform_saas_select_invoice_currency(
              p_invoice,p_currency
          );
          IF (result->>'quote_day')::date IS DISTINCT FROM p_day
             OR (result->>'rub_per_unit')::numeric IS DISTINCT FROM p_rate
             OR (result->>'amount')::numeric IS DISTINCT FROM p_amount THEN
            -- Any insert made by the delegated function rolls back.
            RAISE EXCEPTION 'Settlement changed during currency lock'
              USING ERRCODE='22023';
          END IF;
          RETURN result;
        END;
        $fn$
    """)
    for signature in (
        "platform_saas_preview_invoice_currency(uuid,text)",
        "platform_saas_invoice_credit_history(uuid)",
        "platform_saas_lock_invoice_currency_expected(uuid,text,date,numeric,numeric)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles
                    WHERE rolname='membership_runtime') THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_preview_invoice_currency(uuid,text),
              public.platform_saas_invoice_credit_history(uuid),
              public.platform_saas_lock_invoice_currency_expected(
                uuid,text,date,numeric,numeric
              )
            TO membership_runtime;
          END IF;
        END $grants$
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_lock_invoice_currency_expected("
        "uuid,text,date,numeric,numeric)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_invoice_credit_history(uuid)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_preview_invoice_currency(uuid,text)"
    )
