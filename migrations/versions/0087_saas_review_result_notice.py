"""Durable owner-facing decision messages from the original payment bot.

Revision ID: 0087_saas_review_result_notice
Revises: 0086_saas_owner_hold

The original immutable bank decision enqueues its Telegram result in the
same transaction. A failed customer-bot delivery cannot undo/replay a
confirmed financial operation; one existing central worker retries it.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0087_saas_review_result_notice"
down_revision: str | None = "0086_saas_owner_hold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE public.platform_saas_review_result_outbox (
        attempt_id uuid PRIMARY KEY REFERENCES
          public.platform_saas_payment_attempts_v2(id) ON DELETE RESTRICT,
        decision text NOT NULL CHECK (decision IN ('approved','rejected')),
        status text NOT NULL DEFAULT 'pending' CHECK (
          status IN ('pending','sending','sent')
        ),
        attempts integer NOT NULL DEFAULT 0 CHECK (attempts>=0),
        available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        claimed_at timestamptz,
        delivered_at timestamptz
      )
    """)
    op.execute("""
      CREATE INDEX ix_saas_review_result_due
      ON public.platform_saas_review_result_outbox(available_at,attempt_id)
      WHERE status IN ('pending','sending')
    """)
    op.execute("""
      ALTER TABLE public.platform_saas_review_result_outbox
      ENABLE ROW LEVEL SECURITY
    """)
    op.execute("""
      ALTER TABLE public.platform_saas_review_result_outbox
      FORCE ROW LEVEL SECURITY
    """)
    op.execute("""
      REVOKE ALL ON TABLE public.platform_saas_review_result_outbox FROM PUBLIC
    """)
    op.execute("""
      DO $revoke$
      BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
        THEN
          REVOKE ALL ON TABLE public.platform_saas_review_result_outbox
          FROM membership_runtime;
        END IF;
      END $revoke$
    """)

    op.execute("""
      CREATE FUNCTION public.platform_saas_enqueue_review_result()
      RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      BEGIN
        -- Only real submitted proofs have a recipient. Both decision tables
        -- preserve one immutable row per attempt.
        IF EXISTS(
          SELECT 1 FROM public.platform_saas_payment_proofs_v2 p
          WHERE p.attempt_id=NEW.attempt_id
        ) THEN
          INSERT INTO public.platform_saas_review_result_outbox(
            attempt_id,decision
          ) VALUES (
            NEW.attempt_id,
            CASE WHEN TG_TABLE_NAME='platform_saas_payment_rejections_v2'
              THEN 'rejected' ELSE 'approved' END
          )
          ON CONFLICT (attempt_id) DO NOTHING;
        END IF;
        RETURN NEW;
      END; $f$
    """)
    for table, trigger in (
        ("platform_saas_payment_allocations_v2", "trg_saas_review_result_approved"),
        ("platform_saas_payment_rejections_v2", "trg_saas_review_result_rejected"),
    ):
        op.execute(f"""
          CREATE TRIGGER {trigger}
          AFTER INSERT ON public.{table}
          FOR EACH ROW
          EXECUTE FUNCTION public.platform_saas_enqueue_review_result()
        """)

    op.execute("""
      CREATE FUNCTION public.platform_saas_review_result_claim()
      RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      DECLARE item record; now_at timestamptz:=clock_timestamp();
      BEGIN
        PERFORM public.platform_saas_require_owner();
        SELECT n.attempt_id,n.decision INTO item
        FROM public.platform_saas_review_result_outbox n
        WHERE n.available_at<=now_at
          AND (
            n.status='pending'
            OR (n.status='sending'
                AND n.claimed_at<now_at-interval '5 minutes')
          )
        ORDER BY n.available_at,n.attempt_id
        LIMIT 1 FOR UPDATE OF n SKIP LOCKED;
        IF NOT FOUND THEN RETURN NULL; END IF;

        UPDATE public.platform_saas_review_result_outbox n
        SET status='sending',attempts=n.attempts+1,claimed_at=now_at
        WHERE n.attempt_id=item.attempt_id;

        RETURN (
          SELECT jsonb_build_object(
            'id',n.attempt_id,'decision',n.decision,
            'generation',n.attempts,
            'source_bot',p.source_bot_id,
            'source_chat',p.source_chat_id,
            'source_project',b.project_id,
            'amount',a.amount_currency::text,'currency',a.currency_code,
            'reason',CASE WHEN n.decision='rejected' THEN (
              SELECT r.reason FROM public.platform_saas_payment_rejections_v2 r
              WHERE r.attempt_id=n.attempt_id
            ) ELSE NULL END
          )
          FROM public.platform_saas_review_result_outbox n
          JOIN public.platform_saas_payment_attempts_v2 a
            ON a.id=n.attempt_id
          JOIN public.platform_saas_payment_proofs_v2 p
            ON p.attempt_id=n.attempt_id
          LEFT JOIN public.bot_instances b
            ON b.telegram_bot_id=p.source_bot_id AND b.status='registered'
          WHERE n.attempt_id=item.attempt_id
        );
      END; $f$
    """)
    op.execute("""
      CREATE FUNCTION public.platform_saas_review_result_finish(
        p_attempt uuid,p_generation integer,p_sent boolean
      ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
      SET search_path=pg_catalog,public AS $f$
      DECLARE changed integer;
      BEGIN
        PERFORM public.platform_saas_require_owner();
        IF p_attempt IS NULL OR p_generation IS NULL OR p_sent IS NULL
        THEN RAISE EXCEPTION 'Invalid review-result acknowledgement'
          USING ERRCODE='22023'; END IF;

        UPDATE public.platform_saas_review_result_outbox n
        SET status=CASE WHEN p_sent THEN 'sent' ELSE 'pending' END,
            delivered_at=CASE WHEN p_sent THEN clock_timestamp()
                              ELSE NULL END,
            available_at=CASE WHEN p_sent THEN n.available_at
              ELSE clock_timestamp()+make_interval(
                secs=>LEAST(1800,30*(1<<LEAST(n.attempts,6)))
              ) END,
            claimed_at=NULL
        WHERE n.attempt_id=p_attempt AND n.status='sending'
          AND n.attempts=p_generation;
        GET DIAGNOSTICS changed=ROW_COUNT;
        RETURN changed=1;
      END; $f$
    """)
    op.execute("""
      REVOKE ALL ON FUNCTION
        public.platform_saas_enqueue_review_result() FROM PUBLIC
    """)
    for signature in (
        "platform_saas_review_result_claim()",
        "platform_saas_review_result_finish(uuid,integer,boolean)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"""
          DO $grant$
          BEGIN
            IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='membership_runtime')
            THEN
              GRANT EXECUTE ON FUNCTION public.{signature}
              TO membership_runtime;
            END IF;
          END $grant$
        """)


def downgrade() -> None:
    raise RuntimeError("Do not silently discard pending financial decisions")
