from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_membership_saas.db_base import Base


class EntitlementSource(StrEnum):
    PAYMENT = "payment"
    MANUAL = "manual"
    OWNER_SERVICE = "owner_service"
    PROMOCODE = "promocode"
    TRIAL = "trial"
    REFERRAL = "referral"
    IMPORT = "import"
    COMPENSATION = "compensation"


class EntitlementGrantStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class EntitlementGrant(Base):
    __tablename__ = "entitlement_grants"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_entitlement_grants_id_project"),
        UniqueConstraint(
            "project_id",
            "source",
            "source_id",
            "resource_id",
            name="uq_entitlement_grants_source_resource",
        ),
        ForeignKeyConstraint(
            ["customer_id", "project_id"],
            ["project_customers.id", "project_customers.project_id"],
            name="fk_entitlement_grants_customer_project",
        ),
        ForeignKeyConstraint(
            ["resource_id", "project_id"],
            ["resources.id", "resources.project_id"],
            name="fk_entitlement_grants_resource_project",
        ),
        CheckConstraint(
            "source IN ('payment', 'manual', 'owner_service', 'promocode', 'trial', "
            "'referral', 'import', 'compensation')",
            name="ck_entitlement_grants_source",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_entitlement_grants_status",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_entitlement_grants_validity",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= valid_from",
            name="ck_entitlement_grants_revoked_at",
        ),
        CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_entitlement_grants_status_revocation",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    customer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=EntitlementGrantStatus.ACTIVE.value,
        server_default=EntitlementGrantStatus.ACTIVE.value,
        nullable=False,
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
