from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from telegram_membership_saas.modules.access.models import AccessActionType


@dataclass(frozen=True, slots=True)
class AccessCommand:
    action_id: UUID
    project_id: UUID
    customer_id: UUID
    resource_id: UUID
    action_type: AccessActionType


class AccessAdapter(Protocol):
    async def apply(self, command: AccessCommand) -> None: ...
