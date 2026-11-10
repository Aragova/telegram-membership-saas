"""Dormant overdue SaaS invoice grace and new-commerce hold gates.

Revision ID: 0046_saas_commercial_holds
Revises: 0045_saas_trial_grant_quota

No existing bots or paid rights change. No invoice is created or settled here.
Hold gates activate only after deliberate platform-owner rollout of BOTH the
existing SaaS billing control and a separate hold control, disabled by default.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_saas_commercial_holds"
down_revision: str | None = "0045_saas_trial_grant_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saas_hold_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_days", sa.Integer(), nullable=False, server_default="7"),
        sa.CheckConstraint("id=1", name="ck_saas_hold_control_singleton"),
        sa.CheckConstraint(
            "NOT enabled OR effective_from IS NOT NULL",
            name="ck_saas_hold_effective_required",
        ),
        sa.CheckConstraint(
            "grace_days BETWEEN 0 AND 30", name="ck_saas_hold_grace_range",
        ),
    )
    op.execute("""
        INSERT INTO public.platform_saas_hold_control
            (id,enabled,effective_from,grace_days)
        VALUES (1,false,NULL,7)
    """)
    op.execute(
        "ALTER TABLE public.platform_saas_hold_control ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_hold_control FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
        DO $priv$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            REVOKE ALL ON public.platform_saas_hold_control
            FROM membership_runtime;
          END IF;
        END $priv$
    """)

    # A read-only decision over an account's immutable issued invoices and
    # independently approved receipts. The grace period begins no sooner
    # than due_at, issued_at and the explicitly configured rollout instant.
    # Invoices issued before the rollout cutoff do NOT suddenly block a
    # running business; deliberate future charges must be issued afterwards.
    op.execute("""
        CREATE FUNCTION public.platform_saas_owner_hold_state(
            p_owner uuid, p_at timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          hold_enabled boolean;
          billing_enabled boolean;
          started timestamptz;
          grace integer;
          pending bigint;
          overdue bigint;
        BEGIN
          IF p_owner IS NULL OR p_at IS NULL THEN
            RAISE EXCEPTION 'Invalid hold check'
              USING ERRCODE='22023';
          END IF;
          SELECT enabled,effective_from,grace_days
            INTO hold_enabled,started,grace
            FROM public.platform_saas_hold_control WHERE id=1;
          SELECT enabled INTO billing_enabled
            FROM public.platform_saas_billing_control WHERE id=1;
          IF hold_enabled IS DISTINCT FROM true
             OR billing_enabled IS DISTINCT FROM true
             OR started IS NULL OR p_at<started THEN
            RETURN 'disabled';
          END IF;
          SELECT count(*),count(*) FILTER(
            WHERE greatest(i.due_at,i.issued_at,started)
                  + make_interval(days=>grace)<=p_at
          ) INTO pending,overdue
          FROM public.platform_saas_invoices i
          WHERE i.owner_user_id=p_owner
            AND i.status='issued'
            AND i.issued_at IS NOT NULL
            AND i.issued_at>=started
            AND NOT EXISTS(
              SELECT 1 FROM public.platform_saas_receipts r
              WHERE r.invoice_id=i.id
            );
          IF overdue>0 THEN RETURN 'held'; END IF;
          IF pending>0 THEN RETURN 'grace'; END IF;
          RETURN 'clear';
        END;
        $f$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
          public.platform_saas_owner_hold_state(uuid,timestamptz)
        FROM PUBLIC
    """)

    # The only new-commerce hold gate. A verified creator-owned channel
    # can continue selling via its SAME bot/channel, but role=platform owner
    # or Telegram administrator alone does not imply an exemption.
    # Adding a new bot or a new channel is not covered by any old exemption.
    op.execute("""
        CREATE FUNCTION public.platform_saas_guard_new_commerce()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $guard$
        DECLARE
          liable_owner uuid;
          merchant uuid;
          bot_uuid uuid;
          bot_count bigint;
          project_hold text;
          at_time timestamptz;
        BEGIN
          IF TG_OP<>'INSERT' THEN RETURN NEW; END IF;
          IF TG_TABLE_NAME='resources'
             AND NEW.resource_type NOT IN (
               'telegram_channel','telegram_group'
             ) THEN RETURN NEW; END IF;
          SELECT p.owner_user_id,p.merchant_id
            INTO liable_owner,merchant
          FROM public.projects p WHERE p.id=NEW.project_id;
          IF liable_owner IS NULL THEN
            RETURN NEW; -- FK/tenant RLS will reject a nonexistent project
          END IF;
          at_time:=clock_timestamp();
          project_hold:=public.platform_saas_owner_hold_state(
            liable_owner,at_time
          );
          IF project_hold<>'held' THEN RETURN NEW; END IF;

          -- Do not inherit a creator exemption when connecting a different
          -- bot or publishing an additional resource; require fresh proof.
          IF TG_TABLE_NAME IN (
             'payment_requests','file_sale_manual_orders',
             'file_sale_purchases'
          ) THEN
            IF TG_TABLE_NAME='payment_requests' THEN
              SELECT count(*),max(b.id::text)::uuid
              INTO bot_count,bot_uuid FROM public.bot_instances b
              WHERE b.project_id=NEW.project_id AND b.status='registered';
              IF bot_count<>1 THEN bot_uuid:=NULL; END IF;
            ELSE
              SELECT b.id INTO bot_uuid FROM public.bot_instances b
              WHERE b.project_id=NEW.project_id
                AND b.telegram_bot_id=NEW.telegram_bot_id
                AND b.status='registered';
            END IF;
            IF bot_uuid IS NOT NULL AND
               public.platform_saas_exemption_state(
                 NEW.project_id,bot_uuid,merchant,liable_owner,at_time
               )='exempt' THEN
              RETURN NEW;
            END IF;
          END IF;

          RAISE EXCEPTION 'New ViMi Pay checkout/connection is on payment hold'
            USING ERRCODE='P6711';
        END;
        $guard$
    """)
    # INSERT only. Existing pending orders can be reviewed, settled and
    # delivered. Paid subscriptions and purchased files stay accessible.
    for table, trigger in (
        ("payment_requests", "trg_saas_hold_new_subscription_checkout"),
        ("file_sale_manual_orders", "trg_saas_hold_new_manual_file_checkout"),
        ("file_sale_purchases", "trg_saas_hold_new_stars_file_checkout"),
        ("bot_instances", "trg_saas_hold_new_bot_connection"),
        ("resources", "trg_saas_hold_new_channel"),
    ):
        op.execute(f"""
            CREATE TRIGGER {trigger}
            BEFORE INSERT ON public.{table}
            FOR EACH ROW
            EXECUTE FUNCTION public.platform_saas_guard_new_commerce()
        """)

    # One compact status within the already existing central Finances ->
    # Invoices panel. Platform-only, with the old financial report intact.
    op.execute("""
        CREATE FUNCTION public.platform_saas_hold_invoice_report(
            p_page integer, p_size integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          report jsonb;
          enabled boolean;
          billing_enabled boolean;
          started timestamptz;
          grace integer;
          held bigint := 0;
          pending bigint := 0;
          account uuid;
          state text;
          now_at timestamptz := clock_timestamp();
        BEGIN
          PERFORM public.platform_saas_require_owner();
          report:=public.platform_saas_invoice_report(p_page,p_size);
          SELECT c.enabled,c.effective_from,c.grace_days
          INTO enabled,started,grace
          FROM public.platform_saas_hold_control c WHERE c.id=1;
          SELECT c.enabled INTO billing_enabled
          FROM public.platform_saas_billing_control c WHERE c.id=1;
          enabled:=coalesce(enabled,false)
                   AND coalesce(billing_enabled,false);
          IF enabled AND started IS NOT NULL AND now_at>=started THEN
            FOR account IN
              SELECT DISTINCT i.owner_user_id
              FROM public.platform_saas_invoices i
              WHERE i.status='issued' AND i.issued_at>=started
                AND NOT EXISTS(
                  SELECT 1 FROM public.platform_saas_receipts r
                  WHERE r.invoice_id=i.id
                )
            LOOP
              state:=public.platform_saas_owner_hold_state(account,now_at);
              IF state='held' THEN held:=held+1;
              ELSIF state='grace' THEN pending:=pending+1;
              END IF;
            END LOOP;
          END IF;
          RETURN report || jsonb_build_object(
            'holds_enabled',enabled,
            'grace_days',grace,
            'held_owners',held,
            'grace_owners',pending
          );
        END;
        $f$
    """)
    op.execute("""
        REVOKE ALL ON FUNCTION
          public.platform_saas_hold_invoice_report(integer,integer)
        FROM PUBLIC
    """)
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_hold_invoice_report(integer,integer)
            TO membership_runtime;
          END IF;
        END $grants$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.platform_saas_guard_new_commerce()"
        " FROM PUBLIC"
    )


def downgrade() -> None:
    op.execute("""
        DO $safe$
        BEGIN
          IF EXISTS(
            SELECT 1 FROM public.platform_saas_hold_control WHERE enabled
          ) THEN
            RAISE EXCEPTION 'Cannot roll back active commercial holds'
              USING ERRCODE='42501';
          END IF;
        END $safe$
    """)
    for table, trigger in (
        ("resources", "trg_saas_hold_new_channel"),
        ("bot_instances", "trg_saas_hold_new_bot_connection"),
        ("file_sale_purchases", "trg_saas_hold_new_stars_file_checkout"),
        ("file_sale_manual_orders", "trg_saas_hold_new_manual_file_checkout"),
        ("payment_requests", "trg_saas_hold_new_subscription_checkout"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON public.{table}")
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_hold_invoice_report(integer,integer)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_guard_new_commerce()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_owner_hold_state(uuid,timestamptz)"
    )
    op.drop_table("platform_saas_hold_control")
