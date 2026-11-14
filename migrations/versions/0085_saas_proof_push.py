"""Durable central-bot push for newly submitted ViMi Pay bank proofs.

Revision ID: 0085_saas_proof_push
Revises: 0084_saas_bot_month_billing

Proofs remain immutable and private to their receiving bot. A trigger adds one
operator notification per NEW proof in the same transaction. No old receipts
are replayed on deployment, and no billing activation switches are touched.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0085_saas_proof_push"
down_revision: str | None = "0084_saas_bot_month_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE public.platform_saas_proof_alert_outbox (
        attempt_id uuid PRIMARY KEY REFERENCES
          public.platform_saas_payment_attempts_v2(id) ON DELETE RESTRICT,
        status text NOT NULL DEFAULT 'pending' CHECK (
          status IN ('pending','sending','sent','cancelled')
        ),
        attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        claimed_at timestamptz,
        delivered_at timestamptz
      )
    """)
    op.execute("""
      CREATE INDEX ix_saas_proof_alert_due
      ON public.platform_saas_proof_alert_outbox(available_at,attempt_id)
      WHERE status IN ('pending','sending')
    """)
    op.execute("""
      ALTER TABLE public.platform_saas_proof_alert_outbox
      ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
      ALTER TABLE public.platform_saas_proof_alert_outbox
      FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
      REVOKE ALL ON TABLE public.platform_saas_proof_alert_outbox FROM PUBLIC
    """)
    op.execute("""
      DO $revoke$
      BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
        THEN
          REVOKE ALL ON TABLE public.platform_saas_proof_alert_outbox
          FROM membership_runtime;
        END IF;
      END $revoke$
    """)

    op.execute("""
      CREATE FUNCTION public.platform_saas_enqueue_proof_alert()
      RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      BEGIN
        INSERT INTO public.platform_saas_proof_alert_outbox(attempt_id)
        VALUES(NEW.attempt_id) ON CONFLICT (attempt_id) DO NOTHING;
        RETURN NEW;
      END; $f$
    """)
    op.execute("""
      CREATE TRIGGER trg_saas_proof_operator_alert
      AFTER INSERT ON public.platform_saas_payment_proofs_v2
      FOR EACH ROW EXECUTE FUNCTION public.platform_saas_enqueue_proof_alert()
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_proof_alert_claim()
      RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      DECLARE item record; now_at timestamptz := clock_timestamp();
      BEGIN
        -- The configured platform owner must also have a DB operator grant.
        PERFORM public.platform_saas_require_owner();
        -- A bank decision cancels an unsent notification. No new queue.
        UPDATE public.platform_saas_proof_alert_outbox n
        SET status='cancelled'
        WHERE n.status IN ('pending','sending')
          AND (EXISTS (
            SELECT 1 FROM public.platform_saas_payment_allocations_v2 paid
            WHERE paid.attempt_id=n.attempt_id
          ) OR EXISTS (
            SELECT 1 FROM public.platform_saas_payment_rejections_v2 rejected
            WHERE rejected.attempt_id=n.attempt_id
          ));

        SELECT n.attempt_id,n.attempts INTO item
        FROM public.platform_saas_proof_alert_outbox n
        WHERE n.available_at <= now_at
          AND (
            n.status='pending'
            OR (n.status='sending'
                AND n.claimed_at < now_at - interval '5 minutes')
          )
        ORDER BY n.available_at,n.attempt_id
        LIMIT 1 FOR UPDATE OF n SKIP LOCKED;
        IF NOT FOUND THEN RETURN NULL; END IF;

        UPDATE public.platform_saas_proof_alert_outbox n
        SET status='sending',
            attempts=n.attempts+1,
            claimed_at=now_at
        WHERE n.attempt_id=item.attempt_id;

        RETURN (
          SELECT jsonb_build_object(
            'id',n.attempt_id,'generation',n.attempts,
            'amount',a.amount_currency::text,
            'currency',a.currency_code,
            'pending_count',(
              SELECT count(*) FROM public.platform_saas_payment_proofs_v2 p
              WHERE NOT EXISTS (
                SELECT 1 FROM public.platform_saas_payment_allocations_v2 ok
                WHERE ok.attempt_id=p.attempt_id
              ) AND NOT EXISTS (
                SELECT 1 FROM public.platform_saas_payment_rejections_v2 bad
                WHERE bad.attempt_id=p.attempt_id
              )
            )
          )
          FROM public.platform_saas_proof_alert_outbox n
          JOIN public.platform_saas_payment_attempts_v2 a
            ON a.id=n.attempt_id
          WHERE n.attempt_id=item.attempt_id
        );
      END; $f$
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_proof_alert_finish(
        p_attempt uuid,p_generation integer,p_sent boolean
      ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      DECLARE changed integer;
      BEGIN
        PERFORM public.platform_saas_require_owner();
        IF p_attempt IS NULL OR p_generation IS NULL OR p_sent IS NULL THEN
          RAISE EXCEPTION 'Invalid operator alert acknowledgment'
            USING ERRCODE='22023';
        END IF;
        UPDATE public.platform_saas_proof_alert_outbox n
        SET status=CASE WHEN p_sent THEN 'sent' ELSE 'pending' END,
            delivered_at=CASE WHEN p_sent THEN clock_timestamp()
                              ELSE NULL END,
            available_at=CASE WHEN p_sent THEN n.available_at
              ELSE clock_timestamp() + make_interval(
                secs => LEAST(1800,30 * (1 << LEAST(n.attempts,6)))
              ) END,
            claimed_at=NULL
        WHERE n.attempt_id=p_attempt
          AND n.status='sending' AND n.attempts=p_generation;
        GET DIAGNOSTICS changed = ROW_COUNT;
        RETURN changed=1;
      END; $f$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
        public.platform_saas_enqueue_proof_alert() FROM PUBLIC
    """)
    for signature in (
        "platform_saas_proof_alert_claim()",
        "platform_saas_proof_alert_finish(uuid,integer,boolean)",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC"
        )
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
    op.execute(
        "DROP TRIGGER IF EXISTS trg_saas_proof_operator_alert "
        "ON public.platform_saas_payment_proofs_v2"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_proof_alert_finish(uuid,integer,boolean)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_proof_alert_claim()"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.platform_saas_enqueue_proof_alert()"
    )
    op.execute(
        "DROP TABLE IF EXISTS public.platform_saas_proof_alert_outbox"
    )
