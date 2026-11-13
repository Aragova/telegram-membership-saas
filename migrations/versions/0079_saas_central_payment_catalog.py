"""Central platform payment methods and exact-invoice method selection.

Revision ID: 0079_saas_payment_catalog
Revises: 0078_saas_test_void

The prior owner screen exposed only RUB 150 / USD 3 service profile toggles;
these are NOT payment methods. This adds a single central reusable payment
catalog with named instructions + currency links, immutable method-scoped
attempts, and the owner-scoped selector. Existing bank details are seeded
once and old immutable receipts/attempts are preserved. No billing switches
or buyer entitlements are modified.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079_saas_payment_catalog"
down_revision: str | None = "0078_saas_test_void"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VALID_CURRENCIES = "'RUB','USD','EUR','UAH','BYN'"

def upgrade() -> None:
    op.create_table(
        "platform_saas_payment_catalog_methods",
        sa.Column("id",sa.Uuid(),primary_key=True),
        sa.Column("name",sa.String(80),nullable=False),
        sa.Column("instructions",sa.Text(),nullable=False),
        sa.Column("enabled",sa.Boolean(),nullable=False,server_default=sa.true()),
        # Stable identity of an imported legacy method, even after rename.
        sa.Column("legacy_source",sa.String(16),unique=True),
        sa.Column("removed_at",sa.DateTime(timezone=True)),
        sa.Column("created_by",sa.Uuid()),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 2 AND 80",
                           name="ck_saas_method_name"),
        sa.CheckConstraint("length(btrim(instructions)) BETWEEN 8 AND 900",
                           name="ck_saas_method_instructions"),
    )
    op.create_table(
        "platform_saas_payment_catalog_currencies",
        sa.Column("method_id",sa.Uuid(),sa.ForeignKey(
            "platform_saas_payment_catalog_methods.id",ondelete="RESTRICT"
        ),primary_key=True),
        sa.Column("currency_code",sa.String(3),primary_key=True),
        sa.CheckConstraint(
            f"currency_code IN ({VALID_CURRENCIES})",
            name="ck_saas_catalog_currency",
        ),
    )
    for table in (
        "platform_saas_payment_catalog_methods",
        "platform_saas_payment_catalog_currencies",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
    op.add_column("platform_saas_payment_attempts_v2",
        sa.Column("method_id",sa.Uuid(),sa.ForeignKey(
            "platform_saas_payment_catalog_methods.id",ondelete="RESTRICT"
        )))
    # One-time import from current central one-per-currency entries.
    # Stale disabled entries stay disabled rather than resurrecting payments.
    op.execute("""
      WITH legacy AS (
        INSERT INTO public.platform_saas_payment_catalog_methods(
          id,name,instructions,enabled,created_by,legacy_source
        )
        SELECT gen_random_uuid(),
          ('💳 '||currency_code)::varchar(80),instructions,
          enabled,updated_by,'v2:'||currency_code
        FROM public.platform_saas_payment_methods_v2
        RETURNING id,name
      )
      INSERT INTO public.platform_saas_payment_catalog_currencies(
        method_id,currency_code
      )
      SELECT id,right(name,3) FROM legacy
    """)
    op.execute("""
      WITH legacy AS (
        INSERT INTO public.platform_saas_payment_catalog_methods(
          id,name,instructions,enabled,legacy_source
        )
        SELECT gen_random_uuid(),'💳 RUB',d.instructions,true,'bank:RUB'
        FROM public.platform_saas_payment_details d
        WHERE d.id=1 AND d.instructions IS NOT NULL
          AND length(btrim(d.instructions)) BETWEEN 8 AND 900
          AND NOT EXISTS(
            SELECT 1 FROM public.platform_saas_payment_methods_v2
            WHERE currency_code='RUB'
          )
        RETURNING id
      )
      INSERT INTO public.platform_saas_payment_catalog_currencies(
        method_id,currency_code
      )
      SELECT id,'RUB' FROM legacy
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_catalog_read()
      RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      DECLARE result jsonb;
      BEGIN
        PERFORM public.platform_saas_require_owner();
        SELECT coalesce(jsonb_agg(jsonb_build_object(
          'id',m.id,'name',m.name,'instructions',m.instructions,
          'enabled',m.enabled,'currencies',coalesce(
             (SELECT jsonb_agg(c.currency_code ORDER BY c.currency_code)
              FROM public.platform_saas_payment_catalog_currencies c
              WHERE c.method_id=m.id),'[]'::jsonb)
        ) ORDER BY m.created_at,m.id),'[]'::jsonb) INTO result
        FROM public.platform_saas_payment_catalog_methods m
        WHERE m.removed_at IS NULL;
        RETURN result;
      END; $f$
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_catalog_create(
         p_name text,p_instructions text,p_currency text
      ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      DECLARE actor uuid; new_id uuid;
      BEGIN
        actor:=public.platform_saas_require_owner();
        IF p_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
          OR length(btrim(p_name)) NOT BETWEEN 2 AND 80
          OR length(btrim(p_instructions)) NOT BETWEEN 8 AND 900 THEN
          RAISE EXCEPTION 'Invalid platform method'
            USING ERRCODE='22023';
        END IF;
        new_id:=gen_random_uuid();
        INSERT INTO public.platform_saas_payment_catalog_methods(
          id,name,instructions,created_by
        ) VALUES(new_id,btrim(p_name),btrim(p_instructions),actor);
        INSERT INTO public.platform_saas_payment_catalog_currencies(
          method_id,currency_code
        ) VALUES(new_id,p_currency);
        RETURN new_id;
      END; $f$
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_catalog_update(
         p_id uuid,p_name text,p_instructions text
      ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      BEGIN
        PERFORM public.platform_saas_require_owner();
        IF length(btrim(p_name)) NOT BETWEEN 2 AND 80
          OR length(btrim(p_instructions)) NOT BETWEEN 8 AND 900 THEN
          RAISE EXCEPTION 'Invalid method edits' USING ERRCODE='22023';
        END IF;
        UPDATE public.platform_saas_payment_catalog_methods
        SET name=btrim(p_name),instructions=btrim(p_instructions),
            updated_at=clock_timestamp()
        WHERE id=p_id AND removed_at IS NULL;
        IF NOT FOUND THEN
          RAISE EXCEPTION 'Method no longer exists' USING ERRCODE='42501';
        END IF;
        RETURN true;
      END; $f$
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_catalog_currency_set(
         p_id uuid,p_currency text,p_enabled boolean
      ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      BEGIN
        PERFORM public.platform_saas_require_owner();
        IF p_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
           OR NOT EXISTS(
             SELECT 1 FROM public.platform_saas_payment_catalog_methods
             WHERE id=p_id AND removed_at IS NULL
           ) THEN
          RAISE EXCEPTION 'Invalid method/currency' USING ERRCODE='42501';
        END IF;
        IF p_enabled THEN
          INSERT INTO public.platform_saas_payment_catalog_currencies(
            method_id,currency_code
          ) VALUES(p_id,p_currency) ON CONFLICT DO NOTHING;
        ELSE
          DELETE FROM public.platform_saas_payment_catalog_currencies
          WHERE method_id=p_id AND currency_code=p_currency;
        END IF;
        RETURN true;
      END; $f$
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_catalog_toggle(
        p_id uuid,p_enabled boolean
      ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      BEGIN
        PERFORM public.platform_saas_require_owner();
        UPDATE public.platform_saas_payment_catalog_methods
        SET enabled=p_enabled,updated_at=clock_timestamp()
        WHERE id=p_id AND removed_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'Method not found'
          USING ERRCODE='42501'; END IF;
        RETURN true;
      END; $f$
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_catalog_remove(
        p_id uuid
      ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      BEGIN
        PERFORM public.platform_saas_require_owner();
        UPDATE public.platform_saas_payment_catalog_methods
        SET enabled=false,removed_at=clock_timestamp(),
            updated_at=clock_timestamp()
        WHERE id=p_id AND removed_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'Method not found'
          USING ERRCODE='42501'; END IF;
        RETURN true;
      END; $f$
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_catalog_for_invoice(
        p_invoice uuid
      ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      DECLARE actor uuid; answer jsonb;
      BEGIN
        actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
        IF actor IS NULL OR NOT EXISTS(
            SELECT 1 FROM public.platform_saas_invoices i
            WHERE i.id=p_invoice AND i.owner_user_id=actor
              AND i.status='issued' AND
              public.platform_saas_v2_remaining(i.id)>0
        ) THEN
          RAISE EXCEPTION 'Invoice not payable by this owner'
            USING ERRCODE='42501';
        END IF;
        SELECT coalesce(jsonb_agg(jsonb_build_object(
          'id',m.id,'name',m.name,
          'currency',c.currency_code
        ) ORDER BY c.currency_code,m.name,m.id),'[]'::jsonb)
          INTO answer
        FROM public.platform_saas_payment_catalog_methods m
        JOIN public.platform_saas_payment_catalog_currencies c
        ON c.method_id=m.id
        WHERE m.enabled AND m.removed_at IS NULL;
        RETURN answer;
      END; $f$
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v3_create_attempt(
      p_invoice uuid,p_requested_rub numeric,p_currency text,p_method uuid
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
      method_label text;
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
      -- The chosen method and currency must still be active, and the
      -- immutable attempt snapshots the exact central method instructions.
      SELECT m.instructions,m.name INTO bank,method_label
      FROM public.platform_saas_payment_catalog_methods m
      JOIN public.platform_saas_payment_catalog_currencies c
        ON c.method_id=m.id AND c.currency_code=p_currency
      WHERE m.id=p_method AND m.enabled AND m.removed_at IS NULL
      FOR SHARE OF m;
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
        instructions_snapshot,expires_at,method_id
      ) VALUES(
        attempt,item.id,actor,snapped_rub,p_currency,rate,
        converted,day,fx_day,bank,deadline,p_method
      );
      RETURN jsonb_build_object(
        'id',attempt,'invoice_id',item.id,'amount_rub',snapped_rub::text,
        'currency',p_currency,'amount',converted::text,
        'rub_per_unit',rate::text,'quote_date',day,
        'source_date',fx_day,'expires_at',deadline,
        'instructions',bank,'method_name',method_label,
        'method_id',p_method,'remaining_before',due::text
      );
    END; $f$
    """)
    # Old, already-sent Telegram keyboards and historical API clients cannot
    # bypass a disabled or edited payment method. Keep v2 methods in sync
    # only for backwards-compatibility, not as a second visible catalog.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_v2_bank_method(
       p_currency text,p_instructions text
    ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE actor uuid; match_id uuid;
    BEGIN
      actor:=public.platform_saas_require_owner();
      IF p_currency NOT IN ('RUB','USD','EUR','UAH','BYN')
         OR (p_instructions IS NOT NULL AND
             length(btrim(p_instructions)) NOT BETWEEN 8 AND 900) THEN
        RAISE EXCEPTION 'Invalid central bank instructions'
          USING ERRCODE='22023';
      END IF;
      IF p_instructions IS NULL THEN
        UPDATE public.platform_saas_payment_methods_v2
        SET enabled=false,updated_at=clock_timestamp(),updated_by=actor
        WHERE currency_code=p_currency;
      ELSE
        INSERT INTO public.platform_saas_payment_methods_v2(
          currency_code,instructions,enabled,updated_by
        ) VALUES(p_currency,btrim(p_instructions),true,actor)
        ON CONFLICT(currency_code) DO UPDATE SET
          instructions=excluded.instructions,enabled=true,
          updated_by=actor,updated_at=clock_timestamp();
      END IF;
      -- Keep old operator/API entry points synchronized with the catalog.
      -- Previously issued attempts still retain their exact old snapshot.
      SELECT m.id INTO match_id
      FROM public.platform_saas_payment_catalog_methods m
      JOIN public.platform_saas_payment_catalog_currencies c
        ON c.method_id=m.id AND c.currency_code=p_currency
      WHERE m.removed_at IS NULL
        AND (m.legacy_source='v2:'||p_currency
          OR (p_currency='RUB' AND m.legacy_source='bank:RUB'))
      ORDER BY CASE WHEN m.legacy_source='v2:'||p_currency
                    THEN 0 ELSE 1 END
      LIMIT 1 FOR UPDATE OF m;
      IF p_instructions IS NULL THEN
        IF match_id IS NOT NULL THEN
          UPDATE public.platform_saas_payment_catalog_methods
          SET enabled=false,legacy_source='v2:'||p_currency,
              updated_at=clock_timestamp()
          WHERE id=match_id;
        END IF;
      ELSIF match_id IS NOT NULL THEN
        UPDATE public.platform_saas_payment_catalog_methods
        SET instructions=btrim(p_instructions),enabled=true,
            legacy_source='v2:'||p_currency,
            updated_at=clock_timestamp() WHERE id=match_id;
      ELSE
        match_id:=gen_random_uuid();
        INSERT INTO public.platform_saas_payment_catalog_methods(
          id,name,instructions,enabled,created_by,legacy_source
        ) VALUES(
          match_id,'💳 '||p_currency,btrim(p_instructions),true,actor,
          'v2:'||p_currency
        );
        INSERT INTO public.platform_saas_payment_catalog_currencies(
          method_id,currency_code
        ) VALUES(match_id,p_currency);
      END IF;
      RETURN true;
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
      -- No obsolete legacy bank message may bypass operator decisions
      -- made in the authoritative named-method catalog. Old callbacks
      -- normally reroute to the new selector, and this DB guard protects
      -- any stale RPC client that still attempts the old flow.
      IF NOT EXISTS(
        SELECT 1 FROM public.platform_saas_payment_catalog_methods m
        JOIN public.platform_saas_payment_catalog_currencies c
          ON c.method_id=m.id AND c.currency_code=p_currency
        WHERE m.enabled AND m.removed_at IS NULL
          AND m.instructions=bank
      ) THEN
        RAISE EXCEPTION 'Legacy bank method no longer available'
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
    # The legacy bank edit RPC may still be invoked by older clients.
    # Update its single imported method by stable provenance (not mutable name).
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_payment_details_set(p_text text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $fn$
DECLARE actor uuid;
BEGIN
  actor:=public.platform_saas_require_owner();
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
  IF NOT FOUND THEN RETURN false; END IF;
  -- Stale legacy calls cannot silently leave a previously imported RUB
  -- method advertising old data. Only the original bank import is touched.
  -- A catalog-disabled method is NEVER implicitly re-enabled by an old edit.
  UPDATE public.platform_saas_payment_catalog_methods
    SET instructions=CASE WHEN p_text IS NULL THEN instructions
                          ELSE btrim(p_text) END,
        enabled=enabled AND p_text IS NOT NULL,
        updated_at=clock_timestamp()
  WHERE legacy_source='bank:RUB' AND removed_at IS NULL;
  RETURN true;
END; $fn$
    """)
    # Both scheduling and claiming use the authoritative catalog. There
    # must be no reminders after all named methods are disabled/archived.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_notice_schedule()
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
          -- Refund offsets are sourced before any due/overdue notice.
          IF started IS NOT NULL AND started<=now_at THEN
            PERFORM public.platform_saas_reconcile_verified_refunds(NULL);
            started:=coalesce(public.platform_saas_first_activation(),started);
          END IF;
          -- Payment reminders must use the same live catalog as checkout.
          IF started IS NULL OR started>now_at
              OR NOT EXISTS (
                SELECT 1 FROM public.platform_saas_payment_catalog_methods m
                JOIN public.platform_saas_payment_catalog_currencies c
                  ON c.method_id=m.id
                WHERE m.enabled AND m.removed_at IS NULL
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
            AND public.platform_saas_v2_remaining(i.id)>0
          ON CONFLICT (invoice_id,kind) DO NOTHING;
          GET DIAGNOSTICS notice_count = ROW_COUNT;
          UPDATE public.platform_saas_notice_outbox n
            SET status='cancelled'
          FROM public.platform_saas_invoices i
          WHERE n.invoice_id=i.id AND n.status='pending'
            AND (
              i.status<>'issued' OR public.platform_saas_v2_remaining(i.id)=0
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
CREATE OR REPLACE FUNCTION public.platform_saas_notice_claim()
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          item record;
          now_at timestamptz := clock_timestamp();
        BEGIN
          PERFORM public.platform_saas_require_owner();
          -- Reminders link back to the buyer's actual account checkout,
          -- so no obsolete bank instructions are ever sent in notices.
          IF NOT EXISTS (
              SELECT 1 FROM public.platform_saas_payment_catalog_methods m
              JOIN public.platform_saas_payment_catalog_currencies c
                ON c.method_id=m.id
              WHERE m.enabled AND m.removed_at IS NULL
            ) OR NOT EXISTS (
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
          IF item.status<>'issued' OR public.platform_saas_v2_remaining(item.invoice_id)=0
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
            'amount_rub',public.platform_saas_v2_remaining(item.invoice_id)::text,
            'due_at',item.due_at
          );
        END;
        $fn$
    """)
    signatures = [
        "platform_saas_catalog_read()",
        "platform_saas_catalog_create(text,text,text)",
        "platform_saas_catalog_update(uuid,text,text)",
        "platform_saas_catalog_currency_set(uuid,text,boolean)",
        "platform_saas_catalog_toggle(uuid,boolean)",
        "platform_saas_catalog_remove(uuid)",
        "platform_saas_catalog_for_invoice(uuid)",
        "platform_saas_v3_create_attempt(uuid,numeric,text,uuid)",
    ]
    for signature in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"""DO $grant$
          BEGIN
            IF EXISTS(SELECT 1 FROM pg_roles
              WHERE rolname='membership_runtime') THEN
              GRANT EXECUTE ON FUNCTION public.{signature}
              TO membership_runtime;
            END IF;
          END $grant$""")


def downgrade() -> None:
    raise RuntimeError(
        "Cannot downgrade immutable SaaS payment method snapshots"
    )
