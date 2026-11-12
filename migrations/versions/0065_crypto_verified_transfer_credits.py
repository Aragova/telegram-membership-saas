"""Original-app verified Crypto Pay transfer credits, audit-only.

Revision ID: 0065_crypto_verified_transfer_credits
Revises: 0064_crypto_sale_usd_evidence

No payouts, invoice netting, SaaS billing activation or buyer controls.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_crypto_verified_transfer_credits"
down_revision: str | None = "0064_crypto_sale_usd_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saas_crypto_transfer_credits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("original_sale_id", sa.Uuid(), sa.ForeignKey(
            "platform_saas_crypto_sales.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("provider_transfer_id", sa.BigInteger(), nullable=False),
        sa.Column("spend_id", sa.String(100), nullable=False),
        sa.Column("recipient_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("asset_code", sa.String(16), nullable=False),
        sa.Column("asset_amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("original_usd_per_unit", sa.Numeric(38, 18), nullable=False),
        sa.Column("gross_credit_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("commission_credit_usd", sa.Numeric(38, 18), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_by", sa.Uuid(), sa.ForeignKey(
            "telegram_users.id", ondelete="RESTRICT",
        ), nullable=False),
        sa.Column("evidence_source", sa.String(40), nullable=False,
                  server_default="cryptobot_getTransfers"),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.UniqueConstraint(
            "app_id", "provider_transfer_id",
            name="uq_saas_crypto_transfer_id_per_app",
        ),
        sa.UniqueConstraint(
            "app_id", "spend_id",
            name="uq_saas_crypto_transfer_spend_per_app",
        ),
        sa.CheckConstraint(
            "provider_transfer_id>0 AND recipient_telegram_id>0 "
            "AND asset_amount>0 AND original_usd_per_unit>0 "
            "AND gross_credit_usd>0 AND commission_credit_usd>=0",
            name="ck_saas_crypto_refund_positive",
        ),
        sa.CheckConstraint(
            "evidence_source='cryptobot_getTransfers'",
            name="ck_saas_crypto_refund_source",
        ),
    )
    op.create_index(
        "ix_saas_crypto_transfer_original",
        "platform_saas_crypto_transfer_credits", ["original_sale_id"],
    )
    op.create_index(
        "ix_saas_crypto_transfer_owner",
        "platform_saas_crypto_transfer_credits",
        ["owner_user_id", "completed_at"],
    )
    op.execute(
        "ALTER TABLE public.platform_saas_crypto_transfer_credits "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_crypto_transfer_credits "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
        CREATE TRIGGER trg_saas_crypto_transfer_immutable
        BEFORE UPDATE OR DELETE
        ON public.platform_saas_crypto_transfer_credits
        FOR EACH ROW EXECUTE FUNCTION
          public.platform_saas_crypto_evidence_immutable()
    """)
    op.execute("""
        DO $role$
        BEGIN
          IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime') THEN
            REVOKE ALL ON public.platform_saas_crypto_transfer_credits
              FROM membership_runtime;
          END IF;
        END $role$
    """)
    # The probe leaks only the minimum original identity to the configured
    # platform operator. It does not return or decrypt merchant credentials.
    op.execute("""
        CREATE FUNCTION public.platform_saas_crypto_refund_candidate(
          p_sale uuid, p_spend_id text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE original record;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          SELECT s.*, i.telegram_user_id, i.status AS payment_status,
                 i.paid_at AS invoice_paid_at, a.environment
            INTO original
          FROM public.platform_saas_crypto_sales s
          JOIN public.crypto_pay_invoices i
            ON i.id=s.invoice_id AND i.project_id=s.project_id
               AND i.app_id=s.app_id
          JOIN public.crypto_pay_apps a
            ON a.id=s.app_id AND a.project_id=s.project_id
          WHERE s.id=p_sale;
          IF original.id IS NULL OR original.payment_status<>'paid'
             OR original.invoice_paid_at IS DISTINCT FROM original.paid_at
             OR original.currency_type<>'crypto'
             OR original.environment<>'mainnet'
             OR p_spend_id IS NULL
             OR p_spend_id !~ ('^vimi-refund:'||
                   original.invoice_id::text||':[0-9a-f]{8,16}$')
          THEN
            RAISE EXCEPTION 'No verifiable Crypto Pay refund candidate'
              USING ERRCODE='42501';
          END IF;
          RETURN jsonb_build_object(
            'sale_id',original.id,'invoice_id',original.invoice_id,
            'project_id',original.project_id,'app_id',original.app_id,
            'bot_id',original.telegram_bot_id,
            'buyer_id',original.telegram_user_id,
            'asset',original.currency_code,
            'original_paid_at',original.paid_at
          );
        END $f$
    """)
    # This operator-only recording RPC must be called AFTER the trusted
    # service verifies original merchant getTransfers. Provider transfer is
    # not itself proof that the original order was paid (0064 already is).
    # Source-row lock serializes any two partial credits and ensures a hard
    # cap: total credited asset/fee can never exceed the original sale.
    op.execute("""
        CREATE FUNCTION public.platform_saas_record_crypto_transfer_credit(
          p_sale uuid,p_transfer_id bigint,p_spend_id text,
          p_recipient bigint,p_asset text,p_amount numeric,
          p_completed_at timestamptz
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          actor uuid;
          original record;
          previous record;
          used_amount numeric;
          used_gross numeric;
          used_fee numeric;
          credited_gross numeric;
          credited_fee numeric;
          new_id uuid;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF p_transfer_id IS NULL OR p_transfer_id<=0
             OR p_recipient IS NULL OR p_recipient<=0
             OR p_asset IS NULL OR p_amount IS NULL OR p_amount<=0
             OR round(p_amount,18)<>p_amount
             OR p_completed_at IS NULL OR p_completed_at>clock_timestamp()+interval '5 minutes'
          THEN
            RAISE EXCEPTION 'Invalid Crypto Pay transfer credit'
              USING ERRCODE='22023';
          END IF;
          SELECT s.*,i.telegram_user_id,i.status AS payment_status,
                 i.paid_at AS invoice_paid_at,a.environment
          INTO original
          FROM public.platform_saas_crypto_sales s
          JOIN public.crypto_pay_invoices i
            ON i.id=s.invoice_id AND i.project_id=s.project_id
               AND i.app_id=s.app_id
          JOIN public.crypto_pay_apps a
            ON a.id=s.app_id AND a.project_id=s.project_id
          WHERE s.id=p_sale FOR UPDATE OF s;
          IF original.id IS NULL OR original.currency_type<>'crypto'
             OR original.environment<>'mainnet'
             OR original.payment_status<>'paid'
             OR original.invoice_paid_at IS DISTINCT FROM original.paid_at
             OR original.telegram_user_id IS DISTINCT FROM p_recipient
             OR original.currency_code IS DISTINCT FROM p_asset
             OR p_completed_at<original.paid_at
             OR p_spend_id IS NULL
             OR p_spend_id !~ ('^vimi-refund:'||
                    original.invoice_id::text||':[0-9a-f]{8,16}$')
          THEN
            RAISE EXCEPTION 'Transfer does not match original paid purchase'
              USING ERRCODE='42501';
          END IF;
          SELECT * INTO previous
          FROM public.platform_saas_crypto_transfer_credits c
          WHERE c.app_id=original.app_id AND
            (c.provider_transfer_id=p_transfer_id OR c.spend_id=p_spend_id);
          IF FOUND THEN
            IF previous.original_sale_id IS DISTINCT FROM p_sale
               OR previous.provider_transfer_id IS DISTINCT FROM p_transfer_id
               OR previous.spend_id IS DISTINCT FROM p_spend_id
               OR previous.recipient_telegram_id IS DISTINCT FROM p_recipient
               OR previous.asset_code IS DISTINCT FROM p_asset
               OR previous.asset_amount IS DISTINCT FROM p_amount
               OR previous.completed_at IS DISTINCT FROM p_completed_at
            THEN
              RAISE EXCEPTION 'Conflicting Crypto Pay transfer replay'
                USING ERRCODE='23505';
            END IF;
            RETURN previous.id;
          END IF;
          SELECT coalesce(sum(asset_amount),0),
                 coalesce(sum(gross_credit_usd),0),
                 coalesce(sum(commission_credit_usd),0)
          INTO used_amount,used_gross,used_fee
          FROM public.platform_saas_crypto_transfer_credits c
          WHERE c.original_sale_id=original.id;
          IF used_amount+p_amount>original.original_amount THEN
            RAISE EXCEPTION 'Crypto Pay refund exceeds original purchase'
              USING ERRCODE='23514';
          END IF;
          IF used_amount+p_amount=original.original_amount THEN
            -- Last partial refund receives exact remaining fixed-point
            -- balance; repeated independent rounding cannot overcredit.
            credited_gross:=original.gross_usd-used_gross;
            credited_fee:=original.commission_usd-used_fee;
          ELSE
            credited_gross:=round(p_amount*original.usd_per_unit,18);
            credited_fee:=round(credited_gross*0.05,18);
          END IF;
          IF credited_gross<=0 OR credited_fee<0
             OR credited_gross>original.gross_usd-used_gross
             OR credited_fee>original.commission_usd-used_fee THEN
            RAISE EXCEPTION 'Crypto Pay refund precision is not billable'
              USING ERRCODE='23514';
          END IF;
          new_id:=gen_random_uuid();
          INSERT INTO public.platform_saas_crypto_transfer_credits(
            id,original_sale_id,invoice_id,project_id,app_id,owner_user_id,
            provider_transfer_id,spend_id,recipient_telegram_id,asset_code,
            asset_amount,original_usd_per_unit,gross_credit_usd,
            commission_credit_usd,completed_at,verified_by
          ) VALUES(
            new_id,original.id,original.invoice_id,original.project_id,
            original.app_id,original.owner_user_id,p_transfer_id,
            p_spend_id,p_recipient,p_asset,p_amount,original.usd_per_unit,
            credited_gross,credited_fee,p_completed_at,actor
          );
          RETURN new_id;
        END $f$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_crypto_refund_summary(
          p_project uuid,p_month date
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $f$
        DECLARE
          actor uuid;
          started timestamptz;
          ended timestamptz;
          n integer;
          gross numeric;
          fee numeric;
          billable numeric;
        BEGIN
          actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF actor IS NULL OR p_project IS NULL OR p_month IS NULL
             OR extract(day from p_month)<>1
             OR NOT EXISTS(
               SELECT 1 FROM public.projects p
               WHERE p.id=p_project AND p.owner_user_id=actor
             ) THEN
            RAISE EXCEPTION 'Crypto Pay refund report requires project owner'
              USING ERRCODE='42501';
          END IF;
          started:=p_month::timestamp AT TIME ZONE 'Europe/Moscow';
          ended:=(p_month::timestamp+interval '1 month')
                    AT TIME ZONE 'Europe/Moscow';
          SELECT count(*),coalesce(sum(c.gross_credit_usd),0),
                 coalesce(sum(c.commission_credit_usd),0),
                 coalesce(sum(c.commission_credit_usd)
                   FILTER(WHERE s.billing_eligible),0)
          INTO n,gross,fee,billable
          FROM public.platform_saas_crypto_transfer_credits c
          JOIN public.platform_saas_crypto_sales s
            ON s.id=c.original_sale_id
          WHERE c.project_id=p_project AND c.owner_user_id=actor
            AND c.completed_at>=started AND c.completed_at<ended;
          RETURN jsonb_build_object(
            'project_id',p_project,'refund_month',p_month,'refunds',n,
            'gross_credit_usd',gross::text,
            'commission_credit_usd',fee::text,
            'eligible_commission_credit_usd',billable::text,
            'applied_to_invoices',false,'preview_only',true,
            'source','original_app_getTransfers'
          );
        END $f$
    """)
    for signature in (
        "platform_saas_crypto_refund_candidate(uuid,text)",
        "platform_saas_record_crypto_transfer_credit("
            "uuid,bigint,text,bigint,text,numeric,timestamptz)",
        "platform_saas_crypto_refund_summary(uuid,date)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"""
          DO $grant$
          BEGIN
            IF EXISTS(
              SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
            ) THEN
              GRANT EXECUTE ON FUNCTION public.{signature}
                TO membership_runtime;
            END IF;
          END $grant$
        """)


def downgrade() -> None:
    raise RuntimeError(
        "Refuse rollback of immutable Crypto Pay financial refund evidence"
    )
