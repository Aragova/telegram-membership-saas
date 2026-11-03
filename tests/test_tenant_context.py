from uuid import uuid4

import pytest

from telegram_membership_saas.tenant_context import (
    TenantContext,
    TenantContextError,
    get_tenant_context,
    tenant_context,
)


def test_tenant_context_is_scoped_and_reset() -> None:
    context = TenantContext(user_id=uuid4(), project_id=uuid4())

    with pytest.raises(TenantContextError):
        get_tenant_context()

    with tenant_context(context):
        assert get_tenant_context() == context
        assert get_tenant_context(require_project=True).project_id == context.project_id

    with pytest.raises(TenantContextError):
        get_tenant_context()


def test_project_can_be_required() -> None:
    context = TenantContext(user_id=uuid4())

    with tenant_context(context):
        assert get_tenant_context().project_id is None
        with pytest.raises(TenantContextError):
            get_tenant_context(require_project=True)
