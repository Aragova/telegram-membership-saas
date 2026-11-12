"""Reconcile legacy invoice screens/notices/holds with partial bank receipts.

Revision ID: 0069_saas_balance
Revises: 0068_saas_multirail

No retroactive billing: existing issued invoices and credited receipts remain
immutable; read-only report and hold evaluation are corrected consistently.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0069_saas_balance"
down_revision: str | None = "0068_saas_multirail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only privileged SECURITY DEFINER invoice functions may call this
    # helper; runtime/buyers must not enumerate other merchants' debts.
    op.execute("""
CREATE FUNCTION public.platform_saas_v2_remaining(p_invoice uuid)
RETURNS numeric LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public AS $f$
DECLARE original numeric; received numeric; credited numeric; invoice_status text;
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
  IF received+credited>original THEN
    RAISE EXCEPTION 'Invoice overpayment violates accounting invariant'
      USING ERRCODE='23514';
  END IF;
  RETURN original-received-credited;
END; $f$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION public.platform_saas_v2_remaining(uuid)
      FROM PUBLIC
    """)
    # Account-scoped platform now sees both legacy and partial receipts.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_invoice_report(
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
                         WHERE i.status='issued' AND public.platform_saas_v2_remaining(i.id)>0),
              'unpaid_rub', (SELECT coalesce(sum(public.platform_saas_v2_remaining(i.id)),0)::text
                             FROM public.platform_saas_invoices i
                             WHERE i.status='issued' AND public.platform_saas_v2_remaining(i.id)>0),
              'received_rub', (
                (SELECT coalesce(sum(r.amount_rub),0)
                 FROM public.platform_saas_receipts r)
                +(SELECT coalesce(sum(a.allocated_rub),0)
                  FROM public.platform_saas_payment_allocations_v2 a)
                )::text,
              'pages', (SELECT CASE WHEN count(*)=0 THEN 1
                               ELSE ceil(count(*)::numeric/p_size)::int END
                        FROM public.platform_saas_invoices
                        WHERE status='issued'),
              'invoices', coalesce((
                  SELECT jsonb_agg(jsonb_build_object(
                    'id',j.id::text, 'owner',j.owner_user_id::text,
                    'merchant',j.merchant_id::text,
                    'kind',j.kind, 'total_rub',j.total_rub::text,
                    'remaining_rub',public.platform_saas_v2_remaining(j.id)::text,
                    'invoice_key',j.invoice_key,
                    'bot',j.bot_instance_id::text,
                    'period_start',j.period_start,'period_end',j.period_end,
                    'due_at',j.due_at, 'status',
                    CASE WHEN public.platform_saas_v2_remaining(j.id)=0 THEN 'paid'
                         ELSE j.status END
                  ) ORDER BY j.due_at DESC,j.id DESC)
                  FROM (
                    SELECT i.*
                    FROM public.platform_saas_invoices i
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
    # Account-scoped merchant now sees both legacy and partial receipts.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_owner_invoice_report(
            p_page integer, p_size integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          actor uuid;
          bank text;
          result jsonb;
        BEGIN
          actor:=NULLIF(
            current_setting('app.current_user_id',true),''
          )::uuid;
          IF actor IS NULL OR NOT EXISTS(
            SELECT 1 FROM public.telegram_users u WHERE u.id=actor
          ) THEN
            RAISE EXCEPTION 'Authenticated owner required'
              USING ERRCODE='42501';
          END IF;
          IF p_page IS NULL OR p_page<0 OR p_page>100000
             OR p_size IS NULL OR p_size<1 OR p_size>8 THEN
            RAISE EXCEPTION 'Invalid statement page' USING ERRCODE='22023';
          END IF;
          SELECT instructions INTO bank
            FROM public.platform_saas_payment_details WHERE id=1;
          SELECT jsonb_build_object(
            'total',(SELECT count(*) FROM public.platform_saas_invoices i
                     WHERE i.owner_user_id=actor AND i.status='issued'),
            'unpaid',(SELECT count(*) FROM public.platform_saas_invoices i
                      WHERE i.owner_user_id=actor AND i.status='issued'
                        AND public.platform_saas_v2_remaining(i.id)>0),
            'outstanding_rub',(
                SELECT coalesce(sum(public.platform_saas_v2_remaining(i.id)),0)::text
                FROM public.platform_saas_invoices i
                WHERE i.owner_user_id=actor AND i.status='issued'
                  AND public.platform_saas_v2_remaining(i.id)>0
            ),
            'details_configured',bank IS NOT NULL,
            'instructions',CASE WHEN EXISTS(
                SELECT 1 FROM public.platform_saas_invoices visible
                WHERE visible.owner_user_id=actor AND visible.status='issued'
            ) THEN bank ELSE NULL END,
            'invoices',coalesce((
              SELECT jsonb_agg(jsonb_build_object(
                  'id',f.id::text,
                  'kind',f.kind,
                  'amount_rub',f.total_rub::text,
                  'period_start',f.period_start,
                  'period_end',f.period_end,
                  'due_at',f.due_at,
                  'issued_at',f.issued_at,
                  'status',CASE WHEN public.platform_saas_v2_remaining(f.id)=0
                                THEN 'paid' ELSE 'unpaid' END,
                  'remaining_rub',public.platform_saas_v2_remaining(f.id)::text
              ) ORDER BY f.due_at DESC,f.id DESC)
              FROM (
                SELECT i.* FROM public.platform_saas_invoices i
                WHERE i.owner_user_id=actor AND i.status='issued'
                ORDER BY i.due_at DESC,i.id DESC
                LIMIT p_size OFFSET p_page*p_size
              ) f
            ),'[]'::jsonb)
          ) INTO result;
          RETURN result;
        END;
        $fn$
    """)
    # Account-scoped hold now sees both legacy and partial receipts.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_owner_hold_state(
            p_owner uuid, p_at timestamptz
        ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          hold_enabled boolean;
          billing_enabled boolean;
          billing_started timestamptz;
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
          SELECT enabled, effective_from
            INTO billing_enabled,billing_started
            FROM public.platform_saas_billing_control WHERE id=1;
          IF hold_enabled IS DISTINCT FROM true
             OR billing_enabled IS DISTINCT FROM true
             OR started IS NULL OR billing_started IS NULL
             OR p_at<greatest(started,billing_started) THEN
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
            AND public.platform_saas_v2_remaining(i.id)>0;
          IF overdue>0 THEN RETURN 'held'; END IF;
          IF pending>0 THEN RETURN 'grace'; END IF;
          RETURN 'clear';
        END;
        $f$
    """)
    # Account-scoped holdreport now sees both legacy and partial receipts.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_hold_invoice_report(
            p_page integer, p_size integer
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $f$
        DECLARE
          report jsonb;
          enabled boolean;
          billing_enabled boolean;
          billing_started timestamptz;
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
          SELECT c.enabled,c.effective_from
          INTO billing_enabled,billing_started
          FROM public.platform_saas_billing_control c WHERE c.id=1;
          enabled:=coalesce(enabled,false)
                   AND coalesce(billing_enabled,false)
                   AND started IS NOT NULL
                   AND billing_started IS NOT NULL
                   AND now_at>=greatest(started,billing_started);
          IF enabled THEN
            FOR account IN
              SELECT DISTINCT i.owner_user_id
              FROM public.platform_saas_invoices i
              WHERE i.status='issued' AND i.issued_at>=started
                AND public.platform_saas_v2_remaining(i.id)>0
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
    # Account-scoped schedule now sees both legacy and partial receipts.
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
          IF started IS NULL OR started>now_at
             OR NOT EXISTS(
               SELECT 1 FROM public.platform_saas_payment_details
               WHERE id=1 AND instructions IS NOT NULL
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
    # Account-scoped claim now sees both legacy and partial receipts.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_notice_claim()
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
        DECLARE
          item record;
          bank text;
          now_at timestamptz := clock_timestamp();
        BEGIN
          PERFORM public.platform_saas_require_owner();
          SELECT instructions INTO bank
          FROM public.platform_saas_payment_details WHERE id=1;
          IF bank IS NULL OR NOT EXISTS(
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
            'due_at',item.due_at,'instructions',bank
          );
        END;
        $fn$
    """)
    # Account-scoped currency now sees both legacy and partial receipts.
    op.execute("""
CREATE OR REPLACE FUNCTION public.platform_saas_invoice_currency_snapshot(
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
              'paid',public.platform_saas_v2_remaining(s.invoice_id)=0
            )
            FROM public.platform_saas_currency_settlements s
            WHERE s.invoice_id=p_invoice
          ), jsonb_build_object('invoice_id',p_invoice,'locked',false));
        END;
        $fn$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Do not roll back outstanding debt and access/hold accounting"
    )
