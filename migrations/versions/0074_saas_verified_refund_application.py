"""Apply only verified provider refund evidence to real ViMi Pay balances.

Revision ID: 0074_saas_refund_application
Revises: 0073_saas_windows
No new buyer UI, manual refund command, debt mutation or activation at migration.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074_saas_refunds"
down_revision: str | None = "0073_saas_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saas_verified_credit_claims",
        sa.Column("id",sa.Uuid(),primary_key=True),
        sa.Column("source_kind",sa.String(24),nullable=False),
        sa.Column("source_credit_id",sa.Uuid(),nullable=False),
        sa.Column("invoice_line_id",sa.Uuid(),sa.ForeignKey(
            "platform_saas_invoice_lines.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("owner_user_id",sa.Uuid(),nullable=False),
        sa.Column("credit_rub",sa.Numeric(38,2),nullable=False),
        sa.Column("recorded_at",sa.DateTime(timezone=True),nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("source_kind","source_credit_id",
                            name="uq_saas_verified_refund_proof"),
        sa.CheckConstraint(
            "source_kind IN ('stars_file','stars_subscription','crypto_transfer') "
            "AND credit_rub>=0",name="ck_saas_verified_credit"),
    )
    op.create_index("ix_saas_verified_claims_owner",
                    "platform_saas_verified_credit_claims",
                    ["owner_user_id","recorded_at"])
    op.create_table(
        "platform_saas_verified_credit_allocations",
        sa.Column("id",sa.Uuid(),primary_key=True),
        sa.Column("claim_id",sa.Uuid(),sa.ForeignKey(
            "platform_saas_verified_credit_claims.id",ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("invoice_id",sa.Uuid(),sa.ForeignKey(
            "platform_saas_invoices.id",ondelete="RESTRICT"),nullable=False),
        sa.Column("amount_rub",sa.Numeric(38,2),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint("claim_id","invoice_id",
                            name="uq_saas_verified_credit_alloc"),
        sa.CheckConstraint("amount_rub>0",name="ck_saas_verified_alloc_positive"),
    )
    op.create_index("ix_saas_verified_alloc_invoice",
                    "platform_saas_verified_credit_allocations",["invoice_id"])
    for table in ("platform_saas_verified_credit_claims",
                  "platform_saas_verified_credit_allocations"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
          CREATE TRIGGER trg_{table}_immutable
          BEFORE UPDATE OR DELETE ON public.{table}
          FOR EACH ROW EXECUTE FUNCTION public.platform_saas_credit_entry_immutable()
        """)
    op.execute("""
       DO $revoke$ BEGIN
         IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
           REVOKE ALL ON public.platform_saas_verified_credit_claims,
             public.platform_saas_verified_credit_allocations
           FROM membership_runtime;
         END IF;
       END $revoke$
    """)
    op.execute("""CREATE FUNCTION public.platform_saas_reconcile_verified_refunds(
    p_owner uuid DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $fn$
DECLARE
  actor uuid;
  is_operator boolean;
  entry record;
  claim record;
  bill record;
  already_claimed numeric;
  claim_amount numeric;
  used_amount numeric;
  available numeric;
  due numeric;
  applied numeric;
  created integer:=0;
  allocated integer:=0;
BEGIN
  actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  SELECT EXISTS (
    SELECT 1 FROM public.telegram_users u
    JOIN public.platform_operators o ON o.telegram_user_id=u.telegram_user_id
    WHERE u.id=actor AND o.role='owner'
  ) INTO is_operator;
  IF actor IS NULL OR (p_owner IS NULL AND NOT is_operator)
    OR (p_owner IS NOT NULL AND actor<>p_owner AND NOT is_operator)
  THEN
    RAISE EXCEPTION 'Verified credits require owning account or platform owner'
      USING ERRCODE='42501';
  END IF;
  -- Monetary account ledger: one source and one allocation are immutable.
  -- Serialize across capture, bank confirmation and overdue balance reads.
  PERFORM pg_advisory_xact_lock(
    hashtextextended('vimi:saas:verified-refunds',0)
  );
  FOR entry IN
    SELECT src.*,l.id AS line_id,l.charge_rub,fx.rub_per_unit
    FROM (
      SELECT 'stars_file'::text AS refund_kind,c.id AS proof_id,
             s.owner_user_id,s.source_kind,s.source_id,
             s.paid_at,c.commission_credit_usd
      FROM public.platform_saas_star_refund_credits c
      JOIN public.platform_saas_star_sales s ON s.id=c.original_sale_id
        AND s.owner_user_id=c.owner_user_id
        AND s.source_kind='file_stars' AND s.billing_eligible
      UNION ALL
      SELECT 'stars_subscription',c.id,s.owner_user_id,
             s.source_kind,s.source_id,s.paid_at,c.commission_credit_usd
      FROM public.platform_saas_star_subscription_credits c
      JOIN public.platform_saas_star_sales s ON s.id=c.original_sale_id
        AND s.owner_user_id=c.owner_user_id
        AND s.source_kind='subscription_stars' AND s.billing_eligible
      UNION ALL
      SELECT 'crypto_transfer',c.id,s.owner_user_id,
             s.source_kind,s.invoice_id,s.paid_at,c.commission_credit_usd
      FROM public.platform_saas_crypto_transfer_credits c
      JOIN public.platform_saas_crypto_sales s ON s.id=c.original_sale_id
        AND s.owner_user_id=c.owner_user_id AND s.billing_eligible
    ) src
    JOIN public.platform_saas_invoice_lines l
      ON l.source_kind=src.source_kind AND l.source_id=src.source_id
      AND l.fee_rate=0.05
    JOIN public.platform_saas_invoices i ON i.id=l.invoice_id
      AND i.owner_user_id=src.owner_user_id AND i.kind='commission'
      AND i.status='issued'
    JOIN public.platform_saas_daily_fx_rates fx
      ON fx.rate_date=(src.paid_at AT TIME ZONE 'Europe/Moscow')::date
      AND fx.currency_code='USD' AND fx.source_name='CBR'
      AND fx.source_date<=fx.rate_date
      AND fx.source_date>=fx.rate_date-interval '14 days'
    WHERE (p_owner IS NULL OR src.owner_user_id=p_owner)
      AND NOT EXISTS(
        SELECT 1 FROM public.platform_saas_verified_credit_claims c
        WHERE c.source_kind=src.refund_kind AND c.source_credit_id=src.proof_id
      )
    ORDER BY src.paid_at,src.proof_id LIMIT 200
  LOOP
    SELECT coalesce(sum(credit_rub),0) INTO already_claimed
    FROM public.platform_saas_verified_credit_claims c
    WHERE c.invoice_line_id=entry.line_id;
    claim_amount:=greatest(0,least(
      round(entry.commission_credit_usd*entry.rub_per_unit,2),
      round(entry.charge_rub,2)-already_claimed
    ));
    INSERT INTO public.platform_saas_verified_credit_claims(
      id,source_kind,source_credit_id,invoice_line_id,owner_user_id,
      credit_rub
    ) VALUES(
      gen_random_uuid(),entry.refund_kind,entry.proof_id,
      entry.line_id,entry.owner_user_id,claim_amount
    ) ON CONFLICT(source_kind,source_credit_id) DO NOTHING;
    IF FOUND THEN created:=created+1; END IF;
  END LOOP;
  -- Each source-linked verified refund can be used only up to its face
  -- value. A refund recorded after invoice settlement becomes account
  -- credit for later invoices, not a negative historical payment.
  FOR claim IN
    SELECT c.*, c.credit_rub-coalesce((
       SELECT sum(a.amount_rub)
       FROM public.platform_saas_verified_credit_allocations a
       WHERE a.claim_id=c.id
    ),0) AS available
    FROM public.platform_saas_verified_credit_claims c
    WHERE (p_owner IS NULL OR c.owner_user_id=p_owner)
      AND c.credit_rub>coalesce((
        SELECT sum(a.amount_rub)
        FROM public.platform_saas_verified_credit_allocations a
        WHERE a.claim_id=c.id
      ),0)
      AND EXISTS(
        SELECT 1 FROM public.platform_saas_invoices i
        WHERE i.owner_user_id=c.owner_user_id AND i.status='issued'
          AND public.platform_saas_v2_remaining(i.id)>0
      )
    ORDER BY c.recorded_at,c.id LIMIT 200
  LOOP
    available:=claim.available;
    FOR bill IN
      SELECT i.id FROM public.platform_saas_invoices i
      WHERE i.owner_user_id=claim.owner_user_id AND i.status='issued'
        AND NOT EXISTS(
          SELECT 1 FROM public.platform_saas_verified_credit_allocations a
          WHERE a.claim_id=claim.id AND a.invoice_id=i.id
        )
      ORDER BY CASE WHEN i.id=(
        SELECT l.invoice_id FROM public.platform_saas_invoice_lines l
        WHERE l.id=claim.invoice_line_id
      ) THEN 0 ELSE 1 END, i.due_at,i.id
      FOR UPDATE
    LOOP
      EXIT WHEN available<0.01;
      due:=public.platform_saas_v2_remaining(bill.id);
      applied:=least(available,due);
      IF applied>=0.01 THEN
        INSERT INTO public.platform_saas_verified_credit_allocations(
          id,claim_id,invoice_id,amount_rub
        ) VALUES(gen_random_uuid(),claim.id,bill.id,applied);
        available:=available-applied;
        allocated:=allocated+1;
      END IF;
    END LOOP;
  END LOOP;
  RETURN jsonb_build_object('claimed',created,'allocated',allocated);
END; $fn$""")
    op.execute("""REVOKE ALL ON FUNCTION
      public.platform_saas_reconcile_verified_refunds(uuid) FROM PUBLIC""")
    op.execute("""
      DO $grant$ BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
          GRANT EXECUTE ON FUNCTION public.platform_saas_reconcile_verified_refunds(uuid)
          TO membership_runtime;
        END IF;
      END $grant$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_remaining(p_invoice uuid)
RETURNS numeric LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE original numeric; received numeric; credited numeric;
        verified_credit numeric; invoice_status text;
BEGIN
  SELECT total_rub,status INTO original,invoice_status FROM public.platform_saas_invoices
  WHERE id=p_invoice;
  IF original IS NULL THEN
    RAISE EXCEPTION 'Issued invoice required for balance'
      USING ERRCODE='42501';
  END IF;
  IF invoice_status<>'issued' THEN RETURN 0; END IF;
  SELECT coalesce(sum(amount_rub),0) INTO received
  FROM public.platform_saas_receipts WHERE invoice_id=p_invoice;
  SELECT coalesce(sum(allocated_rub),0) INTO credited
  FROM public.platform_saas_payment_allocations_v2 WHERE invoice_id=p_invoice;
  SELECT coalesce(sum(amount_rub),0) INTO verified_credit
  FROM public.platform_saas_verified_credit_allocations
  WHERE invoice_id=p_invoice;
  IF received+credited+verified_credit>original THEN
    RAISE EXCEPTION 'Invoice overpayment violates accounting invariant'
      USING ERRCODE='23514';
  END IF;
  RETURN original-received-credited-verified_credit;
END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_invoice_balance(
       p_invoice uuid
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; operator_access boolean; i record;
            legacy_paid numeric; allocated numeric; verified_credit numeric;
            remaining numeric;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      SELECT EXISTS(SELECT 1 FROM public.telegram_users u
        JOIN public.platform_operators o ON o.telegram_user_id=u.telegram_user_id
        WHERE u.id=actor AND o.role='owner') INTO operator_access;
      SELECT * INTO i FROM public.platform_saas_invoices
      WHERE id=p_invoice;
      IF i.id IS NULL OR i.status<>'issued'
         OR (i.owner_user_id IS DISTINCT FROM actor AND NOT operator_access) THEN
        RAISE EXCEPTION 'Invoice unavailable' USING ERRCODE='42501';
      END IF;
      SELECT coalesce(sum(amount_rub),0) INTO legacy_paid
        FROM public.platform_saas_receipts WHERE invoice_id=i.id;
      SELECT coalesce(sum(allocated_rub),0) INTO allocated
        FROM public.platform_saas_payment_allocations_v2
        WHERE invoice_id=i.id;
      SELECT coalesce(sum(amount_rub),0) INTO verified_credit
      FROM public.platform_saas_verified_credit_allocations
      WHERE invoice_id=p_invoice;
      remaining:=i.total_rub-legacy_paid-allocated-verified_credit;
      IF remaining<0 THEN
        RAISE EXCEPTION 'Overpaid bill: accounting inconsistent'
          USING ERRCODE='23514';
      END IF;
      RETURN jsonb_build_object(
        'invoice_id',i.id,'owner_user_id',i.owner_user_id,
        'total_rub',i.total_rub::text,
        'settled_rub',(legacy_paid+allocated)::text,
        'remaining_rub',remaining::text,
        'paid',remaining=0,'partial_rub',allocated::text,
        'legacy_rub',legacy_paid::text,
        'verified_refund_credit_rub',verified_credit::text
      );
    END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_create_attempt(
      p_invoice uuid,p_requested_rub numeric,p_currency text
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      actor uuid;
      item record;
      paid numeric;
      due numeric;
      rate numeric;
      fx_day date;
      day date;
      bank text;
      converted numeric;
      snapped_rub numeric;
      attempt uuid;
      deadline timestamptz;
      native_usd numeric;
      usd_rate_today numeric;
    BEGIN
      actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
      IF actor IS NULL OR p_invoice IS NULL
         OR p_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
         OR p_requested_rub IS NULL OR p_requested_rub<=0
         OR p_requested_rub<>round(p_requested_rub,2) THEN
        RAISE EXCEPTION 'Invalid payment amount/currency'
          USING ERRCODE='22023';
      END IF;
      PERFORM public.platform_saas_reconcile_verified_refunds(actor);
      SELECT * INTO item FROM public.platform_saas_invoices
        WHERE id=p_invoice FOR UPDATE;
      IF item.id IS NULL OR item.status<>'issued'
         OR item.owner_user_id IS DISTINCT FROM actor THEN
        RAISE EXCEPTION 'Account invoice is not payable'
          USING ERRCODE='42501';
      END IF;
      SELECT coalesce(sum(amount_rub),0) INTO paid
      FROM public.platform_saas_receipts WHERE invoice_id=p_invoice;
      SELECT paid+coalesce(sum(allocated_rub),0) INTO paid
      FROM public.platform_saas_payment_allocations_v2
      WHERE invoice_id=p_invoice;
      due:=public.platform_saas_v2_remaining(item.id);
      IF p_requested_rub>due THEN
        RAISE EXCEPTION 'Requested amount exceeds unpaid invoice'
          USING ERRCODE='23514';
      END IF;
      SELECT instructions INTO bank
      FROM public.platform_saas_payment_methods_v2
      WHERE currency_code=p_currency AND enabled;
      -- Legacy RUB details are a fallback only when the owner has never
      -- configured a v2 RUB row. An explicit disabled row always wins.
      IF bank IS NULL AND p_currency='RUB' AND NOT EXISTS(
        SELECT 1 FROM public.platform_saas_payment_methods_v2
        WHERE currency_code='RUB'
      ) THEN
        SELECT instructions INTO bank
        FROM public.platform_saas_payment_details WHERE id=1;
      END IF;
      IF bank IS NULL THEN
        RAISE EXCEPTION 'Platform bank details not published'
          USING ERRCODE='42501';
      END IF;
      day:=(clock_timestamp() AT TIME ZONE 'Europe/Moscow')::date;
      IF p_currency='RUB' THEN rate:=1;fx_day:=NULL;
      ELSE
        SELECT rub_per_unit,source_date INTO rate,fx_day
        FROM public.platform_saas_daily_fx_rates
        WHERE rate_date=day AND currency_code=p_currency
          AND source_name='CBR' AND source_date<=day
          AND source_date>=day-interval '14 days';
      END IF;
      IF rate IS NULL OR rate<=0 THEN
        RAISE EXCEPTION 'Verified bank currency conversion unavailable'
          USING ERRCODE='42501';
      END IF;
      -- Foreign currency is paid in 2 decimal places. A rounded bank
      -- transfer can differ slightly from the RUB obligation. Preserve the
      -- exact RUB amount requested; the platform absorbs that bounded FX
      -- rounding difference rather than creating an unpayable residual.
      -- Preserve the original $3 service obligation across FX dates.
      -- RUB allocations remain at their immutable issue-day book value;
      -- only the actual bank transfer quote uses the current rate.
      SELECT l.original_amount INTO native_usd
      FROM public.platform_saas_invoice_lines l
      WHERE l.invoice_id=item.id AND l.source_kind='bot_fee'
        AND l.original_currency='USD'
      LIMIT 1;
      IF native_usd IS NULL THEN
        converted:=round(p_requested_rub/rate,2);
      ELSIF p_currency='USD' THEN
        converted:=round(native_usd*p_requested_rub/item.total_rub,2);
      ELSE
        SELECT rub_per_unit INTO usd_rate_today
        FROM public.platform_saas_daily_fx_rates
        WHERE rate_date=day AND currency_code='USD'
          AND source_name='CBR' AND source_date<=day
          AND source_date>=day-interval '14 days';
        IF usd_rate_today IS NULL OR usd_rate_today<=0 THEN
          RAISE EXCEPTION 'Current USD service quote unavailable'
            USING ERRCODE='42501';
        END IF;
        converted:=round(
          native_usd*p_requested_rub/item.total_rub*usd_rate_today/rate,2
        );
      END IF;
      snapped_rub:=p_requested_rub;
      IF converted<=0 OR snapped_rub>due
         OR (native_usd IS NULL AND
             abs(converted*rate-snapped_rub)>rate*0.005+0.005) THEN
        RAISE EXCEPTION 'Converted payment amount outside remaining debt'
          USING ERRCODE='23514';
      END IF;
      deadline:=clock_timestamp()+interval '48 hours';
      attempt:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_attempts_v2(
        id,invoice_id,owner_user_id,amount_rub,currency_code,
        rub_per_unit,amount_currency,quote_date,source_date,
        instructions_snapshot,expires_at
      ) VALUES(
        attempt,item.id,actor,snapped_rub,p_currency,rate,
        converted,day,fx_day,bank,deadline
      );
      RETURN jsonb_build_object(
        'id',attempt,'invoice_id',item.id,'amount_rub',snapped_rub::text,
        'currency',p_currency,'amount',converted::text,
        'rub_per_unit',rate::text,'quote_date',day,
        'source_date',fx_day,'expires_at',deadline,
        'instructions',bank,'remaining_before',due::text
      );
    END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_approve_attempt(
      p_attempt uuid,p_reference text
    ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; original record; invoice record;
            existing record; used numeric; allocated_id uuid;
    BEGIN
      actor:=public.platform_saas_require_owner();
      IF p_attempt IS NULL
         OR length(btrim(coalesce(p_reference,''))) NOT BETWEEN 8 AND 160 THEN
        RAISE EXCEPTION 'Verified bank receipt reference required'
          USING ERRCODE='22023';
      END IF;
      -- Approval and rejection must lock the SAME row before reading
      -- opposite outcomes. Invoice lock remains second, for balance safety.
      SELECT * INTO original FROM public.platform_saas_payment_attempts_v2
      WHERE id=p_attempt FOR UPDATE;
      IF original.id IS NULL THEN
        RAISE EXCEPTION 'Unknown partial payment attempt'
          USING ERRCODE='42501';
      END IF;
      SELECT * INTO invoice FROM public.platform_saas_invoices
      WHERE id=original.invoice_id FOR UPDATE;
      IF invoice.id IS NULL OR invoice.status<>'issued'
         OR invoice.owner_user_id IS DISTINCT FROM original.owner_user_id THEN
        RAISE EXCEPTION 'Original bill/owner has changed'
          USING ERRCODE='42501';
      END IF;
      SELECT * INTO existing
      FROM public.platform_saas_payment_allocations_v2
      WHERE attempt_id=p_attempt;
      IF existing.id IS NOT NULL THEN
        IF existing.bank_reference IS DISTINCT FROM btrim(p_reference) THEN
          RAISE EXCEPTION 'Conflicting receipt replay'
            USING ERRCODE='23505';
        END IF;
        RETURN existing.id;
      END IF;
      IF NOT EXISTS(
        SELECT 1 FROM public.platform_saas_payment_proofs_v2
        WHERE attempt_id=p_attempt
      ) OR EXISTS(
        SELECT 1 FROM public.platform_saas_payment_rejections_v2
        WHERE attempt_id=p_attempt
      ) THEN
        RAISE EXCEPTION 'Proof missing or payment rejected'
          USING ERRCODE='42501';
      END IF;
      SELECT coalesce(sum(amount_rub),0) INTO used
      FROM public.platform_saas_receipts
      WHERE invoice_id=invoice.id;
      SELECT used+coalesce(sum(allocated_rub),0) INTO used
      FROM public.platform_saas_payment_allocations_v2
      WHERE invoice_id=invoice.id;
      IF original.amount_rub>public.platform_saas_v2_remaining(invoice.id) THEN
        RAISE EXCEPTION 'Partial payment would over-allocate invoice'
          USING ERRCODE='23514';
      END IF;
      allocated_id:=gen_random_uuid();
      INSERT INTO public.platform_saas_payment_allocations_v2(
        id,attempt_id,invoice_id,received_currency,received_amount,
        allocated_rub,bank_reference,confirmed_by
      ) VALUES(
        allocated_id,p_attempt,invoice.id,original.currency_code,
        original.amount_currency,original.amount_rub,
        btrim(p_reference),actor
      );
      RETURN allocated_id;
    END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_issue_due_batch()
    RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE
      started timestamptz;
      now_at timestamptz:=clock_timestamp();
      period_start timestamptz;
      prior record;
      bot record;
      accounting record;
      previous_month date;
      bot_issued integer:=0;
      commission_issued integer:=0;
      deferred integer:=0;
      invoice uuid;
      first_trial timestamptz;
      owner_period_start timestamptz;
      last_issued_end timestamptz;
      progress record;
      cursor_bot_at timestamptz;
      cursor_bot_id uuid;
      cursor_month date;
      cursor_owner_id uuid;
      cursor_merchant_id uuid;
      recovery_owner record;
      recovery_merchant uuid;
    BEGIN
      PERFORM public.platform_saas_require_owner();
      SELECT effective_from INTO started
      FROM public.platform_saas_billing_control
      WHERE id=1 AND enabled;
      IF started IS NULL OR started>now_at THEN
        RETURN jsonb_build_object(
          'enabled',false,'bot_invoices',0,
          'commission_invoices',0,'deferred',0
        );
      END IF;
      IF NOT pg_try_advisory_xact_lock(
        hashtextextended('vimi:saas:due-reconcile',0)
      ) THEN
        RETURN jsonb_build_object('enabled',true,'busy',true);
      END IF;
      SELECT * INTO progress FROM public.platform_saas_due_progress
      WHERE id=1 FOR UPDATE;
      -- During the activation delay, first-bot trial triggers are inactive.
      -- Reconcile those accounts without rewriting any historical claim.
      FOR recovery_owner IN
        SELECT p.owner_user_id,min(greatest(b.created_at,mp.started_at)) AS first_connected
        FROM public.bot_instances b
        JOIN public.projects p ON p.id=b.project_id
        JOIN public.merchant_project_periods mp
          ON mp.project_id=p.id AND mp.owner_user_id=p.owner_user_id
         AND mp.ended_at IS NULL
        JOIN public.telegram_users u ON u.id=p.owner_user_id
        LEFT JOIN public.platform_operators po
          ON po.telegram_user_id=u.telegram_user_id AND po.role='owner'
        WHERE b.status='registered' AND po.telegram_user_id IS NULL
        GROUP BY p.owner_user_id
      LOOP
        INSERT INTO public.platform_saas_commercial_enrollments(
          owner_user_id,effective_from
        ) VALUES(recovery_owner.owner_user_id,started)
        ON CONFLICT DO NOTHING;
        IF NOT EXISTS(
          SELECT 1 FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=recovery_owner.owner_user_id
            AND t.policy_key='vimi-saas-72h-v1' AND t.scope='owner'
        ) THEN
          SELECT m.id INTO recovery_merchant
          FROM public.merchant_identities m
          WHERE m.initial_owner_user_id=recovery_owner.owner_user_id
          ORDER BY m.created_at,m.id LIMIT 1;
          IF recovery_merchant IS NULL THEN
            recovery_merchant:=gen_random_uuid();
            INSERT INTO public.merchant_identities(
              id,initial_owner_user_id
            ) VALUES(recovery_merchant,recovery_owner.owner_user_id);
          END IF;
          INSERT INTO public.merchant_platform_trial_claims(
            id,merchant_id,owner_user_id,policy_key,scope,claimed_at
          ) VALUES(
            gen_random_uuid(),recovery_merchant,
            recovery_owner.owner_user_id,'vimi-saas-72h-v1','owner',
            greatest(recovery_owner.first_connected,started-interval '72 hours')
          ) ON CONFLICT DO NOTHING;
        END IF;
      END LOOP;
      -- Connected LIVE accounts only. The operator's own network is
      -- explicitly excluded. A bot/account can never be invoiced twice
      -- because the canonical issuer locks the bot and invoice_key.
      FOR bot IN
        SELECT b.id,b.created_at,p.owner_user_id,p.id AS project_id
        FROM public.bot_instances b
        JOIN public.projects p ON p.id=b.project_id
        JOIN public.telegram_users u ON u.id=p.owner_user_id
        LEFT JOIN public.platform_operators o
          ON o.telegram_user_id=u.telegram_user_id AND o.role='owner'
        WHERE b.status='registered' AND o.telegram_user_id IS NULL
        ORDER BY
          CASE WHEN progress.last_bot_id IS NOT NULL
            AND (b.created_at,b.id) <=
                (progress.last_bot_connected_at,progress.last_bot_id)
               THEN 1 ELSE 0 END,
          b.created_at,b.id LIMIT 100
      LOOP
        cursor_bot_at:=bot.created_at;
        cursor_bot_id:=bot.id;
        BEGIN
          SELECT min(claimed_at) INTO first_trial
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=bot.owner_user_id AND t.scope='owner'
            AND t.policy_key='vimi-saas-72h-v1';
          IF first_trial IS NULL THEN deferred:=deferred+1;CONTINUE;END IF;
          SELECT mp.started_at INTO owner_period_start
          FROM public.merchant_project_periods mp
          WHERE mp.project_id=bot.project_id
            AND mp.owner_user_id=bot.owner_user_id
            AND mp.ended_at IS NULL;
          IF owner_period_start IS NULL THEN
            deferred:=deferred+1;CONTINUE;
          END IF;
          -- A transferred bot must not inherit the previous owner's debt,
          -- but service periods of both owners must never overlap.
          SELECT max(i.period_end) INTO last_issued_end
          FROM public.platform_saas_invoices i
          WHERE i.bot_instance_id=bot.id AND i.kind='prepayment'
            AND i.status='issued';
          SELECT i.id,i.period_end,i.status
            INTO prior
          FROM public.platform_saas_invoices i
          WHERE i.bot_instance_id=bot.id AND i.kind='prepayment'
            AND i.status='issued'
            AND i.owner_user_id=bot.owner_user_id
            AND i.period_start>=owner_period_start
          ORDER BY i.period_end DESC LIMIT 1;
          IF prior.id IS NOT NULL THEN
            -- Do not stack future debts while the prior month's bill
            -- remains unpaid. Once paid, renew with a real calendar month.
            IF public.platform_saas_v2_remaining(prior.id)>0 THEN
              CONTINUE;
            END IF;
            period_start:=greatest(
              prior.period_end,now_at,bot.created_at,owner_period_start,
              coalesce(last_issued_end,now_at)
            );
          ELSE
            period_start:=greatest(
              started,first_trial+interval '72 hours',
              bot.created_at,owner_period_start,
              coalesce(last_issued_end,started)
            );
          END IF;
          IF period_start>now_at+interval '3 days' THEN CONTINUE; END IF;
          invoice:=public.platform_saas_issue_bot_invoice(
            bot.id,period_start,period_start+interval '1 month'
          );
          IF invoice IS NOT NULL THEN bot_issued:=bot_issued+1; END IF;
        EXCEPTION WHEN OTHERS THEN
          -- Audit-safe deferred invoice: never issue with uncertain
          -- owner, FX, source or exemption evidence. Retry later.
          deferred:=deferred+1;
        END;
      END LOOP;
      IF cursor_bot_id IS NOT NULL THEN
        UPDATE public.platform_saas_due_progress SET
          last_bot_connected_at=cursor_bot_at,
          last_bot_id=cursor_bot_id,updated_at=clock_timestamp()
        WHERE id=1;
      END IF;
      -- Closed Moscow calendar month, once per actual historical
      -- merchant+owner. Source completeness and FX are revalidated by
      -- the canonical 0068 issuer, which fails closed rather than
      -- issuing a partial Stars/Crypto invoice.
      previous_month:=date_trunc(
        'month',(now_at AT TIME ZONE 'Europe/Moscow')::timestamp
        -interval '1 month'
      )::date;
      -- Revisit ALL unsettled closed periods since commercial activation,
      -- not just the latest month. Late FX/Stars/Crypto evidence must
      -- eventually generate the missing bill without duplicate keys.
      FOR accounting IN
        SELECT q.merchant_id,q.owner_user_id,q.billing_month
        FROM (
          SELECT DISTINCT mp.merchant_id,mp.owner_user_id,
                 months.m::date AS billing_month
          FROM generate_series(
            date_trunc('month',public.platform_saas_first_activation()
                         AT TIME ZONE 'Europe/Moscow'),
            date_trunc('month',
                       (now_at AT TIME ZONE 'Europe/Moscow')
                       -interval '1 month'),
            interval '1 month'
          ) AS months(m)
          JOIN public.merchant_project_periods mp
            ON mp.started_at <
              ((months.m+interval '1 month') AT TIME ZONE 'Europe/Moscow')
           AND (mp.ended_at IS NULL OR mp.ended_at>=
                (months.m AT TIME ZONE 'Europe/Moscow'))
          JOIN public.telegram_users u ON u.id=mp.owner_user_id
          LEFT JOIN public.platform_operators po
            ON po.telegram_user_id=u.telegram_user_id AND po.role='owner'
          WHERE po.telegram_user_id IS NULL
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_invoices i
              WHERE i.invoice_key='commission:'||mp.merchant_id::text
                ||':'||mp.owner_user_id::text||':'
                ||to_char(months.m,'YYYY-MM-DD')
                AND i.status='issued'
            )
        ) q
        ORDER BY
          CASE WHEN progress.last_commission_month IS NOT NULL
            AND (q.billing_month,q.owner_user_id,q.merchant_id)<=
                (progress.last_commission_month,
                 progress.last_commission_owner_id,
                 progress.last_commission_merchant_id)
               THEN 1 ELSE 0 END,
          q.billing_month,q.owner_user_id,q.merchant_id
        LIMIT 100
      LOOP
        cursor_month:=accounting.billing_month;
        cursor_owner_id:=accounting.owner_user_id;
        cursor_merchant_id:=accounting.merchant_id;
        BEGIN
          invoice:=public.platform_saas_issue_commission_invoice(
            accounting.merchant_id,accounting.owner_user_id,
            accounting.billing_month,'{}'::jsonb
          );
          IF invoice IS NOT NULL THEN
            commission_issued:=commission_issued+1;
          END IF;
        EXCEPTION WHEN OTHERS THEN
          deferred:=deferred+1;
        END;
      END LOOP;
      IF cursor_month IS NOT NULL THEN
        UPDATE public.platform_saas_due_progress SET
          last_commission_month=cursor_month,
          last_commission_owner_id=cursor_owner_id,
          last_commission_merchant_id=cursor_merchant_id,
          updated_at=clock_timestamp() WHERE id=1;
      END IF;
      PERFORM public.platform_saas_reconcile_verified_refunds(NULL);
      RETURN jsonb_build_object(
        'enabled',true,'bot_invoices',bot_issued,
        'commission_invoices',commission_issued,'deferred',deferred
      );
    END; $f$
    """)
    # Independent operator bank confirmation must not over-allocate a bill
    # after a source-linked credit. Existing receipts are never rewritten.
    op.execute("""
      CREATE FUNCTION public.platform_saas_verified_receipt_guard()
      RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $fn$
      DECLARE due numeric;
      BEGIN
        SELECT public.platform_saas_v2_remaining(NEW.invoice_id) INTO due;
        IF NEW.amount_rub>due THEN
          RAISE EXCEPTION 'Bank receipt exceeds outstanding verified balance'
          USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END; $fn$
    """)
    op.execute("""
      CREATE TRIGGER trg_saas_verified_receipt_cap
      BEFORE INSERT ON public.platform_saas_receipts
      FOR EACH ROW EXECUTE FUNCTION public.platform_saas_verified_receipt_guard()
    """)
    op.execute("""REVOKE ALL ON FUNCTION
      public.platform_saas_verified_receipt_guard() FROM PUBLIC""")


def downgrade() -> None:
    raise RuntimeError("Never discard verified immutable refund allocations")
