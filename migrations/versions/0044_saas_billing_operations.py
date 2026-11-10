"""Opt-in SaaS invoice issuing, monthly settlement and owner-only reporting.

Revision ID: 0044_saas_billing_operations
Revises: 0043_platform_saas_invoices

All billing remains DISABLED by default. No cron jobs or bot flows charge
customers. Authorized platform-owner RPCs are the sole runtime entry points.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_saas_billing_operations"
down_revision: str | None = "0043_platform_saas_invoices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_saas_billing_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_saas_billing_singleton"),
        sa.CheckConstraint(
            "NOT enabled OR effective_from IS NOT NULL",
            name="ck_saas_billing_start_required",
        ),
    )
    op.execute(
        "INSERT INTO public.platform_saas_billing_control"
        " (id, enabled, effective_from) VALUES (1, false, NULL)"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_billing_control"
        " ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.platform_saas_billing_control"
        " FORCE ROW LEVEL SECURITY"
    )
    op.execute("""
        DO $rev$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles
                     WHERE rolname = 'membership_runtime') THEN
            REVOKE ALL ON public.platform_saas_billing_control
              FROM membership_runtime;
          END IF;
        END $rev$
    """)

    # Every function is SECURITY DEFINER with a locked search_path and
    # authenticates the platform owner from app.current_user_id AND the
    # migration-provisioned platform_operators table on every invocation.
    op.execute("""
        CREATE FUNCTION public.platform_saas_require_owner()
        RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE actor uuid;
        BEGIN
          actor := NULLIF(
            current_setting('app.current_user_id',true),''
          )::uuid;
          IF actor IS NULL OR NOT EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=actor AND o.role='owner'
          ) THEN
            RAISE EXCEPTION 'Platform owner only'
              USING ERRCODE='42501';
          END IF;
          RETURN actor;
        END;
        $f$
    """)
    op.execute("""
        CREATE FUNCTION public.platform_saas_require_rollout()
        RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE moment timestamptz;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          SELECT effective_from INTO moment
          FROM public.platform_saas_billing_control
          WHERE id=1 AND enabled=true;
          IF moment IS NULL THEN
            RAISE EXCEPTION 'ViMi Pay commercial billing is disabled'
              USING ERRCODE='42501';
          END IF;
          RETURN moment;
        END;
        $f$
    """)

    # Report does not create invoices. Useful for reviewing balances before
    # any commercial launch. Does not confuse merchant gross with platform
    # revenue: only manual owner-confirmed receipts are counted as received.
    op.execute("""
        CREATE FUNCTION public.platform_saas_invoice_report(
            p_page integer, p_size integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE result jsonb;
        BEGIN
          PERFORM public.platform_saas_require_owner();
          IF p_page IS NULL OR p_page<0 OR p_page>100000
             OR p_size IS NULL OR p_size<1 OR p_size>10 THEN
            RAISE EXCEPTION 'Invalid invoice page' USING ERRCODE='22023';
          END IF;
          SELECT jsonb_build_object(
              'enabled', (SELECT enabled FROM public.platform_saas_billing_control
                          WHERE id=1),
              'total', (SELECT count(*) FROM public.platform_saas_invoices
                        WHERE status='issued'),
              'unpaid', (SELECT count(*) FROM public.platform_saas_invoices i
                         WHERE i.status='issued' AND NOT EXISTS(
                           SELECT 1 FROM public.platform_saas_receipts r
                           WHERE r.invoice_id=i.id
                         )),
              'unpaid_rub', (SELECT coalesce(sum(i.total_rub),0)::text
                             FROM public.platform_saas_invoices i
                             WHERE i.status='issued' AND NOT EXISTS(
                               SELECT 1 FROM public.platform_saas_receipts r
                               WHERE r.invoice_id=i.id
                             )),
              'received_rub', (SELECT coalesce(sum(r.amount_rub),0)::text
                               FROM public.platform_saas_receipts r),
              'pages', (SELECT CASE WHEN count(*)=0 THEN 1
                               ELSE ceil(count(*)::numeric/p_size)::int END
                        FROM public.platform_saas_invoices
                        WHERE status='issued'),
              'invoices', coalesce((
                  SELECT jsonb_agg(jsonb_build_object(
                    'id',j.id::text, 'owner',j.owner_user_id::text,
                    'merchant',j.merchant_id::text,
                    'kind',j.kind, 'total_rub',j.total_rub::text,
                    'invoice_key',j.invoice_key,
                    'bot',j.bot_instance_id::text,
                    'period_start',j.period_start,'period_end',j.period_end,
                    'due_at',j.due_at, 'status',
                    CASE WHEN j.received_at IS NOT NULL THEN 'paid'
                         ELSE j.status END
                  ) ORDER BY j.due_at DESC,j.id DESC)
                  FROM (
                    SELECT i.*, r.confirmed_at AS received_at
                    FROM public.platform_saas_invoices i
                    LEFT JOIN public.platform_saas_receipts r
                      ON r.invoice_id=i.id
                    WHERE i.status='issued'
                    ORDER BY i.due_at DESC,i.id DESC
                    LIMIT p_size OFFSET p_page*p_size
                  ) j
              ),'[]'::jsonb)
          ) INTO result;
          RETURN result;
        END;
        $f$
    """)

    # Issue prepaid service fees only after the trial and explicit rollout.
    # Platform-owned channels require an actual creator-verification record;
    # platform operator role alone is not proof of channel ownership.
    op.execute("""
        CREATE FUNCTION public.platform_saas_issue_bot_invoice(
            p_bot uuid, p_start timestamptz, p_end timestamptz
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          first_charge timestamptz;
          entry record;
          trial_started timestamptz;
          invoice_id uuid;
          invoice_status text;
          exemption text;
          service_key text;
        BEGIN
          first_charge := public.platform_saas_require_rollout();
          IF p_bot IS NULL OR p_start IS NULL OR p_end IS NULL
             OR p_start<first_charge OR p_end<>p_start+interval '1 month'
             OR p_end<=p_start OR p_start>clock_timestamp()+interval '35 days' THEN
            RAISE EXCEPTION 'Invalid service period or rollout date'
              USING ERRCODE='22023';
          END IF;
          PERFORM pg_advisory_xact_lock(
            hashtextextended('saas:bot:'||p_bot::text,0)
          );
          SELECT b.project_id, p.merchant_id, p.owner_user_id,
                 b.created_at AS connected_at
          INTO entry FROM public.bot_instances b
          JOIN public.projects p ON p.id=b.project_id
          WHERE b.id=p_bot AND b.status='registered';
          IF NOT FOUND OR p_start<entry.connected_at THEN
            RAISE EXCEPTION 'Connected billable bot required'
              USING ERRCODE='22023';
          END IF;
          -- Only the owner who actually held this project for the ENTIRE
          -- service period may be charged. A mid-period transfer requires
          -- operator resolution, not an invoice against the new owner.
          IF NOT EXISTS(
            SELECT 1 FROM public.merchant_project_periods mp
            WHERE mp.project_id=entry.project_id
              AND mp.merchant_id=entry.merchant_id
              AND mp.owner_user_id=entry.owner_user_id
              AND mp.started_at<=p_start
              AND (mp.ended_at IS NULL OR mp.ended_at>=p_end)
          ) THEN
            RAISE EXCEPTION 'Service period owner attribution needs review'
              USING ERRCODE='42501';
          END IF;
          -- A platform owner may administer third-party channels.
          -- Never treat DB owner role as proof of channel creator status.
          -- A matching creator-verified record is required to waive fees;
          -- without one, block billing pending an ownership review.
          IF EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=entry.owner_user_id AND o.role='owner'
          ) THEN
            exemption := public.platform_saas_exemption_state(
                entry.project_id,p_bot,entry.merchant_id,
                entry.owner_user_id,p_start);
            IF exemption='exempt' THEN RETURN NULL; END IF;
            RAISE EXCEPTION 'Platform-admin project requires creator review'
              USING ERRCODE='42501';
          END IF;
          SELECT min(claimed_at) INTO trial_started
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=entry.owner_user_id
            AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
          IF trial_started IS NULL
             OR p_start<trial_started+interval '72 hours' THEN
            RAISE EXCEPTION 'Owner trial state is not billable yet'
              USING ERRCODE='42501';
          END IF;
          exemption := public.platform_saas_exemption_state(
              entry.project_id,p_bot,entry.merchant_id,entry.owner_user_id,p_start);
          IF exemption='exempt' THEN RETURN NULL; END IF;
          IF exemption<>'not_exempt' THEN
            RAISE EXCEPTION 'Channel exemption needs manual verification'
              USING ERRCODE='42501';
          END IF;
          -- UTC rendering is canonical across DB session TimeZone values.
          service_key := 'bot:'||p_bot::text||':'||
                         to_char(p_start AT TIME ZONE 'UTC',
                                 'YYYYMMDD"T"HH24MISS.US');
          -- Prevent overlapping monthly charges for the same connection.
          IF EXISTS(
            SELECT 1 FROM public.platform_saas_invoices prior
            WHERE prior.bot_instance_id=p_bot
              AND prior.kind='prepayment' AND prior.status<>'void'
              AND prior.invoice_key<>service_key
              AND tstzrange(prior.period_start,prior.period_end,'[)')
                  && tstzrange(p_start,p_end,'[)')
          ) THEN
            RAISE EXCEPTION 'Overlapping service invoices are forbidden'
              USING ERRCODE='23514';
          END IF;
          SELECT i.id,i.status INTO invoice_id,invoice_status
            FROM public.platform_saas_invoices i
            WHERE i.invoice_key=service_key FOR UPDATE;
          IF invoice_id IS NOT NULL THEN
            IF invoice_status<>'issued' THEN
              RAISE EXCEPTION 'Existing invoice is not issued'
                USING ERRCODE='42501';
            END IF;
            RETURN invoice_id;
          END IF;
          invoice_id := gen_random_uuid();
          INSERT INTO public.platform_saas_invoices(
             id,invoice_key,merchant_id,owner_user_id,kind,
             bot_instance_id,period_start,period_end,due_at,total_rub
          ) VALUES (
             invoice_id,service_key,entry.merchant_id,entry.owner_user_id,
             'prepayment',p_bot,p_start,p_end,p_start,150.00
          );
          INSERT INTO public.platform_saas_invoice_lines(
             id,invoice_id,project_id,source_kind,source_id,
             bot_instance_id,original_amount,original_currency,
             rub_per_unit,fee_rate,charge_rub
          ) VALUES (
             gen_random_uuid(),invoice_id,entry.project_id,'bot_fee',p_bot,
             p_bot,150,'RUB',1,1,150
          );
          UPDATE public.platform_saas_invoices SET status='issued'
          WHERE id=invoice_id;
          RETURN invoice_id;
        END;
        $f$
    """)

    # Commission can only be charged for closed Moscow calendar months.
    # All approved purchases are tied to their historical Merchant owner,
    # to the original active bot and to verified user-entered FX snapshots.
    # A month must not be partially invoiced: any uncertain attribution,
    # missing rate, or unsupported source rejects the entire operation.
    op.execute("""
        CREATE FUNCTION public.platform_saas_issue_commission_invoice(
            p_merchant uuid, p_owner uuid, p_month date, p_fx jsonb
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          first_charge timestamptz;
          trial_started timestamptz;
          period_start timestamptz;
          period_end timestamptz;
          source record;
          original_bot uuid;
          n_bots integer;
          quote jsonb := '[]'::jsonb;
          datum jsonb;
          fx numeric;
          amount_rub numeric;
          total numeric := 0;
          invoice_id uuid;
          previous record;
          v_invoice_key text;
          exemption text;
        BEGIN
          first_charge := public.platform_saas_require_rollout();
          IF p_merchant IS NULL OR p_owner IS NULL
             OR p_month IS NULL OR extract(day from p_month)<>1
             OR p_fx IS NULL OR jsonb_typeof(p_fx)<>'object' THEN
            RAISE EXCEPTION 'Invalid commission month or FX policy'
              USING ERRCODE='22023';
          END IF;
          period_start := p_month::timestamp AT TIME ZONE 'Europe/Moscow';
          period_end := (p_month::timestamp + interval '1 month')
                        AT TIME ZONE 'Europe/Moscow';
          IF period_end<=first_charge OR period_end>clock_timestamp() THEN
            RAISE EXCEPTION 'Commission month not closed or before rollout'
              USING ERRCODE='42501';
          END IF;
          IF NOT EXISTS(
            SELECT 1 FROM public.merchant_project_periods mp
            WHERE mp.merchant_id=p_merchant AND mp.owner_user_id=p_owner
          ) OR EXISTS(
            SELECT 1 FROM public.telegram_users u
            JOIN public.platform_operators o
              ON o.telegram_user_id=u.telegram_user_id
            WHERE u.id=p_owner AND o.role='owner'
          ) THEN
            RAISE EXCEPTION 'Merchant is not billable'
              USING ERRCODE='42501';
          END IF;
          SELECT min(claimed_at) INTO trial_started
          FROM public.merchant_platform_trial_claims t
          WHERE t.owner_user_id=p_owner
            AND t.scope='owner' AND t.policy_key='vimi-saas-72h-v1';
          IF trial_started IS NULL THEN
            RAISE EXCEPTION 'Trial claim is not provisioned'
              USING ERRCODE='42501';
          END IF;
          v_invoice_key := 'commission:'||p_merchant::text||':'||
                         p_owner::text||':'||to_char(p_month,'YYYY-MM-DD');
          PERFORM pg_advisory_xact_lock(
            hashtextextended('saas:'||v_invoice_key,0)
          );
          SELECT i.id,i.status INTO previous
            FROM public.platform_saas_invoices i
            WHERE i.invoice_key=v_invoice_key FOR UPDATE;
          IF previous.id IS NOT NULL THEN
            IF previous.status<>'issued' THEN
              RAISE EXCEPTION 'Existing commission invoice is not issued'
                USING ERRCODE='42501';
            END IF;
            RETURN previous.id;
          END IF;

          FOR source IN
            SELECT 'subscription'::text AS source_kind,p.id,p.project_id,
                   p.final_amount AS amount,p.currency_code,
                   p.approved_at AS paid_at
            FROM public.payments p
            UNION ALL
            SELECT 'file_manual'::text,o.id,o.project_id,o.amount,
                   o.currency_code,o.paid_at
            FROM public.file_sale_manual_orders o
            WHERE o.status='approved'
          LOOP
            IF source.paid_at IS NULL OR source.paid_at<period_start
               OR source.paid_at>=period_end
               OR source.paid_at<greatest(first_charge,trial_started+interval '72 hours')
               OR NOT EXISTS(
                 SELECT 1 FROM public.merchant_project_periods mp
                 WHERE mp.merchant_id=p_merchant
                   AND mp.owner_user_id=p_owner
                   AND mp.project_id=source.project_id
                   AND mp.started_at<=source.paid_at
                   AND (mp.ended_at IS NULL OR mp.ended_at>=source.paid_at)
               ) THEN CONTINUE; END IF;
            SELECT count(*),max(e.source_id::text)::uuid
              INTO n_bots,original_bot
            FROM public.platform_usage_events e
            WHERE e.project_id=source.project_id
              AND e.event_type='bot_connected'
              AND e.occurred_at<=source.paid_at
              AND NOT EXISTS(
                SELECT 1 FROM public.platform_usage_events gone
                WHERE gone.project_id=source.project_id
                  AND gone.event_type='bot_removed'
                  AND gone.source_id=e.source_id
                  AND gone.occurred_at<=source.paid_at
              );
            IF n_bots<>1 THEN
              RAISE EXCEPTION 'Ambiguous historical bot attribution'
                USING ERRCODE='23514';
            END IF;
            exemption := public.platform_saas_exemption_state(
              source.project_id,original_bot,p_merchant,p_owner,source.paid_at);
            IF exemption='exempt' THEN CONTINUE; END IF;
            IF exemption<>'not_exempt' THEN
              RAISE EXCEPTION 'Ambiguous exemption, review before charging'
                USING ERRCODE='42501';
            END IF;
            IF source.currency_code='RUB' THEN
              fx := 1;
            ELSE
              IF NOT (p_fx ? source.currency_code)
                 OR jsonb_typeof(p_fx->source.currency_code)<>'number' THEN
                RAISE EXCEPTION 'Missing numeric monthly FX rate for %',
                  source.currency_code USING ERRCODE='22023';
              END IF;
              fx := (p_fx->>source.currency_code)::numeric;
            END IF;
            IF fx<=0 OR fx>1000000000 THEN
              RAISE EXCEPTION 'Invalid monthly conversion rate'
                USING ERRCODE='22023';
            END IF;
            amount_rub := round(source.amount*fx*0.05,2);
            IF amount_rub<=0 THEN CONTINUE; END IF;
            total := total+amount_rub;
            quote := quote || jsonb_build_array(jsonb_build_object(
              'source_kind',source.source_kind,'id',source.id,
              'project',source.project_id,'bot',original_bot,
              'amount',source.amount,'currency',source.currency_code,
              'fx',fx,'charge',amount_rub
            ));
          END LOOP;
          IF total=0 THEN RETURN NULL; END IF;
          invoice_id:=gen_random_uuid();
          INSERT INTO public.platform_saas_invoices(
             id,invoice_key,merchant_id,owner_user_id,kind,
             period_start,period_end,due_at,total_rub
          ) VALUES (
             invoice_id,v_invoice_key,p_merchant,p_owner,'commission',
             period_start,period_end,period_end+interval '7 days',total
          );
          FOR datum IN SELECT value FROM jsonb_array_elements(quote) LOOP
            INSERT INTO public.platform_saas_invoice_lines(
               id,invoice_id,project_id,source_kind,source_id,
               bot_instance_id,original_amount,original_currency,
               rub_per_unit,fee_rate,charge_rub
            ) VALUES (
               gen_random_uuid(),invoice_id,(datum->>'project')::uuid,
               datum->>'source_kind',(datum->>'id')::uuid,
               (datum->>'bot')::uuid,(datum->>'amount')::numeric,
               datum->>'currency',(datum->>'fx')::numeric,
               0.05,(datum->>'charge')::numeric
            );
          END LOOP;
          UPDATE public.platform_saas_invoices SET status='issued'
          WHERE id=invoice_id;
          RETURN invoice_id;
        END;
        $f$
    """)

    op.execute("""
        CREATE FUNCTION public.platform_saas_confirm_receipt(
            p_invoice uuid, p_reference text
        ) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE actor uuid;
        record_id uuid;
        entry record;
        BEGIN
          actor:=public.platform_saas_require_owner();
          IF p_invoice IS NULL OR p_reference IS NOT NULL
             AND (length(p_reference)>160 OR
                  length(btrim(p_reference))=0) THEN
            RAISE EXCEPTION 'Invalid payment reference'
              USING ERRCODE='22023';
          END IF;
          SELECT i.id,i.total_rub,i.status INTO entry
            FROM public.platform_saas_invoices i
            WHERE i.id=p_invoice FOR UPDATE;
          IF entry.id IS NULL OR entry.status<>'issued' THEN
            RAISE EXCEPTION 'Issued invoice required'
              USING ERRCODE='42501';
          END IF;
          SELECT id INTO record_id FROM public.platform_saas_receipts
            WHERE invoice_id=p_invoice;
          IF record_id IS NOT NULL THEN RETURN record_id; END IF;
          record_id:=gen_random_uuid();
          INSERT INTO public.platform_saas_receipts(
             id,invoice_id,confirmed_by,amount_rub,reference
          ) VALUES (
             record_id,p_invoice,actor,entry.total_rub,p_reference
          );
          RETURN record_id;
        END;
        $f$
    """)

    functions = (
        "platform_saas_require_owner()",
        "platform_saas_require_rollout()",
        "platform_saas_invoice_report(integer,integer)",
        "platform_saas_issue_bot_invoice(uuid,timestamptz,timestamptz)",
        "platform_saas_issue_commission_invoice(uuid,uuid,date,jsonb)",
        "platform_saas_confirm_receipt(uuid,text)",
    )
    for fn in functions:
        op.execute(f"REVOKE ALL ON FUNCTION public.{fn} FROM PUBLIC")
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles
                     WHERE rolname='membership_runtime') THEN
            GRANT EXECUTE ON FUNCTION
              public.platform_saas_invoice_report(integer,integer),
              public.platform_saas_issue_bot_invoice(uuid,timestamptz,timestamptz),
              public.platform_saas_issue_commission_invoice(uuid,uuid,date,jsonb),
              public.platform_saas_confirm_receipt(uuid,text)
            TO membership_runtime;
          END IF;
        END
        $grants$
    """)


def downgrade() -> None:
    # Never drop a configured active billing control or financial activity.
    op.execute("""
        DO $safe$
        BEGIN
          IF (SELECT enabled FROM public.platform_saas_billing_control
              WHERE id=1) OR EXISTS(
                SELECT 1 FROM public.platform_saas_invoices
              ) THEN
            RAISE EXCEPTION 'Cannot roll back active SaaS billing/history';
          END IF;
        END $safe$
    """)
    for fn in (
        "platform_saas_confirm_receipt(uuid,text)",
        "platform_saas_issue_commission_invoice(uuid,uuid,date,jsonb)",
        "platform_saas_issue_bot_invoice(uuid,timestamptz,timestamptz)",
        "platform_saas_invoice_report(integer,integer)",
        "platform_saas_require_rollout()",
        "platform_saas_require_owner()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{fn}")
    op.drop_table("platform_saas_billing_control")
