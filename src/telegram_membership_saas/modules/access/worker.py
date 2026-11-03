from telegram_membership_saas.modules.access.adapter import AccessAdapter, AccessCommand
from telegram_membership_saas.modules.access.models import AccessAction, AccessActionType


async def apply_access_action(adapter: AccessAdapter, action: AccessAction) -> None:
    if action.attempt_count <= 0:
        raise ValueError("Access action must be claimed before it can be applied")
    await adapter.apply(
        AccessCommand(
            action_id=action.id,
            project_id=action.project_id,
            customer_id=action.customer_id,
            resource_id=action.resource_id,
            action_type=AccessActionType(action.action_type),
            claim_attempt=action.attempt_count,
        )
    )
