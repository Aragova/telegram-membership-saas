"""Isolated ViMi Pay SaaS invoice ledger. Disabled until an approved billing rollout.

Revision ID: 0043_platform_saas_invoices
Revises: 0042_platform_monitor_alerts

No user-facing billing, new charges, policy activation, or changes to tenant
payments, grants, checkout or customer-supervisor. All writes are privileged
until a separately reviewed, operator-authorized billing application exists.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_platform_saas_invoices"
down_revision: str | None = "0042_platform_monitor_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "platform_saas_owner_exemptions",
    "platform_saas_invoices",
    "platform_saas_invoice_lines",
    "platform_saas_receipts",
)


def upgrade() -> None:
    # A verified Telegram channel creator must be recorded by a privileged
    # operator. Merely being an administrator of someone else's chat is NOT
    # proof of ownership. There is deliberately no tenant-editable toggle.
    op.create_table(
        "platform_saas_owner_exemptions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("merchant_id", sa.Uuid(),
                  sa.ForeignKey("merchant_identities.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("bot_instance_id", sa.Uuid(), nullable=False),
        sa.Column("channel_chat_id", sa.String(32), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(),
                  sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("verified_by", sa.Uuid(),
                  sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("proof_kind", sa.String(32), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "proof_kind = 'telegram_creator_verified'",
            name="ck_saas_exemption_proof",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at > verified_at",
            name="ck_saas_exemption_end",
        ),
    )
    op.create_index(
        "uq_saas_owner_exempt_active_bot", "platform_saas_owner_exemptions",
        ["bot_instance_id"], unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_saas_owner_exempt_project", "platform_saas_owner_exemptions",
        ["project_id", "merchant_id"],
    )

    op.create_table(
        "platform_saas_invoices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invoice_key", sa.String(160), nullable=False, unique=True),
        sa.Column("merchant_id", sa.Uuid(),
                  sa.ForeignKey("merchant_identities.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("owner_user_id", sa.Uuid(),
                  sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        # UUID is historical identity, never an FK to a deletable bot.
        sa.Column("bot_instance_id", sa.Uuid(), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_rub", sa.Numeric(38, 2), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(kind = 'prepayment' AND bot_instance_id IS NOT NULL) OR "
            "(kind = 'commission' AND bot_instance_id IS NULL)",
            name="ck_saas_invoice_kind_bot",
        ),
        sa.CheckConstraint(
            "status IN ('draft','issued','void')",
            name="ck_saas_invoice_status",
        ),
        sa.CheckConstraint("total_rub > 0", name="ck_saas_invoice_total"),
        sa.CheckConstraint(
            "period_end > period_start AND due_at >= period_start",
            name="ck_saas_invoice_period",
        ),
    )
    op.create_index(
        "uq_saas_bot_service_month", "platform_saas_invoices",
        ["bot_instance_id", "period_start"], unique=True,
        postgresql_where=sa.text("kind = 'prepayment'"),
    )
    op.create_index(
        "uq_saas_merchant_commission_month", "platform_saas_invoices",
        ["merchant_id", "owner_user_id", "period_start"], unique=True,
        postgresql_where=sa.text("kind = 'commission'"),
    )
    op.create_index(
        "ix_saas_invoice_owner", "platform_saas_invoices",
        ["owner_user_id", "due_at"],
    )
    op.create_index(
        "ix_saas_invoice_merchant", "platform_saas_invoices",
        ["merchant_id", "period_start"],
    )

    op.create_table(
        "platform_saas_invoice_lines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invoice_id", sa.Uuid(),
                  sa.ForeignKey("platform_saas_invoices.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        # Historical bot identity at the moment of the approved purchase.
        # Never infer exemption solely from the Merchant/project.
        sa.Column("bot_instance_id", sa.Uuid(), nullable=False),
        sa.Column("original_amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("original_currency", sa.String(16), nullable=False),
        sa.Column("rub_per_unit", sa.Numeric(38, 18), nullable=False),
        sa.Column("fee_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("charge_rub", sa.Numeric(38, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint(
            "source_kind IN ('bot_fee','subscription','file_manual')",
            name="ck_saas_line_kind",
        ),
        sa.CheckConstraint(
            "original_amount > 0 AND rub_per_unit > 0 "
            "AND fee_rate > 0 AND fee_rate <= 1 AND charge_rub > 0",
            name="ck_saas_line_amounts",
        ),
        sa.UniqueConstraint(
            "invoice_id", "source_kind", "source_id",
            name="uq_saas_invoice_line_local_source",
        ),
    )
    # Subscription/file IDs are globally idempotent across ALL invoices,
    # accounts, deleted bots and retries. Prepayment can repeat per service
    # period and is instead protected by the unique invoice_key.
    op.create_index(
        "uq_saas_once_per_paid_source", "platform_saas_invoice_lines",
        ["source_kind", "source_id"], unique=True,
        postgresql_where=sa.text("source_kind IN ('subscription','file_manual')"),
    )

    op.create_table(
        "platform_saas_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invoice_id", sa.Uuid(),
                  sa.ForeignKey("platform_saas_invoices.id", ondelete="RESTRICT"),
                  unique=True, nullable=False),
        sa.Column("confirmed_by", sa.Uuid(),
                  sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("amount_rub", sa.Numeric(38, 2), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.Column("reference", sa.String(160), nullable=True),
        sa.CheckConstraint("amount_rub > 0", name="ck_saas_receipt_positive"),
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        # No runtime RLS policies. Even platform operators need explicit
        # audited APIs in a future PR; tenants cannot view/write raw rows.
    op.execute("""
        DO $revoke$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime') THEN
            REVOKE ALL ON
              public.platform_saas_owner_exemptions,
              public.platform_saas_invoices,
              public.platform_saas_invoice_lines,
              public.platform_saas_receipts
            FROM membership_runtime;
          END IF;
        END $revoke$
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_exemption_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          owner_telegram bigint;
          verifier_telegram bigint;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Platform owner exemptions cannot be deleted'
              USING ERRCODE = '42501';
          END IF;
          IF TG_OP = 'UPDATE' THEN
            IF OLD.ended_at IS NOT NULL OR NEW.ended_at IS NULL
               OR (NEW.id,NEW.merchant_id,NEW.project_id,NEW.bot_instance_id,
                   NEW.channel_chat_id,NEW.owner_user_id,NEW.verified_by,
                   NEW.proof_kind,NEW.verified_at)
                  IS DISTINCT FROM
                  (OLD.id,OLD.merchant_id,OLD.project_id,OLD.bot_instance_id,
                   OLD.channel_chat_id,OLD.owner_user_id,OLD.verified_by,
                   OLD.proof_kind,OLD.verified_at) THEN
              RAISE EXCEPTION 'Exemption history is immutable'
                USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
          END IF;
          SELECT u.telegram_user_id INTO owner_telegram
            FROM public.projects p
            JOIN public.telegram_users u ON u.id = p.owner_user_id
           WHERE p.id=NEW.project_id
             AND p.merchant_id=NEW.merchant_id
             AND p.owner_user_id=NEW.owner_user_id;
          SELECT u.telegram_user_id INTO verifier_telegram
            FROM public.telegram_users u WHERE u.id=NEW.verified_by;
          IF owner_telegram IS NULL OR verifier_telegram IS NULL OR
             NOT EXISTS(
               SELECT 1 FROM public.platform_operators po
               WHERE po.telegram_user_id=owner_telegram AND po.role='owner'
             ) OR NOT EXISTS(
               SELECT 1 FROM public.platform_operators po
               WHERE po.telegram_user_id=verifier_telegram AND po.role='owner'
             ) OR NOT EXISTS(
               SELECT 1 FROM public.bot_instances b
               WHERE b.id=NEW.bot_instance_id AND b.project_id=NEW.project_id
             ) OR NOT EXISTS(
               SELECT 1 FROM public.resources r
               WHERE r.project_id=NEW.project_id
                 AND r.external_id=NEW.channel_chat_id
                 AND r.resource_type IN ('telegram_channel','telegram_group')
                 AND r.status='active'
             ) THEN
            RAISE EXCEPTION 'Verified platform-owned bot/channel required'
              USING ERRCODE = '42501';
          END IF;
          RETURN NEW;
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_owner_exemption_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_saas_owner_exemptions
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_exemption_guard()
    """)

    # Temporal, bot-scoped owner exemption. It never propagates from a
    # deleted bot to its replacement or to another channel in the project.
    # Ambiguous multi-channel projects require an operator review; do not
    # accidentally charge an owner's own content or waive another's fee.
    op.execute("""
        CREATE FUNCTION public.platform_saas_exemption_state(
            p_project uuid, p_bot uuid, p_merchant uuid,
            p_owner uuid, p_effective timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
          exempt record;
          channel_count integer;
          matched boolean;
        BEGIN
          SELECT * INTO exempt
            FROM public.platform_saas_owner_exemptions e
           WHERE e.project_id=p_project
             AND e.bot_instance_id=p_bot
             AND e.merchant_id=p_merchant
             AND e.owner_user_id=p_owner
             AND e.verified_at<=p_effective
             AND (e.ended_at IS NULL OR e.ended_at>p_effective)
           ORDER BY e.verified_at DESC LIMIT 1;
          IF NOT FOUND THEN
            RETURN 'not_exempt';
          END IF;
          SELECT count(*),
                 bool_or(c.telegram_chat_id=exempt.channel_chat_id)
            INTO channel_count,matched
            FROM public.merchant_channel_periods c
           WHERE c.project_id=p_project AND c.merchant_id=p_merchant
             AND c.started_at<=p_effective
             AND (c.ended_at IS NULL OR c.ended_at>p_effective);
          IF channel_count <> 1 OR matched IS DISTINCT FROM true THEN
            RETURN 'needs_review';
          END IF;
          RETURN 'exempt';
        END;
        $function$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.platform_saas_exemption_state("
        "uuid,uuid,uuid,uuid,timestamptz) FROM PUBLIC"
    )

    op.execute("""
        CREATE FUNCTION public.platform_saas_line_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          parent record;
          sale record;
          source_bot uuid;
          bot_count bigint;
          effective_at timestamptz;
          exemption_state text;
        BEGIN
          IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'Platform invoice lines are immutable'
              USING ERRCODE='42501';
          END IF;
          SELECT * INTO parent FROM public.platform_saas_invoices
            WHERE id=NEW.invoice_id FOR UPDATE;
          IF parent.id IS NULL OR parent.status <> 'draft' THEN
            RAISE EXCEPTION 'Invoice is not editable' USING ERRCODE='42501';
          END IF;
          IF NEW.source_kind = 'bot_fee' THEN
            IF parent.kind <> 'prepayment'
               OR NEW.source_id IS DISTINCT FROM parent.bot_instance_id
               OR NEW.original_currency <> 'RUB'
               OR NEW.original_amount <> 150
               OR NEW.rub_per_unit <> 1
               OR NEW.fee_rate <> 1
               OR NEW.charge_rub <> 150
               OR NOT EXISTS (
                 SELECT 1 FROM public.bot_instances b
                 JOIN public.projects p ON p.id=b.project_id
                 WHERE b.id=NEW.source_id
                   AND b.project_id=NEW.project_id
                   AND p.merchant_id=parent.merchant_id
                   AND p.owner_user_id=parent.owner_user_id
               ) THEN
              RAISE EXCEPTION 'Invalid bot prepayment source'
                USING ERRCODE='23514';
            END IF;
            source_bot := NEW.source_id;
            effective_at := parent.period_start;
          ELSE
            IF parent.kind <> 'commission' OR NEW.fee_rate <> 0.05 THEN
              RAISE EXCEPTION 'Invalid commission policy'
                USING ERRCODE='23514';
            END IF;
            IF NEW.source_kind = 'subscription' THEN
              SELECT project_id,final_amount AS amount,currency_code,
                     approved_at AS paid_at INTO sale
                FROM public.payments WHERE id=NEW.source_id;
            ELSIF NEW.source_kind = 'file_manual' THEN
              SELECT project_id,amount,currency_code,paid_at
                INTO sale FROM public.file_sale_manual_orders
               WHERE id=NEW.source_id AND status='approved';
            ELSE
              RAISE EXCEPTION 'Unknown paid source' USING ERRCODE='23514';
            END IF;
            -- Source identity is reconstructed from immutable bot-connected
            -- and bot-removed events at the approval time, not the bot that
            -- happens to be connected when month-end billing runs.
            -- Ambiguous histories are not silently assigned a replacement.
            SELECT count(*), max(connected.source_id::text)::uuid
              INTO bot_count,source_bot
              FROM public.platform_usage_events connected
             WHERE connected.project_id=NEW.project_id
               AND connected.event_type='bot_connected'
               AND connected.occurred_at<=sale.paid_at
               AND NOT EXISTS(
                   SELECT 1 FROM public.platform_usage_events removed
                   WHERE removed.project_id=NEW.project_id
                     AND removed.source_id=connected.source_id
                     AND removed.event_type='bot_removed'
                     AND removed.occurred_at<=sale.paid_at
               );
            IF bot_count<>1 THEN
              RAISE EXCEPTION 'Paid source bot history is ambiguous'
                USING ERRCODE='23514';
            END IF;
            effective_at := sale.paid_at;
            IF sale.project_id IS DISTINCT FROM NEW.project_id
               OR sale.amount IS DISTINCT FROM NEW.original_amount
               OR sale.currency_code IS DISTINCT FROM NEW.original_currency
               OR sale.paid_at IS NULL
               OR NOT EXISTS (
                  SELECT 1 FROM public.merchant_project_periods mp
                  WHERE mp.project_id=NEW.project_id
                    AND mp.merchant_id=parent.merchant_id
                    AND mp.owner_user_id=parent.owner_user_id
                    AND mp.started_at<=sale.paid_at
                    AND (mp.ended_at IS NULL OR mp.ended_at>=sale.paid_at)
               )
               OR sale.paid_at < parent.period_start
               OR sale.paid_at >= parent.period_end
               OR (NEW.original_currency='RUB' AND NEW.rub_per_unit <> 1)
               OR NEW.charge_rub IS DISTINCT FROM
                  round(sale.amount * NEW.rub_per_unit * 0.05, 2)
            THEN
              RAISE EXCEPTION 'Approved purchase / commission snapshot mismatch'
                USING ERRCODE='23514';
            END IF;
          END IF;
          IF NEW.bot_instance_id IS DISTINCT FROM source_bot THEN
            RAISE EXCEPTION 'Historical bot identity mismatch'
              USING ERRCODE='23514';
          END IF;
          SELECT public.platform_saas_exemption_state(
            NEW.project_id, source_bot, parent.merchant_id,
            parent.owner_user_id, effective_at
          ) INTO exemption_state;
          IF exemption_state <> 'not_exempt' THEN
            RAISE EXCEPTION 'Verified platform channel exempt or needs review'
              USING ERRCODE='42501';
          END IF;
          RETURN NEW;
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_line_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_saas_invoice_lines
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_line_guard()
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_invoice_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          sum_lines numeric;
          line_count bigint;
        BEGIN
          IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'SaaS invoices cannot be deleted'
              USING ERRCODE='42501';
          END IF;
          IF TG_OP='INSERT' THEN
            IF NEW.status <> 'draft' OR NEW.issued_at IS NOT NULL
               OR NEW.voided_at IS NOT NULL
               OR NOT EXISTS(
                 SELECT 1 FROM public.merchant_project_periods mp
                 WHERE mp.merchant_id=NEW.merchant_id
                   AND mp.owner_user_id=NEW.owner_user_id
               ) THEN
              RAISE EXCEPTION 'Invalid SaaS invoice origin'
                USING ERRCODE='23514';
            END IF;
            RETURN NEW;
          END IF;
          IF (NEW.id,NEW.invoice_key,NEW.merchant_id,NEW.owner_user_id,
              NEW.kind,NEW.bot_instance_id,NEW.period_start,NEW.period_end,
              NEW.due_at,NEW.total_rub,NEW.created_at)
             IS DISTINCT FROM
             (OLD.id,OLD.invoice_key,OLD.merchant_id,OLD.owner_user_id,
              OLD.kind,OLD.bot_instance_id,OLD.period_start,OLD.period_end,
              OLD.due_at,OLD.total_rub,OLD.created_at) THEN
            RAISE EXCEPTION 'Invoice financial snapshot is immutable'
              USING ERRCODE='42501';
          END IF;
          IF OLD.status='draft' AND NEW.status='issued'
             AND NEW.voided_at IS NULL AND NEW.issued_at IS NULL THEN
            SELECT count(*),COALESCE(sum(charge_rub),0)
            INTO line_count,sum_lines FROM public.platform_saas_invoice_lines
            WHERE invoice_id=OLD.id;
            IF line_count=0 OR sum_lines<>OLD.total_rub
               OR EXISTS (
                 SELECT 1 FROM public.platform_saas_invoice_lines l
                 WHERE l.invoice_id=OLD.id AND
                 public.platform_saas_exemption_state(
                   l.project_id, l.bot_instance_id,
                   OLD.merchant_id, OLD.owner_user_id,
                   CASE l.source_kind
                     WHEN 'bot_fee' THEN OLD.period_start
                     WHEN 'subscription' THEN (
                       SELECT p.approved_at FROM public.payments p
                       WHERE p.id=l.source_id
                     )
                     WHEN 'file_manual' THEN (
                       SELECT f.paid_at FROM public.file_sale_manual_orders f
                       WHERE f.id=l.source_id
                     )
                   END
                 ) <> 'not_exempt'
               ) THEN
              RAISE EXCEPTION 'Invoice lines / exemption mismatch'
                USING ERRCODE='23514';
            END IF;
            NEW.issued_at := clock_timestamp();
            RETURN NEW;
          END IF;
          IF OLD.status='issued' AND NEW.status='void'
             AND NEW.issued_at IS NOT DISTINCT FROM OLD.issued_at
             AND NEW.voided_at IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM public.platform_saas_receipts
               WHERE invoice_id=OLD.id
             ) THEN
            NEW.voided_at := clock_timestamp();
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'Invalid SaaS invoice state change'
            USING ERRCODE='42501';
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_invoice_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_saas_invoices
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_invoice_guard()
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_receipt_guard()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          actor uuid;
          invoice record;
        BEGIN
          IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'Confirmed SaaS receipt is immutable'
              USING ERRCODE='42501';
          END IF;
          actor := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
          IF actor IS NULL OR actor<>NEW.confirmed_by
             OR NOT EXISTS (
               SELECT 1 FROM public.telegram_users u
               JOIN public.platform_operators po
                 ON po.telegram_user_id=u.telegram_user_id
               WHERE u.id=actor AND po.role='owner'
             ) THEN
            RAISE EXCEPTION 'Only ViMi Pay owner may confirm SaaS receipts'
              USING ERRCODE='42501';
          END IF;
          SELECT status,total_rub INTO invoice
          FROM public.platform_saas_invoices
          WHERE id=NEW.invoice_id FOR UPDATE;
          IF invoice.status IS DISTINCT FROM 'issued'
             OR invoice.total_rub IS DISTINCT FROM NEW.amount_rub
          THEN
            RAISE EXCEPTION 'Receipt requires an issued, fully paid invoice'
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
        $guard$
    """)
    op.execute("""
        CREATE TRIGGER trg_saas_receipt_guard
        BEFORE INSERT OR UPDATE OR DELETE ON public.platform_saas_receipts
        FOR EACH ROW EXECUTE FUNCTION public.platform_saas_receipt_guard()
    """)
    for fn in (
        "platform_saas_exemption_guard",
        "platform_saas_line_guard",
        "platform_saas_invoice_guard",
        "platform_saas_receipt_guard",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn}() FROM PUBLIC")


def downgrade() -> None:
    # Downgrade must not erase real financial history after activation.
    op.execute("""
        DO $protect$
        BEGIN
          IF EXISTS(SELECT 1 FROM public.platform_saas_invoices)
             OR EXISTS(SELECT 1 FROM public.platform_saas_receipts) THEN
            RAISE EXCEPTION 'Cannot drop SaaS invoice or receipt history';
          END IF;
        END $protect$
    """)
    for table, name in (
        ("platform_saas_receipts", "receipt"),
        ("platform_saas_invoices", "invoice"),
        ("platform_saas_invoice_lines", "line"),
        ("platform_saas_owner_exemptions", "owner_exemption"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_saas_{name}_guard ON public.{table}")
    for fn in (
        "platform_saas_receipt_guard",
        "platform_saas_invoice_guard",
        "platform_saas_line_guard",
        "platform_saas_exemption_guard",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{fn}()")
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_exemption_state("
        "uuid,uuid,uuid,uuid,timestamptz)"
    )
    for table in reversed(TABLES):
        op.drop_table(table)
