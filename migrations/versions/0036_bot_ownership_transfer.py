"""Dual-consent bot management transfer with immutable merchant periods.

Revision ID: 0036_bot_ownership_transfer
Revises: 0035_support_moderation_audit
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_bot_ownership_transfer"
down_revision: str | None = "0035_support_moderation_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"
SCOPE = "NULLIF(current_setting('app.current_project_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "project_ownership_transfers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id", sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "from_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "to_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_username", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("encrypted_new_token", sa.String(4096), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("from_user_id <> to_user_id", name="ck_ownership_transfer_distinct"),
        sa.CheckConstraint("status IN ('pending','accepted','cancelled','completed')",
                           name="ck_ownership_transfer_status"),
        sa.CheckConstraint("expires_at > created_at", name="ck_ownership_transfer_expiry"),
    )
    op.create_index(
        "uq_project_one_open_transfer", "project_ownership_transfers",
        ["project_id"], unique=True,
        postgresql_where=sa.text("status IN ('pending', 'accepted')"),
    )
    op.create_index(
        "ix_project_transfers_recipient", "project_ownership_transfers",
        ["to_user_id", "status"],
    )
    op.execute("ALTER TABLE project_ownership_transfers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_ownership_transfers FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY transfer_view ON project_ownership_transfers FOR SELECT
        USING (from_user_id = {USER} OR to_user_id = {USER})
    """)
    op.execute(f"""
        CREATE POLICY transfer_create ON project_ownership_transfers FOR INSERT
        WITH CHECK (
            project_id = {SCOPE} AND from_user_id = {USER}
            AND status = 'pending' AND encrypted_new_token IS NULL
            AND accepted_at IS NULL AND completed_at IS NULL
            AND EXISTS (
                SELECT 1 FROM projects p WHERE p.id = project_id
                  AND p.owner_user_id = {USER} AND p.status = 'active'
            )
        )
    """)
    op.execute(f"""
        CREATE POLICY transfer_modify ON project_ownership_transfers FOR UPDATE
        USING (from_user_id = {USER} OR to_user_id = {USER})
        WITH CHECK (from_user_id = {USER} OR to_user_id = {USER})
    """)
    # Every status transition is checked against the actor, not just its
    # access to the row. In particular, only an inner project UPDATE trigger
    # can mark a transfer completed; no direct transfer-row DML can do it.
    op.execute(f"""
        CREATE FUNCTION public.guard_project_ownership_transfer()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $transfer$
        DECLARE who uuid := {USER};
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.from_user_id IS DISTINCT FROM OLD.from_user_id
                OR NEW.to_user_id IS DISTINCT FROM OLD.to_user_id
                OR NEW.telegram_bot_id IS DISTINCT FROM OLD.telegram_bot_id
                OR NEW.bot_username IS DISTINCT FROM OLD.bot_username
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
            THEN
                RAISE EXCEPTION 'Ownership transfer identity is immutable'
                    USING ERRCODE = '42501';
            END IF;
            IF OLD.status = 'pending' AND NEW.status = 'accepted'
               AND who = OLD.to_user_id AND OLD.expires_at > now()
               AND NEW.encrypted_new_token IS NOT NULL
               AND NEW.accepted_at IS NOT NULL
               AND NEW.completed_at IS NULL AND NEW.cancelled_at IS NULL
               AND EXISTS(
                    SELECT 1 FROM public.projects p
                    WHERE p.id = OLD.project_id AND p.owner_user_id = OLD.from_user_id
                      AND p.status = 'active'
               )
            THEN
                RETURN NEW;
            END IF;
            IF OLD.status IN ('pending','accepted') AND NEW.status = 'cancelled'
               AND who IN (OLD.from_user_id, OLD.to_user_id)
               AND NEW.encrypted_new_token IS NULL
               AND NEW.cancelled_at IS NOT NULL AND NEW.completed_at IS NULL
            THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'accepted' AND NEW.status = 'completed'
               AND pg_trigger_depth() > 1 AND who = OLD.from_user_id
               AND NEW.encrypted_new_token IS NULL
               AND NEW.completed_at IS NOT NULL AND NEW.cancelled_at IS NULL
               AND EXISTS(
                    SELECT 1 FROM public.projects p
                    WHERE p.id = OLD.project_id AND p.owner_user_id = OLD.to_user_id
               )
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Unauthorized ownership transfer transition'
                USING ERRCODE = '42501';
        END $transfer$
    """)
    op.execute("""
        CREATE TRIGGER trg_guard_project_ownership_transfer
        BEFORE UPDATE ON project_ownership_transfers
        FOR EACH ROW EXECUTE FUNCTION public.guard_project_ownership_transfer()
    """)
    op.execute("REVOKE ALL ON FUNCTION public.guard_project_ownership_transfer() FROM PUBLIC")
    # Keep original INSERT/DELETE history behavior. An owner change is only
    # possible after the recipient has supplied and verified a fresh bot token
    # in their own Telegram session, and the initiator finalizes the transfer.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.merchant_track_project()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $merchant$
        DECLARE
            approved uuid;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO public.merchant_project_periods(
                    id, merchant_id, project_id, owner_user_id, started_at
                ) VALUES (
                    gen_random_uuid(), NEW.merchant_id, NEW.id,
                    NEW.owner_user_id, COALESCE(NEW.created_at, now())
                );
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                IF NEW.merchant_id IS DISTINCT FROM OLD.merchant_id THEN
                    RAISE EXCEPTION 'Commercial identity cannot be reassigned'
                        USING ERRCODE = '42501';
                END IF;
                IF NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id THEN
                    IF {USER} IS DISTINCT FROM OLD.owner_user_id
                       OR {SCOPE} IS DISTINCT FROM OLD.id THEN
                        RAISE EXCEPTION 'Transfer project owner denied'
                            USING ERRCODE = '42501';
                    END IF;
                    SELECT t.id INTO approved FROM public.project_ownership_transfers t
                    WHERE t.project_id = OLD.id
                      AND t.from_user_id = OLD.owner_user_id
                      AND t.to_user_id = NEW.owner_user_id
                      AND t.status = 'accepted'
                      AND t.encrypted_new_token IS NOT NULL
                      AND t.expires_at > now()
                      AND EXISTS (
                          SELECT 1 FROM public.bot_instances bot
                          WHERE bot.project_id = OLD.id
                            AND bot.telegram_bot_id = t.telegram_bot_id
                            AND bot.status = 'registered'
                            AND bot.token_ciphertext = t.encrypted_new_token
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM public.payment_methods method
                          WHERE method.project_id = OLD.id AND method.status = 'active'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM public.payment_requests request
                          WHERE request.project_id = OLD.id
                            AND request.status IN ('waiting_confirmation', 'waiting_review')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM public.entitlement_grants grant_row
                          WHERE grant_row.project_id = OLD.id
                            AND grant_row.source = 'owner_service'
                            AND grant_row.status = 'active'
                      )
                    ORDER BY t.created_at DESC LIMIT 1 FOR UPDATE;
                    IF approved IS NULL THEN
                        RAISE EXCEPTION
                            'Verified dual-consent and liability checks required'
                            USING ERRCODE = '42501';
                    END IF;
                    IF EXISTS(
                        SELECT 1 FROM public.platform_owner_bot_limits lim
                        WHERE lim.owner_user_id = NEW.owner_user_id
                        AND (
                            SELECT COUNT(*) FROM public.bot_instances b
                            JOIN public.projects p ON p.id = b.project_id
                            WHERE p.owner_user_id = NEW.owner_user_id
                              AND b.status = 'registered'
                        ) > lim.max_connected_bots
                    ) THEN
                        RAISE EXCEPTION 'Recipient connected-bot quota exceeded'
                            USING ERRCODE = 'P6701';
                    END IF;
                    UPDATE public.merchant_project_periods
                    SET ended_at = GREATEST(now(), started_at)
                    WHERE project_id = OLD.id AND merchant_id = OLD.merchant_id
                      AND owner_user_id = OLD.owner_user_id
                      AND ended_at IS NULL;
                    INSERT INTO public.merchant_project_periods(
                        id, merchant_id, project_id, owner_user_id, started_at
                    ) VALUES (
                        gen_random_uuid(), OLD.merchant_id, OLD.id,
                        NEW.owner_user_id, now()
                    );
                    UPDATE public.project_ownership_transfers
                    SET status = 'completed', completed_at = now(),
                        encrypted_new_token = NULL
                    WHERE id = approved;
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                UPDATE public.merchant_project_periods
                SET ended_at = GREATEST(now(), started_at)
                WHERE project_id = OLD.id AND ended_at IS NULL;
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'Invalid merchant project operation';
        END $merchant$
    """)


    # A plain tenant UPDATE is intentionally blocked by projects FORCE RLS:
    # the old owner must not SELECT the row under its new owner after UPDATE.
    # A narrow privileged function performs only the checked, consumed
    # transfer and leaves the merchant history trigger in control.
    op.execute(f"""
        CREATE FUNCTION public.complete_bot_ownership_transfer(
            target_project uuid, transfer_id uuid
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $transfer$
        DECLARE
            actor uuid := {USER};
            scope uuid := {SCOPE};
            project_owner uuid;
            recipient uuid;
        BEGIN
            IF actor IS NULL OR scope IS DISTINCT FROM target_project THEN
                RAISE EXCEPTION 'Transfer scope denied' USING ERRCODE = '42501';
            END IF;
            SELECT owner_user_id INTO project_owner
            FROM public.projects
            WHERE id = target_project AND status = 'active'
            FOR UPDATE;
            IF project_owner IS NULL OR project_owner IS DISTINCT FROM actor THEN
                RAISE EXCEPTION 'Only the current owner can finalize'
                    USING ERRCODE = '42501';
            END IF;
            SELECT to_user_id INTO recipient
            FROM public.project_ownership_transfers
            WHERE id = transfer_id AND project_id = target_project
              AND from_user_id = actor AND status = 'accepted'
              AND expires_at > now() AND encrypted_new_token IS NOT NULL
            FOR UPDATE;
            IF recipient IS NULL THEN
                RAISE EXCEPTION 'Recipient consent missing' USING ERRCODE = '42501';
            END IF;
            -- merchant_track_project performs final token/payee/liability
            -- checks, writes both historical owner periods and consumes
            -- the accepted invitation in this same transaction.
            UPDATE public.projects SET owner_user_id = recipient
            WHERE id = target_project AND owner_user_id = actor;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Project owner changed during transfer'
                    USING ERRCODE = '42501';
            END IF;
        END $transfer$
    """)
    op.execute(
        "REVOKE ALL ON FUNCTION public.complete_bot_ownership_transfer(uuid,uuid) "
        "FROM PUBLIC"
    )
    op.execute("""
        DO $transfer_grant$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'membership_runtime'
            ) THEN
                GRANT EXECUTE ON FUNCTION
                    public.complete_bot_ownership_transfer(uuid,uuid)
                TO membership_runtime;
            END IF;
        END $transfer_grant$
    """)


def downgrade() -> None:
    # Completed transfers must retain immutable merchant history. A rollback
    # cannot silently erase the consent/ownership audit records.
    op.execute("""
        DO $$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM project_ownership_transfers WHERE status = 'completed'
          ) THEN
              RAISE EXCEPTION 'Cannot downgrade completed ownership transfers';
          END IF;
        END $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.merchant_track_project()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $merchant$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO public.merchant_project_periods(
                    id, merchant_id, project_id, owner_user_id, started_at
                ) VALUES (
                    gen_random_uuid(), NEW.merchant_id, NEW.id,
                    NEW.owner_user_id, COALESCE(NEW.created_at, now())
                );
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                IF NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
                   OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id THEN
                    RAISE EXCEPTION 'Merchant/owner reassignment needs verified transfer workflow'
                        USING ERRCODE = '42501';
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                UPDATE public.merchant_project_periods
                SET ended_at = GREATEST(now(), started_at)
                WHERE project_id = OLD.id AND ended_at IS NULL;
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'Invalid merchant project operation';
        END $merchant$
    """)
    op.execute("DROP FUNCTION public.complete_bot_ownership_transfer(uuid,uuid)")
    op.execute("DROP TRIGGER trg_guard_project_ownership_transfer ON project_ownership_transfers")
    op.execute("DROP FUNCTION public.guard_project_ownership_transfer()")
    op.drop_index("ix_project_transfers_recipient", table_name="project_ownership_transfers")
    op.drop_index("uq_project_one_open_transfer", table_name="project_ownership_transfers")
    op.drop_table("project_ownership_transfers")
