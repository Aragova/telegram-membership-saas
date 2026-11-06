"""Permanent merchant identities and historical project/channel observations.

Revision ID: 0026_merchant_continuity
Revises: 0025_owner_notifications

Schema-only foundation. Every legacy project starts with its own merchant;
no business or financial history is auto-merged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_merchant_continuity"
down_revision: str | None = "0025_owner_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "initial_owner_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_merchant_identities_initial_owner", "merchant_identities",
        ["initial_owner_user_id"],
    )
    op.create_table(
        "merchant_project_periods",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "merchant_id", sa.Uuid(),
            sa.ForeignKey("merchant_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # These are historical references, not cascading project links.
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "owner_user_id", sa.Uuid(),
            sa.ForeignKey("telegram_users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_merchant_project_period_dates",
        ),
    )
    op.create_index(
        "uq_merchant_project_current", "merchant_project_periods",
        ["project_id"], unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_merchant_project_merchant", "merchant_project_periods",
        ["merchant_id", "started_at"],
    )
    op.create_table(
        "merchant_channel_periods",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "merchant_id", sa.Uuid(),
            sa.ForeignKey("merchant_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_chat_id", sa.String(32), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "resource_type IN ('telegram_channel', 'telegram_group')",
            name="ck_merchant_channel_resource_type",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_merchant_channel_period_dates",
        ),
    )
    op.create_index(
        "uq_merchant_channel_current_resource", "merchant_channel_periods",
        ["resource_id"], unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index(
        "ix_merchant_channel_chat", "merchant_channel_periods",
        ["telegram_chat_id", "started_at"],
    )
    op.create_index(
        "ix_merchant_channel_merchant", "merchant_channel_periods",
        ["merchant_id", "started_at"],
    )

    # Keep private platform continuity tables inaccessible to standard RLS
    # tenant connections, even if the runtime role has broad DML grants.
    for table in (
        "merchant_identities", "merchant_project_periods",
        "merchant_channel_periods",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")

    # Backfill: one new independent identity per existing project. In
    # particular, an equal Telegram owner is NOT proof of one business.
    op.add_column("projects", sa.Column("merchant_id", sa.Uuid(), nullable=True))
    op.execute("""
        UPDATE public.projects SET merchant_id = gen_random_uuid()
        WHERE merchant_id IS NULL
    """)
    op.execute("""
        INSERT INTO public.merchant_identities(
            id, initial_owner_user_id, created_at
        )
        SELECT merchant_id, owner_user_id, created_at FROM public.projects
    """)
    op.execute("""
        INSERT INTO public.merchant_project_periods(
            id, merchant_id, project_id, owner_user_id, started_at
        )
        SELECT gen_random_uuid(), merchant_id, id, owner_user_id, created_at
        FROM public.projects
    """)
    op.alter_column(
        "projects", "merchant_id", nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )
    op.create_foreign_key(
        "fk_projects_merchant_id", "projects",
        "merchant_identities", ["merchant_id"], ["id"], ondelete="RESTRICT",
    )

    # Historical Telegram chat IDs are merely observations until later
    # ownership verification. Multiple projects may mention the same chat.
    op.execute("""
        INSERT INTO public.merchant_channel_periods(
            id, merchant_id, project_id, resource_id, telegram_chat_id,
            resource_type, started_at
        )
        SELECT gen_random_uuid(), p.merchant_id, r.project_id,
               r.id, r.external_id, r.resource_type, r.created_at
        FROM public.resources r
        JOIN public.projects p ON p.id = r.project_id
        WHERE r.external_id ~ '^-?[1-9][0-9]{0,18}$'
    """)

    # Function owner is the privileged migration role; callers cannot
    # directly insert merchant identities or choose an existing Merchant ID.
    op.execute("""
        CREATE FUNCTION public.merchant_assign_project_identity()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $merchant$
        BEGIN
            NEW.merchant_id := gen_random_uuid();
            INSERT INTO public.merchant_identities(
                id, initial_owner_user_id, created_at
            ) VALUES (NEW.merchant_id, NEW.owner_user_id,
                      COALESCE(NEW.created_at, now()));
            RETURN NEW;
        END
        $merchant$
    """)
    op.execute("""
        CREATE TRIGGER trg_merchant_project_assign
        BEFORE INSERT ON public.projects FOR EACH ROW
        EXECUTE FUNCTION public.merchant_assign_project_identity()
    """)

    op.execute("""
        CREATE FUNCTION public.merchant_track_project()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $merchant$
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
                    RAISE EXCEPTION
                      'Merchant/owner reassignment needs verified transfer workflow'
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
        END
        $merchant$
    """)
    for operation in ("INSERT", "UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_merchant_project_{operation.lower()} "
            f"AFTER {operation} ON public.projects FOR EACH ROW "
            "EXECUTE FUNCTION public.merchant_track_project()"
        )
    # AFTER UPDATE is too late for caller side effects? It still runs in
    # the same transaction and rolls everything back on the raised error.

    op.execute("""
        CREATE FUNCTION public.merchant_track_channel()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $merchant$
        DECLARE
            mid uuid;
        BEGIN
            IF TG_OP IN ('DELETE', 'UPDATE') THEN
                IF TG_OP = 'UPDATE' AND NEW.external_id = OLD.external_id
                  AND NEW.project_id = OLD.project_id
                  AND NEW.resource_type = OLD.resource_type THEN
                    RETURN NEW;
                END IF;
                UPDATE public.merchant_channel_periods
                   SET ended_at = GREATEST(now(), started_at)
                 WHERE resource_id = OLD.id AND ended_at IS NULL;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
            END IF;
            IF NEW.external_id !~ '^-?[1-9][0-9]{0,18}$' THEN
                RETURN NEW;
            END IF;
            SELECT p.merchant_id INTO STRICT mid
              FROM public.projects p WHERE p.id = NEW.project_id;
            INSERT INTO public.merchant_channel_periods(
                id, merchant_id, project_id, resource_id, telegram_chat_id,
                resource_type, started_at
            ) VALUES (
                gen_random_uuid(), mid, NEW.project_id, NEW.id,
                NEW.external_id, NEW.resource_type, now()
            );
            RETURN NEW;
        END
        $merchant$
    """)
    for operation in ("INSERT", "UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_merchant_channel_{operation.lower()} "
            f"AFTER {operation} ON public.resources FOR EACH ROW "
            "EXECUTE FUNCTION public.merchant_track_channel()"
        )

    for fn in (
        "merchant_assign_project_identity",
        "merchant_track_project", "merchant_track_channel",
    ):
        op.execute(
            f"REVOKE ALL ON FUNCTION public.{fn}() FROM PUBLIC"
        )


def downgrade() -> None:
    for table, prefix in (
        ("resources", "channel"), ("projects", "project"),
    ):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(
                f"DROP TRIGGER IF EXISTS "
                f"trg_merchant_{prefix}_{operation.lower()} ON public.{table}"
            )
    op.execute("DROP TRIGGER IF EXISTS trg_merchant_project_assign ON public.projects")
    for fn in (
        "merchant_track_channel", "merchant_track_project",
        "merchant_assign_project_identity",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{fn}()")
    op.drop_constraint("fk_projects_merchant_id", "projects", type_="foreignkey")
    op.drop_column("projects", "merchant_id")
    op.drop_table("merchant_channel_periods")
    op.drop_table("merchant_project_periods")
    op.drop_table("merchant_identities")
