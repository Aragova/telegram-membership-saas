from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


class TenantContextError(RuntimeError):
    """Base error for missing or invalid tenant context."""


class TenantAccessDenied(TenantContextError):
    """Raised when the current user cannot activate a project scope."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    user_id: UUID
    project_id: UUID | None = None


_current_tenant_context: ContextVar[TenantContext | None] = ContextVar(
    "current_tenant_context",
    default=None,
)


def get_tenant_context(*, require_project: bool = False) -> TenantContext:
    context = _current_tenant_context.get()
    if context is None:
        raise TenantContextError("Tenant context is not set")
    if require_project and context.project_id is None:
        raise TenantContextError("Project scope is required")
    return context


@contextmanager
def tenant_context(context: TenantContext) -> Iterator[TenantContext]:
    token = _current_tenant_context.set(context)
    try:
        yield context
    finally:
        _current_tenant_context.reset(token)
