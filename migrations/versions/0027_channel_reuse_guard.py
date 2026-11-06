"""Prevent same-owner Telegram channel reuse from resetting commercial identity.

Revision ID: 0027_channel_reuse_guard
Revises: 0026_merchant_continuity

A verified channel already associated with an existing connection of the
same owner must be re-used via that original project. Never silently merge
projects or financial rows, and never reveal another owner's history.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0027_channel_reuse_guard"
down_revision: str | None = "0026_merchant_continuity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION public.merchant_guard_channel_reuse()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $merchant$
        DECLARE
            current_owner uuid;
        BEGIN
            -- A disabled channel cannot be sold; check again on activation.
            IF NEW.status <> 'active'
               OR NEW.external_id !~ '^-?[1-9][0-9]{0,18}$' THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
               AND NEW.project_id IS NOT DISTINCT FROM OLD.project_id
               AND NEW.external_id IS NOT DISTINCT FROM OLD.external_id
               AND NEW.resource_type IS NOT DISTINCT FROM OLD.resource_type
               AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;

            SELECT p.owner_user_id INTO current_owner
              FROM public.projects p WHERE p.id = NEW.project_id;
            IF current_owner IS NULL THEN
                RETURN NEW; -- The resource FK will reject this project.
            END IF;

            -- Serialize even concurrent channel attachments in two projects:
            -- the second transaction must see the first committed observation.
            PERFORM pg_advisory_xact_lock(
                hashtextextended('vimi:channel:' || NEW.external_id, 0)
            );

            IF EXISTS (
                SELECT 1
                FROM public.merchant_channel_periods observation
                JOIN public.projects previous
                  ON previous.id = observation.project_id
                WHERE observation.telegram_chat_id = NEW.external_id
                  AND observation.project_id <> NEW.project_id
                  AND previous.owner_user_id = current_owner
            ) THEN
                RAISE EXCEPTION
                  'Channel belongs to another existing connection of this owner'
                  USING ERRCODE = 'P6501';
            END IF;

            RETURN NEW;
        END
        $merchant$
    """)
    op.execute("""
        CREATE TRIGGER trg_merchant_channel_reuse_insert
        BEFORE INSERT ON public.resources
        FOR EACH ROW
        EXECUTE FUNCTION public.merchant_guard_channel_reuse()
    """)
    op.execute("""
        CREATE TRIGGER trg_merchant_channel_reuse_update
        BEFORE UPDATE ON public.resources
        FOR EACH ROW
        EXECUTE FUNCTION public.merchant_guard_channel_reuse()
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.merchant_guard_channel_reuse() FROM PUBLIC"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_merchant_channel_reuse_update ON public.resources"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_merchant_channel_reuse_insert ON public.resources"
    )
    op.execute("DROP FUNCTION IF EXISTS public.merchant_guard_channel_reuse()")
