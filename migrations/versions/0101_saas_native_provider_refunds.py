"""Immutable native USD refund evidence for platform provider settlements.

Revision ID: 0101_saas_native_provider_refunds
Revises: 0100_saas_native_gateway

A verified provider refund in USD is translated into the exact corresponding
book-RUB liability under the same invoice/charge lock and recorded separately
in immutable native evidence. Operator command is NOT a refund API.
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0101_saas_native_provider_refunds"
down_revision: str | None = "0100_saas_native_gateway"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE public.platform_saas_gateway_refund_native_evidence(
  refund_id uuid PRIMARY KEY REFERENCES public.platform_saas_gateway_refunds(id),
  currency text NOT NULL CHECK(currency IN ('RUB','USD')),
  amount_native numeric(38,2) NOT NULL CHECK(amount_native>0),
  amount_book_rub numeric(38,2) NOT NULL CHECK(amount_book_rub>0),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
)
    """)
    op.execute("""
ALTER TABLE public.platform_saas_gateway_refund_native_evidence
 ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
ALTER TABLE public.platform_saas_gateway_refund_native_evidence
 FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
REVOKE ALL ON TABLE public.platform_saas_gateway_refund_native_evidence
 FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
  REVOKE ALL ON TABLE public.platform_saas_gateway_refund_native_evidence
   FROM membership_runtime;
 END IF;
END $r$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_native_refund_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
BEGIN
 RAISE EXCEPTION 'Native provider refund evidence is immutable'
  USING ERRCODE='42501';
END; $f$
    """)
    op.execute("""
CREATE TRIGGER trg_saas_gateway_native_refund_immutable
BEFORE UPDATE OR DELETE ON public.platform_saas_gateway_refund_native_evidence
FOR EACH ROW EXECUTE FUNCTION
 public.platform_saas_gateway_native_refund_immutable()
    """)
    op.execute("""
REVOKE ALL ON FUNCTION
 public.platform_saas_gateway_native_refund_immutable() FROM PUBLIC
    """)
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_gateway_operator_refund(
 p_provider text,p_provider_invoice_id text,p_refund_id text,
 p_amount_rub numeric
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE pay_ccy text; target_order uuid; inserted boolean; verified record;
BEGIN
 PERFORM public.platform_saas_require_owner();
 SELECT o.id,o.pay_currency INTO target_order,pay_ccy
 FROM public.platform_saas_gateway_charges c
 JOIN public.platform_saas_gateway_orders o ON o.id=c.order_id
 WHERE c.provider=p_provider AND c.provider_invoice_id=p_provider_invoice_id;
 IF pay_ccy='USD' THEN
   RAISE EXCEPTION 'Use native USD amount for provider refund'
      USING ERRCODE='22023';
 END IF;
 IF target_order IS NULL THEN
   RAISE EXCEPTION 'No verified provider charge' USING ERRCODE='42501';
 END IF;
 inserted:=public.platform_saas_gateway_record_refund(
  target_order,p_provider,p_provider_invoice_id,
  p_refund_id,p_amount_rub,clock_timestamp()
 );
 SELECT r.* INTO verified FROM public.platform_saas_gateway_refunds r
 WHERE r.provider=p_provider AND r.provider_refund_id=p_refund_id;
 RETURN jsonb_build_object(
  'recorded',inserted,'amount_rub',verified.amount_rub::text,
  'credit_reversed_rub',verified.reversed_rub::text
 );
END; $f$
    """)
    op.execute("""
CREATE FUNCTION public.platform_saas_gateway_operator_refund_native(
 p_provider text,p_provider_invoice_id text,p_refund_id text,
 p_amount_native numeric,p_currency text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE o record; c record; prev_native numeric; prev_book numeric;
        refunded_native numeric; refunded_book numeric;
        book_amount numeric; inserted boolean; verified record; evidence record;
BEGIN
 PERFORM public.platform_saas_require_owner();
 IF p_provider NOT IN ('crypto','lava') OR p_currency<>'USD'
   OR p_provider_invoice_id IS NULL
   OR length(p_provider_invoice_id) NOT BETWEEN 1 AND 128
   OR p_refund_id IS NULL OR length(p_refund_id) NOT BETWEEN 1 AND 128
   OR p_amount_native IS NULL OR p_amount_native<=0
   OR p_amount_native<>round(p_amount_native,2) THEN
  RAISE EXCEPTION 'Invalid native USD refund evidence'
   USING ERRCODE='22023';
 END IF;
 SELECT o.* INTO o FROM public.platform_saas_gateway_orders o
 WHERE o.provider=p_provider
   AND o.provider_invoice_id=p_provider_invoice_id;
 IF o.id IS NULL OR o.pay_currency<>'USD' THEN
  RAISE EXCEPTION 'Original USD paid provider order required'
   USING ERRCODE='42501';
 END IF;
 -- Check and book under original invoice lock, the same ordering as
 -- record_refund. This serializes with other refund evidence.
 PERFORM 1 FROM public.platform_saas_invoices i
 WHERE i.id=o.invoice_id FOR UPDATE;
 SELECT * INTO c FROM public.platform_saas_gateway_charges c
 WHERE c.order_id=o.id AND c.provider=p_provider
   AND c.provider_invoice_id=p_provider_invoice_id FOR UPDATE;
 IF c.id IS NULL THEN
  RAISE EXCEPTION 'Original paid provider receipt required'
   USING ERRCODE='42501';
 END IF;
 SELECT r.*,n.amount_native,n.currency AS native_ccy INTO verified
 FROM public.platform_saas_gateway_refunds r
 LEFT JOIN public.platform_saas_gateway_refund_native_evidence n
  ON n.refund_id=r.id
 WHERE r.provider=p_provider AND r.provider_refund_id=p_refund_id;
 IF verified.id IS NOT NULL THEN
  IF verified.charge_id<>c.id OR verified.amount_native IS DISTINCT FROM
    p_amount_native OR verified.native_ccy IS DISTINCT FROM p_currency THEN
   RAISE EXCEPTION 'Conflicting native provider refund replay'
     USING ERRCODE='23505';
  END IF;
  RETURN jsonb_build_object(
    'recorded',false,'currency',p_currency,
    'amount_native',verified.amount_native::text,
    'amount_rub',verified.amount_rub::text,
    'credit_reversed_rub',verified.reversed_rub::text
  );
 END IF;
 SELECT coalesce(sum(n.amount_native),0),coalesce(sum(n.amount_book_rub),0)
 INTO prev_native,prev_book
 FROM public.platform_saas_gateway_refunds r
 JOIN public.platform_saas_gateway_refund_native_evidence n
   ON n.refund_id=r.id
 WHERE r.charge_id=c.id;
 SELECT coalesce(sum(r.amount_rub),0) INTO refunded_book
 FROM public.platform_saas_gateway_refunds r WHERE r.charge_id=c.id;
 -- Do not guess how old RUB-only evidence maps to the provider's USD
 -- refunds. Operator reconciliation is required before proceeding.
 IF refunded_book<>prev_book THEN
  RAISE EXCEPTION 'Legacy refund requires native reconciliation'
    USING ERRCODE='42501';
 END IF;
 IF prev_native+p_amount_native>o.pay_amount THEN
  RAISE EXCEPTION 'USD refund exceeds original provider charge'
    USING ERRCODE='23514';
 END IF;
 IF prev_native+p_amount_native=o.pay_amount THEN
  book_amount:=c.amount_rub-prev_book;
 ELSE
  book_amount:=round(c.amount_rub*p_amount_native/o.pay_amount,2);
 END IF;
 IF book_amount<=0 OR book_amount>c.amount_rub-prev_book THEN
  RAISE EXCEPTION 'Native refund cannot be allocated exactly'
   USING ERRCODE='23514';
 END IF;
 inserted:=public.platform_saas_gateway_record_refund(
  o.id,p_provider,p_provider_invoice_id,p_refund_id,
  book_amount,clock_timestamp()
 );
 IF inserted IS DISTINCT FROM true THEN
   RAISE EXCEPTION 'Unexpected native refund replay' USING ERRCODE='23505';
 END IF;
 SELECT r.* INTO verified FROM public.platform_saas_gateway_refunds r
 WHERE r.provider=p_provider AND r.provider_refund_id=p_refund_id;
 INSERT INTO public.platform_saas_gateway_refund_native_evidence(
   refund_id,currency,amount_native,amount_book_rub)
 VALUES(verified.id,p_currency,p_amount_native,book_amount);
 RETURN jsonb_build_object(
   'recorded',true,'currency',p_currency,
   'amount_native',p_amount_native::text,
   'amount_rub',verified.amount_rub::text,
   'credit_reversed_rub',verified.reversed_rub::text
 );
END; $f$
    """)
    op.execute("""
REVOKE ALL ON FUNCTION
 public.platform_saas_gateway_operator_refund_native(
  text,text,text,numeric,text) FROM PUBLIC
    """)
    op.execute("""
DO $r$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
  GRANT EXECUTE ON FUNCTION
    public.platform_saas_gateway_operator_refund_native(
     text,text,text,numeric,text)
  TO membership_runtime;
 END IF;
END $r$
    """)


def downgrade() -> None:
    raise RuntimeError("Native provider refund financial evidence is irreversible")
