"""Owner-only, read-only lookup for an existing pending receipt's source bot.

Revision ID: 0080_saas_proof_view
Revises: 0079_saas_payment_catalog

Returns no token, media bytes or tenant-secret field. The central operator
may load a proof through its original bot only while the proof is awaiting
a bank decision. A NULL project also permits a genuine central-bot
proof, verified against the active central Bot ID in the handler.
No billing switches change.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0080_saas_proof_view"
down_revision: str | None = "0079_saas_payment_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE FUNCTION public.platform_saas_pending_proof_source(
      p_attempt uuid
    ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog,public AS $f$
    DECLARE payload jsonb;
    BEGIN
      PERFORM public.platform_saas_require_owner();
      IF p_attempt IS NULL THEN
        RAISE EXCEPTION 'Missing payment attempt'
          USING ERRCODE='22023';
      END IF;
      SELECT jsonb_build_object(
        'id',a.id,'invoice_id',a.invoice_id,
        'amount',a.amount_currency::text,'currency',a.currency_code,
        'instructions',a.instructions_snapshot,
        'source_bot',p.source_bot_id,
        'source_project',b.project_id,
        'source_file',p.source_file_id,'file_type',p.file_type
      ) INTO payload
      FROM public.platform_saas_payment_attempts_v2 a
      JOIN public.platform_saas_payment_proofs_v2 p
        ON p.attempt_id=a.id
      LEFT JOIN public.bot_instances b
        ON b.telegram_bot_id=p.source_bot_id
       AND b.status='registered'
      WHERE a.id=p_attempt
        AND NOT EXISTS (
          SELECT 1 FROM public.platform_saas_payment_allocations_v2 ok
          WHERE ok.attempt_id=a.id
        )
        AND NOT EXISTS (
          SELECT 1 FROM public.platform_saas_payment_rejections_v2 no
          WHERE no.attempt_id=a.id
        );
      RETURN payload;
    END; $f$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "public.platform_saas_pending_proof_source(uuid) FROM PUBLIC"
    )
    op.execute("""
      DO $grant$
      BEGIN
        IF EXISTS (
          SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
        ) THEN
          GRANT EXECUTE ON FUNCTION
            public.platform_saas_pending_proof_source(uuid)
            TO membership_runtime;
        END IF;
      END $grant$
    """)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.platform_saas_pending_proof_source(uuid)"
    )
