"""Durable Telegram Stars precheckout authorization without mutating paid rows.

Revision ID: 0063_file_stars_authorized
Revises: 0062_crypto_recovery_delivery
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_file_stars_authorized"
down_revision: str | None = "0062_crypto_recovery_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The purchase table's existing trigger only permits actual Stars
    # payment confirmation. Do not relax it for authorization states.
    op.create_unique_constraint(
        "uq_file_purchase_id_project", "file_sale_purchases",
        ["id", "project_id"],
    )
    op.create_table(
        "file_sale_star_authorizations",
        sa.Column("purchase_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "authorized_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["purchase_id", "project_id"],
            ["file_sale_purchases.id", "file_sale_purchases.project_id"],
            name="fk_file_star_auth_purchase_project", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "purchase_id", "project_id", name="uq_file_star_auth_project",
        ),
    )
    op.create_table(
        "file_sale_star_authorization_rejections",
        sa.Column("purchase_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "rejected_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["purchase_id", "project_id"],
            [
                "file_sale_star_authorizations.purchase_id",
                "file_sale_star_authorizations.project_id",
            ],
            name="fk_file_star_auth_rejection_project", ondelete="RESTRICT",
        ),
    )
    for table in (
        "file_sale_star_authorizations",
        "file_sale_star_authorization_rejections",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {table}_scope ON public.{table}
            FOR ALL USING (
                project_id = NULLIF(
                    current_setting('app.current_project_id',true),''
                )::uuid
            ) WITH CHECK (
                project_id = NULLIF(
                    current_setting('app.current_project_id',true),''
                )::uuid
            )
        """)
    op.execute("""
        CREATE FUNCTION public.guard_file_star_authorization_immutable()
        RETURNS trigger LANGUAGE plpgsql
        SET search_path=pg_catalog,public AS $f$
        BEGIN
          RAISE EXCEPTION 'Stars precheckout authorization is immutable'
            USING ERRCODE='42501';
        END $f$
    """)
    op.execute("""
        CREATE TRIGGER trg_file_star_authorization_immutable
        BEFORE UPDATE OR DELETE ON public.file_sale_star_authorizations
        FOR EACH ROW EXECUTE FUNCTION
          public.guard_file_star_authorization_immutable()
    """)
    op.execute("""
        CREATE TRIGGER trg_file_star_auth_rejection_immutable
        BEFORE UPDATE OR DELETE ON public.file_sale_star_authorization_rejections
        FOR EACH ROW EXECUTE FUNCTION
          public.guard_file_star_authorization_immutable()
    """)
    op.execute("""
        DO $grants$
        BEGIN
          IF EXISTS (
              SELECT 1 FROM pg_roles WHERE rolname='membership_runtime'
          ) THEN
            GRANT SELECT,INSERT ON public.file_sale_star_authorizations
              TO membership_runtime;
            GRANT SELECT,INSERT
              ON public.file_sale_star_authorization_rejections
              TO membership_runtime;
          END IF;
        END $grants$
    """)


def downgrade() -> None:
    raise RuntimeError(
        "Cannot discard Stars precheckout authorizations without verified reconciliation."
    )
