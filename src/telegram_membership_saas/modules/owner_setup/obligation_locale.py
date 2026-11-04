"""Presentation-only translations for owner-approved retirement obligations.

Never change the underlying financial decision, audit record, or stored
owner-authored text according to a chat language. Only outbound UI varies.
"""
from telegram_membership_saas.localization import tr

_OBLIGATION_MESSAGES_EN = {
    "Заявка на оплату закрыта владельцем. "
    "Если деньги были переведены, свяжитесь с владельцем канала.":
        "The channel owner closed your payment request. "
        "If you already transferred the money, contact the channel owner.",
    "Владелец отклонил заявку. Если вы уже перевели деньги, "
    "свяжитесь с владельцем канала.":
        "The channel owner rejected your payment request. "
        "If you already sent money, contact the channel owner.",
    "Владелец завершил подписку. Оплаченный срок мог не закончиться; "
    "по вопросам возврата свяжитесь с владельцем.":
        "The channel owner ended your subscription before its paid term "
        "may have expired. Contact the owner about any refund.",
    "Владелец отозвал доступ к ресурсу. "
    "Если остались другие действующие права, доступ сохраняется.":
        "The channel owner revoked one access grant. "
        "If other active grants remain, your access is preserved.",
    "Заявка уже закрыта": "This payment request is already closed.",
    "Подписка уже завершена": "This subscription has already ended.",
    "Право уже отозвано": "This access grant has already been revoked.",
    "Доступ сохраняется по другому действующему праву":
        "Access remains active because another valid grant exists.",
    "Отзыв поставлен в очередь Telegram":
        "Access revocation was queued for Telegram.",
    "Состояние синхронизировано; проверьте фактический доступ":
        "Access state synchronized; verify actual Telegram membership.",
}

_OBLIGATION_ERRORS_EN = {
    "Действие доступно только владельцу проекта":
        "Only the project owner can perform this action.",
    "Недопустимая страница": "Invalid page.",
    "Заявка не найдена в этом проекте": "Payment request not found in this project.",
    "Заявку уже обработали. Обновите список.":
        "This request was already reviewed. Refresh the list.",
    "Подписка не найдена в этом проекте":
        "Subscription not found in this project.",
    "Подписка уже неактивна": "This subscription is no longer active.",
    "Право доступа не найдено": "Access grant not found.",
    "Оплаченный доступ закрывается через завершение подписки":
        "End the paid subscription before revoking this grant.",
    "Доступ не найден в этом проекте": "Access state not found in this project.",
}


def obligation_message(message: str) -> str:
    return tr(message, _OBLIGATION_MESSAGES_EN.get(message, message))


def obligation_error(message: str) -> str:
    """Do not expose unrecognized internals to English-facing Telegram alerts."""
    return tr(message, _OBLIGATION_ERRORS_EN.get(message, "Action unavailable. Refresh the list."))


def obligation_status(status: str) -> str:
    labels = {
        "waiting_confirmation": "Awaiting payment",
        "waiting_review": "Pending review",
        "approved": "Approved",
        "rejected": "Rejected",
        "cancelled": "Canceled",
        "active": "Active",
        "expired": "Expired",
        "revoked": "Revoked",
        "granted": "Granted",
        "pending": "Pending",
        "in_sync": "Synchronized",
    }
    return tr(status, labels.get(status, status))
