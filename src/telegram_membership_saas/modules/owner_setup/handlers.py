"""Owner bot administration with a restricted central connection registrar."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from html import escape
from secrets import randbelow
from time import monotonic
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import AiogramError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    KeyboardButtonRequestManagedBot,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from cryptography.fernet import InvalidToken
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.currency_display import display_currency, display_price
from telegram_membership_saas.db import (
    system_project_session_scope,
    tenant_session_scope,
)
from telegram_membership_saas.localization import (
    current_language,
    has_explicit_language_choice,
    recipient_language,
    tr,
)
from telegram_membership_saas.modules import platform_saas_gateway_handlers as gateway_ui
from telegram_membership_saas.modules import platform_saas_lava_handlers as lava_ui
from telegram_membership_saas.modules import platform_saas_stars_handlers as stars_ui
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.deletion_service import (
    request_bot_retirement,
    retire_unreachable_bot_with_orphan_actions,
)
from telegram_membership_saas.modules.bots.service import (
    TOKEN_PATTERN,
    decrypt_token,
    register_customer_bot,
)
from telegram_membership_saas.modules.broadcasts.service import cancel_draft
from telegram_membership_saas.modules.crypto_pay.owner_token_router import (
    remember_crypto_pay_owner,
)
from telegram_membership_saas.modules.crypto_pay.service import (
    connect_crypto_pay,
    disconnect_crypto_pay,
    read_crypto_pay_connection,
)
from telegram_membership_saas.modules.owner_notifications import (
    read_owner_settings,
    set_owner_setting,
)
from telegram_membership_saas.modules.owner_setup.account_bill_ui import (
    account_bill_classic,
    account_bill_rich,
)
from telegram_membership_saas.modules.owner_setup.bot_bill_ui import (
    bot_bill_classic,
    bot_bill_rich,
)
from telegram_membership_saas.modules.owner_setup.bulk_sales import (
    read_bulk_sales_state,
    switch_all_sales,
)
from telegram_membership_saas.modules.owner_setup.buyer_preview import (
    preview_catalog_screen,
    preview_crypto_method_screen,
    preview_method_screen,
    preview_offer_screen,
)
from telegram_membership_saas.modules.owner_setup.central_connection_flow_ui import (
    disconnect_card,
    obligations_card,
    project_disposal_card,
    project_disposal_success,
    rename_input_card,
    token_input_card,
)
from telegram_membership_saas.modules.owner_setup.channel_management import (
    read_channel_insights,
    remove_channel_without_paid_purchases,
)
from telegram_membership_saas.modules.owner_setup.channel_stage2_ui import (
    connection_problem_code,
    connection_result_classic,
    connection_result_rich,
    directory_classic,
    directory_rich,
    manual_classic,
    manual_rich,
    picker_classic,
    setup_stage_two_classic,
    setup_stage_two_rich,
)
from telegram_membership_saas.modules.owner_setup.connection_cards import (
    connection_management_classic,
    connection_management_rich,
    connection_success_classic,
    connection_success_rich,
)
from telegram_membership_saas.modules.owner_setup.connection_management import (
    archive_owner_connection,
    get_connection_disposition,
    list_archived_owner_connections,
    remove_unused_connection,
    rename_owner_connection,
    restore_owner_connection,
)
from telegram_membership_saas.modules.owner_setup.connection_registry_ui import (
    connection_registry_classic,
    connection_registry_rich,
)
from telegram_membership_saas.modules.owner_setup.conversations import (
    ADMIN_MESSAGE_HEADER,
    ADMIN_MESSAGE_HEADER_EN,
    BUYER_MESSAGE_HEADER,
    BUYER_MESSAGE_HEADER_EN,
    BUYER_REPLY_PROMPT,
    BUYER_REPLY_PROMPT_EN,
    MAX_DIALOG_TEXT,
    customer_ref,
    find_buyer_conversation,
    is_admin_message,
    parse_buyer_notification,
)
from telegram_membership_saas.modules.owner_setup.currencies import (
    disable_owner_stars_price,
    enable_owner_currency,
)
from telegram_membership_saas.modules.owner_setup.customer_shortcodes import (
    parse_customer_command,
)
from telegram_membership_saas.modules.owner_setup.dashboard_finances import (
    DashboardFinances,
    read_dashboard_finances,
)
from telegram_membership_saas.modules.owner_setup.dashboard_ui import (
    classic_dashboard,
    rich_dashboard,
)
from telegram_membership_saas.modules.owner_setup.error_locale import owner_error
from telegram_membership_saas.modules.owner_setup.labels import (
    owner_button_label,
    owner_runtime_label,
)
from telegram_membership_saas.modules.owner_setup.managed_bots import (
    connect_managed_bot_event,
    managed_bot_id_for_project,
    stage_managed_bot_candidate,
    sync_managed_bot_token,
)
from telegram_membership_saas.modules.owner_setup.method_management import (
    change_owner_method_status_guarded,
    delete_unused_owner_method,
    detach_owner_method_currency,
    edit_owner_method_instructions,
    rename_owner_method,
)
from telegram_membership_saas.modules.owner_setup.moderator_handlers import (
    notify_support_of_buyer_reply,
)
from telegram_membership_saas.modules.owner_setup.moderators import (
    list_project_moderators,
    set_project_moderator,
)
from telegram_membership_saas.modules.owner_setup.native_channel_stars import (
    NATIVE_PERIOD_SECONDS,
    activate_native_stars_link,
    deactivate_native_stars_link,
    read_native_stars_target,
)
from telegram_membership_saas.modules.owner_setup.network_home_service import (
    owner_invoice_statement_page,
    read_owner_network_home,
)
from telegram_membership_saas.modules.owner_setup.network_home_ui import (
    owner_network_home_classic,
    owner_network_home_rich,
)
from telegram_membership_saas.modules.owner_setup.network_profiles import (
    get_project_import_status,
    read_owner_network_profile,
    save_owner_network_profile,
    set_network_auto_apply,
)
from telegram_membership_saas.modules.owner_setup.obligation_locale import (
    obligation_error,
    obligation_message,
    obligation_status,
)
from telegram_membership_saas.modules.owner_setup.obligations import (
    close_owner_payment,
    end_owner_subscription,
    list_owner_obligations,
    retry_owner_access,
    revoke_owner_grant,
)
from telegram_membership_saas.modules.owner_setup.onboarding_ui import (
    owner_setup_step_classic,
    owner_setup_step_rich,
    wizard_primary_label,
    wizard_primary_style,
)
from telegram_membership_saas.modules.owner_setup.plan_management import (
    archive_owner_plan,
    delete_unused_owner_plan,
    edit_owner_draft_duration,
    edit_owner_draft_resource,
    rename_owner_plan,
)
from telegram_membership_saas.modules.owner_setup.premium_welcome import (
    central_connection_classic,
    central_connection_rich,
    central_welcome_classic,
    central_welcome_rich,
    trial_status_classic,
)
from telegram_membership_saas.modules.owner_setup.rich_admin_ui import (
    rich_channel_card,
    rich_owner_statistics,
    rich_subscriber_card,
    rich_subscriber_invoices,
    replace_owner_rich,
    send_owner_rich,
)
from telegram_membership_saas.modules.owner_setup.service import (
    SetupFlow,
    advance_flow,
    back_owner_flow,
    bind_prompt,
    can_manage_project,
    cancel_owner_flow,
    choose_project,
    consume_button_flow,
    consume_flow,
    create_one_month_stars_plan,
    create_owner_payment_method,
    create_owner_plan,
    create_owner_project,
    ensure_telegram_actor,
    get_selected_project,
    is_expected_prompt,
    owner_available_currencies,
    owner_home,
    publish_owner_plan,
    register_new_owner_bot,
    save_owner_price,
    set_default_project_name_from_bot,
    set_flow,
    setup_overview,
    verify_channel_and_add,
)
from telegram_membership_saas.modules.owner_setup.statistics import read_owner_statistics
from telegram_membership_saas.modules.owner_setup.subscribers import (
    FILTERS as SUBSCRIBER_FILTERS,
)
from telegram_membership_saas.modules.owner_setup.subscribers import (
    MANUAL_DURATIONS,
    SUBSCRIBER_PAGE_SIZE,
    activate_manual_plan,
    customer_id_by_telegram_id,
    get_active_customer_subscription,
    grant_manual_access,
    list_activatable_plans,
    list_active_customer_subscriptions,
    list_subscriber_invoices,
    list_subscribers,
    revoke_manual_access,
    revoke_manual_plan,
    set_customer_ban,
    subscriber_detail,
)
from telegram_membership_saas.modules.owner_setup.tenant_admin import (
    METHOD_PAGE_SIZE,
    attach_owner_method_currency,
    get_owner_method_info,
    get_owner_plan_info,
    list_owner_methods,
)
from telegram_membership_saas.modules.payments.models import PaymentMethod, PaymentRequest
from telegram_membership_saas.modules.plans import Plan
from telegram_membership_saas.modules.platform_saas_bill_ui import (
    service_currency_hint,
    unified_merchant_bill_classic,
    unified_merchant_bill_rich,
)
from telegram_membership_saas.modules.platform_saas_invoice_ui import (
    issued_invoice_classic,
    issued_invoice_rich,
    issued_quote_classic,
    issued_quote_rich,
    quote_fingerprint,
)
from telegram_membership_saas.modules.platform_saas_payment_service import (
    merchant_saas_bill_summary,
    merchant_saas_bot_native_sales_preview,
    merchant_saas_statements,
    merchant_saas_trial_status,
)
from telegram_membership_saas.modules.platform_saas_settlement_service import (
    lock_saas_invoice_currency_expected,
    preview_saas_invoice_currency,
    saas_invoice_credit_history,
    saas_invoice_currency_snapshot,
)
from telegram_membership_saas.modules.platform_saas_v2_payment_service import (
    saas_bind_payment_prompt,
    saas_catalog_for_invoice,
    saas_price_profile,
    saas_v2_invoice_balance,
    saas_v3_create_attempt,
)
from telegram_membership_saas.modules.projects.models import Project
from telegram_membership_saas.modules.telegram_admin.service import (
    _require_project_manager,
    change_project_timezone,
)
from telegram_membership_saas.modules.telegram_commerce.handlers import (
    _review_keyboard,
    _review_text,
)
from telegram_membership_saas.modules.telegram_commerce.service import (
    decode_uuid,
    encode_uuid,
    format_amount,
    format_period,
    list_pending_reviews_for_actor,
)
from telegram_membership_saas.modules.telegram_commerce.storefront import list_buyer_offers
from telegram_membership_saas.payment_review_ui import (
    rich_subscription_review,
    send_payment_review,
)
from telegram_membership_saas.telegram_dates import native_telegram_time
from telegram_membership_saas.telegram_menus import set_chat_commands
from telegram_membership_saas.tenant_context import TenantContext

DURATIONS = {
    "week": (1, "week", "7 дней"),
    "month": (1, "month", "1 месяц"),
    "quarter": (3, "month", "3 месяца"),
    "half": (6, "month", "6 месяцев"),
    "year": (1, "year", "1 год"),
    "lifetime": (None, "lifetime", "Навсегда"),
}


def _button(label: str, data: str, style: str = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=owner_button_label(label), callback_data="own:" + data, style=style,
    )


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def remove_native_owner_keyboard(bot: Bot, user_id: int) -> None:
    """Hide a Telegram reply keyboard when leaving a native picker flow."""
    try:
        await bot.send_message(
            user_id,
            tr("⌨️ Системная клавиатура закрыта.",
               "⌨️ Telegram system keyboard closed."),
            reply_markup=ReplyKeyboardRemove(),
        )
    except AiogramError:
        logging.getLogger(__name__).warning(
            "Failed to hide stale native owner keyboard"
        )


def _statistics_amount(amount: Decimal) -> str:
    """Preserve integer trailing zeroes while trimming fractional padding."""
    value = format(amount, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _encode_saas_page(page: int) -> str:
    """Compact base36 page token: even page 100000 fits Telegram callbacks."""
    if page<0 or page>100000:
        raise ValueError("Invoice page out of range")
    digits="0123456789abcdefghijklmnopqrstuvwxyz"
    result=""
    while page:
        result=digits[page%36]+result
        page//=36
    return result or "0"


def _decode_saas_page(token: str) -> int:
    if not token or len(token)>4 or any(
        x not in "0123456789abcdefghijklmnopqrstuvwxyz" for x in token
    ):
        raise ValueError("Invalid invoice page")
    page=int(token,36)
    if page>100000:
        raise ValueError("Invoice page out of range")
    return page


async def _show_saas_payment_step(
    bot: Bot, query: CallbackQuery, html: str,
    rows: list[list[InlineKeyboardButton]],
) -> None:
    """Edit the current selector instead of stacking payment menu cards.

    A Rich/old Telegram message may not be editable; one fallback send is
    allowed. Creating the actual immutable payment attempt is separate.
    """
    markup = _markup(rows)
    msg = query.message
    message_id = getattr(msg, "message_id", None)
    if message_id is not None:
        try:
            await bot.edit_message_text(
                html, chat_id=query.from_user.id,
                message_id=message_id, parse_mode="HTML",
                reply_markup=markup,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except AiogramError:
            # Rich or historical message cannot be edited. A fresh selector
            # is still better than leaving the payer at a dead button.
            pass
    await bot.send_message(
        query.from_user.id, html, parse_mode="HTML", reply_markup=markup,
    )


def _saas_currency_label(code: str) -> str:
    return {
        "RUB": tr("₽ Рубли", "₽ Rubles"),
        "USD": tr("$ Доллары", "$ US dollars"),
        "EUR": tr("€ Евро", "€ Euros"),
        "UAH": tr("₴ Гривны", "₴ Hryvnias"),
        "BYN": tr("Br Бел. рубли", "Br Belarusian rubles"),
    }[code]


def _saas_method_label(name: object, currency: str) -> str:
    label = str(name).strip()
    if not label or label.upper() == currency:
        return tr("Оплата по реквизитам", "Payment instructions")
    return label


def _status(value: str) -> str:
    return {
        "active": tr("🟢 Активно", "🟢 Active"),
        "registered": tr("✅ Подключён", "✅ Connected"),
        "draft": tr("📝 Черновик", "📝 Draft"),
        "disabled": tr("⚪ Отключено", "⚪ Disabled"),
        "archived": tr("📦 Архив", "📦 Archived"),
        "deleting": tr("🟡 Удаляется", "🟡 Removing"),
    }.get(value, tr("Неизвестно", "Unknown"))


def _next_setup_step(snapshot) -> tuple[str, str] | None:
    """One contextual action at a time, not an error-driven wizard."""
    if not any(status == "active" for _, _, status in snapshot.resources):
        return tr("📢 1. Подключить канал", "📢 1. Connect a channel"), "resources"
    if not any(status == "active" for _, _, status in snapshot.methods) and (
        not snapshot.sales_configured
    ):
        return tr("💳 2. Настроить оплату", "💳 2. Set up payments"), "methods"
    if not any(status == "active" for _, _, status in snapshot.plans):
        return tr("📦 3. Подготовить тариф", "📦 3. Create a plan"), "plans"
    if not snapshot.sales_configured:
        # A published plan can lose its last currency-compatible payment
        # method without losing its draft prices. Readiness must lead to an
        # actionable repair, never back to itself.
        return tr("💳 4. Проверить валюты оплаты", "💳 4. Check payment currencies"), "methods"
    return None


def _private(message: Message) -> bool:
    return message.chat.type == "private" and message.from_user is not None


def _readiness_steps(
    snapshot, *, channel_verified: bool | None = None,
) -> tuple[tuple[str, bool], ...]:
    """Four actionable checks on the already-connected bot's dashboard."""
    resource_active = any(
        status == "active" for _, _, status in snapshot.resources
    )
    if channel_verified is not None:
        resource_active = resource_active and channel_verified
    return (
        (tr("Канал / группа", "Channel / group"), resource_active),
        (tr("Тариф", "Plan"),
         any(status == "active" for _, _, status in snapshot.plans)),
        (tr("Способ оплаты", "Payment method"),
         any(status == "active" for _, _, status in snapshot.methods)
         or snapshot.sales_configured),
        (tr("Бот работает", "Bot running"), bool(snapshot.bot_running)),
    )


def _progress(snapshot) -> tuple[str, ...]:
    """Central connection readiness retains the bot-registration check.

    The connected bot's own dashboard intentionally omits this tautology.
    """
    steps = (
        (tr("Собственный бот", "Connected bot"),
         any(status == "registered" for _, status in snapshot.bots)),
        *_readiness_steps(snapshot),
    )
    return tuple(
        ("✅ " if ready else "▫️ ") + label for label, ready in steps
    )


async def _project_saas_hold_state(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, bot_id: UUID,
) -> str | None:
    """Project-scoped read-only commerce hold from checkout's real SQL."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id),
    ) as session:
        state = await session.scalar(
            text(
                "SELECT public.platform_saas_project_commerce_state("
                ":project,:bot)"
            ),
            {"bot": bot_id, "project": project_id},
        )
    return str(state) if state in {"clear", "held", "disabled"} else None


def _sales_status_text(*, configured: bool, hold_state: str | None) -> str:
    if not configured:
        return tr(
            "Настройте продажи по порядку. Уже сохранённые данные "
            "останутся на месте.",
            "Complete the setup steps. Saved settings will remain intact.",
        )
    if hold_state == "held":
        return tr(
            "⛔ Новые продажи приостановлены до погашения долга "
            "ViMi Pay. Ранее оплаченный доступ сохраняется.",
            "⛔ New sales are on hold until the ViMi Pay debt is "
            "settled. Previously paid access remains valid.",
        )
    if hold_state not in {"clear", "disabled"}:
        return tr(
            "⚠️ Не удалось проверить ограничения продаж. "
            "Проверьте 🧾 Счёт.",
            "⚠️ Sales restrictions could not be checked. Open 🧾 Bill.",
        )
    return tr(
        "✅ Продажи доступны. Управляйте тарифами и заявками.",
        "✅ Sales available. Manage plans and payment requests.",
    )


def _paid_service_status_text(
    summary: dict, *, timezone: str, now: datetime | None = None,
) -> str | None:
    """Render paid service only from a settled, currently active real invoice."""
    if not isinstance(summary, dict) or summary.get("invoice_id") is None:
        return None
    try:
        charged = Decimal(str(summary["total_rub"]))
        outstanding = Decimal(str(summary["total_due_rub"]))
        settled = (
            Decimal(str(summary.get("paid_rub", 0)))
            + Decimal(str(summary.get("credit_rub", 0)))
        )
        begins = datetime.fromisoformat(
            str(summary["period_start"]).replace("Z", "+00:00")
        )
        ends = datetime.fromisoformat(
            str(summary["period_end"]).replace("Z", "+00:00")
        )
        zone = ZoneInfo(timezone)
        checked_at = now if now is not None else datetime.now(zone)
        if (
            charged <= 0 or outstanding != 0 or settled != charged
            or begins.tzinfo is None or ends.tzinfo is None
            or checked_at.tzinfo is None
            or not begins <= checked_at < ends
        ):
            return None
        through = escape(ends.astimezone(zone).strftime("%d.%m.%Y"))
        return tr(
            "✅ Обслуживание оплачено до ",
            "✅ Service paid until ",
        ) + through
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


class StrayBotTokenFilter(BaseFilter):
    """Scrub credentials even when there is no live ForceReply wizard."""

    async def __call__(self, message: Message) -> bool:
        return bool(
            _private(message)
            and message.text is not None
            and TOKEN_PATTERN.fullmatch(message.text.strip())
        )


class SetupReplyFilter(BaseFilter):
    def __init__(
        self, factory: async_sessionmaker[AsyncSession],
        fixed_project_id: UUID | None = None,
    ) -> None:
        self.factory = factory
        self.fixed_project_id = fixed_project_id

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if (
            not _private(message) or message.text is None
            or message.text.startswith("/")
        ):
            return False
        origin = message.reply_to_message
        if origin is not None and (
            origin.from_user is None or origin.from_user.id != bot.id
        ):
            return False
        user = message.from_user
        actor_id = await ensure_telegram_actor(
            self.factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )
        if self.fixed_project_id is not None and not await can_manage_project(
            self.factory, actor_id=actor_id, project_id=self.fixed_project_id,
        ):
            return False
        return await is_expected_prompt(
            self.factory, actor_id=actor_id,
            reply_message_id=origin.message_id if origin is not None else None,
            bot_scope_id=bot.id if self.fixed_project_id else 0,
        )


class BuyerConversationReplyFilter(BaseFilter):
    """Accept an authentic bot reply or the next message after Reply was tapped.

    A pending single-message window replaces ForceReply: Telegram's native
    reply composer cannot be reliably dismissed by Bot API messages.
    """

    def __init__(self, pending_until: dict[int, float] | None = None) -> None:
        self.pending_until = pending_until if pending_until is not None else {}

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if not _private(message) or not message.text or message.text.startswith("/"):
            return False
        parent = message.reply_to_message
        if parent is not None:
            # Never consume a checkout receipt or an unrelated quoted message.
            return bool(
                parent.from_user and parent.from_user.id == bot.id
                and is_admin_message(parent.text)
            )
        return self.pending_until.get(message.from_user.id, 0) > monotonic()


class OwnerConversationReplyFilter(BaseFilter):
    """Owner can reply directly to the bot's contextual buyer notification."""

    async def __call__(self, message: Message, bot: Bot) -> bool:
        if not _private(message) or not message.text or message.text.startswith("/"):
            return False
        parent = message.reply_to_message
        if not parent or not parent.from_user or parent.from_user.id != bot.id:
            return False
        try:
            parse_buyer_notification(parent.text)
        except ValueError:
            return False
        return True


class ClearOwnerInputOnNavigation(BaseMiddleware):
    """When leaving a plain-text form, discard its bot-scoped capture.

    Installed as an inner aiogram callback middleware; direct handler calls
    in unit tests still test just the handler, while the full dispatcher
    exercises this shared navigation policy.
    """

    def __init__(
        self, factory: async_sessionmaker[AsyncSession],
        fixed_project_id: UUID | None,
    ) -> None:
        self.factory = factory
        self.fixed_project_id = fixed_project_id

    async def __call__(self, handler, event: CallbackQuery, data: dict):
        action = (event.data or "")
        bot = data.get("bot")
        if (
            action.startswith("own:")
            and action != "own:cancel"
            and not action.startswith((
                "own:flowback:", "own:planname:",
                "own:duration:", "own:use:",
                # Managed bot confirmation must retain the staged candidate
                # and generation until atomic registration consumes them.
                "own:managedconfirm:", "own:managedcancel:",
                "own:promptback:", "own:promptcancel:",
            ))
            and bot is not None
            and event.message is not None
            and event.message.chat.type == "private"
            and event.message.chat.id == event.from_user.id
        ):
            user = event.from_user
            actor_id = await ensure_telegram_actor(
                self.factory, telegram_id=user.id,
                username=user.username, first_name=user.first_name,
            )
            was_native = await cancel_owner_flow(
                self.factory, actor_id=actor_id,
                bot_scope_id=bot.id if self.fixed_project_id else 0,
                report_native_keyboard=True,
            )
            if was_native is True:
                await remove_native_owner_keyboard(bot, user.id)
            if self.fixed_project_id is not None:
                await cancel_draft(
                    self.factory, project_id=self.fixed_project_id,
                    actor_id=actor_id,
                )
        return await handler(event, data)


class OwnerHomeFilter(BaseFilter):
    """Owner / tenant manager sees administration; ordinary buyers fall through."""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession],
        fixed_project_id: UUID | None,
    ) -> None:
        self.factory = factory
        self.fixed_project_id = fixed_project_id

    async def __call__(self, message: Message) -> bool:
        if not _private(message):
            return False
        if self.fixed_project_id is None:
            return True
        user = message.from_user
        actor_id = await ensure_telegram_actor(
            self.factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )
        return await can_manage_project(
            self.factory, actor_id=actor_id, project_id=self.fixed_project_id,
        )


def create_owner_setup_router(
    factory: async_sessionmaker[AsyncSession], *,
    fixed_project_id: UUID | None = None,
    language_menu_renderers: list[
        Callable[[Bot, User], Awaitable[bool]]
    ] | None = None,
) -> Router:
    router = Router(name="owner-tenant" if fixed_project_id else "central-registrar")
    # One reply at a time per buyer inside this connected bot. No sticky
    # Telegram ForceReply UI, no new database/migration.
    pending_buyer_replies: dict[int, float] = {}
    router.callback_query.middleware(
        ClearOwnerInputOnNavigation(factory, fixed_project_id),
    )

    async def actor(user) -> UUID:
        return await ensure_telegram_actor(
            factory, telegram_id=user.id,
            username=user.username, first_name=user.first_name,
        )

    def platform_navigation(user_id: int) -> list[InlineKeyboardButton]:
        """Switch roles only on central and only for configured owner."""
        if (
            fixed_project_id is None
            and getattr(get_settings(), "platform_owner_telegram_id", None) == user_id
        ):
            return [InlineKeyboardButton(
                text=tr("👑 Панель платформы", "👑 Platform dashboard"), callback_data="plat:home",
            )]
        return []

    def onboarding_language_rows() -> list[list[InlineKeyboardButton]]:
        """The two choices on the complete first-run welcome card."""
        return [[
            InlineKeyboardButton(
                text="🇷🇺 Русский", callback_data="lng:set:ru",
                style="success",
            ),
            InlineKeyboardButton(
                text="🇬🇧 English", callback_data="lng:set:en",
                style="primary",
            ),
        ]]

    async def show_onboarding_language(bot: Bot, user_id: int) -> None:
        """Return to screen 1, not the compact general language selector."""
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=central_welcome_rich(language_selected=False),
            classic_html=central_welcome_classic(language_selected=False),
            reply_markup=_markup(onboarding_language_rows()),
        )

    async def show_home(bot: Bot, user_id: int, actor_id: UUID) -> None:
        home = await owner_home(
            factory, actor_id=actor_id,
            bot_scope_id=bot.id if fixed_project_id else 0,
            fixed_project_id=fixed_project_id,
        )
        if fixed_project_id is None:
            # A selected bot must NEVER hijack the central /start or /menu.
            # First-run language choice is the only pre-dashboard gate.
            if (
                not home.projects and not home.archived_count
                and not await has_explicit_language_choice(factory, user_id)
            ):
                await show_onboarding_language(bot, user_id)
                return
            rows: list[list[InlineKeyboardButton]] = [
                [
                    _button(tr("🤖 Мои подключения", "🤖 My connections"),
                            "projects", "success"),
                    _button(tr("✨ Добавить бота", "✨ Add bot"),
                            "substart", "success"),
                ],
                [
                    _button(tr("🧾 Счёт аккаунта", "🧾 Account bill"),
                            "saasinvoices:0", "primary"),
                    InlineKeyboardButton(
                        text="🌐 Language / Язык",
                        callback_data="lng:menu", style="primary",
                    ),
                ],
                [InlineKeyboardButton(
                    text=tr("🛟 Поддержка", "🛟 Support"),
                    url="https://t.me/vimi_support_bot", style="success",
                )],
            ]
            try:
                report = await read_owner_network_home(
                    factory, actor_id=actor_id,
                )
            except PermissionError:
                raise
            except Exception:
                # A reporting RPC must not make the central menu unusable.
                # Never turn missing statistics into fictional zero figures.
                logging.getLogger(__name__).exception(
                    "ViMi Pay network home snapshot unavailable"
                )
                title = "✦ ViMi Pay · " + tr(
                    "ГЛАВНОЕ МЕНЮ", "MAIN MENU",
                )
                notice = "⚠️ " + tr(
                    "Сводка сети временно недоступна. "
                    "Управление подключениями и счетами работает.",
                    "The network summary is temporarily unavailable. "
                    "Connections and invoices remain accessible.",
                )
                rich = "<h3>" + title + "</h3><p>" + notice + "</p>"
                classic = "<b>" + title + "</b>\n\n" + notice
            else:
                rich = owner_network_home_rich(report)
                classic = owner_network_home_classic(report)
                nearest = report["billing"].get("next")
                if nearest:
                    nearest_id = UUID(str(nearest["invoice_id"]))
                    name = nearest.get("username")
                    label = (
                        "🤖 @" + str(name).lstrip("@")[:28]
                        if name else tr("🧾 Ближайший счёт", "🧾 Next invoice")
                    )
                    rows.insert(2, [_button(
                        label, "saasnearest:" + encode_uuid(nearest_id),
                        "primary",
                    )])
            if platform_navigation(user_id):
                rows.append(platform_navigation(user_id))
            await send_owner_rich(
                bot, chat_id=user_id, rich_html=rich,
                classic_html=classic, reply_markup=_markup(rows),
            )
            return
        if home.selected is not None:
            await show_dashboard(bot, user_id, actor_id, home.selected)
            return
        lines = [tr("✨ <b>ViMi Pay · Управление подписками</b>",
                    "✨ <b>ViMi Pay · Membership management</b>"), ""]
        rows: list[list[InlineKeyboardButton]] = []
        if not home.projects:
            lines.append(
                tr("Подключите собственного Telegram-бота, затем "
                   "откройте его для настройки тарифов и оплаты.",
                   "Connect your Telegram bot, then open it to configure "
                   "subscriptions and payments.")
            )
        else:
            lines.append(tr("Выберите проект для управления:", "Select a connection to manage:"))
            for project_id, name in home.projects[:20]:
                rows.append([_button("🤖 " + name[:45], "select:" + project_id.hex)])
        # First-run language is asked only until the explicit preference
        # exists. The callback returns to /menu in the chosen language.
        language_selected = (
            await has_explicit_language_choice(factory, user_id)
            if fixed_project_id is None and not home.projects else True
        )
        if not language_selected:
            rows.extend(onboarding_language_rows())
        else:
            rows.append([_button(
                tr("🔐 Платные подписки", "🔐 Paid subscriptions"),
                "substart", "success",
            )])
            if not home.projects:
                rows.append([_button(
                    tr("⬅️ Назад", "⬅️ Back"), "onboardlanguage", "primary",
                )])
        if home.projects:
            # Existing connections retain account invoices and language access.
            # /billing remains available even when all connections are deleted.
            rows.append([_button(
                tr("🧾 Счета ViMi Pay", "🧾 ViMi Pay invoices"),
                "saasinvoices:0",
            )])
            rows.append([InlineKeyboardButton(
                text="🌐 Language / Язык", callback_data="lng:menu",
            )])
            if platform_navigation(user_id):
                rows.append(platform_navigation(user_id))
        if not home.projects:
            await send_owner_rich(
                bot, chat_id=user_id,
                rich_html=central_welcome_rich(
                    language_selected=language_selected,
                ),
                classic_html=central_welcome_classic(
                    language_selected=language_selected,
                ),
                reply_markup=_markup(rows),
            )
        else:
            await bot.send_message(
                user_id, "\n".join(lines),
                parse_mode="HTML", reply_markup=_markup(rows),
            )

    async def show_subscription_start(bot: Bot, user_id: int) -> None:
        """Future-proof product choice; only supported subscriptions are shown."""
        rows = [
            [
                _button(
                    tr("✨ Создать бота", "✨ Create bot"),
                    "newmanaged", "success",
                ),
                _button(
                    tr("🔑 Через BotFather", "🔑 Via BotFather"),
                    "new", "primary",
                ),
            ],
            [_button(tr("⬅️ Назад", "⬅️ Back"), "onboardhome", "primary")],
        ]
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=(
                "<h3>" + tr("🔐 Платные подписки", "🔐 Paid subscriptions")
                + "</h3><p><b>" + tr(
                    "🤖 Выберите способ подключения Telegram-бота",
                    "🤖 Choose how to connect your Telegram bot",
                ) + "</b></p><p><b>" + tr(
                    "✨ Создать бота", "✨ Create bot",
                ) + "</b><br>" + tr(
                    "Telegram предложит указать имя и @username. Подтвердите "
                    "создание, и ViMi Pay подключит нового бота автоматически. "
                    "Копировать API-токен не понадобится.",
                    "Telegram will ask for a name and @username. Confirm "
                    "creation, and ViMi Pay will connect the new bot "
                    "automatically. No API token to copy.",
                ) + "</p><p><b>" + tr(
                    "🔑 Через BotFather", "🔑 Via BotFather",
                ) + "</b><br>" + tr(
                    "Для уже созданного бота или ручного создания нового:",
                    "For an existing bot or to create a new one manually:",
                ) + "</p><p>" + tr(
                    "1. Откройте @BotFather: /mybots для существующего бота "
                    "или /newbot для нового.<br>"
                    "2. Получите API-токен и отправьте его в ViMi Pay.<br>"
                    "3. Мы проверим токен и удалим сообщение с ним из чата.",
                    "1. Open @BotFather: /mybots for an existing bot or "
                    "/newbot for a new one.<br>"
                    "2. Get the API token and send it to ViMi Pay.<br>"
                    "3. We'll verify the token and delete its chat message.",
                ) + "</p><p>" + tr(
                    "После подключения откройте своего бота, чтобы настроить "
                    "канал, тарифы и оплату.",
                    "After connecting, open your own bot to configure "
                    "your channel, plans and payments.",
                ) + "</p>"
            ),
            classic_html=(
                tr("🔐 <b>Платные подписки</b>", "🔐 <b>Paid subscriptions</b>")
                + "\n\n<b>" + tr(
                    "🤖 Выберите способ подключения Telegram-бота",
                    "🤖 Choose how to connect your Telegram bot",
                ) + "</b>\n\n<b>" + tr(
                    "✨ Создать бота", "✨ Create bot",
                ) + "</b>\n" + tr(
                    "Укажите имя и @username в Telegram. ViMi Pay подключит "
                    "бота автоматически, без копирования API-токена.",
                    "Set the name and @username in Telegram. ViMi Pay "
                    "connects the bot automatically, no token to copy.",
                ) + "\n\n<b>" + tr(
                    "🔑 Через BotFather", "🔑 Via BotFather",
                ) + "</b>\n" + tr(
                    "1. @BotFather → /mybots (существующий) или /newbot (новый).\n"
                    "2. Получите и отправьте API-токен в ViMi Pay.\n"
                    "3. Мы проверим токен и удалим сообщение с ним.",
                    "1. @BotFather → /mybots (existing) or /newbot (new).\n"
                    "2. Get the API token and send it to ViMi Pay.\n"
                    "3. We verify the token and delete its message.",
                ) + "\n\n" + tr(
                    "После подключения откройте своего бота для настройки "
                    "канала, тарифов и оплаты.",
                    "After connecting, open your bot to configure "
                    "the channel, plans and payments.",
                )
            ),
            reply_markup=_markup(rows),
        )

    async def show_dashboard(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        data = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        if fixed_project_id is None:
            # Central is a connection control panel, not a tenant sales console.
            # Actions use existing callbacks; the bot link opens the real
            # connected tenant admin rather than another central submenu.
            connected = next(
                (username for _, username, status in data.bot_instances
                 if status == "registered"), None,
            ) or next(
                (username for username, status in data.bots
                 if status == "registered"), None,
            )
            runtime = owner_runtime_label(data.bot_runtime_label)
            if connected:
                rows = [[
                    InlineKeyboardButton(
                        text=tr("🚀 Открыть бота", "🚀 Open bot"),
                        url="https://t.me/" + connected,
                        style="success",
                    ),
                    _button(
                        tr("⚙️ Управление", "⚙️ Manage"), "bots", "success",
                    ),
                ]]
            else:
                rows = [[
                    _button(
                        tr("⚙️ Подключить бота", "⚙️ Connect bot"),
                        "bots", "success",
                    ),
                    _button(
                        tr("✨ Создать бота", "✨ Create bot"),
                        "newmanaged", "success",
                    ),
                ]]
            # Alternating green/blue rows, always two actions per row.
            rows.extend([
                [
                    _button(
                        tr("📁 Все подключения", "📁 All connections"),
                        "projects", "primary",
                    ),
                    _button(
                        tr("🧾 Счёт аккаунта", "🧾 Account bill"),
                        "saasinvoices:0", "primary",
                    ),
                ],
                [
                    _button(
                        tr("❔ Как это работает", "❔ How it works"),
                        "help", "success",
                    ),
                    InlineKeyboardButton(
                        text="🌐 Language / Язык",
                        callback_data="lng:menu", style="success",
                    ),
                ],
            ])
            if platform_navigation(user_id):
                rows.append(platform_navigation(user_id))
            await send_owner_rich(
                bot, chat_id=user_id,
                rich_html=central_connection_rich(
                    project_name=data.project_name, bot_username=connected,
                    runtime_label=runtime,
                ),
                classic_html=central_connection_classic(
                    project_name=data.project_name, bot_username=connected,
                    runtime_label=runtime,
                ),
                reply_markup=_markup(rows),
            )
        else:
            next_step = _next_setup_step(data)
            # Delegated project admins can manage the bot but do not own
            # its account-level SaaS finances. Never block their dashboard
            # just because the owner-only trial read is forbidden.
            try:
                trial = await merchant_saas_trial_status(
                    factory, actor_user_id=actor_id, project_id=project_id,
                )
            except PermissionError:
                trial = None
            # A configured/live bot may still reject new checkouts due to
            # ViMi Pay debt. Never claim sales are ready from setup alone.
            hold_state = None
            registered_bots = [
                item for item in data.bot_instances if item[2] == "registered"
            ]
            if len(registered_bots) == 1:
                try:
                    hold_state = await _project_saas_hold_state(
                        factory, actor_id=actor_id, project_id=project_id,
                        bot_id=registered_bots[0][0],
                    )
                except Exception:
                    logging.getLogger(__name__).warning(
                        "ViMi Pay admin sale restrictions unavailable"
                    )
            trial_banner = (
                trial_status_classic(trial, timezone=data.timezone)
                if trial is not None else ""
            )
            service_summary = None
            if trial is not None and trial.get("state") == "ended":
                # Trial eligibility never resets on payment. Consult the
                # existing authenticated invoice report for ACTIVE service.
                try:
                    service_summary = await merchant_saas_bill_summary(
                        factory, actor_user_id=actor_id,
                        month=datetime.now(
                            ZoneInfo("Europe/Moscow")
                        ).date().replace(day=1),
                        project_id=project_id,
                    )
                    trial_banner = (
                        _paid_service_status_text(
                            service_summary, timezone=data.timezone,
                        ) or trial_banner
                    )
                except Exception:
                    logging.getLogger(__name__).warning(
                        "ViMi Pay active service status unavailable"
                    )
                    trial_banner = tr(
                        "⚠️ Не удалось проверить статус обслуживания. "
                        "Откройте 🧾 Счёт.",
                        "⚠️ Service status could not be checked. "
                        "Open 🧾 Bill.",
                    )
            try:
                finances = await read_dashboard_finances(
                    factory, actor_id=actor_id, project_id=project_id, bot=bot,
                )
            except Exception:
                # Live Telegram reads never block the existing admin menu.
                logging.getLogger(__name__).exception(
                    "ViMi Pay dashboard finance data unavailable"
                )
                finances = DashboardFinances(
                    bot_stars=None, channels=(), available=False,
                )
            # The connected bot owns this profile; no central/global
            # currency is involved. Failure to read finances never fabricates
            # a settled invoice or a zero debt.
            currency = None
            debt_label = None
            profile = None
            try:
                profile = await saas_price_profile(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                currency = str(profile["currency"])
            except Exception:
                logging.getLogger(__name__).warning(
                    "ViMi Pay per-bot service currency unavailable",
                )
            if service_summary is None:
                try:
                    service_summary = await merchant_saas_bill_summary(
                        factory, actor_user_id=actor_id,
                        month=datetime.now(
                            ZoneInfo("Europe/Moscow")
                        ).date().replace(day=1),
                        project_id=project_id,
                    )
                except Exception:
                    logging.getLogger(__name__).warning(
                        "ViMi Pay per-bot invoice summary unavailable",
                    )
            if service_summary is not None:
                try:
                    outstanding = Decimal(str(service_summary["total_due_rub"]))
                    if (service_summary.get("service_currency") == "USD"
                            and service_summary.get("issued_due_usd") is not None):
                        debt_label = "$" + format(Decimal(str(
                            service_summary["issued_due_usd"],
                        )), ".2f")
                    else:
                        debt_label = format(outstanding, ".2f") + " ₽"
                except (TypeError, ValueError, KeyError):
                    pass
            # Existing bots are grandfathered in by migration 0102.
            # Newly registered bots get a real 4-step wizard instead of the
            # dense dashboard; all actions reuse established handlers.
            if profile is not None and profile.get("currency_selected") is False:
                if next_step is None:
                    next_step = (
                        tr("💱 4/4 · Валюта обслуживания",
                           "💱 4/4 · Service currency"),
                        "saasprofile",
                    )
                label, action = next_step
                bot_name = (
                    data.live_bot_username
                    or next((username for username, status in data.bots
                             if status == "registered"), "")
                    or data.project_name
                )
                # The guided setup already tells the owner what to do.
                # One scoped forward action, no distracting help/language
                # buttons on any of the 4 wizard steps.
                wizard_rows = [[_button(
                    wizard_primary_label(action, label),
                    action, wizard_primary_style(action),
                )]]
                await send_owner_rich(
                    bot, chat_id=user_id,
                    rich_html=owner_setup_step_rich(
                        bot_name=bot_name, timezone=data.timezone,
                        action=action,
                    ),
                    classic_html=owner_setup_step_classic(
                        bot_name=bot_name, timezone=data.timezone,
                        action=action,
                    ),
                    reply_markup=_markup(wizard_rows),
                )
                return
            view = {
                "bot_status": (
                    tr("🟢 Работает", "🟢 Running")
                    if data.bot_running else
                    owner_runtime_label(data.bot_runtime_label)
                ),
                "channels_count": sum(
                    status == "active" for _, _, status in data.resources
                ),
                "active_plans_count": sum(
                    status == "active" for _, _, status in data.plans
                ),
                "payment_ready": bool(data.sales_configured),
                "service_currency": currency,
                "service_debt": debt_label,
                "project_name": (
                    "@" + str(
                        data.live_bot_username
                        or next((username for username, status in data.bots
                                 if status == "registered"), "")
                    ).lstrip("@")
                    if (data.live_bot_username or any(
                        status == "registered" for _, status in data.bots
                    ))
                    else data.project_name
                ),
                "timezone": data.timezone,
                "trial_banner": trial_banner,
                "readiness": _readiness_steps(
                    data, channel_verified=finances.channel_ready,
                ),
                "sales_status": _sales_status_text(
                    configured=data.sales_configured, hold_state=hold_state,
                ),
                "finances": finances,
                "exempt": trial is not None and trial.get("state") == "exempt",
            }
            text = classic_dashboard(**view)
            rich_html = rich_dashboard(**view)
            rows = []
            if next_step is not None:
                label, action = next_step
                rows.append([_button(label, action, "success")])
            rows.extend([
                # Consistent row-based colors: blue / green / blue / green / blue.
                [_button("📢 Каналы", "resources", "success"),
                 _button("💳 Оплата", "methods")],
                [_button("📦 Тарифы", "plans"),
                 _button("📋 Заявки", "reviews", "success")],
                [_button(tr("👥 Подписчики", "👥 Subscribers"), "subscribers",
                         "success"),
                 _button(tr("📊 Статистика", "📊 Statistics"), "statistics:7")],
                [InlineKeyboardButton(
                    text=tr("📨 Рассылки", "📨 Broadcasts"),
                    callback_data="bc:home", style="primary",
                ), InlineKeyboardButton(
                    text=tr("📂 Продажа файлов", "📂 File sales"),
                    callback_data="fs:home", style="success",
                )],
                [_button(tr("👀 Смотреть гостем", "👀 View as guest"),
                         "preview", "success"),
                 _button(tr("⚙️ Настройки", "⚙️ Settings"), "settings")],
                [
                    _button(tr("🧾 Счёт", "🧾 Bill"), "saasinvoices:0"),
                    InlineKeyboardButton(
                        text="🌐 Language / Язык", callback_data="lng:menu",
                        style="success",
                    ),
                ],
            ])
        if fixed_project_id is not None:
            await send_owner_rich(
                bot, chat_id=user_id, rich_html=rich_html,
                classic_html=text, reply_markup=_markup(rows),
            )

    async def show_settings(
        bot: Bot, user_id: int,
    ) -> None:
        """A single place for connected-bot configuration."""
        if fixed_project_id is None:
            raise PermissionError("Settings only available in connected bot")
        await bot.send_message(
            user_id,
            tr("⚙️ <b>Настройки</b>\n\nВыберите раздел:",
               "⚙️ <b>Settings</b>\n\nChoose a section:"),
            parse_mode="HTML",
            reply_markup=_markup([
                [_button(tr("🔔 Уведомления", "🔔 Notifications"), "notifications")],
                [_button(tr("📋 Готовность", "📋 Readiness"), "readiness"),
                 _button("🌍 TimeZone", "timezone")],
                [_button(tr("📁 Профиль сети", "📁 Network profile"), "network")],
                [_button(tr("🛡 Модераторы", "🛡 Moderators"), "modmanage")],
                [_button(tr("🔁 Передать бота", "🔁 Transfer bot"), "transfer")],
                [_button(tr("❔ Помощь", "❔ Help"), "help")],
                [_button(tr("⬅️ Панель администратора", "⬅️ Admin dashboard"),
                         "dashboard")],
            ]),
        )

    async def show_saas_service_profile(
        bot: Bot, user_id: int, actor_id: UUID,
    ) -> None:
        """Only each connected bot selects its own future service currency."""
        if fixed_project_id is None:
            await bot.send_message(
                user_id, tr(
                    "💱 Валюта обслуживания выбирается в разделе «🧾 Счёт» "
                    "соответствующего бота. Общей валюты аккаунта нет.",
                    "💱 Set the service currency in each bot's 🧾 Bill "
                    "section. There is no account-wide currency.",
                ),
            )
            return
        try:
            data = await saas_price_profile(
                factory, actor_id=actor_id, project_id=fixed_project_id,
            )
        except DBAPIError:
            # Delegated project managers may administer content, but cannot
            # access or change the financial owner's service currency.
            await bot.send_message(
                user_id, tr(
                    "⚠️ Валюту обслуживания может менять только владелец бота.",
                    "⚠️ Only the bot owner can change service currency.",
                ),
            )
            return
        currency = str(data["currency"])
        first_choice = data.get("currency_selected") is False
        label = (
            tr("🇺🇸 USD · $5/мес.", "🇺🇸 USD · $5/month")
            if currency == "USD"
            else tr("🇷🇺 RUB · 150 ₽/мес.", "🇷🇺 RUB · 150 ₽/month")
        )
        caption = (
            tr(
                "Последний шаг настройки. Выберите валюту счёта именно "
                "этого бота: 150 ₽ или $5 в месяц. Первый счёт содержит "
                "только абонплату; далее +5% подтверждённых продаж. "
                "Показанная сейчас валюта пока не подтверждена.",
                "Final setup step. Choose this bot's service invoice currency: "
                "RUB 150 or USD 5 monthly. The first invoice is service-only; "
                "later invoices add 5% of confirmed sales. The suggested "
                "currency has not yet been confirmed.",
            )
            if first_choice else tr(
                "Только будущие счета этого бота. "
                "При долге именно по этому боту смена недоступна.",
                "This bot's future invoices only. "
                "Currency is locked while this bot has unpaid invoices.",
            )
            if fixed_project_id is not None
            else tr(
                "Валюта по умолчанию для новых ботов аккаунта. "
                "Для подключённых ботов валюта настраивается отдельно.",
                "Default currency for new bots. "
                "Connected bots have independent service currency settings.",
            )
        )
        keyboard = _markup([
            [
                _button(
                    ("✅ " if currency == "RUB" and not first_choice else "")
                    + "🇷🇺 RUB · 150 ₽",
                    "saasprice:RUB",
                    "success" if currency == "RUB" and not first_choice else "primary",
                ),
                _button(
                    ("✅ " if currency == "USD" and not first_choice else "")
                    + "🇺🇸 USD · $5",
                    "saasprice:USD",
                    "success" if currency == "USD" and not first_choice else "primary",
                ),
            ],
            [_button(
                tr("⬅️ К настройке", "⬅️ Back to setup") if first_choice
                else tr("⬅️ К счёту", "⬅️ Back to bill"),
                "dashboard" if first_choice else "saasinvoices:0",
            )],
        ])
        heading = tr(
            "💱 4/4 · Выберите валюту обслуживания" if first_choice
            else "💱 Валюта обслуживания",
            "💱 4/4 · Choose service currency" if first_choice
            else "💱 Service currency",
        )
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=(
                "<h3>" + heading + "</h3>"
                "<table bordered compact>"
                "<tr><th>" + (tr("Предложено", "Suggested") if first_choice
                    else tr("Сейчас", "Current")) + "</th><td><b>"
                + label + "</b></td></tr></table>"
                "<p>" + caption + "</p>"
            ),
            classic_html=(
                "<b>" + heading + "</b>\n\n"
                + (tr("Предложено: ", "Suggested: ") if first_choice
                    else tr("Сейчас: ", "Current: ")) + "<b>" + label + "</b>"
                + "\n\n" + caption
            ),
            reply_markup=keyboard,
        )

    async def show_account_invoice_list(
        bot: Bot, user_id: int, actor_id: UUID, *, page: int = 0,
    ) -> None:
        """Account-wide historical invoices, including retired bot debts."""
        report = await merchant_saas_statements(
            factory, actor_user_id=actor_id, page=page,
        )
        if page > 0 and page * 6 >= report.total:
            raise ValueError(tr("Эта страница больше не существует.",
                                "This invoice page is no longer available."))
        rows: list[list[InlineKeyboardButton]] = []
        for item in report.invoices:
            due = Decimal(str(item.get("remaining_rub", item["amount_rub"])))
            if item.get("status") == "paid":
                due = Decimal("0")
            label = (
                (tr("🔴 К оплате", "🔴 Due") + " · "
                 + format(due, ".2f") + " ₽")
                if due > 0 else
                (tr("✅ Оплачено", "✅ Paid") + " · "
                 + format(Decimal(str(item["amount_rub"])), ".2f") + " ₽")
            )
            due_day = str(item.get("due_at") or "")[:10]
            if len(due_day) == 10 and due_day[4:5] == "-":
                label += " · " + due_day[8:10] + "." + due_day[5:7] + "." + due_day[:4]
            rows.append([_button(
                label, "saasbill:" + encode_uuid(UUID(str(item["id"])))
                + ":" + str(page), "primary",
            )])
        if not report.invoices:
            empty = tr("📋 Счетов пока нет.", "📋 No invoices yet.")
        else:
            empty = tr("Выберите счёт, чтобы открыть его подробности.",
                       "Choose an invoice to view its details.")
        nav: list[InlineKeyboardButton] = []
        if page:
            nav.append(_button("⬅️", "saasaccountlist:" + str(page - 1)))
        if (page + 1) * 6 < report.total:
            nav.append(_button("➡️", "saasaccountlist:" + str(page + 1)))
        if nav:
            rows.append(nav)
        rows.append([_button(
            tr("⬅️ Счёт аккаунта", "⬅️ Account bill"),
            "saasinvoices:0", "primary",
        )])
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=(
                "<h3>" + tr("📋 ВЫСТАВЛЕННЫЕ СЧЕТА",
                            "📋 ISSUED INVOICES") + "</h3>"
                "<p><i>" + empty + "</i></p><p><b>"
                + tr("📄 Страница ", "📄 Page ") + str(page + 1)
                + " / " + str(max(1, (report.total + 5) // 6))
                + "</b></p>"
            ),
            classic_html=(
                "<b>" + tr("📋 ВЫСТАВЛЕННЫЕ СЧЕТА",
                           "📋 ISSUED INVOICES") + "</b>\n"
                + empty + "\n" + tr("Страница ", "Page ")
                + str(page + 1) + " / " + str(max(1, (report.total + 5) // 6))
            ),
            reply_markup=_markup(rows),
        )

    async def show_saas_statements(
        bot: Bot, user_id: int, actor_id: UUID,
        project_id: UUID | None, *, page: int = 0,
    ) -> None:
        """One issued invoice per bot; never sum another bot's finances."""
        if project_id is None:
            report = await merchant_saas_statements(
                factory, actor_user_id=actor_id, page=0,
            )
            # Only real issued invoices and the owner-scoped fleet ledger
            # contribute figures. Never manufacture future obligations.
            home = await read_owner_network_home(factory, actor_id=actor_id)
            billing = home["billing"]
            rows: list[list[InlineKeyboardButton]] = []
            nearest = billing.get("next") if report.unpaid else None
            if isinstance(nearest, dict) and nearest.get("invoice_id"):
                rows.append([_button(
                    tr("💳 Открыть ближайший счёт",
                       "💳 Open next invoice"),
                    "saasnearest:" + encode_uuid(UUID(str(nearest["invoice_id"]))),
                    "success",
                )])
            if report.unpaid:
                rows.append([_button(
                    tr("📋 Все выставленные счета",
                       "📋 All issued invoices"),
                    "saasaccountlist:0", "primary",
                )])
            rows.append([
                _button(tr("📂 Мои подключения", "📂 My connections"),
                        "projects", "primary"),
                _button(tr("🏠 Главное меню", "🏠 Main menu"),
                        "onboardhome", "primary"),
            ])
            await send_owner_rich(
                bot, chat_id=user_id,
                rich_html=account_bill_rich(report, billing),
                classic_html=account_bill_classic(report, billing),
                reply_markup=_markup(rows),
            )
            return
        # This project must have exactly one connected bot. SQL verifies
        # live ownership and returns one canonical issued invoice and lines.
        summary = await merchant_saas_bill_summary(
            factory, actor_user_id=actor_id,
            month=datetime.now(ZoneInfo("Europe/Moscow")).date().replace(day=1),
            project_id=project_id,
        )
        profile = await saas_price_profile(
            factory, actor_id=actor_id, project_id=project_id,
        )
        rows: list[list[InlineKeyboardButton]] = []
        due = Decimal(str(summary["total_due_rub"]))
        invoice_id = summary.get("invoice_id")
        if due > 0 and invoice_id is not None:
            rows.append([_button(
                tr("💳 Оплатить", "💳 Pay"),
                "spm:" + encode_uuid(UUID(str(invoice_id))) + ":RUB:0",
                "success",
            )])
        rows.append([
            _button(
                tr("💱 Валюта обслуживания", "💱 Service currency"),
                "saasprofile", "primary",
            ),
            _button(
                tr("🏠 Главное меню", "🏠 Main menu"),
                "dashboard", "success",
            ),
        ])
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=unified_merchant_bill_rich(
                summary, future_profile_currency=str(profile["currency"]),
            ),
            classic_html=unified_merchant_bill_classic(
                summary, future_profile_currency=str(profile["currency"]),
            ),
            reply_markup=_markup(rows),
        )

    async def show_central_bot_bill(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        """Owner's current per-bot bill, not the central account invoice index.

        Every monetary value comes from an existing owner/project-scoped
        read-only RPC. A sales preview never creates a payable amount.
        """
        if fixed_project_id is not None:
            raise PermissionError("The central per-bot bill belongs to ViMi Pay")
        month = datetime.now(ZoneInfo("Europe/Moscow")).date().replace(day=1)
        bill = await merchant_saas_bill_summary(
            factory, actor_user_id=actor_id, month=month,
            project_id=project_id,
        )
        if str(bill.get("project_id")) != str(project_id):
            raise ValueError(tr(
                "Данные счёта не совпадают с выбранным ботом. Откройте подключение заново.",
                "Invoice data do not match the selected bot. Reopen the connection.",
            ))
        info = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        username = next(
            (name for _, name, status in info.bot_instances
             if status == "registered"), None,
        )
        if username is None:
            raise ValueError(tr(
                "Бот больше не подключён. Откройте историю счетов аккаунта.",
                "This bot is no longer connected. Open account invoice history.",
            ))
        profile = await saas_price_profile(
            factory, actor_id=actor_id, project_id=project_id,
        )
        future_currency = str(profile["currency"])
        if future_currency not in ("RUB", "USD"):
            raise ValueError(tr(
                "Валюта обслуживания этого бота недоступна.",
                "This bot's service currency is unavailable.",
            ))

        trial: dict | None = None
        sales: dict | None = None
        if bill.get("invoice_id") is None:
            try:
                trial = await merchant_saas_trial_status(
                    factory, actor_user_id=actor_id, project_id=project_id,
                )
            except PermissionError:
                raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "Could not read per-bot trial status"
                )
        if bill.get("invoice_id") is None or bill.get("first_period"):
            # The live calendar-month preview is informational only. Never
            # mislabel it as the closed period of an issued commission invoice.
            try:
                sales = await merchant_saas_bot_native_sales_preview(
                    factory, actor_user_id=actor_id,
                    month=month, project_id=project_id,
                )
            except PermissionError:
                raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "Current-month per-bot sales preview is unavailable"
                )
                sales = None

        due = Decimal(str(bill.get("total_due_rub") or "0"))
        invoice_id = bill.get("invoice_id")
        rows: list[list[InlineKeyboardButton]] = []
        if invoice_id is not None and due > 0:
            rows.append([_button(
                tr("💳 Оплатить счёт", "💳 Pay invoice"),
                "spm:" + encode_uuid(UUID(str(invoice_id))) + ":RUB:0",
                "success",
            )])
        rows.append([_button(
            tr("⬅️ Назад к управлению ботом", "⬅️ Back to bot management"),
            "select:" + project_id.hex, "primary",
        )])
        args = dict(
            username=username, bill=bill, trial=trial,
            timezone=info.timezone, sales=sales,
            future_currency=future_currency,
        )
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=bot_bill_rich(**args),
            classic_html=bot_bill_classic(**args),
            reply_markup=_markup(rows),
        )

    async def show_saas_issued_invoice(
        bot: Bot, user_id: int, actor_id: UUID,
        invoice_id: UUID, *, page: int,
    ) -> None:
        """The existing invoice list drills down to one real issued bill.

        Central accounts remain accessible after the last bot is transferred.
        Historical liable owner is reverified on every callback.
        """
        report = await merchant_saas_statements(
            factory, actor_user_id=actor_id,
            project_id=fixed_project_id, page=page,
        )
        item = next(
            (row for row in report.invoices if row["id"] == str(invoice_id)),
            None,
        )
        if item is None:
            raise ValueError(tr(
                "Счёт отсутствует на этой странице.",
                "Invoice not present on this page.",
            ))
        snapshot = await saas_invoice_currency_snapshot(
            factory, actor_user_id=actor_id, invoice_id=invoice_id,
        )
        credits = await saas_invoice_credit_history(
            factory, actor_user_id=actor_id, invoice_id=invoice_id,
        )
        balance = await saas_v2_invoice_balance(
            factory, actor_id=actor_id, invoice_id=invoice_id,
            project_id=fixed_project_id,
        )
        profile = await saas_price_profile(
            factory, actor_id=actor_id, project_id=fixed_project_id,
        )
        buttons: list[list[InlineKeyboardButton]] = []
        if Decimal(str(balance["remaining_rub"])) > 0:
            buttons.append([_button(
                tr("💳 Оплатить", "💳 Pay"),
                "spm:" + encode_uuid(invoice_id) + ":RUB:" + _encode_saas_page(page),
                "success",
            )])
        # New payments use one full/partial v2 path. Historical locked
        # currency snapshots remain visible, but no new legacy lock is offered.
        buttons.append([_button(
            tr("⬅️ К счетам", "⬅️ Invoices"),
            ("saasaccountlist:" if fixed_project_id is None
             else "saasinvoices:") + str(page),
        )])
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=(issued_invoice_rich(item, snapshot, credits, balance)
                       + "\n" + service_currency_hint(
                           str(profile["currency"]), rich=True,
                       )),
            classic_html=(issued_invoice_classic(item, snapshot, credits, balance)
                          + "\n\n" + service_currency_hint(
                              str(profile["currency"]),
                          )),
            reply_markup=_markup(buttons),
        )

    async def show_moderators(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, page: int = 0,
    ) -> None:
        total, people = await list_project_moderators(
            factory, actor_id=actor_id, project_id=project_id, page=page,
        )
        buttons: list[list[InlineKeyboardButton]] = []
        for account in people:
            name = ("@" + account.username if account.username
                    else str(account.telegram_id))
            buttons.append([_button(
                "🗑 " + name[:36],
                "modremoveask:" + str(account.telegram_id), "danger",
            )])
        nav: list[InlineKeyboardButton] = []
        if page:
            nav.append(_button("◀️", "modmanage:" + str(page - 1)))
        if (page + 1) * 8 < total:
            nav.append(_button("▶️", "modmanage:" + str(page + 1)))
        if nav:
            buttons.append(nav)
        buttons.extend([
            [_button(tr("➕ Назначить модератора", "➕ Add moderator"),
                     "modadd", "success")],
            [_button(tr("⬅️ Настройки", "⬅️ Settings"), "settings")],
        ])
        await bot.send_message(
            user_id,
            tr("🛡 <b>Модераторы</b> · ", "🛡 <b>Moderators</b> · ")
            + str(total) + "\n"
            + tr(
                "Модератор видит покупателей и ожидающие заявки "
                "(без сумм и чеков), может писать сообщения и блокировать спам. "
                "Оплаты, тарифы, рассылки, выдача доступа и управление "
                "ботом ему недоступны.\n\n"
                "После назначения сотрудник открывает /mod в этом боте.",
                "Moderators see customers and pending requests "
                "(no amounts or receipts), message buyers, and ban spam. "
                "Payments, plans, broadcasts, grants and bot ownership "
                "remain unavailable.\n\n"
                "After assignment the moderator opens /mod in this bot.",
            ),
            parse_mode="HTML", reply_markup=_markup(buttons),
        )

    async def show_invoice(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        request_id: UUID,
    ) -> None:
        if fixed_project_id is None:
            raise PermissionError("Invoice details are connected-bot only")
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        ) as session:
            project = await _require_project_manager(
                session, project_id=project_id, actor_user_id=actor_id,
            )
            request = await session.scalar(
                select(PaymentRequest).where(
                    PaymentRequest.id == request_id,
                    PaymentRequest.project_id == project_id,
                )
            )
            if request is None:
                raise ValueError(tr("Счёт не найден", "Invoice not found"))
            plan_name = await session.scalar(
                select(Plan.name).where(
                    Plan.project_id == project_id, Plan.id == request.plan_id,
                )
            )
            method_name = await session.scalar(
                select(PaymentMethod.name).where(
                    PaymentMethod.project_id == project_id,
                    PaymentMethod.id == request.payment_method_id,
                )
            )
        names = {
            "waiting_confirmation": tr("Ожидает оплату", "Awaiting payment"),
            "waiting_review": tr("Чек на проверке", "Receipt under review"),
            "approved": tr("Оплата подтверждена", "Payment approved"),
            "rejected": tr("Отклонена", "Rejected"),
            "cancelled": tr("Отменена", "Cancelled"),
            "expired": tr("Истекла", "Expired"),
        }
        status = names.get(request.status, tr("Неизвестно", "Unknown"))
        ends = request.created_at.astimezone(ZoneInfo(project.timezone))
        await bot.send_message(
            user_id,
            tr("🧾 <b>Счёт</b>\n\n", "🧾 <b>Invoice</b>\n\n")
            + tr("№ ", "Ref ") + request.id.hex[:10].upper() + "\n"
            + tr("🕒 Создан: ", "🕒 Created: ")
            + f"{ends:%d.%m.%Y %H:%M} ({escape(project.timezone)})\n"
            + tr("📦 Тариф: ", "📦 Plan: ") + escape(plan_name or "—") + "\n"
            + tr("📅 Срок: ", "📅 Period: ")
            + escape(format_period(request.duration_value, request.duration_unit)) + "\n"
            + tr("💳 Способ: ", "💳 Method: ") + escape(method_name or "—") + "\n"
            + "💰 <b>" + escape(format_amount(request.final_amount, 18))
            + " " + escape(request.currency_code) + "</b>\n"
            + tr("Статус: ", "Status: ") + escape(status),
            parse_mode="HTML",
            reply_markup=_markup([
                [_button(
                    tr("👤 Подписчик", "👤 Subscriber"),
                    "subscriber:" + encode_uuid(request.customer_id),
                )],
                [_button(tr("⬅️ Счета покупателя", "⬅️ Customer invoices"),
                         "subinvoices:" + encode_uuid(request.customer_id) + ":0")],
                [_button(tr("⬅️ Панель администратора", "⬅️ Dashboard"), "dashboard")],
            ]),
        )

    async def show_notifications(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        if fixed_project_id is None:
            raise PermissionError("Notification settings are only in connected bots")
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id),
        ) as session:
            owner = await session.scalar(
                select(Project.owner_user_id).where(Project.id == project_id)
            )
            flags = await read_owner_settings(session, project_id)
        if owner != actor_id:
            await bot.send_message(
                user_id,
                tr("Настройки уведомлений доступны только владельцу.",
                   "Only the project owner can edit notifications."),
                reply_markup=_markup([[_button(tr("⬅️ Настройки", "⬅️ Settings"), "settings")]]),
            )
            return
        invoice_status = (
            tr("✅ Включены", "✅ Enabled") if flags["invoice"]
            else tr("🔕 Выключены", "🔕 Disabled")
        )
        expiry_status = (
            tr("✅ Включены", "✅ Enabled") if flags["expiry"]
            else tr("🔕 Выключены", "🔕 Disabled")
        )
        access_status = (
            tr("✅ Включены", "✅ Enabled") if flags["access"]
            else tr("🔕 Выключены", "🔕 Disabled")
        )
        daily_status = (
            tr("✅ Включён", "✅ Enabled") if flags["daily"]
            else tr("🔕 Выключен", "🔕 Disabled")
        )
        await bot.send_message(
            user_id,
            tr("🔔 <b>Уведомления владельца</b>\n\n"
               "🧾 Новый счёт: ", "🔔 <b>Owner notifications</b>\n\n"
               "🧾 New invoice: ")
            + invoice_status
            + tr(
                "\n⏳ Окончание подписок: ",
                "\n⏳ Subscription expiry: ",
            )
            + expiry_status
            + tr("\n🚨 Проблемы доступа: ", "\n🚨 Access problems: ")
            + access_status
            + tr("\n📊 Финансовый отчёт: ", "\n📊 Financial summary: ")
            + daily_status
            + tr(
                "\n\n🕙 Подписки и финансовый отчёт — в 10:00 "
                "по часовому поясу проекта. Отчёт показывает подтверждённые "
                "оплаты за предыдущий день, отдельно по валютам."
                "\n\n📩 Чеки и решения об оплате работают как прежде. "
                "Сводка не заменяет проверку банка.",
                "\n\n🕙 Subscription alerts and financial summaries "
                "arrive at 10:00 in the project timezone. The report shows "
                "yesterday's admin-approved payments by currency."
                "\n\n📩 Receipt reviews and decisions are unchanged. "
                "The summary is not a bank statement.",
            ),
            parse_mode="HTML",
            reply_markup=_markup([
                [_button(
                    tr("🧾 Выключить счета", "🧾 Mute invoices")
                    if flags["invoice"]
                    else tr("🧾 Включить счета", "🧾 Enable invoices"),
                    "notifyset:invoice:" + ("0" if flags["invoice"] else "1"),
                )],
                [_button(
                    tr("⏳ Выключить сроки", "⏳ Mute expiry")
                    if flags["expiry"]
                    else tr("⏳ Включить сроки", "⏳ Enable expiry"),
                    "notifyset:expiry:" + ("0" if flags["expiry"] else "1"),
                )],
                [_button(
                    tr("🚨 Выключить ошибки доступа", "🚨 Mute access problems")
                    if flags["access"]
                    else tr("🚨 Включить ошибки доступа", "🚨 Enable access problems"),
                    "notifyset:access:" + ("0" if flags["access"] else "1"),
                )],
                [_button(
                    tr("📊 Выключить отчёт", "📊 Mute daily summary")
                    if flags["daily"]
                    else tr("📊 Включить отчёт", "📊 Enable daily summary"),
                    "notifyset:daily:" + ("0" if flags["daily"] else "1"),
                )],
                [_button(tr("⬅️ Настройки", "⬅️ Settings"), "settings")],
            ]),
        )

    async def show_statistics(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        period: str = "7",
    ) -> None:
        if fixed_project_id is None:
            raise PermissionError("Статистика доступна в подключённом боте")
        stats = await read_owner_statistics(
            factory, actor_id=actor_id, project_id=project_id, period=period,
        )
        labels = {
            "7": tr("7 дней", "7 days"),
            "30": tr("30 дней", "30 days"),
            "all": tr("Всё время", "All time"),
        }
        bounds = (
            f"{stats.since:%d.%m.%Y} — {stats.through:%d.%m.%Y}"
            if stats.since is not None else tr("За всё время", "All time")
        )
        sales = (
            "\n".join(
                f"• {escape(display_currency(currency))}: {_statistics_amount(amount)}"
                f" · {count}"
                for currency, amount, count in stats.currency_totals
            )
            if stats.currency_totals else tr("Пока нет оплат", "No confirmed payments yet")
        )
        text = (
            tr("📊 <b>Статистика · ViMi Pay</b>\n", "📊 <b>Statistics · ViMi Pay</b>\n")
            + f"<b>{escape(stats.project_name)}</b>\n"
            + f"🌍 {escape(stats.timezone)}\n"
            + f"🗓️ {bounds}\n\n"
            + tr("💰 <b>Подтверждённые оплаты</b>\n", "💰 <b>Confirmed payments</b>\n")
            + tr("Платежей: ", "Payments: ") + str(stats.payment_count) + "\n"
            + sales + "\n\n"
            + tr("👥 <b>Покупатели</b>\n", "👥 <b>Customers</b>\n")
            + tr("Новых за период: ", "New in period: ") + str(stats.new_customers) + "\n"
            + tr("Всего зарегистрировано: ", "Total registered: ")
            + str(stats.total_customers) + "\n\n"
            + tr("🔹 <b>Сейчас</b>\n", "🔹 <b>Current status</b>\n")
            + tr("Активных платных подписчиков: ", "Active paying subscribers: ")
            + str(stats.active_paid_customers) + "\n"
            + tr("Истекают за 7 дней: ", "Expiring within 7 days: ")
            + str(stats.expiring_paid_customers) + "\n"
            + tr("Активных ручных прав: ", "Active manual grants: ")
            + str(stats.active_manual_grants) + "\n"
            + tr("Заявок на проверке: ", "Pending payment reviews: ")
            + str(stats.waiting_review) + "\n\n"
            + tr("Суммы не конвертируются. Оплаты подтверждены владельцем, "
                 "не банком или платформой.",
                 "Currencies are not converted. Payments are owner-confirmed, "
                 "not independently bank-verified.")
        )
        rows = [
            [_button(("✅ " if period == key else "") + label,
                     "statistics:" + key)
             for key, label in labels.items()],
            [_button(tr("⬅️ Панель администратора", "⬅️ Admin dashboard"),
                     "dashboard")],
        ]
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=rich_owner_statistics(stats),
            classic_html=text, reply_markup=_markup(rows),
        )

    async def show_network_profile(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        # Profiles are owned by the actor, not by project administrators.
        profile = await read_owner_network_profile(factory, actor_id=actor_id)
        if profile is None:
            lines = [tr(
                "📁 <b>Профиль сети</b>\n\n"
                "Сохраните тарифы, цены и способы оплаты одного бота. "
                "Новые подключённые боты получат независимые черновики "
                "и отключённую оплату.",
                "📁 <b>Network profile</b>\n\n"
                "Save one bot's plans, prices and payment methods. "
                "Future connected bots receive independent drafts "
                "and disabled payment methods.",
            )]
            buttons = [[_button(
                tr("📋 Сохранить настройки", "📋 Save settings"),
                "networkask", "success",
            )]]
        else:
            lines = [
                tr("📁 <b>Профиль сети</b>", "📁 <b>Network profile</b>"),
                tr("Источник: ", "Source: ") + escape(profile.source),
                tr("Версия: ", "Version: ") + str(profile.version),
                tr("Тарифы: ", "Plans: ") + str(profile.plan_count),
                tr("Способы оплаты: ", "Payment methods: ")
                + str(profile.method_count),
                (tr("Реквизиты: копируются", "Instructions: copied")
                 if profile.copy_instructions else tr(
                    "Реквизиты: заполняются отдельно",
                    "Instructions: set separately",
                )),
                tr("Автоприменение: ", "Auto-apply: ") + (
                    tr("✅ Включено", "✅ Enabled") if profile.enabled
                    else tr("⏸️ Отключено", "⏸️ Disabled")
                ),
                "",
                tr(
                    "Изменения профиля затронут только будущие подключения. "
                    "Существующие боты и подписчики не меняются.",
                    "Profile changes affect only future connections. "
                    "Existing bots and subscribers remain unchanged.",
                ),
            ]
            buttons = [[_button(
                tr("🔄 Обновить из этого бота", "🔄 Update from this bot"),
                "networkask",
            )]]
            buttons.append([_button(
                tr("⏸️ Отключить автокопирование", "⏸️ Pause auto-copy")
                if profile.enabled else tr(
                    "▶️ Включить автокопирование", "▶️ Resume auto-copy",
                ),
                "networkoff" if profile.enabled else "networkon",
            )])
        buttons.append([_button(
            tr("⬅️ Настройки", "⬅️ Settings"), "settings",
        )])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(buttons),
        )


    async def prompt(
        bot: Bot, user_id: int, actor_id: UUID,
        *, step: str, text: str, payload: dict | None = None,
        source_flow: SetupFlow | None = None,
        rich_html: str | None = None, show_main_menu: bool = True,
    ) -> None:
        if source_flow is None:
            generation = await set_flow(
                factory, actor_id=actor_id, step=step, payload=payload,
                project_id=fixed_project_id,
                bot_scope_id=bot.id if fixed_project_id else 0,
            )
        else:
            generation = await advance_flow(
                factory, actor_id=actor_id, source=source_flow,
                step=step, payload=payload or {},
                bot_scope_id=bot.id if fixed_project_id else 0,
            )
        # Generation-bound buttons prevent stale prompts cancelling a newer one.
        rows = [[_button(
            tr("⬅️ Назад", "⬅️ Back"), "promptback:" + generation,
        )]]
        if show_main_menu:
            rows.append([_button(
                tr("✖️ В меню", "✖️ Main menu"),
                "promptcancel:" + generation,
            )])
        if rich_html is not None:
            # Keep the real sent message ID for secure token capture while
            # rendering the full Rich message and its classic fallback.
            sent = await send_owner_rich(
                bot, chat_id=user_id, rich_html=rich_html,
                classic_html=text, reply_markup=_markup(rows),
            )
        else:
            sent = await bot.send_message(
                user_id, text, parse_mode="HTML", reply_markup=_markup(rows),
            )
        await bind_prompt(
            factory, actor_id=actor_id, step=step,
            message_id=sent.message_id, generation=generation,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )

    async def show_connections(
        bot: Bot, user_id: int, actor_id: UUID,
        *, page: int = 0, archived: bool = False,
    ) -> None:
        """Only a connection selector: 12 per page, stable paging for 100+ bots."""
        if archived:
            projects = await list_archived_owner_connections(
                factory, actor_id=actor_id,
            )
        else:
            home = await owner_home(factory, actor_id=actor_id)
            projects = home.projects
        page_size = 12
        total_pages = max(1, (len(projects) + page_size - 1) // page_size)
        page = min(max(page, 0), total_pages - 1)
        listed = projects[page * page_size:(page + 1) * page_size]
        rows: list[list[InlineKeyboardButton]] = [
            [_button(
                ("📦 " if archived else "📁 ") + name[:42],
                ("archview:" if archived else "select:") + pid.hex,
                "primary",
            )]
            for pid, name in listed
        ]
        if total_pages > 1:
            nav = []
            verb = "archives:" if archived else "projects:"
            if page > 0:
                nav.append(_button("◀️", verb + str(page - 1), "primary"))
            # The counter also refreshes the current page on demand.
            nav.append(_button(f"📄 {page + 1}/{total_pages}",
                               verb + str(page), "primary"))
            if page + 1 < total_pages:
                nav.append(_button("▶️", verb + str(page + 1), "primary"))
            rows.append(nav)
        if archived:
            rows.append([_button(
                tr("⬅️ Мои подключения", "⬅️ My connections"),
                "projects", "primary",
            )])
        else:
            rows.append([
                _button(tr("✨ Создать бота", "✨ Create a bot"),
                        "newmanaged", "success"),
                _button(tr("🔑 Через BotFather", "🔑 Via BotFather"),
                        "new", "primary"),
            ])
            if await list_archived_owner_connections(factory, actor_id=actor_id):
                rows.append([_button(
                    tr("📦 Архив подключений", "📦 Archived connections"),
                    "archives", "primary",
                )])
        # The account bill belongs only to /billing and the main dashboard.
        # The platform owner dashboard is also accessible from the main menu.
        rows.append([_button(
            tr("🏠 Главное меню", "🏠 Main menu"),
            "onboardhome", "primary",
        )])
        content_args = dict(
            total=len(projects), page=page,
            total_pages=total_pages, archived=archived,
        )
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=connection_registry_rich(**content_args),
            classic_html=connection_registry_classic(**content_args),
            reply_markup=_markup(rows),
        )

    async def show_archived_connection(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        archived = dict(await list_archived_owner_connections(
            factory, actor_id=actor_id,
        ))
        name = archived.get(project_id)
        if name is None:
            raise ValueError(tr(
                "Подключение не найдено в архиве.", "Connection is not archived.",
            ))
        await bot.send_message(
            user_id,
            tr("📦 <b>В архиве:</b> ", "📦 <b>Archived:</b> ")
            + escape(name) + "\n\n"
            + tr(
                "Бот отключён, финансовая история сохранена. "
                "Подключение не отображается в активном списке.",
                "Bot detached, financial records retained. "
                "This connection is hidden from the active list.",
            ),
            parse_mode="HTML",
            reply_markup=_markup([
                [_button(
                    tr("↩️ Восстановить подключение", "↩️ Restore connection"),
                    "restore:" + project_id.hex, "success",
                )],
                [_button(tr("⬅️ Архив", "⬅️ Archive"), "archives")],
            ]),
        )


    async def show_bots(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        """Explain available connection actions without exposing owner-only actions."""
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        retiring = any(status == "deleting" for _, _, status in data.bot_instances)
        connected = next(
            (username for _, username, status in data.bot_instances
             if status == "registered"), None,
        )
        rows: list[list[InlineKeyboardButton]] = []
        # Green = main live actions. A status check does not force a restart.
        quick_actions: list[InlineKeyboardButton] = []
        if connected:
            quick_actions.append(InlineKeyboardButton(
                text=tr("🚀 Открыть бота", "🚀 Open bot"),
                url="https://t.me/" + connected, style="success",
            ))
        if data.bot_instances:
            quick_actions.append(_button(
                tr("🔄 Обновить статус", "🔄 Refresh status"),
                "bots", "success",
            ))
        if quick_actions:
            rows.append(quick_actions)

        # Blue = setup, service and navigation. Keep two buttons per row
        # where possible, without hiding any action for disconnected bots.
        service_actions: list[InlineKeyboardButton] = []
        if not retiring and data.can_delete_bot:
            service_actions.append(_button(
                tr("🔑 Обновить токен" if connected else "🔑 Подключить бота",
                   "🔑 Update token" if connected else "🔑 Connect bot"),
                "connect", "primary",
            ))
            if connected:
                service_actions.append(_button(
                    tr("🤖 Синхронизация", "🤖 Sync Managed Bot"),
                    "managedsync:" + encode_uuid(project_id), "primary",
                ))
        if data.can_delete_bot:
            service_actions.append(_button(
                tr("✏️ Переименовать", "✏️ Rename"),
                "connrename:" + project_id.hex, "primary",
            ))
        # Account invoices remain separate from the connected bot's shop.
        service_actions.append(_button(
            tr("🧾 Счёт бота" if connected else "🧾 Счета аккаунта",
               "🧾 Bot bill" if connected else "🧾 Account invoices"),
            ("saasbotbill:" + encode_uuid(project_id)
             if connected else "saasinvoices:0"), "primary",
        ))
        service_actions.append(_button(
            tr("⬅️ Все подключения", "⬅️ All connections"),
            "projects", "primary",
        ))
        rows.extend(
            service_actions[index:index + 2]
            for index in range(0, len(service_actions), 2)
        )

        # Red = protected destructive flow, never mixed with blue or green.
        danger_kind: str | None = None
        if data.can_delete_bot:
            if data.bot_instances:
                for instance_id, username, status in data.bot_instances:
                    if status != "deleting":
                        danger_kind = "detach"
                        rows.append([_button(
                            tr("🗑 Отключить @", "🗑 Disconnect @") + username[:30],
                            "drop:" + encode_uuid(project_id)
                            + ":" + encode_uuid(instance_id),
                            "danger",
                        )])
            else:
                disposition = await get_connection_disposition(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                if disposition.has_history:
                    danger_kind = "archive"
                    rows.append([_button(
                        tr("📦 Убрать в архив", "📦 Archive connection"),
                        "connarchive:" + project_id.hex, "danger",
                    )])
                else:
                    danger_kind = "delete"
                    rows.append([_button(
                        tr("🗑 Удалить подключение", "🗑 Delete connection"),
                        "connremove:" + project_id.hex, "danger",
                    )])
        if platform_navigation(user_id):
            rows.append(platform_navigation(user_id))

        card_args = dict(
            project_name=data.project_name,
            bots=data.bots,
            runtime_label=owner_runtime_label(data.bot_runtime_label),
            retiring=retiring,
            has_instances=bool(data.bot_instances),
            editable=data.can_delete_bot,
            danger_kind=danger_kind,
        )
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=connection_management_rich(**card_args),
            classic_html=connection_management_classic(**card_args),
            reply_markup=_markup(rows),
        )

    def subscriber_label(user_id: int, username: str | None,
                         first_name: str | None) -> str:
        name = ("@" + username if username else first_name) or str(user_id)
        return str(name)[:45]

    def subscriber_date(value, timezone: str, *, fmt: str = "d") -> str:
        if value is None:
            return tr("бессрочно", "lifetime")
        return native_telegram_time(value, timezone, fmt=fmt)

    async def show_subscribers(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, status: str = "all", page: int = 0, search: str = "",
    ) -> None:
        snapshot = await list_subscribers(
            factory, actor_id=actor_id, project_id=project_id,
            status=status, page=page, search=search,
        )
        rows: list[list[InlineKeyboardButton]] = []
        for item in snapshot.customers:
            indicator = (
                "🟢" if item.paid or item.noncommercial else "⚪"
            )
            rows.append([_button(
                indicator + " " + subscriber_label(
                    item.telegram_id, item.username, item.first_name,
                ),
                "subscriber:" + encode_uuid(item.id),
            )])
        if not rows:
            message = tr("Пока никого нет.", "No subscribers found.")
        else:
            message = tr(
                "Нажмите на подписчика, чтобы увидеть оплаты, сроки "
                "и доступ к каналам.",
                "Select a subscriber to see payments, subscription periods "
                "and channel access.",
            )
        if search:
            message = tr("Поиск: ", "Search: ") + escape(search) + "\n" + message
        text = (
            tr("👥 <b>Подписчики</b>", "👥 <b>Subscribers</b>")
            + "\n" + tr("Найдено: ", "Found: ") + str(snapshot.total)
            + "\n" + message
        )
        # Search callbacks carry only a short, validated username/ID. The
        # rest of state remains in the database, not a global mutable cache.
        controls = [
            ("📋 " + tr("Все", "All"), "all"),
            ("🟢 " + tr("Активные", "Active"), "active"),
            ("⏳ " + tr("Истекают", "Expiring"), "expiring"),
            ("⚪ " + tr("Истекли", "Expired"), "expired"),
        ]
        for i in (0, 2):
            rows.append([_button(label, "sublist:" + key + ":0")
                         for label, key in controls[i:i + 2]])
        pages = max(1, (snapshot.total + SUBSCRIBER_PAGE_SIZE - 1)
                    // SUBSCRIBER_PAGE_SIZE)
        if page < pages - 1 or page > 0:
            nav: list[InlineKeyboardButton] = []
            prefix = "subfind:" if search else "sublist:" + status + ":"
            if page > 0:
                nav.append(_button(
                    "◀️", prefix + str(page - 1) + (
                        ":" + search if search else ""
                    ),
                ))
            nav.append(_button(str(page + 1) + "/" + str(pages),
                               "subscribers"))
            if page + 1 < pages:
                nav.append(_button(
                    "▶️", prefix + str(page + 1) + (
                        ":" + search if search else ""
                    ),
                ))
            rows.append(nav)
        rows.append([_button(
            tr("🔎 Найти по @username / ID",
               "🔎 Find by @username / ID"), "subsearch",
        )])
        rows.append([_button(
            tr("⬅️ Панель администратора", "⬅️ Admin dashboard"), "dashboard",
        )])
        await bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_subscriber_invoices(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        customer_id: UUID, *, page: int = 0,
    ) -> None:
        data = await list_subscriber_invoices(
            factory, actor_id=actor_id, project_id=project_id,
            customer_id=customer_id, page=page,
        )
        who = subscriber_label(
            data.telegram_id, data.username, data.first_name,
        )
        lines = [
            "🧾 <b>" + tr("Счета покупателя", "Customer invoices") + "</b>",
            "👤 " + escape(who) + " · ID " + str(data.telegram_id),
            tr("Всего счетов: ", "Total invoices: ") + str(data.total),
        ]
        rows: list[list[InlineKeyboardButton]] = []
        for invoice in data.invoices:
            status_icon = {
                "waiting_confirmation": "⏳",
                "waiting_review": "🕒",
                "approved": "✅",
                "rejected": "❌",
                "cancelled": "⚪",
                "expired": "⌛",
            }.get(invoice.status, "📄")
            status = {
                "waiting_confirmation": tr("Ожидает оплату", "Awaiting payment"),
                "waiting_review": tr("На проверке", "Under review"),
                "approved": tr("Подтверждён", "Approved"),
                "rejected": tr("Отклонён", "Rejected"),
                "cancelled": tr("Отменён", "Cancelled"),
                "expired": tr("Истёк", "Expired"),
            }.get(invoice.status, invoice.status)
            ref = invoice.id.hex[:10].upper()
            amount = format_amount(Decimal(invoice.amount), 18)
            lines.append(
                status_icon + " " + ref + " · " + escape(invoice.plan_name[:35])
                + " · " + escape(amount + " " + invoice.currency)
                + " · " + escape(status)
                + " · " + subscriber_date(invoice.created_at, data.project_timezone)
            )
            rows.append([_button(
                status_icon + " " + ref + " · "
                + amount[:20] + " " + invoice.currency[:8],
                "invoice:" + encode_uuid(invoice.id),
            )])
        if not data.invoices:
            lines.append(tr("Счетов пока нет.", "No invoices yet."))
        if data.total > 8:
            nav = []
            prefix = "subinvoices:" + encode_uuid(customer_id) + ":"
            if page:
                nav.append(_button("◀️", prefix + str(page - 1)))
            if (page + 1) * 8 < data.total:
                nav.append(_button("▶️", prefix + str(page + 1)))
            if nav:
                rows.append(nav)
        rows.append([_button(
            tr("👤 Карточка покупателя", "👤 Customer card"),
            "subscriber:" + encode_uuid(customer_id),
        )])
        rows.append([_button(
            tr("⬅️ Подписчики", "⬅️ Subscribers"), "subscribers",
        )])
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=rich_subscriber_invoices(data, who=who),
            classic_html="\n".join(lines), reply_markup=_markup(rows),
        )

    async def show_subscriber_card(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        customer_id: UUID, *, history_page: int = 0,
        grant_page: int = 0,
    ) -> None:
        data = await subscriber_detail(
            factory, actor_id=actor_id, project_id=project_id,
            customer_id=customer_id, history_page=history_page,
            grant_page=grant_page,
        )
        who = subscriber_label(
            data.telegram_id, data.username, data.first_name,
        )
        lines = [
            "👤 <b>" + escape(who) + "</b>",
            tr("Telegram ID: ", "Telegram ID: ") + str(data.telegram_id),
            "",
            tr("📦 <b>Подписки</b>", "📦 <b>Subscriptions</b>"),
        ]
        if not data.subscriptions:
            lines.append(tr("Нет покупок по тарифам", "No plan subscriptions"))
        for sub in data.subscriptions[:8]:
            status_label = {
                "active": tr("активна", "active"),
                "expired": tr("истекла", "expired"),
                "cancelled": tr("отменена", "cancelled"),
            }.get(sub.status, sub.status)
            lines.append(
                "• " + escape(sub.plan_name[:45]) + " · "
                + escape(status_label) + " · "
                + subscriber_date(sub.period_end, data.project_timezone, fmt="dt")
            )
        lines.extend([
            "",
            tr("💳 <b>Оплаченные покупки</b> (последние 5)",
               "💳 <b>Confirmed purchases</b> (latest 5)"),
            tr("Всего: ", "Total: ") + str(data.total_payments),
        ])
        for item in data.payments:
            lines.append(
                "• " + escape(item.plan_name[:28]) + " · "
                + escape(format_amount(Decimal(item.amount), 18) + " " + item.currency)
                + " · " + subscriber_date(item.approved_at, data.project_timezone)
            )
        lines.extend([
            "", tr("🧾 Счетов: ", "🧾 Invoices: ") + str(data.total_invoices)
            + tr(" · незавершённых: ", " · unfinished: ")
            + str(data.pending_invoices),
            ("🚫 " + tr("Заблокирован", "Banned") if data.is_banned
             else "✅ " + tr("Не заблокирован", "Not banned")),
        ])
        lines.extend(["", tr("🔑 <b>Основания доступа</b>",
                             "🔑 <b>Access grants</b>")
                      + " · " + str(data.total_grants)])
        if not data.grants:
            lines.append(tr("Нет действующих прав", "No active grants"))
        for grant in data.grants[:8]:
            label = {
                "payment": tr("Оплата", "Paid"),
                "stars_payment": tr("Оплата Stars", "Stars paid"),
                "manual": tr("Ручной", "Manual"),
                "owner_service": tr("Служебный", "Service"),
            }.get(grant.source, grant.source)
            lines.append(
                "• " + escape(grant.resource_name[:35]) + ": "
                + escape(label) + " · "
                + subscriber_date(grant.valid_until, data.project_timezone, fmt="dt")
            )
        if data.access:
            lines.extend([
                "", tr("📣 <b>Доступ к каналам</b>",
                       "📣 <b>Channel access</b>"),
            ])
            for entry in data.access[:8]:
                mark = "✅" if entry.actual == "granted" else "⏳" if entry.desired else "⚪"
                access_label = {
                    "granted": tr("выдан", "granted"),
                    "revoked": tr("отозван", "revoked"),
                    "unknown": tr("не подтверждён", "not verified"),
                }.get(entry.actual, entry.actual)
                lines.append(
                    mark + " " + escape(entry.resource_name[:35])
                    + " · " + escape(access_label)
                )
        rows: list[list[InlineKeyboardButton]] = []
        if data.owner_only_actions:
            cid_ref = encode_uuid(customer_id)
            rows.extend([
                [_button(tr("✉️ Написать сообщение", "✉️ Send message"),
                         "submsg:" + cid_ref)],
                [
                    _button(tr("🔑 Активировать", "🔑 Activate"),
                            "subactivate:" + cid_ref, "success"),
                    _button(tr("🗑 Аннулировать", "🗑 Revoke"),
                            "subcancel:" + cid_ref, "danger"),
                ],
                [_button(
                    tr("✅ Разблокировать", "✅ Unban") if data.is_banned
                    else tr("🚫 Заблокировать", "🚫 Ban"),
                    "subbanask:" + cid_ref + (":0" if data.is_banned else ":1"),
                    "success" if data.is_banned else "danger",
                )],
                [_button(tr("🛡 Доступ модератора", "🛡 Moderator access"),
                         "submod:" + cid_ref)],
            ])
        rows.append([_button(
            tr("🧾 История счетов", "🧾 Invoice history"),
            "subinvoices:" + encode_uuid(customer_id) + ":0",
        )])
        if data.total_payments > 5:
            nav: list[InlineKeyboardButton] = []
            if history_page:
                nav.append(_button(
                    "◀️", "subhistory:" + encode_uuid(customer_id)
                    + ":" + str(history_page - 1),
                ))
            if (history_page + 1) * 5 < data.total_payments:
                nav.append(_button(
                    "▶️ " + tr("Покупки", "Purchases"),
                    "subhistory:" + encode_uuid(customer_id)
                    + ":" + str(history_page + 1),
                ))
            if nav:
                rows.append(nav)
        if data.total_grants > 8:
            grant_nav: list[InlineKeyboardButton] = []
            cid_ref = encode_uuid(customer_id)
            if grant_page:
                grant_nav.append(_button(
                    "◀️ " + tr("Права", "Grants"),
                    "subgrantpage:" + cid_ref + ":" + str(grant_page - 1),
                ))
            if (grant_page + 1) * 8 < data.total_grants:
                grant_nav.append(_button(
                    tr("Права", "Grants") + " ▶️",
                    "subgrantpage:" + cid_ref + ":" + str(grant_page + 1),
                ))
            if grant_nav:
                rows.append(grant_nav)
        rows.append([_button(
            tr("🔄 Обновить карточку", "🔄 Refresh"),
            "subscriber:" + encode_uuid(customer_id),
        )])
        rows.append([_button(
            tr("⬅️ Подписчики", "⬅️ Subscribers"), "subscribers",
        )])
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=rich_subscriber_card(data, who=who),
            classic_html="\n".join(lines), reply_markup=_markup(rows),
        )

    async def show_manual_plan_picker(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        customer_id: UUID, *, page: int = 0,
    ) -> None:
        detail = await subscriber_detail(
            factory, actor_id=actor_id, project_id=project_id,
            customer_id=customer_id,
        )
        if not detail.owner_only_actions:
            raise PermissionError("Тариф может активировать только владелец")
        plans = await list_activatable_plans(
            factory, actor_id=actor_id, project_id=project_id,
            customer_id=customer_id, page=page,
        )
        cid = encode_uuid(customer_id)
        rows = [[_button(
            "📦 " + plan.name[:36] + " · " + format_period(
                plan.duration_value, plan.duration_unit,
            ),
            "subactask:" + cid + ":" + encode_uuid(plan.id),
        )] for plan in plans]
        if len(plans) == 8:
            rows.append([_button(
                tr("➡️ Далее", "➡️ Next"),
                "subactivate:" + cid + ":" + str(page + 1),
            )])
        if page:
            rows.append([_button(
                tr("⬅️ Назад", "⬅️ Previous"),
                "subactivate:" + cid + ":" + str(page - 1),
            )])
        rows.append([_button(
            tr("🔑 Доступ к отдельному каналу", "🔑 Single-channel access"),
            "subgrantstart:" + cid,
        )])
        rows.append([_button(
            tr("⬅️ Карточка покупателя", "⬅️ Customer card"),
            "subscriber:" + cid,
        )])
        await bot.send_message(
            user_id, tr(
                "🔑 <b>Активация тарифа</b>\n\n"
                "Выберите тариф для бесплатной выдачи. "
                "Оплата и подписка не создаются, доступ действует "
                "по сроку тарифа и может быть аннулирован.",
                "🔑 <b>Activate plan</b>\n\n"
                "Select a complimentary plan. No payment or paid "
                "subscription is created. Access follows the plan "
                "period and can be revoked.",
            ),
            parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_customer_cancel_picker(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        customer_id: UUID, *, page: int = 0, paid_page: int = 0,
    ) -> None:
        data = await subscriber_detail(
            factory, actor_id=actor_id, project_id=project_id,
            customer_id=customer_id, grant_page=page,
        )
        active = await list_active_customer_subscriptions(
            factory, actor_id=actor_id, project_id=project_id,
            customer_id=customer_id, page=paid_page,
        )
        if not data.owner_only_actions:
            raise PermissionError("Аннулирование доступно только владельцу")
        cid = encode_uuid(customer_id)
        rows: list[list[InlineKeyboardButton]] = []
        for sub in active.subscriptions:
            if sub.status == "active" and sub.id is not None:
                rows.append([_button(
                    "💳 " + sub.plan_name[:36],
                    "subendask:" + cid + ":" + encode_uuid(sub.id),
                    "danger",
                )])
        manual_plans: set[UUID] = set()
        for grant in data.grants:
            if grant.source != "manual":
                continue
            if grant.manual_plan_id is not None and grant.source_id is not None:
                if grant.source_id in manual_plans:
                    continue
                manual_plans.add(grant.source_id)
                rows.append([_button(
                    "🔑 " + (grant.manual_plan_name or "Тариф")[:36],
                    "sra:" + cid + ":" + encode_uuid(grant.source_id)
                    + ":" + str(page),
                    "danger",
                )])
            elif grant.manual_plan_id is None:
                rows.append([_button(
                    "🔑 " + grant.resource_name[:36],
                    "subrevoke:" + cid + ":" + encode_uuid(grant.id)
                    + ":" + str(page),
                    "danger",
                )])
        if not rows:
            rows.append([_button(
                tr("Нет прав на этой странице", "No grants on this page"),
                "subscriber:" + cid,
            )])
        if paid_page or (paid_page + 1) * 8 < active.total:
            paid_nav: list[InlineKeyboardButton] = []
            if paid_page:
                paid_nav.append(_button(
                    "💳◀️", "subcancel:" + cid + ":" + str(paid_page - 1)
                    + ":" + str(page),
                ))
            if (paid_page + 1) * 8 < active.total:
                paid_nav.append(_button(
                    "💳▶️", "subcancel:" + cid + ":" + str(paid_page + 1)
                    + ":" + str(page),
                ))
            rows.append(paid_nav)
        if page or (page + 1) * 8 < data.total_grants:
            grant_nav: list[InlineKeyboardButton] = []
            if page:
                grant_nav.append(_button(
                    "🔑◀️", "subcancel:" + cid + ":" + str(paid_page)
                    + ":" + str(page - 1),
                ))
            if (page + 1) * 8 < data.total_grants:
                grant_nav.append(_button(
                    "🔑▶️", "subcancel:" + cid + ":" + str(paid_page)
                    + ":" + str(page + 1),
                ))
            rows.append(grant_nav)
        rows.append([_button(
            tr("⬅️ Карточка покупателя", "⬅️ Customer card"),
            "subscriber:" + cid,
        )])
        await bot.send_message(
            user_id, tr(
                "🗑 <b>Аннулировать доступ</b>\n\n"
                "Выберите конкретный тариф или бесплатную выдачу. "
                "Каждое действие потребует подтверждения. "
                "История оплат сохраняется.",
                "🗑 <b>Revoke access</b>\n\n"
                "Select a specific paid subscription or complimentary "
                "grant. Each action requires confirmation. "
                "Payment history is preserved.",
            ),
            parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_manual_resource_picker(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        customer_id: UUID, *, page: int = 0,
    ) -> None:
        data = await subscriber_detail(
            factory, actor_id=actor_id, project_id=project_id,
            customer_id=customer_id,
        )
        if not data.owner_only_actions:
            raise PermissionError("Ручной доступ выдаёт только владелец бота")
        if page < 0 or page > 100:
            raise ValueError("Недопустимая страница")
        rows = [
            [_button(
                "📣 " + name[:45],
                "subtarget:" + encode_uuid(customer_id)
                + ":" + encode_uuid(resource_id),
            )]
            for resource_id, name in data.resources[page * 8:(page + 1) * 8]
        ]
        if (page + 1) * 8 < len(data.resources):
            rows.append([_button(
                "➡️ " + tr("Далее", "Next"),
                "subres:" + encode_uuid(customer_id) + ":" + str(page + 1),
            )])
        if page:
            rows.append([_button(
                "⬅️ " + tr("Назад", "Previous"),
                "subres:" + encode_uuid(customer_id) + ":" + str(page - 1),
            )])
        rows.append([_button(
            tr("⬅️ К подписчику", "⬅️ Subscriber"),
            "subscriber:" + encode_uuid(customer_id),
        )])
        await bot.send_message(
            user_id,
            tr(
                "🔑 <b>Ручной доступ</b>\n\n"
                "Выберите канал. Это отдельное бесплатное право — "
                "оплаченные подписки не меняются.",
                "🔑 <b>Manual access</b>\n\n"
                "Choose a channel. This is a separate complimentary grant; "
                "paid subscriptions are unchanged.",
            ),
            parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_obligations(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        page: int = 0, *, section: str = "overview",
    ) -> None:
        """Owner-friendly buyer checks, with 12 entries per category page."""
        if section not in ("overview", "pay", "sub", "grant", "sync"):
            raise ValueError(tr("Неизвестный раздел", "Unknown section"))
        if not 0 <= page <= 200:
            raise ValueError(tr("Недопустимая страница", "Invalid page"))
        snapshot = await list_owner_obligations(
            factory, actor_id=actor_id, project_id=project_id, page=page,
        )
        counts = snapshot.blockers
        overview = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        active_bot = next((
            (instance_id, name) for instance_id, name, status
            in overview.bot_instances if status != "deleting"
        ), None)
        username = active_bot[1] if active_bot else None
        project_ref = encode_uuid(project_id)
        back_action = (
            "drop:" + project_ref + ":" + encode_uuid(active_bot[0])
            if active_bot else "bots"
        )
        rows: list[list[InlineKeyboardButton]] = []
        records: list[str] = []
        visible = 0
        if section == "overview":
            if counts.requests:
                rows.append([_button(
                    tr("💳 Проверить покупки · ", "💳 Review purchases · ")
                    + str(counts.requests), "oblist:pay", "primary",
                )])
            if counts.subscriptions:
                rows.append([_button(
                    tr("🎟 Проверить подписки · ", "🎟 Review subscriptions · ")
                    + str(counts.subscriptions), "oblist:sub", "primary",
                )])
            if counts.grants:
                rows.append([_button(
                    tr("🔑 Другие доступы · ", "🔑 Other access · ")
                    + str(counts.grants), "oblist:grant", "primary",
                )])
            if counts.access or counts.access_actions or counts.join_tickets:
                rows.append([_button(
                    tr("🔄 Проверить доступы", "🔄 Review access"),
                    "oblist:sync", "primary",
                )])
            if (
                counts.access_actions > 0 and not any((
                    counts.requests, counts.file_orders,
                    counts.stars_checkouts, counts.native_paid_links,
                    counts.subscriptions, counts.grants,
                    counts.access, counts.join_tickets,
                )) and overview.can_delete_bot and active_bot
            ):
                # A deleted/unreachable BotFather bot can leave access
                # operations stuck. Keep the existing explicit, owner-only
                # exception discoverable from the overview. The callback
                # validates the same safety conditions again.
                rows.append([_button(
                    tr("🗑 Отключить недоступного бота",
                       "🗑 Retire unreachable bot"),
                    "orphanask:" + project_ref + ":"
                    + encode_uuid(active_bot[0]), "danger",
                )])
            if (counts.file_orders or counts.stars_checkouts
                    or counts.native_paid_links) and username:
                rows.append([InlineKeyboardButton(
                    text=tr("🚀 Открыть бота и проверить продажи",
                            "🚀 Open bot and review sales"),
                    url="https://t.me/" + username,
                    style="success",
                )])
            rows.append([_button(
                tr("⬅️ К отключению", "⬅️ Back to disconnection"),
                back_action, "primary",
            )])
        elif section == "pay":
            for item in snapshot.payments:
                visible += 1
                identity = ("@" + item.username
                            if item.username else str(item.telegram_user_id))
                records.append(
                    "💳 <b>" + escape(identity, quote=True) + "</b> · "
                    + escape(item.amount + " " + item.currency, quote=True)
                    + " · " + escape(obligation_status(item.status), quote=True)
                )
                rows.append([_button(
                    (tr("💳 Отклонить покупку · ", "💳 Reject purchase · ")
                     if item.status == "waiting_review"
                     else tr("💳 Закрыть покупку · ", "💳 Close purchase · "))
                    + identity[:24],
                    "ob:pay:" + project_ref + ":" + encode_uuid(item.id)
                    + (":" + str(page) if page else ""), "danger",
                )])
        elif section == "sub":
            for item in snapshot.subscriptions:
                visible += 1
                identity = ("@" + item.username
                            if item.username else str(item.telegram_user_id))
                period = (item.period_end.strftime("%d.%m.%Y")
                          if item.period_end else tr("без срока", "no expiry"))
                records.append(
                    "🎟 <b>" + escape(identity, quote=True) + "</b> · "
                    + escape(item.plan_name, quote=True) + " · "
                    + tr("до ", "until ") + period
                )
                rows.append([_button(
                    tr("🎟 Завершить подписку · ", "🎟 End subscription · ")
                    + identity[:24],
                    "ob:sub:" + project_ref + ":" + encode_uuid(item.id)
                    + (":" + str(page) if page else ""), "danger",
                )])
        elif section == "grant":
            for item in snapshot.grants:
                if item.source in ("payment", "stars_payment"):
                    continue
                visible += 1
                records.append(
                    tr("🔑 Дополнительный доступ пользователя ",
                       "🔑 Additional access for user ")
                    + "<b>" + str(item.telegram_user_id) + "</b>"
                )
                rows.append([_button(
                    tr("🔑 Проверить отзыв · ", "🔑 Review revocation · ")
                    + str(item.telegram_user_id),
                    "ob:grant:" + project_ref + ":" + encode_uuid(item.id)
                    + (":" + str(page) if page else ""), "danger",
                )])
        elif section == "sync":
            for item in snapshot.access:
                visible += 1
                records.append(
                    tr("🔄 Состояние доступа", "🔄 Access status")
                    + ": <b>" + escape(obligation_status(item.actual),
                                       quote=True) + "</b>"
                )
                rows.append([_button(
                    tr("🔄 Перепроверить доступ", "🔄 Recheck access"),
                    "ob:sync:" + project_ref + ":" + encode_uuid(item.id)
                    + (":" + str(page) if page else ""), "primary",
                )])
            if counts.access_actions or counts.join_tickets:
                records.append(
                    "⏳ " + tr(
                        "Бот ещё обрабатывает изменения доступа и приглашения. "
                        "Проверяйте состояние после завершения.",
                        "The bot is still processing access changes and invites. "
                        "Check again after processing finishes.",
                    )
                )
            if (
                counts.access_actions and not any((
                    counts.requests, counts.subscriptions, counts.grants,
                    counts.access, counts.join_tickets,
                )) and overview.can_delete_bot
            ):
                for instance_id, _name, status in overview.bot_instances:
                    if status != "deleting":
                        records.append("⚠️ " + tr(
                            "Только для бота, окончательно недоступного "
                            "в BotFather: отдельное подтверждение остановки "
                            "без ложного отчёта об успешной доставке доступа.",
                            "Only if the bot is permanently unavailable in "
                            "BotFather: separately approve retirement without "
                            "claiming successful access delivery.",
                        ))
                        rows.append([_button(
                            tr("🗑 Отключить недоступного бота",
                               "🗑 Retire unreachable bot"),
                            "orphanask:" + project_ref + ":"
                            + encode_uuid(instance_id), "danger",
                        )])
        if section != "overview":
            if not visible:
                records.append(tr(
                    "✅ На этой странице нет записей для ручной проверки. "
                    "Если отключение всё ещё заблокировано, "
                    "ожидайте завершения обработки доступа.",
                    "✅ No items need manual review on this page. "
                    "If disconnection remains blocked, wait for access "
                    "processing to finish.",
                ))
            nav: list[InlineKeyboardButton] = []
            if page:
                nav.append(_button(
                    "⬅️", "obpage:" + project_ref + ":"
                    + section + ":" + str(page - 1), "primary",
                ))
            # Pagination follows source rows, not only editable rows.
            # Paid grants can appear alongside manual grants and are skipped
            # as actions; they must not accidentally hide subsequent pages.
            source_size = {
                "pay": len(snapshot.payments),
                "sub": len(snapshot.subscriptions),
                "grant": len(snapshot.grants),
                "sync": len(snapshot.access),
            }[section]
            if source_size == 12 and page < 200:
                nav.append(_button(
                    "➡️", "obpage:" + project_ref + ":"
                    + section + ":" + str(page + 1), "primary",
                ))
            if nav:
                rows.append(nav)
            rows.append([_button(
                tr("⬅️ Покупатели и доступы", "⬅️ Customers and access"),
                "oblist", "primary",
            )])
        base_rich = obligations_card(
            username=username, blockers=counts, section=section,
            page=page, rich=True,
        )
        base_classic = obligations_card(
            username=username, blockers=counts, section=section,
            page=page, rich=False,
        )
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=(
                base_rich + "".join("<p>" + record + "</p>"
                                    for record in records)
            ),
            classic_html=base_classic + (
                "\n\n" + "\n".join(records) if records else ""
            ),
            reply_markup=_markup(rows),
        )

    async def notify_buyer(
        central_bot: Bot, owner_tg_id: int, actor_id: UUID,
        project_id: UUID, change,
    ) -> None:
        if not change.changed or change.telegram_user_id is None:
            return
        key = get_settings().bot_token_encryption_key
        if not key:
            await central_bot.send_message(
                owner_tg_id, tr("⚠️ Состояние сохранено, но уведомление покупателя недоступно.",
                                "⚠️ Change saved, but buyer notifications are unavailable."),
            )
            return
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            instance = await session.scalar(select(BotInstance).where(
                BotInstance.project_id == project_id,
                BotInstance.status == "registered",
            ).limit(1))
            cipher = instance.token_ciphertext if instance else None
        if not cipher:
            await central_bot.send_message(
                owner_tg_id, tr("⚠️ Изменение сохранено, но бот покупателя отключён.",
                                "⚠️ Change saved, but the buyer bot is disconnected."),
            )
            return
        try:
            customer_bot = Bot(token=decrypt_token(cipher, key=key))
            try:
                async with recipient_language(factory, change.telegram_user_id):
                    await customer_bot.send_message(
                        change.telegram_user_id, obligation_message(change.message),
                    )
            finally:
                await customer_bot.session.close()
        except (AiogramError, ValueError, RuntimeError, InvalidToken):
            await central_bot.send_message(
                owner_tg_id,
                tr("⚠️ Изменение сохранено, но Telegram не доставил уведомление покупателю.",
                   "⚠️ Change saved, but Telegram could not notify the buyer."),
            )


    async def initial_setup_active(actor_id: UUID, project_id: UUID, data=None) -> bool:
        """Use the real initial-setup flag, never assume every bot is new."""
        if fixed_project_id is None:
            return False
        if data is None:
            data = await setup_overview(
                factory, actor_id=actor_id, project_id=project_id,
            )
        if not any(status == "registered" for _, _, status in data.bot_instances):
            return False
        try:
            profile = await saas_price_profile(
                factory, actor_id=actor_id, project_id=project_id,
            )
        except PermissionError:
            return False
        except Exception:
            logging.getLogger(__name__).exception(
                "ViMi Pay setup state unavailable"
            )
            return not data.sales_configured
        return profile.get("currency_selected") is False

    async def show_resources(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, source_message_id: int | None = None,
    ) -> None:
        """No resource/statistics buttons in the initial setup catalog."""
        data = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        onboarding = await initial_setup_active(actor_id, project_id, data)
        rows = (
            [] if onboarding else [
                [_button("📢 " + name[:43], "resourceview:" + encode_uuid(rid))]
                for rid, name, _ in data.resources
            ]
        )
        rows.extend([
            [_button(tr("➕ Добавить канал", "➕ Add channel"),
                     "pick:channel", "success")],
            [_button(tr("➕ Добавить группу", "➕ Add group"),
                     "pick:group", "success")],
            [_button(tr("✍️ Добавить по ID", "✍️ Add by ID"),
                     "resource:manual", "primary")],
            [_button(tr("⬅️ Назад", "⬅️ Back"), "dashboard", "primary")],
        ])
        await replace_owner_rich(
            bot, chat_id=user_id, message_id=source_message_id,
            rich_html=directory_rich(len(data.resources), onboarding=onboarding),
            classic_html=directory_classic(len(data.resources), onboarding=onboarding),
            reply_markup=_markup(rows),
        )

    async def show_channel_setup_step_two(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, source_message_id: int | None = None,
    ) -> None:
        """Explicit Back destination: real channel step 2/4, not the work panel."""
        data = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        rows = [
            [_button(tr("📢 Каналы и группы", "📢 Channels and groups"),
                     "resources", "primary")],
            [_button(tr("➡️ Этап 3/4 · Способы оплаты",
                        "➡️ Step 3/4 · Payment methods"),
                     "setupstep3", "primary")],
        ]
        await replace_owner_rich(
            bot, chat_id=user_id, message_id=source_message_id,
            rich_html=setup_stage_two_rich(len(data.resources)),
            classic_html=setup_stage_two_classic(len(data.resources)),
            reply_markup=_markup(rows),
        )

    async def guided_channel_selection(actor_id: UUID, project_id: UUID) -> bool:
        """Treat an empty connection list as a new channel setup attempt.

        A completed bot may remove all channels to reconnect them. Keep its
        billing/currency history intact but continue the channel wizard.
        Capture this before saving the newly selected resource.
        """
        if await initial_setup_active(actor_id, project_id):
            return True
        overview = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        return not overview.resources

    async def delete_native_chat_picker_card(
        bot: Bot, user_id: int, flow: SetupFlow | None,
    ) -> None:
        """Remove only the old instruction message of a matched picker flow."""
        if (
            flow is None or flow.step != "chat_picker"
            or flow.project_id != fixed_project_id
            or not isinstance(flow.prompt_message_id, int)
            or flow.prompt_message_id <= 0
        ):
            return
        try:
            await bot.delete_message(user_id, flow.prompt_message_id)
        except (AiogramError, TypeError):
            # Deletion can fail for an old message. Still deliver the result.
            pass

    async def show_payment_setup_step(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, source_message_id: int | None = None,
    ) -> None:
        """Show step 3 directly after a verified channel, with no success interstitial.

        Keep account currency, old invoices and subscriptions untouched even
        when an existing owner reconnects their first channel.
        """
        if fixed_project_id is None or project_id != fixed_project_id:
            raise PermissionError("Setup step is tenant-only")
        overview = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        if not any(status == "active" for _, _, status in overview.resources):
            raise ValueError(tr(
                "Сначала подключите канал или группу.",
                "Connect a channel or group first.",
            ))
        bot_name = (
            overview.live_bot_username
            or next((name for name, state in overview.bots
                     if state == "registered"), "")
            or overview.project_name
        )
        await replace_owner_rich(
            bot, chat_id=user_id, message_id=source_message_id,
            rich_html=owner_setup_step_rich(
                bot_name=bot_name, timezone=overview.timezone,
                action="methods",
            ),
            classic_html=owner_setup_step_classic(
                bot_name=bot_name, timezone=overview.timezone,
                action="methods",
            ),
            reply_markup=_markup([[_button(
                wizard_primary_label("methods", "💳"),
                "methods:guided", "primary",
            )]]),
        )

    async def show_connection_result(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, success: bool, kind: str, resource_name: str | None = None,
        problem: str = "permissions", guided: bool = False,
    ) -> None:
        """On success open the destination directly; on failure show one error."""
        if success:
            if guided or await initial_setup_active(actor_id, project_id):
                # No "connected" acknowledgement and no extra Next click.
                await show_payment_setup_step(
                    bot, user_id, actor_id, project_id,
                )
            else:
                # Adding a channel to an already working bot is maintenance,
                # not onboarding: return to the actual connected-resource list.
                await show_resources(bot, user_id, actor_id, project_id)
            return
        username = await connected_bot_handle(actor_id, project_id)
        rows = [[_button(
            tr("⬅️ Каналы и группы", "⬅️ Channels and groups"),
            "resources", "primary",
        )]]
        # Rejected choices keep the single bilingual rich explanation.
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=connection_result_rich(
                success=False, kind=kind, username=username,
                resource_name=resource_name, onboarding=False,
                problem=problem,
            ),
            classic_html=connection_result_classic(
                success=False, kind=kind, username=username,
                resource_name=resource_name, onboarding=False,
                problem=problem,
            ),
            reply_markup=_markup(rows),
        )

    async def connected_bot_handle(
        actor_id: UUID, project_id: UUID,
    ) -> str | None:
        overview = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        return next((
            name for _, name, status in overview.bot_instances
            if status == "registered"
        ), None) or next((
            name for name, status in overview.bots
            if status == "registered"
        ), None)

    async def show_resource_card(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        resource_id: UUID,
    ) -> None:
        metrics = await read_channel_insights(
            factory, actor_id=actor_id, project_id=project_id,
            resource_id=resource_id,
        )
        members_text = tr("Недоступно", "Unavailable")
        try:
            # Bot API getChatMemberCount returns the actual Telegram chat
            # members, distinct from our paid customer count.
            chat_id = int(metrics.external_id)
            members_text = str(await bot.get_chat_member_count(chat_id=chat_id))
        except (ValueError, AiogramError):
            pass
        # Never round stored financial totals to two decimals. Crypto and
        # other assets may use 8+ fractional digits; use plain fixed-point
        # formatting to retain all meaningful decimal places (no 1E-8).
        def precise_amount(amount: Decimal) -> str:
            rendered = format(amount, "f")
            return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered

        revenues = "\n".join(
            f"• {escape(display_currency(currency))}: {precise_amount(amount)} ({count})"
            for currency, amount, count in metrics.sales_by_currency
        ) or tr("Нет подтверждённых продаж", "No confirmed sales")
        type_label = tr("Канал", "Channel") if metrics.resource_type == (
            "telegram_channel"
        ) else tr("Группа", "Group")
        text = (
            "📢 <b>" + escape(metrics.name) + "</b>\n"
            + type_label + "\n\n"
            + tr("👥 Участников Telegram: ", "👥 Telegram members: ")
            + members_text + "\n"
            + tr("💎 Активных платных подписчиков: ",
                 "💎 Active paid subscribers: ")
            + str(metrics.active_paid_customers) + "\n"
            + tr("👤 Уникальных покупателей: ", "👤 Unique paid buyers: ")
            + str(metrics.paid_customers) + "\n"
            + tr("📦 Активных тарифов: ", "📦 Active plans: ")
            + str(metrics.active_plans) + "\n"
            + tr("🧾 Подтверждённых оплат: ", "🧾 Confirmed payments: ")
            + str(metrics.paid_orders) + "\n"
            + tr("⏳ Неоплаченных заявок: ", "⏳ Unpaid invoices: ")
            + str(metrics.pending_invoices) + "\n\n"
            + tr("💰 <b>Продажи за всё время по валютам</b>\n",
                 "💰 <b>All-time sales by currency</b>\n")
            + revenues
        )
        if metrics.paid_orders:
            text += "\n\n" + tr(
                "🔒 Есть подтверждённые покупки: удалить канал нельзя, "
                "но его тарифы можно снять с продажи.",
                "🔒 Confirmed purchases prevent deletion; "
                "you can still disable plans for new sales.",
            )
        buttons = [
            [_button(tr("🔄 Обновить статистику", "🔄 Refresh statistics"),
                     "resourceview:" + encode_uuid(resource_id))],
        ]
        if not metrics.paid_orders:
            buttons.append([_button(
                tr("🗑 Удалить канал", "🗑 Remove channel"),
                "resremoveask:" + encode_uuid(resource_id), "danger",
            )])
        buttons.append([_button(tr("⬅️ Каналы", "⬅️ Channels"), "resources")])
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=rich_channel_card(metrics, members_text=members_text),
            classic_html=text, reply_markup=_markup(buttons),
        )

    async def show_plans(bot: Bot, user_id: int, actor_id: UUID, project_id: UUID):
        data = await setup_overview(factory, actor_id=actor_id, project_id=project_id)
        rows: list[list[InlineKeyboardButton]] = []
        lines = [tr("📦 <b>Тарифы</b>", "📦 <b>Plans</b>"), ""]
        if not data.plans:
            lines.append(tr("Тарифов пока нет.", "There are no plans yet."))
        for pid, name, status in data.plans[:15]:
            lines.append(f"• <b>{escape(name)}</b> — {_status(status)}")
            rows.append([_button("⚙️ " + name[:45], "plan:" + pid.hex)])
        active = any(status == "active" for _, _, status in data.plans)
        archived = any(status == "archived" for _, _, status in data.plans)
        if active or archived:
            rows.append([_button(
                tr("▶️ Включить все тарифы", "▶️ Enable all plans")
                if not active else
                tr("⏸️ Выключить все тарифы", "⏸️ Disable all plans"),
                "bulks:plan:" + ("off:" if active else "on:")
                + encode_uuid(project_id),
                "danger" if active else "success",
            )])
        rows.append([_button("➕ Создать тариф", "newplan", "success")])
        if not data.methods and not getattr(data, "sales_configured", False):
            lines.append(tr(
                "\n💳 Настройте оплату, затем публикуйте тарифы.",
                "\n💳 Configure payments before publishing plans.",
            ))
            rows.append([_button("➕ Настроить оплату", "methods")])
        rows.append([_button("⬅️ Панель администратора", "dashboard")])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML", reply_markup=_markup(rows),
        )


    async def show_plan_card(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, plan_id: UUID,
    ):
        data = await get_owner_plan_info(
            factory, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        rows = []
        codes = await owner_available_currencies(
            factory, actor_id=actor_id, project_id=project_id,
            fiat_only=True,
        )
        fiat_prices = [price for price in data.prices if price.currency in codes]
        lines = [
            "📦 <b>" + escape(data.name) + "</b>",
            tr("Статус: ", "Status: ") + _status(data.status),
            "⏳ " + escape(format_period(data.duration_value, data.duration_unit)),
            "📢 " + escape(data.resource_name or tr("Канал не привязан", "No channel assigned")),
            "",
            tr("<b>Цены и оплата</b>", "<b>Prices and payments</b>"),
        ]
        if not fiat_prices:
            lines.append(tr("Цена в обычной валюте ещё не указана.",
                            "No fiat price has been set yet."))
        for price in fiat_prices:
            lines.append(
                ("✅ " if price.method_count or price.currency == "XTR" else "⚠️ ")
                + escape(display_price(
                    format_amount(price.amount, price.precision), price.currency,
                ))
                + (
                    ""
                    if price.currency == "XTR" else
                    tr(" · оплата настроена", " · payment ready")
                    if price.method_count else
                    tr(" · нужен способ оплаты", " · payment method required")
                )
            )
        rows.extend([
            [_button("💰 " + display_currency(code), "price:" + plan_id.hex + ":" + code)]
            for code in codes
        ])
        rows.append([_button(
            tr("➕ Другая валюта", "➕ Another currency"),
            "currencyadd:price:" + plan_id.hex,
        )])
        if any(not price.method_count for price in fiat_prices):
            rows.append([
                _button("➕ Подключить способ оплаты", "newmethod", "success"),
            ])
        token = encode_uuid(plan_id)
        lines.append("")
        lines.append(tr("<b>Управление тарифом</b>", "<b>Manage plan</b>"))
        rows.append([_button(
            tr("✏️ Изменить название", "✏️ Rename plan"),
            "plrename:" + token,
        )])
        if data.status == "draft":
            rows.append([
                _button(
                    tr("⏳ Изменить срок", "⏳ Change duration"),
                    "plduration:" + token,
                ),
                _button(
                    tr("📢 Изменить канал", "📢 Change channel"),
                    "plresource:" + token,
                ),
            ])
            rows.append([
                _button(
                    "✅ Опубликовать тариф",
                    "publish:" + token + ":" + data.revision, "success",
                ),
            ])
        elif data.status == "active":
            rows.append([_button(
                tr("⏸️ Снять с продажи", "⏸️ Stop new sales"),
                "plarchive:" + token,
            )])
        elif data.status == "archived":
            rows.append([_button(
                tr("▶️ Вернуть в продажу", "▶️ Resume sales"),
                "plrestore:" + token, "success",
            )])
        if not data.has_purchase:
            rows.append([_button(
                tr("🗑 Удалить тариф", "🗑 Delete plan"),
                "pldelete:" + token, "danger",
            )])
            if data.has_history:
                lines.append(tr(
                    "<i>Неоплаченные счета не мешают удалить тариф. "
                    "История заявок и чеки сохранятся.</i>",
                    "<i>Unpaid invoices do not prevent removing this plan. "
                    "Invoice and receipt history remains intact.</i>",
                ))
        else:
            lines.append(tr(
                "<i>Удаление недоступно: по тарифу есть подтверждённые "
                "покупки. Подписки и платежи должны сохраниться.</i>",
                "<i>Deletion is unavailable: this plan has confirmed "
                "purchases. Existing subscriptions and payments remain.</i>",
            ))
        rows.append([_button("⬅️ Все тарифы", "plans")])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(rows),
        )


    async def show_plan_duration_editor(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, plan_id: UUID,
    ) -> None:
        data = await get_owner_plan_info(
            factory, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        if data.status != "draft":
            raise ValueError("Срок можно изменить только у неопубликованного тарифа")
        token = encode_uuid(plan_id)
        rows = [
            [_button(
                tr(label, format_period(value, unit)),
                "plsetperiod:" + token + ":" + key,
            )]
            for key, (value, unit, label) in DURATIONS.items()
        ]
        rows.append([_button(
            tr("⬅️ К тарифу", "⬅️ Back to plan"), "plan:" + plan_id.hex,
        )])
        await bot.send_message(
            user_id, tr(
                "⏳ <b>Изменить срок черновика</b>\n\n"
                "Выберите новый срок. Существующие цены сохранятся.",
                "⏳ <b>Change draft duration</b>\n\n"
                "Choose the new duration. Existing prices are retained.",
            ), parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_plan_resource_editor(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, plan_id: UUID,
    ) -> None:
        data = await get_owner_plan_info(
            factory, actor_id=actor_id, project_id=project_id, plan_id=plan_id,
        )
        if data.status != "draft":
            raise ValueError("Канал можно изменить только у неопубликованного тарифа")
        snapshot = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        token = encode_uuid(plan_id)
        rows = [
            [_button(
                "📢 " + name[:45], "plsetres:" + token + ":" + encode_uuid(rid),
            )]
            for rid, name, status in snapshot.resources if status == "active"
        ]
        rows.append([_button(
            tr("⬅️ К тарифу", "⬅️ Back to plan"), "plan:" + plan_id.hex,
        )])
        await bot.send_message(
            user_id, tr(
                "📢 <b>Изменить канал черновика</b>\n\n"
                "Выберите канал или группу, доступ к которым даст тариф.",
                "📢 <b>Change draft channel</b>\n\n"
                "Select the channel or group this plan will grant access to.",
            ), parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_methods(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        page: int = 0, *, guided: bool | None = None,
        source_message_id: int | None = None,
    ) -> None:
        """Concise payment-method lesson and real manual-method pagination."""
        if not 0 <= page <= 10000:
            raise ValueError("Invalid payment-method page")
        if guided is None:
            guided = await initial_setup_active(actor_id, project_id)
        methods = await list_owner_methods(
            factory, actor_id=actor_id, project_id=project_id, page=page,
        )
        if page and not methods:
            # An old pagination button after deletion must never trap the owner
            # in a blank page or show a meaningless "Page 2".
            await show_methods(
                bot, user_id, actor_id, project_id, page=0, guided=guided,
                source_message_id=source_message_id,
            )
            return
        has_next = False
        if len(methods) == METHOD_PAGE_SIZE and page < 10000:
            has_next = bool(await list_owner_methods(
                factory, actor_id=actor_id, project_id=project_id,
                page=page + 1,
            ))
        rich_parts = [
            "<h3>" + tr("💳 Способы оплаты", "💳 Payment methods") + "</h3>",
        ]
        classic_parts = [
            tr("💳 <b>Способы оплаты</b>", "💳 <b>Payment methods</b>"),
        ]
        if guided:
            rich_parts.append(
                "<p><b>" + tr("🚀 ЭТАП НАСТРОЙКИ · 3/4",
                                "🚀 SETUP STEP · 3/4") + "</b></p>"
            )
            classic_parts.append(tr("🚀 Этап 3/4", "🚀 Step 3/4"))
        rich_parts.extend([
            "<h4>" + tr("📋 ЧТО НУЖНО СДЕЛАТЬ?",
                           "📋 WHAT TO DO?") + "</h4>",
            "<p>" + tr(
                "Подключите <b>хотя бы один способ оплаты</b>, "
                "чтобы покупатели могли оплачивать подписки и файлы.",
                "Enable <b>at least one payment method</b> "
                "so buyers can pay for subscriptions and files.",
            ) + "</p>",
            "<p>" + tr(
                "⭐ <b>Telegram Stars</b> · 💎 <b>Crypto Pay</b> · "
                "✍️ <b>Полуавтомат</b>",
                "⭐ <b>Telegram Stars</b> · 💎 <b>Crypto Pay</b> · "
                "✍️ <b>Manual transfer</b>",
            ) + "</p>",
            "<p>💡 " + tr(
                "Остальные способы можно подключить позже.",
                "You can add other methods later.",
            ) + "</p>",
        ])
        classic_parts.extend([
            tr("Подключите <b>хотя бы один способ</b> для "
               "оплаты подписок и файлов.",
               "Enable <b>at least one method</b> to sell "
               "subscriptions and files."),
            tr("⭐ Telegram Stars · 💎 Crypto Pay · ✍️ Полуавтомат",
               "⭐ Telegram Stars · 💎 Crypto Pay · ✍️ Manual transfer"),
            tr("💡 Остальные способы можно добавить позже.",
               "💡 You can add the others later."),
        ])
        rows: list[list[InlineKeyboardButton]] = []
        if methods:
            rich_parts.append("<h4>" + tr(
                "🧾 Ручные способы", "🧾 Manual payment methods",
            ) + "</h4>")
            for method in methods:
                item = (
                    "• <b>" + escape(method.name) + "</b> · "
                    + _status(method.status) + " · "
                    + escape(", ".join(method.currencies) or tr(
                        "Без валюты", "No currency",
                    ))
                )
                rich_parts.append("<p>" + item + "</p>")
                classic_parts.append(item)
                rows.append([_button(
                    "⚙️ " + method.name[:40], "method:" + method.id.hex,
                    "primary",
                )])
        if page > 0 or has_next:
            # There is no "Page 1" label for a single manual-method page.
            indicator = tr("Страница ", "Page ") + str(page + 1)
            rich_parts.append("<p>" + indicator + "</p>")
            classic_parts.append(indicator)
            pagination = []
            prefix = "methodwizardpage:" if guided else "methodpage:"
            if page:
                pagination.append(_button(
                    tr("◀ Предыдущая", "◀ Previous"),
                    prefix + str(page - 1), "primary",
                ))
            if has_next:
                pagination.append(_button(
                    tr("Следующая ▶", "Next ▶"),
                    prefix + str(page + 1), "primary",
                ))
            rows.append(pagination)
        if methods and not guided:
            bulk = await read_bulk_sales_state(
                factory, actor_id=actor_id, project_id=project_id,
                kind="method",
            )
            if bulk.available:
                rows.append([_button(
                    tr("▶️ Включить ручные оплаты", "▶️ Enable manual payments")
                    if bulk.enable_action else
                    tr("⏸️ Выключить ручные оплаты", "⏸️ Disable manual payments"),
                    "bulks:method:" + ("on:" if bulk.enable_action else "off:")
                    + encode_uuid(project_id),
                    "success" if bulk.enable_action else "danger",
                )])
        rows.extend([
            [
                _button("⭐ Telegram Stars", "starspage:0", "primary"),
                _button("💎 Crypto Pay", "cryptopay", "primary"),
            ],
            [_button(tr("➕ Добавить ручной способ", "➕ Add manual method"),
                     "newmethod", "primary")],
            [_button(tr("⬅️ Назад", "⬅️ Back"),
                     "setupback2" if guided else "dashboard", "primary")],
        ])
        rich_parts.append(RICH_ACTIONS_MARKER)
        await replace_owner_rich(
            bot, chat_id=user_id, message_id=source_message_id,
            rich_html="\n".join(rich_parts),
            classic_html="\n\n".join(classic_parts),
            reply_markup=_markup(rows),
        )


    async def show_stars_payments(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, page: int = 0,
    ) -> None:
        """One native payment method screen. XTR historical prices stay intact."""
        if not 0 <= page <= 10000:
            raise ValueError("Invalid Telegram Stars page")
        overview = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        plans = sorted(
            overview.plans,
            key=lambda item: ((item[1] or "").casefold(), item[0].hex),
        )
        page_size = 8
        current = plans[page * page_size:(page + 1) * page_size]
        lines = [
            "⭐ <b>Telegram Stars</b>",
            tr(
                "Нативная оплата Telegram, без банковских реквизитов.",
                "Native Telegram payments, without bank instructions.",
            ),
            tr(
                "Настройте цену в звёздах для нужных тарифов или отключите "
                "новые покупки. Прошлые платежи и подписки сохраняются.",
                "Set a Stars price for each plan or disable new purchases. "
                "Previous payments and subscriptions are preserved.",
            ),
            "",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        if not plans:
            lines.append(tr(
                "📦 Тарифов пока нет.\n\n"
                "Создайте первый тариф на <b>1 месяц</b> с оплатой ⭐ Stars. "
                "Цену вы укажете сами. После сохранения тариф будет опубликован.",
                "📦 No plans yet.\n\n"
                "Create your first <b>1-month</b> plan with ⭐ Stars. "
                "You set the price; the plan is published after saving.",
            ))
            rows.append([_button(
                tr("✨ Создать тариф на 1 месяц", "✨ Create 1-month plan"),
                "starsnew", "success",
            )])
        elif not current:
            lines.append(tr("На этой странице тарифов нет.",
                            "No plans on this page."))
        for plan_id, plan_name, plan_status in current:
            info = await get_owner_plan_info(
                factory, actor_id=actor_id, project_id=project_id,
                plan_id=plan_id,
            )
            stars_price = next(
                (price for price in info.prices if price.currency == "XTR"),
                None,
            )
            price_label = (
                format_amount(stars_price.amount, stars_price.precision) + " ⭐"
                if stars_price is not None
                else tr("не настроено", "not configured")
            )
            native_active = bool(
                stars_price is not None and stars_price.native_stars_active
            )
            name = plan_name or tr("Без названия", "Unnamed")
            lines.append(
                "• <b>" + escape(name) + "</b> · "
                + escape(price_label) + " · " + _status(plan_status)
                + (" · " + tr("⭐ Баланс канала", "⭐ Channel balance")
                   if native_active else "")
            )
            row = [_button(
                "⚙️ " + name[:30],
                "starsprice:" + plan_id.hex + ":" + str(page),
            )]
            if stars_price is not None:
                row.append(_button(
                    "⏸️", "starsoff:" + plan_id.hex + ":" + str(page),
                    "danger",
                ))
            rows.append(row)
            if stars_price is not None and (
                plan_status == "active" or native_active
            ):
                rows.append([_button(
                    tr("↩️ Вернуть оплату боту", "↩️ Return Stars to bot")
                    if native_active else
                    tr("⭐ На баланс канала", "⭐ To channel balance"),
                    ("starsnativeoff:" if native_active else "starsnative:")
                    + plan_id.hex + ":" + str(page),
                    "success" if not native_active else "danger",
                )])
        nav = []
        if page:
            nav.append(_button("⬅️", "starspage:" + str(page - 1)))
        if (page + 1) * page_size < len(plans):
            nav.append(_button("➡️", "starspage:" + str(page + 1)))
        if nav:
            rows.append(nav)
        rows.append([_button(
            tr("⬅️ Способы оплаты", "⬅️ Payment methods"), "methods",
        )])
        await bot.send_message(
            user_id, "\n".join(lines), parse_mode="HTML",
            reply_markup=_markup(rows),
        )

    async def prompt_first_stars_month(
        bot: Bot, user_id: int, actor_id: UUID, resource_id: UUID,
        *, source_flow: SetupFlow | None = None, retry: bool = False,
    ) -> None:
        warning = (
            tr("⚠️ Цена должна быть целым числом от 1 до 250000.\n\n",
               "⚠️ Enter a whole number between 1 and 250000.\n\n")
            if retry else ""
        )
        await prompt(
            bot, user_id, actor_id,
            step="stars_month_price",
            source_flow=source_flow,
            payload={"resource_id": resource_id.hex},
            text=warning + tr(
                "⭐ <b>Тариф «1 месяц»</b>\n\n"
                "Введите цену подписки в звёздах: целое число от 1 до 250000. "
                "После сохранения тариф автоматически появится в продаже.",
                "⭐ <b>1-month plan</b>\n\n"
                "Enter a whole number of Stars from 1 to 250000. "
                "Saving will publish the plan automatically.",
            ),
        )

    async def show_crypto_pay(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        """One owner-only Crypto Pay setup screen for subscriptions and files."""
        if fixed_project_id != project_id:
            raise PermissionError("Configure merchant payments in the connected bot")
        app = await read_crypto_pay_connection(
            factory, project_id=project_id, bot_id=bot.id, actor_id=actor_id,
        )
        status = (
            tr("🟢 Подключено", "🟢 Connected") + " · "
            + escape(app.app_label) + " · " + escape(app.environment)
            if app is not None else
            tr("⚪ Не подключено", "⚪ Not connected")
        )
        rows = [
            [_button(
                tr("🔑 Подключить Crypto Pay", "🔑 Connect Crypto Pay")
                if app is None else
                tr("🔄 Сменить приложение / токен", "🔄 Replace app / token"),
                "cryptonet", "success",
            )],
        ]
        if app is not None:
            rows.append([_button(
                tr("⏸️ Отключить новые счета", "⏸️ Disable new invoices"),
                "cryptoffask", "danger",
            )])
        rows.append([_button(
            tr("⬅️ Способы оплаты", "⬅️ Payment methods"), "methods",
        )])
        await bot.send_message(
            user_id, tr(
                "💎 <b>Crypto Pay</b>\n",
                "💎 <b>Crypto Pay</b>\n",
            ) + status + tr(
                "\n\nПолучите API-токен в @CryptoBot → Crypto Pay → "
                "Создать приложение. Средства поступают непосредственно "
                "в Crypto Pay вашего аккаунта. Для тестов используйте "
                "@CryptoTestnetBot.\n\n"
                "Подключение не включает оплату у покупателей до запуска "
                "полной автоматической выдачи подписок и файлов.",
                "\n\nCreate an API token in @CryptoBot → Crypto Pay → "
                "Create App. Funds go directly to your own Crypto Pay app. "
                "Use @CryptoTestnetBot for testing.\n\n"
                "Connecting the app will not activate buyer checkout until "
                "verified subscription and file delivery are deployed.",
            ),
            parse_mode="HTML", reply_markup=_markup(rows),
        )

    async def show_method_card(
        bot: Bot, user_id: int, actor_id: UUID,
        project_id: UUID, method_id: UUID,
    ) -> None:
        method = await get_owner_method_info(
            factory, actor_id=actor_id, project_id=project_id,
            method_id=method_id,
        )
        rows = [
            [_button(
                tr("✏️ Изменить название", "✏️ Rename"),
                "methodrename:" + method_id.hex,
            )],
            [_button(
                tr("📝 Изменить реквизиты", "📝 Edit instructions"),
                "methodedit:" + method_id.hex,
            )],
            [_button(
                tr("🌐 Управление валютами", "🌐 Manage currencies"),
                "methodlink:" + method_id.hex,
            )],
        ]
        if method.status == "active":
            rows.append([_button(
                tr("⏸️ Отключить для новых покупок", "⏸️ Disable new sales"),
                "methodconfirmoff:" + method_id.hex, "danger",
            )])
        else:
            rows.append([_button(
                tr("▶️ Включить способ оплаты", "▶️ Enable method"),
                "methodon:" + encode_uuid(method_id) + ":" + method.revision,
                "success",
            )])
        if not method.has_history:
            rows.append([_button(
                tr("🗑 Удалить способ оплаты", "🗑 Delete method"),
                "methoddeleteask:" + method_id.hex, "danger",
            )])
        rows.append([_button(
            tr("⬅️ Все способы оплаты", "⬅️ All payment methods"), "methods",
        )])
        history_note = tr(
            "\nУдаление недоступно: есть история покупок.",
            "\nDeletion unavailable: purchase history exists.",
        ) if method.has_history else ""
        instruction_preview = (
            method.instruction[:350]
            + ("…" if len(method.instruction) > 350 else "")
        )
        await bot.send_message(
            user_id,
            "💳 <b>" + escape(method.name) + "</b>\n"
            + tr("Статус: ", "Status: ") + _status(method.status)
            + tr("\nВалюты: ", "\nCurrencies: ")
            + escape(", ".join(method.currencies) or tr("не выбраны", "none selected"))
            + tr("\n\nИнструкция покупателю:\n", "\n\nBuyer instructions:\n")
            + escape(instruction_preview)
            + history_note,
            parse_mode="HTML",
            reply_markup=_markup(rows),
        )

    async def show_reviews(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
    ) -> None:
        if fixed_project_id is None:
            raise PermissionError("Проверяйте заявки в собственном боте")
        async with system_project_session_scope(factory, project_id) as session:
            items = await list_pending_reviews_for_actor(
                session, project_id=project_id,
                actor_telegram_user_id=user_id, limit=15,
            )
        if not items:
            await bot.send_message(
                user_id,
                tr(
                    "📋 <b>Заявки</b>\n\n"
                    "Сейчас нет платежей, ожидающих проверки.\n"
                    "Новые подтверждения придут сюда вместе с чеком.",
                    "📋 <b>Payment reviews</b>\n\n"
                    "There are no payments awaiting review.\n"
                    "New payment confirmations will appear here with a receipt.",
                ),
                parse_mode="HTML",
                reply_markup=_markup([
                    [_button("⬅️ Панель администратора", "dashboard")],
                ]),
            )
            return
        await bot.send_message(
            user_id,
            tr(
                f"📋 <b>Заявки на проверку — {len(items)}</b>\n\n"
                "Перед подтверждением убедитесь, что деньги "
                "действительно поступили в банк.",
                f"📋 <b>Pending reviews — {len(items)}</b>\n\n"
                "Check your bank balance before approving any payment.",
            ),
            parse_mode="HTML",
        )
        for item in items:
            await send_payment_review(
                bot, chat_id=user_id,
                rich_html=rich_subscription_review(
                    item, receipt_attached=False,
                ),
                legacy_text=_review_text(item),
                reply_markup=_review_keyboard(item.request_id),
            )
        await bot.send_message(
            user_id,
            tr("Все доступные заявки показаны выше.", "All available requests are shown above."),
            reply_markup=_markup([
                [_button("🔄 Обновить заявки", "reviews")],
                [_button("⬅️ Панель администратора", "dashboard")],
            ]),
        )


    async def show_guest_preview(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID,
        *, currency: str | None = None, page: int = 0,
        price_id: UUID | None = None, method_id: UUID | None = None,
        instruction_page: int = 0, message_id: int | None = None,
    ) -> None:
        """Interactive buyer-like display with no checkout/payment writes."""
        if fixed_project_id is None or project_id != fixed_project_id:
            raise PermissionError("Нет прав на управление этим ботом")
        if not await can_manage_project(
            factory, actor_id=actor_id, project_id=project_id,
        ):
            raise PermissionError("Нет прав на управление этим ботом")

        async with system_project_session_scope(factory, project_id) as session:
            # Pass the connected bot identity so this read-only preview has
            # the same enabled Crypto Pay choices as real buyer checkout.
            offers = await list_buyer_offers(
                session, project_id=project_id, bot_id=bot.id,
            )
            if method_id is not None and price_id is not None:
                # Method must still be checkout-eligible for this exact price.
                eligible = any(
                    offer.option.plan_price_id == price_id
                    and any(m.payment_method_id == method_id for m in offer.methods)
                    for offer in offers
                )
                if not eligible:
                    raise ValueError("Способ оплаты недоступен в этом проекте")
                if method_id.int == 1:
                    # Synthetic Crypto Pay checkout method is not a bank
                    # PaymentMethod row. Never open a real provider invoice
                    # from owner preview, even for a stale callback.
                    rendered = preview_crypto_method_screen(
                        offers, price_id=price_id, page=page,
                    )
                else:
                    method = await session.scalar(
                        select(PaymentMethod).where(
                            PaymentMethod.id == method_id,
                            PaymentMethod.project_id == project_id,
                            PaymentMethod.status == "active",
                        )
                    )
                    if method is None:
                        raise ValueError("Способ оплаты недоступен в этом проекте")
                    rendered = preview_method_screen(
                        offers, price_id=price_id, payment_method_id=method_id,
                        instruction_template=method.instruction_template,
                        page=page, instruction_page=instruction_page,
                    )
            elif price_id is not None:
                rendered = preview_offer_screen(
                    offers, price_id=price_id, page=page,
                )
            else:
                rendered = preview_catalog_screen(
                    offers, currency=currency, page=page,
                )
        if rendered is None:
            raise ValueError("Тариф не найден в этом проекте")
        content, keyboard = rendered
        if message_id is not None:
            try:
                await bot.edit_message_text(
                    content, chat_id=user_id, message_id=message_id,
                    parse_mode="HTML", reply_markup=keyboard,
                )
                return
            except AiogramError as exc:
                if "message is not modified" in str(exc).lower():
                    return
                # Older/deleted messages fall back to a new safe preview.
        await bot.send_message(
            user_id, content, parse_mode="HTML", reply_markup=keyboard,
        )


    async def show_help(bot: Bot, user_id: int) -> None:
        rows = (
            [
                [_button("📢 Каналы", "resources")],
                [_button("📦 Тарифы", "plans")],
                [_button("💳 Оплата", "methods")],
                [_button(tr("⬅️ Настройки", "⬅️ Settings"), "settings")],
            ]
            if fixed_project_id is not None
            else [
                [_button("🤖 Управление ботом", "bots")],
                [_button("⬅️ Мои подключения", "projects")],
            ]
        )
        await bot.send_message(
            user_id,
            tr(
                "❔ <b>Как запустить подписки</b>\n\n"
                "1. Подключите токен в @vimipay_bot.\n"
                "2. Откройте собственный бот и добавьте его в канал.\n"
                "3. Настройте валюты и способы оплаты.\n"
                "4. Создайте тариф, цену и опубликуйте.\n\n"
                "Ваши чеки ViMi Pay не хранит — поступления "
                "проверяются администратором в банке.",
                "❔ <b>How to launch subscriptions</b>\n\n"
                "1. Connect a bot token in @vimipay_bot.\n"
                "2. Open your bot and add it to your channel.\n"
                "3. Set up currencies and payment methods.\n"
                "4. Create, price and publish a plan.\n\n"
                "ViMi Pay does not retain receipts. "
                "Channel admins verify transfers in their banks.",
            ),
            parse_mode="HTML", reply_markup=_markup(rows),
        )


    async def show_duration_picker(
        bot: Bot, user_id: int, generation: str,
    ) -> None:
        """Provide a safe Back action tied to this exact wizard generation."""
        rows = [
            [_button(
                tr(label, format_period(value, unit)),
                "duration:" + key + ":" + generation,
            )]
            for key, (value, unit, label) in DURATIONS.items()
        ]
        rows.append([_button(
            tr("✏️ Добавить название (необязательно)",
               "✏️ Add a name (optional)"),
            "planname:" + generation,
        )])
        rows.append([_button(
            tr("⬅️ Все тарифы", "⬅️ All plans"),
            "flowback:plans:" + generation,
        )])
        await bot.send_message(
            user_id, tr(
                "🗓️ Выберите срок подписки. Название необязательно: "
                "можно сразу выбрать срок и перейти к цене.",
                "🗓️ Choose a subscription duration. Naming is optional: "
                "select a duration to continue to pricing.",
            ),
            reply_markup=_markup(rows),
        )

    async def currency_picker(
        bot: Bot, user_id: int, actor_id: UUID, *,
        project_id: UUID, mode: str, plan_id: UUID | None = None,
    ):
        codes = await owner_available_currencies(
            factory, actor_id=actor_id, project_id=project_id,
            fiat_only=True,
        )
        rows = [
            [_button(
                display_currency(code), f"methodcurrency:{code}" if mode == "method"
                else f"price:{plan_id.hex}:{code}"
            )]
            for code in codes
        ]
        rows.append([_button(
            tr("➕ Другая валюта", "➕ Another currency"),
            "currencyadd:" + mode
            + (":" + plan_id.hex if plan_id is not None else ""),
        )])
        rows.append([_button(
            tr("⬅️ К способам оплаты", "⬅️ Payment methods")
            if mode == "method" else tr("⬅️ К тарифу", "⬅️ Back to plan"),
            "methods" if mode == "method" else "plan:" + plan_id.hex,
        )])
        await bot.send_message(
            user_id, tr("💱 Выберите валюту:", "💱 Choose a currency:"),
            reply_markup=_markup(rows),
        )

    async def send_to_customer(bot: Bot, target_id: int, text: str) -> None:
        """Use the *buyer's* language for a new one-to-one support message."""
        async with recipient_language(factory, target_id):
            await bot.send_message(
                target_id,
                tr(ADMIN_MESSAGE_HEADER, ADMIN_MESSAGE_HEADER_EN)
                + "\n\n" + text
                + "\n\n" + tr(
                    "↩️ Ответьте на это сообщение или нажмите кнопку.",
                    "↩️ Reply to this message or tap the button.",
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text=tr("↩️ Ответить", "↩️ Reply"),
                        callback_data="chat:reply",
                    ),
                ]]),
            )

    @router.callback_query(F.data == "chat:reply")
    async def buyer_reply_button(query: CallbackQuery, bot: Bot) -> None:
        if (
            fixed_project_id is None or query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer()
            return
        info = await find_buyer_conversation(
            factory, project_id=fixed_project_id,
            telegram_id=query.from_user.id,
        )
        if info is None or info.is_banned:
            await query.answer(tr(
                "Обращение недоступно.", "Messaging is unavailable.",
            ), show_alert=True)
            return
        pending_buyer_replies[query.from_user.id] = monotonic() + 180
        await query.answer()
        await bot.send_message(
            query.from_user.id,
            tr(BUYER_REPLY_PROMPT, BUYER_REPLY_PROMPT_EN)
            + "\n\n"
            + tr(
                "Напишите ответ следующим сообщением (до 3500 символов). "
                "После отправки обычное поле ввода восстановится. "
                "Режим действует 3 минуты.",
                "Send your reply as the next message (up to 3500 characters). "
                "The normal input field stays available. "
                "This mode expires in 3 minutes.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=tr("✖️ Отмена", "✖️ Cancel"),
                    callback_data="chat:cancel",
                ),
            ]]),
        )

    @router.callback_query(F.data == "chat:cancel")
    async def buyer_cancel_reply(query: CallbackQuery, bot: Bot) -> None:
        if (
            fixed_project_id is None or query.message is None
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer()
            return
        pending_buyer_replies.pop(query.from_user.id, None)
        await query.answer(tr("Отменено", "Canceled"))
        await bot.send_message(
            query.from_user.id,
            tr("✖️ Ответ отменён.", "✖️ Reply canceled."),
        )

    @router.message(BuyerConversationReplyFilter(pending_buyer_replies))
    async def buyer_conversation_reply(message: Message, bot: Bot) -> None:
        if fixed_project_id is None:
            return
        # Consume the one-shot window. The next ordinary message will not
        # be intercepted after this reply, including checkout interactions.
        pending_buyer_replies.pop(message.from_user.id, None)
        info = await find_buyer_conversation(
            factory, project_id=fixed_project_id,
            telegram_id=message.from_user.id,
        )
        if info is None or info.is_banned:
            await bot.send_message(message.chat.id, tr(
                "🚫 Отправка сообщений недоступна.",
                "🚫 Messaging is unavailable.",
            ))
            return
        body = (message.text or "").strip()
        if not 1 <= len(body) <= MAX_DIALOG_TEXT:
            await bot.send_message(message.chat.id, tr(
                "Сообщение должно содержать от 1 до 3500 символов.",
                "Message must contain 1–3500 characters.",
            ))
            return
        identity = ("@" + info.username) if info.username else (
            info.first_name or str(info.customer_telegram_id)
        )
        try:
            # Owner's language can differ from buyer's language.
            async with recipient_language(factory, info.owner_telegram_id):
                title = tr(BUYER_MESSAGE_HEADER, BUYER_MESSAGE_HEADER_EN)
                notify = (
                    title + customer_ref(info) + "\n👤 " + identity[:70]
                    + " · ID " + str(info.customer_telegram_id)
                    + "\n\n" + body
                )
                await bot.send_message(
                    info.owner_telegram_id, notify,
                    reply_markup=_markup([[
                        _button(
                            tr("✉️ Ответить", "✉️ Reply"),
                            "submsg:" + encode_uuid(info.customer_id),
                        ),
                        _button(
                            tr("👤 Карточка", "👤 Customer card"),
                            "subscriber:" + encode_uuid(info.customer_id),
                        ),
                    ]]),
                )
        except AiogramError:
            await bot.send_message(message.chat.id, tr(
                "⚠️ Не удалось передать сообщение администратору.",
                "⚠️ The message could not be delivered to the administrator.",
            ))
            return
        # Legacy prompts forced a native quote UI. Removing the bot's
        # old prompt may clear it in Telegram clients, but is best-effort.
        parent = message.reply_to_message
        if (
            parent is not None and parent.from_user
            and parent.from_user.id == bot.id
            and parent.text
            and parent.text.split("\n", 1)[0] in (
                BUYER_REPLY_PROMPT, BUYER_REPLY_PROMPT_EN,
            )
            and getattr(parent, "message_id", None) is not None
        ):
            try:
                await bot.delete_message(message.chat.id, parent.message_id)
            except AiogramError:
                pass
        # Support inbox is best-effort; owner delivery above is authoritative.
        try:
            await notify_support_of_buyer_reply(
                factory, bot=bot, project_id=fixed_project_id,
                info=info, body=body,
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Support notification failed; owner message was delivered",
            )
        await bot.send_message(message.chat.id, tr(
            "✅ Ответ отправлен администратору.",
            "✅ Your reply has been sent to the administrator.",
        ))

    @router.message(OwnerConversationReplyFilter())
    async def owner_conversation_reply(message: Message, bot: Bot) -> None:
        if fixed_project_id is None:
            return
        telegram_id = parse_buyer_notification(message.reply_to_message.text)
        actor_id = await actor(message.from_user)
        if not await can_manage_project(
            factory, actor_id=actor_id, project_id=fixed_project_id,
        ):
            await bot.send_message(message.chat.id, tr(
                "Нет прав для отправки сообщений покупателям.",
                "You cannot message customers.",
            ))
            return
        try:
            customer_id = await customer_id_by_telegram_id(
                factory, actor_id=actor_id, project_id=fixed_project_id,
                telegram_id=telegram_id,
            )
            detail = await subscriber_detail(
                factory, actor_id=actor_id, project_id=fixed_project_id,
                customer_id=customer_id,
            )
            if not detail.owner_only_actions:
                raise PermissionError("Только владелец может отвечать покупателю")
            body = (message.text or "").strip()
            if not 1 <= len(body) <= MAX_DIALOG_TEXT:
                raise ValueError("Сообщение должно содержать от 1 до 3500 символов")
            await send_to_customer(bot, detail.telegram_id, body)
        except (ValueError, PermissionError) as exc:
            await bot.send_message(message.chat.id, "⚠️ " + str(exc))
            return
        except AiogramError:
            await bot.send_message(message.chat.id, tr(
                "⚠️ Не удалось доставить ответ покупателю.",
                "⚠️ The reply could not be delivered to the customer.",
            ))
            return
        await bot.send_message(message.chat.id, tr(
            "✅ Ответ отправлен покупателю.",
            "✅ Reply sent to customer.",
        ))

    @router.message(
        F.text.regexp(r"^/(?:u[0-9a-z]{1,13}|c[a-z2-7]{26})$")
    )
    async def customer_shortcut_handler(message: Message, bot: Bot) -> None:
        """One tap on the command beside a buyer name opens their real card."""
        if (
            not _private(message)
            or message.chat.id != message.from_user.id
        ):
            return
        if fixed_project_id is None:
            await bot.send_message(message.chat.id, tr(
                "Откройте команду в подключённом боте.",
                "Open the command in your connected bot.",
            ))
            return
        actor_id = await actor(message.from_user)
        if not await can_manage_project(
            factory, actor_id=actor_id, project_id=fixed_project_id,
        ):
            await bot.send_message(message.chat.id, tr(
                "Нет доступа к карточкам покупателей.",
                "You cannot view customer cards.",
            ))
            return
        try:
            telegram_id, customer_id = parse_customer_command(message.text)
            if customer_id is None:
                customer_id = await customer_id_by_telegram_id(
                    factory, actor_id=actor_id, project_id=fixed_project_id,
                    telegram_id=telegram_id,
                )
            await show_subscriber_card(
                bot, message.chat.id, actor_id, fixed_project_id, customer_id,
            )
        except (ValueError, PermissionError):
            await bot.send_message(message.chat.id, tr(
                "Покупатель не найден в этом боте.",
                "Customer not found in this bot.",
            ))

    @router.message(
        Command("start", "menu", "bots", "plans", "help", "support"),
        OwnerHomeFilter(factory, fixed_project_id),
    )
    async def home_message(message: Message, bot: Bot) -> None:
        if not _private(message):
            return
        actor_id = await actor(message.from_user)
        if fixed_project_id is not None:
            await cancel_draft(
                factory, project_id=fixed_project_id, actor_id=actor_id,
            )
        # /menu exits the old text-entry wizard; the next casual message
        # must never be mistaken for old payment instructions or a plan title.
        was_native = await cancel_owner_flow(
            factory, actor_id=actor_id,
            bot_scope_id=bot.id if fixed_project_id else 0,
            report_native_keyboard=True,
        )
        if was_native is True:
            await remove_native_owner_keyboard(bot, message.chat.id)
        command = (
            (getattr(message, "text", None) or "/start")
            .split(maxsplit=1)[0].split("@")[0].lower()
        )
        role = (
            "owner" if fixed_project_id is not None
            else "platform" if message.from_user.id == get_settings().platform_owner_telegram_id
            else "central"
        )
        await set_chat_commands(
            bot, chat_id=message.from_user.id, role=role,
            language=current_language(),
        )
        if command in ("/help", "/support"):
            await show_help(bot, message.chat.id)
        elif command == "/plans" and fixed_project_id is not None:
            await show_plans(bot, message.chat.id, actor_id, fixed_project_id)
        elif command == "/bots":
            if fixed_project_id is None:
                await show_connections(bot, message.chat.id, actor_id)
            else:
                await show_home(bot, message.chat.id, actor_id)
        else:
            await show_home(bot, message.chat.id, actor_id)

    @router.message(
        F.text.in_({"⬅️ Назад", "⬅️ Back"}),
        OwnerHomeFilter(factory, fixed_project_id),
    )
    async def back_native_chat_picker(message: Message, bot: Bot) -> None:
        """Back from Telegram's native chat picker, never cancel newer forms."""
        if not _private(message) or fixed_project_id is None:
            return
        actor_id = await actor(message.from_user)
        try:
            previous = await consume_button_flow(
                factory, actor_id=actor_id, expected_step="chat_picker",
                bot_scope_id=bot.id, discard=True,
                allow_expired_chat_picker=True,
            )
        except ValueError:
            await bot.send_message(message.chat.id, tr(
                "⚠️ Выбор чата уже закрыт. Откройте подключение заново.",
                "⚠️ Chat selection has expired. Reopen the connection screen.",
            ))
            return
        if previous.project_id != fixed_project_id:
            raise PermissionError(tr(
                "Настройка другого бота недоступна",
                "You cannot manage another bot's setup",
            ))
        # Back disposes the matched picker before returning to channels.
        await delete_native_chat_picker_card(bot, message.chat.id, previous)
        # Telegram reply keyboards do not disappear reliably on one_time.
        # Hide only after the correct active picker was atomically dismissed.
        await bot.send_message(
            message.chat.id,
            tr("↩️ Возвращаемся к подключению.",
               "↩️ Back to connections."),
            reply_markup=ReplyKeyboardRemove(),
        )
        await show_resources(
            bot, message.chat.id, actor_id, fixed_project_id,
        )

    @router.message(Command("cancel"), OwnerHomeFilter(factory, fixed_project_id))
    @router.message(
        F.text.in_({"✖️ Отмена", "✖️ Cancel"}),
        OwnerHomeFilter(factory, fixed_project_id),
    )
    async def cancel_setup_message(message: Message, bot: Bot) -> None:
        if not _private(message):
            return
        actor_id = await actor(message.from_user)
        if fixed_project_id is not None:
            await cancel_draft(
                factory, project_id=fixed_project_id, actor_id=actor_id,
            )
        await cancel_owner_flow(
            factory, actor_id=actor_id,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )
        await bot.send_message(
            message.chat.id,
            tr("✖️ Настройка отменена.", "✖️ Setup canceled."),
            reply_markup=ReplyKeyboardRemove(),
        )
        await show_home(bot, message.chat.id, actor_id)

    async def navigate_owner_prompt_back(
        bot: Bot, user_id: int, actor_id: UUID,
        *, expected_generation: str | None = None,
    ) -> bool:
        """Back resolves the current form parent and refuses stale buttons."""
        prior = await back_owner_flow(
            factory, actor_id=actor_id,
            bot_scope_id=bot.id if fixed_project_id else 0,
            expected_generation=expected_generation,
        )
        if expected_generation is not None and prior is None:
            # An old button must not cancel a newer input or session.
            return False
        if fixed_project_id is not None:
            await cancel_draft(
                factory, project_id=fixed_project_id, actor_id=actor_id,
            )
        if prior is not None and prior.step == "new_bot_token":
            # A clean user came via Paid subscriptions; an existing owner
            # came from My connections. Return to the actual parent.
            home = await owner_home(factory, actor_id=actor_id)
            if home.projects or home.archived_count:
                await show_connections(bot, user_id, actor_id)
            else:
                await show_subscription_start(bot, user_id)
            return True
        if (
            prior is None or prior.project_id is None
            or (fixed_project_id is not None and prior.project_id != fixed_project_id)
        ):
            # Also clear a stale chat-picker keyboard after the 15-minute TTL.
            # The expired flow no longer has a reliable step to inspect.
            await bot.send_message(
                user_id,
                tr("↩️ Возврат в главное меню.", "↩️ Back to the main menu."),
                reply_markup=ReplyKeyboardRemove(),
            )
            await show_home(bot, user_id, actor_id)
            return True
        project_id = prior.project_id
        if prior.step == "chat_picker":
            await delete_native_chat_picker_card(bot, user_id, prior)
            await bot.send_message(
                user_id,
                tr("Выбор чата закрыт.", "Chat selection closed."),
                reply_markup=ReplyKeyboardRemove(),
            )
        if prior.step in ("plan_name", "plan_duration", "plan_resource"):
            await show_plans(bot, user_id, actor_id, project_id)
        elif prior.step == "currency_custom_code":
            mode = prior.payload.get("mode")
            target = prior.payload.get("target")
            if mode == "price" and target:
                await show_plan_card(
                    bot, user_id, actor_id, project_id,
                    UUID(hex=target),
                )
            elif mode == "attach" and target:
                await show_method_card(
                    bot, user_id, actor_id, project_id,
                    UUID(hex=target),
                )
            else:
                await show_methods(bot, user_id, actor_id, project_id)
        elif prior.step in ("price_amount", "plan_rename"):
            try:
                plan_id = UUID(hex=prior.payload["plan_id"])
            except (KeyError, TypeError, ValueError, AttributeError):
                await show_plans(bot, user_id, actor_id, project_id)
            else:
                if prior.payload.get("currency") == "XTR":
                    await show_stars_payments(
                        bot, user_id, actor_id, project_id,
                        page=int(prior.payload.get("stars_page", 0)),
                    )
                else:
                    await show_plan_card(
                        bot, user_id, actor_id, project_id, plan_id,
                    )
        elif prior.step in ("method_rename", "method_edit"):
            await show_method_card(
                bot, user_id, actor_id, project_id,
                UUID(hex=prior.payload["method_id"]),
            )
        elif prior.step in ("method_name", "method_instruction"):
            await show_methods(bot, user_id, actor_id, project_id)
        elif prior.step == "crypto_pay_token":
            await show_crypto_pay(bot, user_id, actor_id, project_id)
        elif prior.step in ("chat_picker", "resource_reference"):
            await show_resources(bot, user_id, actor_id, project_id)
        elif prior.step in ("bot_token", "connection_rename") and fixed_project_id is None:
            await show_bots(bot, user_id, actor_id, project_id)
        else:
            await show_dashboard(bot, user_id, actor_id, project_id)
        return True

    @router.message(Command("back"), OwnerHomeFilter(factory, fixed_project_id))
    async def back_setup_message(message: Message, bot: Bot) -> None:
        if not _private(message):
            return
        await navigate_owner_prompt_back(
            bot, message.chat.id, await actor(message.from_user),
        )

    @router.message(Command("billing"))
    async def owner_platform_billing_command(message: Message, bot: Bot) -> None:
        if not _private(message) or message.from_user is None:
            return
        actor_id = await actor(message.from_user)
        try:
            await show_saas_statements(
                bot, message.from_user.id, actor_id,
                fixed_project_id,
            )
        except PermissionError:
            await bot.send_message(
                message.from_user.id, tr(
                    "Управление счетами доступно только владельцу.",
                    "Only the owner can view platform invoices.",
                ),
            )

    @router.callback_query(F.data.startswith("own:"))
    async def owner_callback(query: CallbackQuery, bot: Bot) -> None:
        if query.message is None or query.message.chat.type != "private":
            await query.answer(tr("Откройте личный чат с ViMi Pay",
                              "Open your private chat with ViMi Pay"), show_alert=True)
            return
        user_id = query.from_user.id
        actor_id = await actor(query.from_user)
        action = (query.data or "")[4:]
        if fixed_project_id is not None and not await can_manage_project(
            factory, actor_id=actor_id, project_id=fixed_project_id,
        ):
            await query.answer(tr("Нет доступа к управлению ботом",
                              "You cannot manage this bot"), show_alert=True)
            return
        if fixed_project_id is None:
            is_registry_action = action in {
                "new", "newmanaged", "managedlaunch", "substart", "onboardhome",
                "onboardlanguage",
                "projects", "archives", "dashboard",
                "bots", "connect", "help", "oblist", "cancel",
            } or action.startswith((
                "oblist:", "promptback:", "promptcancel:",
                "select:", "drop:", "dropok:", "ob:", "obok:", "obpage:",
                "orphanask:", "orphanok:",
                "projects:", "archives:", "archview:", "restore:",
                "managedsync:", "managedconfirm:", "managedcancel:",
                "connrename:", "connremove:", "connremoveok:",
                "connarchive:", "connarchiveok:", "saasbotbill:",
                "saasnearest:", "saasinvoices:", "saasaccountlist:", "saaspaychoices:",
                "saasprofile", "saasprice:", "saasbill:", "saasbillchoose:",
                "saasbillquote:", "sl:",
                "saasv2pay:", "saasv2amount:", "saasv2create:",
                "spm:", "spr:", "spb:", "spv:", "spc:",
            ))
            if not is_registry_action:
                await query.answer(
                    tr("Настройте продажи в своём подключённом боте",
                       "Set up sales inside your connected bot"),
                    show_alert=True,
                )
                return
        else:
            if action in {
                "new", "newmanaged", "managedlaunch", "substart", "onboardhome",
                "onboardlanguage",
                "projects", "archives", "bots", "connect", "oblist",
            } or action.startswith((
                "oblist:", "select:", "drop:", "dropok:", "ob:", "obok:", "obpage:",
                "orphanask:", "orphanok:",
                "projects:", "archives:", "archview:", "restore:",
                "managedsync:", "managedconfirm:", "managedcancel:",
                "connrename:", "connremove:", "connremoveok:",
                "connarchive:", "connarchiveok:",
            )):

                await query.answer(
                    tr("Управляйте подключением через @vimipay_bot",
                       "Manage bot connections in @vimipay_bot"),
                    show_alert=True,
                )
                return
        try:
            if action.startswith(("promptback:", "promptcancel:")):
                generation = action.split(":", 1)[1]
                if not generation or len(generation) > 64:
                    await query.answer(
                        tr("Кнопка устарела", "Button expired"), show_alert=True,
                    )
                    return
                if action.startswith("promptback:"):
                    moved = await navigate_owner_prompt_back(
                        bot, user_id, actor_id,
                        expected_generation=generation,
                    )
                else:
                    moved = await cancel_owner_flow(
                        factory, actor_id=actor_id,
                        bot_scope_id=bot.id if fixed_project_id else 0,
                        expected_generation=generation,
                    )
                    if moved:
                        if fixed_project_id is not None:
                            await cancel_draft(
                                factory, project_id=fixed_project_id,
                                actor_id=actor_id,
                            )
                        await show_home(bot, user_id, actor_id)
                if not moved:
                    await query.answer(
                        tr("Эта форма уже закрыта", "This form has expired"),
                        show_alert=True,
                    )
                else:
                    # Lava's outer middleware deliberately deferred cleanup
                    # for this generation-guarded action. Never clear a newer
                    # email request when a stale Back/Menu card is tapped.
                    if fixed_project_id is None:
                        lava_ui.clear_pending_lava_after_owner_navigation(
                            payer_id=user_id, central_bot_id=bot.id,
                        )
                    await query.answer()
                return
            if action == "onboardlanguage":
                await show_onboarding_language(bot, user_id)
                await query.answer()
                return
            if action == "onboardhome":
                # Back must work before the first bot/project exists.
                await show_home(bot, user_id, actor_id)
                await query.answer()
                return
            if action == "substart":
                await show_subscription_start(bot, user_id)
                await query.answer()
                return
            if action == "newmanaged":
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError(tr(
                        "Подключение ботов пока недоступно",
                        "Bot connections are unavailable",
                    ))
                identity = await bot.get_me()
                if not identity.can_manage_bots:
                    await bot.send_message(
                        user_id,
                        tr(
                            "⚠️ Включите <b>Bot Management Mode</b> в "
                            "настройках @vimipay_bot через приложение @BotFather. "
                            "После включения повторите действие. "
                            "Ручное подключение через BotFather остаётся доступным.",
                            "⚠️ Enable <b>Bot Management Mode</b> for "
                            "@vimipay_bot in the @BotFather Mini App. "
                            "Then try again. Manual token connection still works.",
                        ),
                        parse_mode="HTML",
                    )
                    await query.answer()
                    return
                # Telegram's request_managed_bot exists ONLY on native reply
                # keyboards. First explain the action in a clean Rich card.
                # Do not show the system keyboard or create a flow yet.
                await send_owner_rich(
                    bot, chat_id=user_id,
                    rich_html=(
                        "<h3>" + tr(
                            "✨ Создать бота через Telegram",
                            "✨ Create a bot with Telegram",
                        ) + "</h3><p>" + tr(
                            "Нажмите «Продолжить создание». На следующем шаге "
                            "появится временная системная кнопка Telegram. "
                            "Нажмите её, выберите имя и @username нового бота "
                            "и подтвердите создание.",
                            "Tap Continue creation. On the next step, a "
                            "temporary Telegram system button will appear. "
                            "Tap it, choose the new bot's name and @username, "
                            "then confirm creation.",
                        ) + "</p><p>" + tr(
                            "ViMi Pay получит токен автоматически, без отправки "
                            "его в чат. @username должен заканчиваться на bot. "
                            "После подтверждения подключения бот появится в списке.",
                            "ViMi Pay receives the token automatically without "
                            "sending it in chat. The @username must end in bot. "
                            "Once connected, the bot will appear in your list.",
                        ) + "</p><p>" + tr(
                            "⏳ На завершение создания даётся 15 минут.",
                            "⏳ You have 15 minutes to complete creation.",
                        ) + "</p>"
                    ),
                    classic_html=tr(
                        "✨ <b>Создать бота через Telegram</b>\n\n"
                        "Нажмите «Продолжить создание». Появится временная "
                        "системная кнопка Telegram. Нажмите её, выберите "
                        "имя и @username нового бота и подтвердите создание.\n\n"
                        "ViMi Pay получит токен автоматически, без отправки "
                        "его в чат. @username должен заканчиваться на bot. "
                        "После подтверждения подключения бот появится в списке.\n\n"
                        "⏳ На создание даётся 15 минут.",
                        "✨ <b>Create a bot with Telegram</b>\n\n"
                        "Tap Continue creation. A temporary Telegram system "
                        "button will appear. Tap it, choose a name and "
                        "@username, then confirm creation.\n\n"
                        "ViMi Pay receives the token without sending it in "
                        "chat. The @username must end in bot. Once connected, "
                        "the bot will appear in your list.\n\n"
                        "⏳ You have 15 minutes to finish.",
                    ),
                    reply_markup=_markup([
                        [_button(
                            tr("✨ Продолжить создание", "✨ Continue creation"),
                            "managedlaunch", "success",
                        )],
                        [_button(tr("⬅️ Назад", "⬅️ Back"), "substart", "primary")],
                    ]),
                )
                await query.answer()
                return
            if action == "managedlaunch":
                # The native Telegram creation dialog cannot be launched from
                # InlineKeyboardButton or a Rich action. Request it only after
                # the owner explicitly continues from the explanation card.
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError(tr(
                        "Подключение ботов пока недоступно",
                        "Bot connections are unavailable",
                    ))
                identity = await bot.get_me()
                if not identity.can_manage_bots:
                    await bot.send_message(
                        user_id, tr(
                            "⚠️ Режим управления ботами отключён. "
                            "Вернитесь к выбору способа подключения.",
                            "⚠️ Bot Management Mode is disabled. "
                            "Return to the connection method selector.",
                        ),
                    )
                    await query.answer()
                    return
                request_id = randbelow(2_000_000_000) + 1
                generation = await set_flow(
                    factory, actor_id=actor_id, step="managed_bot_create",
                    payload={"request_id": request_id}, bot_scope_id=0,
                )
                # The Telegram-managed bot button must be a native reply
                # button. Its cancellation lives on a separate Rich card.
                await send_owner_rich(
                    bot, chat_id=user_id,
                    rich_html=tr(
                        "<h3>✨ Создание Telegram-бота</h3>"
                        "<p><b>🤖 СОЗДАЙТЕ СВОЕГО БОТА ЗА ПАРУ ШАГОВ</b></p>"
                        "<p>👉 Нажмите <b>«✨ Создать нового бота»</b> внизу чата.<br>"
                        "👉 Укажите <b>ИМЯ</b> и <b>@username</b> бота "
                        "(он должен заканчиваться на bot).<br>"
                        "👉 <b>ПОДТВЕРДИТЕ СОЗДАНИЕ</b> в Telegram. "
                        "ViMi Pay подключит бота автоматически.</p>"
                        "<p><b>🔐 БЕЗ ПЕРЕДАЧИ ТОКЕНА</b><br>"
                        "Копировать и отправлять API-токен в чат не нужно.</p>"
                        "<p>⏳ На подтверждение даётся <b>15 минут</b>.</p>",
                        "<h3>✨ Create your Telegram bot</h3>"
                        "<p><b>🤖 CREATE YOUR BOT IN A FEW STEPS</b></p>"
                        "<p>👉 Tap <b>“✨ Create new bot”</b> below the chat.<br>"
                        "👉 Enter a <b>NAME</b> and <b>@username</b> "
                        "(it must end in bot).<br>"
                        "👉 <b>CONFIRM CREATION</b> in Telegram. "
                        "ViMi Pay will connect the bot automatically.</p>"
                        "<p><b>🔐 NO TOKEN SHARING</b><br>"
                        "You don't need to copy or send an API token.</p>"
                        "<p>⏳ You have <b>15 minutes</b> to confirm.</p>",
                    ),
                    classic_html=tr(
                        "✨ <b>Создание Telegram-бота</b>\n\n"
                        "<b>🤖 СОЗДАЙТЕ СВОЕГО БОТА ЗА ПАРУ ШАГОВ</b>\n\n"
                        "👉 Нажмите <b>«✨ Создать нового бота»</b> внизу.\n"
                        "👉 Укажите <b>ИМЯ</b> и <b>@username</b> (оканчивается на bot).\n"
                        "👉 <b>ПОДТВЕРДИТЕ СОЗДАНИЕ</b> в Telegram.\n\n"
                        "<b>🔐 БЕЗ ПЕРЕДАЧИ ТОКЕНА</b>: "
                        "ViMi Pay получит его автоматически.\n"
                        "⏳ Подтвердите в течение <b>15 минут</b>.",
                        "✨ <b>Create your Telegram bot</b>\n\n"
                        "<b>🤖 CREATE YOUR BOT IN A FEW STEPS</b>\n\n"
                        "👉 Tap <b>“✨ Create new bot”</b> below.\n"
                        "👉 Enter a <b>NAME</b> and <b>@username</b> (ends in bot).\n"
                        "👉 <b>CONFIRM CREATION</b> in Telegram.\n\n"
                        "<b>🔐 NO TOKEN SHARING</b>: "
                        "ViMi Pay receives the token automatically.\n"
                        "⏳ Confirm within <b>15 minutes</b>.",
                    ),
                    reply_markup=_markup([[_button(
                        tr("✖️ Отменить создание", "✖️ Cancel creation"),
                        "managedcancel:" + generation, "primary",
                    )]]),
                )
                # Native request_managed_bot cannot coexist with inline markup.
                await bot.send_message(
                    user_id,
                    tr(
                        "👇 <b>КНОПКА СОЗДАНИЯ</b> находится внизу чата.",
                        "👇 The <b>CREATE BUTTON</b> is below the chat.",
                    ),
                    parse_mode="HTML",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[[
                            KeyboardButton(
                                text=tr("✨ Создать нового бота", "✨ Create new bot"),
                                request_managed_bot=KeyboardButtonRequestManagedBot(
                                    request_id=request_id,
                                    suggested_name="ViMi Pay Channel",
                                ),
                            ),
                        ]],
                        resize_keyboard=True, one_time_keyboard=True,
                    ),
                )
                await query.answer()
                return
            if action.startswith("managedcancel:"):
                generation = action.split(":", 1)[1]
                if not generation or len(generation) > 64:
                    await query.answer(
                        tr("Запрос устарел", "Request expired"), show_alert=True,
                    )
                    return
                try:
                    await consume_button_flow(
                        factory, actor_id=actor_id,
                        expected_step="managed_bot_create",
                        generation=generation, bot_scope_id=0, discard=True,
                    )
                except ValueError:
                    await query.answer(
                        tr("Запрос уже закрыт или устарел",
                           "Request already closed or expired"),
                        show_alert=True,
                    )
                    return
                # The outer Lava middleware defers this guarded action.
                # Only a successfully cancelled current request may clear
                # the user's pending Lava email input.
                lava_ui.clear_pending_lava_after_owner_navigation(
                    payer_id=user_id, central_bot_id=bot.id,
                )
                await bot.send_message(
                    user_id, tr(
                        "✖️ Создание бота отменено.",
                        "✖️ Bot creation cancelled.",
                    ),
                    reply_markup=ReplyKeyboardRemove(),
                )
                await show_subscription_start(bot, user_id)
                await query.answer()
                return
            if action.startswith("managedconfirm:"):
                # Telegram callback acknowledgements expire quickly, while
                # Telegram token lookup and atomic DB onboarding can take longer.
                # Acknowledge the click BEFORE any external work; send a normal
                # private message for every eventual outcome.
                await query.answer(tr(
                    "⏳ Проверяю подключение бота…",
                    "⏳ Checking the bot connection…",
                ))
                try:
                    if get_settings().bot_token_encryption_key is None:
                        raise ValueError(tr(
                            "Подключение ботов пока недоступно",
                            "Bot connections are unavailable",
                        ))
                    _, id_text, generation = action.split(":", 2)
                    if not id_text.isascii() or not id_text.isdecimal():
                        raise ValueError(tr("Некорректный бот", "Invalid bot"))
                    result = await connect_managed_bot_event(
                        factory, central_bot=bot,
                        creator_telegram_id=user_id,
                        managed_bot_id=int(id_text),
                        encryption_key=get_settings().bot_token_encryption_key,
                        generation=generation,
                    )
                except (ValueError, PermissionError) as exc:
                    # The owner must see billing holds, expired requests and
                    # registration limits, even after the callback was acked.
                    await bot.send_message(
                        user_id, "⚠️ " + owner_error(str(exc))[:850],
                    )
                    return
                except (AiogramError, RuntimeError):
                    # Telegram API errors may contain token-bearing URLs.
                    logging.getLogger(__name__).warning(
                        "Managed bot confirmation failed; candidate remains pending"
                    )
                    await bot.send_message(
                        user_id, tr(
                            "⚠️ Не удалось подтвердить подключение. "
                            "Попробуйте эту кнопку ещё раз. "
                            "Если ошибка повторится, используйте "
                            "«Через BotFather» в разделе подключений.",
                            "⚠️ Could not complete the connection. "
                            "Try the same button again. If it keeps failing, "
                            "use Via BotFather in My connections.",
                        ),
                    )
                    return
                except Exception as exc:
                    # Unexpected database/RLS/integration errors must not
                    # leave the owner with an apparently dead button. Do not
                    # log the exception text: provider errors can contain tokens.
                    logging.getLogger(__name__).error(
                        "Managed bot confirmation failed unexpectedly: %s",
                        type(exc).__name__,
                    )
                    await bot.send_message(
                        user_id, tr(
                            "⚠️ Подключение не завершено из-за внутренней ошибки. "
                            "Повторите подтверждение или используйте "
                            "«Через BotFather». Оплаченные счета не изменены.",
                            "⚠️ The bot was not connected due to an internal "
                            "error. Retry confirmation or use Via BotFather. "
                            "Existing paid invoices were not changed.",
                        ),
                    )
                    return
                if result.kind != "created" or result.project_id is None:
                    await bot.send_message(
                        user_id, tr(
                            "⚠️ Этот запрос уже обработан или устарел. "
                            "Проверьте «Мои подключения», затем при "
                            "необходимости создайте новый запрос.",
                            "⚠️ This request was already processed or has "
                            "expired. Check My connections before starting "
                            "another request.",
                        ),
                    )
                    return
                # Telegram's mandatory system keyboard needs a native
                # ReplyKeyboardRemove. Delete the short service message once
                # the one actual success card has been delivered.
                keyboard_notice = await bot.send_message(
                    user_id, tr("⌨️ Клавиатура закрыта.",
                                "⌨️ Keyboard closed."),
                    reply_markup=ReplyKeyboardRemove(),
                )
                try:
                    await send_connected_bot(
                        bot, user_id, actor_id, result.project_id, result.username,
                    )
                finally:
                    try:
                        await bot.delete_message(
                            user_id, keyboard_notice.message_id,
                        )
                    except AiogramError:
                        logging.getLogger(__name__).warning(
                            "Could not delete managed-bot keyboard cleanup message"
                        )
                return
            if action.startswith("managedsync:"):
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError(tr(
                        "Подключение ботов пока недоступно",
                        "Bot connections are unavailable",
                    ))
                project_id = decode_uuid(action.split(":", 1)[1])
                managed_id = await managed_bot_id_for_project(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                try:
                    result = await sync_managed_bot_token(
                        factory, central_bot=bot, actor_id=actor_id,
                        actor_telegram_id=user_id, managed_bot_id=managed_id,
                        encryption_key=get_settings().bot_token_encryption_key,
                    )
                except AiogramError:
                    await query.answer(tr(
                        "Этот бот не управляется ViMi Pay. Используйте "
                        "подключение через BotFather или включите режим управления.",
                        "This bot isn't managed by ViMi Pay. Use BotFather "
                        "token connection or enable Bot Management Mode.",
                    ), show_alert=True)
                    return
                await bot.send_message(
                    user_id,
                    tr("✅ Токен синхронизирован.", "✅ Token synchronized.")
                    if result.kind == "refreshed" else
                    tr("✅ Токен актуален.", "✅ Token is already current."),
                )
                await show_bots(bot, user_id, actor_id, project_id)
                await query.answer()
                return
            if action == "new":
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError(tr(
                        "Подключение ботов пока недоступно",
                        "Bot connections are unavailable",
                    ))
                await prompt(
                    bot, user_id, actor_id, step="new_bot_token",
                    show_main_menu=False,
                    rich_html=(
                        "<h3>" + tr(
                            "🔑 Подключение Telegram-бота",
                            "🔑 Connect your Telegram bot",
                        ) + "</h3><p><b>" + tr(
                            "🤖 УЖЕ ЕСТЬ БОТ ИЛИ СОЗДАЁТЕ НОВОГО?",
                            "🤖 HAVE A BOT OR CREATING A NEW ONE?",
                        ) + "</b></p><p><b>" + tr(
                            "1️⃣ ОТКРОЙТЕ @BotFather",
                            "1️⃣ OPEN @BotFather",
                        ) + "</b><br>" + tr(
                            "👉 <b>Существующий бот:</b> отправьте /mybots, "
                            "выберите бота и откройте API Token.<br>"
                            "👉 <b>Новый бот:</b> отправьте /newbot и "
                            "следуйте инструкциям Telegram.",
                            "👉 <b>Existing bot:</b> send /mybots, "
                            "select your bot and open API Token.<br>"
                            "👉 <b>New bot:</b> send /newbot and "
                            "follow Telegram's instructions.",
                        ) + "</p><p><b>" + tr(
                            "2️⃣ СКОПИРУЙТЕ API-ТОКЕН",
                            "2️⃣ COPY THE API TOKEN",
                        ) + "</b><br>" + tr(
                            "👉 Скопируйте токен, который выдаст @BotFather.",
                            "👉 Copy the token issued by @BotFather.",
                        ) + "</p><p><b>" + tr(
                            "3️⃣ ОТПРАВЬТЕ ТОКЕН СЮДА",
                            "3️⃣ SEND THE TOKEN HERE",
                        ) + "</b><br>" + tr(
                            "👉 <b>ВЕРНИТЕСЬ В ЭТОТ ЧАТ ViMi Pay.</b><br>"
                            "👉 Вставьте токен в обычное поле <b>«Сообщение…»</b> "
                            "внизу Telegram.<br>"
                            "👉 Нажмите <b>➤ ОТПРАВИТЬ</b>.",
                            "👉 <b>RETURN TO THIS ViMi Pay CHAT.</b><br>"
                            "👉 Paste the token into the regular "
                            "<b>Message…</b> box at the bottom of Telegram.<br>"
                            "👉 Tap <b>➤ SEND</b>.",
                        ) + "</p><p><b>" + tr(
                            "✅ ЧТО ДАЛЬШЕ?",
                            "✅ WHAT HAPPENS NEXT?",
                        ) + "</b><br>" + tr(
                            "ViMi Pay проверит токен, удалит сообщение с ним "
                            "из чата и подключит вашего бота.",
                            "ViMi Pay will verify the token, delete its chat "
                            "message and connect your bot.",
                        ) + "</p><p>" + tr(
                            "🔒 Никому больше не передавайте API-токен: "
                            "он даёт доступ к управлению ботом.",
                            "🔒 Never share your API token elsewhere: "
                            "it grants control of your bot.",
                        ) + "</p>"
                    ),
                    text=tr(
                        "🔑 <b>Подключение Telegram-бота</b>\n\n"
                        "<b>🤖 УЖЕ ЕСТЬ БОТ ИЛИ СОЗДАЁТЕ НОВОГО?</b>\n\n"
                        "<b>1️⃣ ОТКРОЙТЕ @BotFather</b>\n"
                        "👉 Существующий бот: /mybots → выберите бота → API Token.\n"
                        "👉 Новый бот: /newbot → следуйте инструкциям.\n\n"
                        "<b>2️⃣ СКОПИРУЙТЕ API-ТОКЕН</b>\n"
                        "👉 Скопируйте токен от @BotFather.\n\n"
                        "<b>3️⃣ ОТПРАВЬТЕ ТОКЕН СЮДА</b>\n"
                        "👉 <b>ВЕРНИТЕСЬ В ЭТОТ ЧАТ ViMi Pay.</b>\n"
                        "👉 Вставьте токен в поле <b>«Сообщение…»</b> внизу.\n"
                        "👉 Нажмите <b>➤ ОТПРАВИТЬ</b>.\n\n"
                        "<b>✅ ЧТО ДАЛЬШЕ?</b> ViMi Pay проверит токен и "
                        "удалит сообщение с ним из чата.\n"
                        "🔒 Не передавайте токен посторонним.",
                        "🔑 <b>Connect your Telegram bot</b>\n\n"
                        "<b>🤖 HAVE A BOT OR CREATING A NEW ONE?</b>\n\n"
                        "<b>1️⃣ OPEN @BotFather</b>\n"
                        "👉 Existing: /mybots → choose your bot → API Token.\n"
                        "👉 New: /newbot → follow the instructions.\n\n"
                        "<b>2️⃣ COPY THE API TOKEN</b>\n"
                        "👉 Copy the token from @BotFather.\n\n"
                        "<b>3️⃣ SEND THE TOKEN HERE</b>\n"
                        "👉 <b>RETURN TO THIS ViMi Pay CHAT.</b>\n"
                        "👉 Paste the token into <b>Message…</b> below.\n"
                        "👉 Tap <b>➤ SEND</b>.\n\n"
                        "<b>✅ WHAT NEXT?</b> ViMi Pay checks the token and "
                        "deletes its message from the chat.\n"
                        "🔒 Never share the token with strangers.",
                    ),
                )
                await query.answer()
                return
            if action == "projects" or action.startswith("projects:"):
                page = 0
                if ":" in action:
                    value = action.split(":", 1)[1]
                    if not value.isascii() or not value.isdecimal() or len(value) > 5:
                        raise ValueError("Недопустимая страница")
                    page = int(value)
                await show_connections(bot, user_id, actor_id, page=page)
                await query.answer()
                return
            if action == "archives" or action.startswith("archives:"):
                page = 0
                if ":" in action:
                    value = action.split(":", 1)[1]
                    if not value.isascii() or not value.isdecimal() or len(value) > 5:
                        raise ValueError("Недопустимая страница")
                    page = int(value)
                await show_connections(
                    bot, user_id, actor_id, page=page, archived=True,
                )
                await query.answer()
                return
            if action.startswith("archview:"):
                await show_archived_connection(
                    bot, user_id, actor_id,
                    UUID(hex=action.split(":", 1)[1]),
                )
                await query.answer()
                return
            if action.startswith("restore:"):
                project_ref = UUID(hex=action.split(":", 1)[1])
                await restore_owner_connection(
                    factory, actor_id=actor_id, project_id=project_ref,
                )
                await bot.send_message(
                    user_id,
                    tr("✅ Подключение восстановлено.", "✅ Connection restored."),
                )
                await show_connections(bot, user_id, actor_id)
                await query.answer()
                return
            if action.startswith("select:"):
                selected = UUID(hex=action.split(":", 1)[1])
                await choose_project(
                    factory, actor_id=actor_id, project_id=selected,
                )
                await show_bots(bot, user_id, actor_id, selected)
                await query.answer()
                return
            if action == "cancel":
                await cancel_owner_flow(
                    factory, actor_id=actor_id,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                await bot.send_message(
                    user_id,
                    tr("✖️ Настройка отменена.", "✖️ Setup canceled."),
                    reply_markup=ReplyKeyboardRemove(),
                )
                await show_home(bot, user_id, actor_id)
                await query.answer()
                return
            if action.startswith(("saasv2pay:","saasv2amount:",
                                  "saasv2create:")):
                # Old Telegram keyboards must NOT bypass disabled central
                # methods by creating a v2 attempt from legacy bank records.
                # They now enter the same authoritative operator selector.
                legacy=action.split(":")
                if len(legacy)<3:
                    raise ValueError("Invalid legacy payment link")
                token=legacy[1]
                decode_uuid(token)
                page_token=legacy[2]
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid legacy invoice page")
                action=("spm:"+token+":RUB:"
                        +_encode_saas_page(int(page_token)))
            if action.startswith(("spm:","spr:","spb:","spv:","spc:")):
                # The platform invoice has ONE payment entry point and a
                # four-rail 2x2 selector. The manual branch reuses the
                # existing proven bank-catalog/payment-proof flow.
                # Automatic rails must fail closed until their OWN verified
                # platform-provider fulfillment is connected. Tenant-owned
                # Crypto credentials or Stars balances cannot settle SaaS.
                parts=action.split(":")
                kind=parts[0]
                if kind in ("spm", "spb") and len(parts)!=4:
                    raise ValueError("Invalid pay selector")
                if kind=="spr" and (len(parts)!=4
                                    or parts[2] not in ("L","S","C","M")):
                    raise ValueError("Invalid payment rail")
                if kind=="spv" and len(parts)!=5:
                    raise ValueError("Invalid payment method")
                if kind=="spc" and len(parts)!=6:
                    raise ValueError("Invalid payment amount")
                invoice_id=decode_uuid(parts[1])
                page_text=parts[-1]
                page=_decode_saas_page(page_text)
                balance=await saas_v2_invoice_balance(
                    factory,actor_id=actor_id,invoice_id=invoice_id,
                    project_id=fixed_project_id,
                )
                remaining=Decimal(str(balance["remaining_rub"]))
                if remaining<=0:
                    await bot.send_message(user_id,tr(
                        "✅ Счёт уже оплачен.","✅ Invoice already paid.",
                    ))
                    await query.answer()
                    return
                invoice_token=encode_uuid(invoice_id)
                if kind=="spm":
                    # One selector with two buttons per row. Do not expose
                    # provider payment links until verified settlement exists.
                    rows=[
                        [
                            _button("💳 Lava.top",
                                    "spr:"+invoice_token+":L:"+page_text),
                            _button("⭐ Telegram Stars",
                                    "spr:"+invoice_token+":S:"+page_text),
                        ],
                        [
                            _button("💎 Crypto Pay",
                                    "spr:"+invoice_token+":C:"+page_text),
                            _button(tr("🏦 Полуавтомат", "🏦 Manual"),
                                    "spr:"+invoice_token+":M:"+page_text,
                                    "success"),
                        ],
                        [_button(tr("⬅️ К счёту","⬅️ Invoice"),
                                 "saasinvoices:0")],
                    ]
                    await _show_saas_payment_step(
                        bot,query,
                        tr("💳 <b>Оплата счёта ViMi Pay</b>\n"
                           "Выберите способ оплаты:",
                           "💳 <b>ViMi Pay invoice payment</b>\n"
                           "Choose how to pay:")
                        +"\n<b>"+format(remaining,".2f")+" ₽</b>",
                        rows,
                    )
                    await query.answer()
                    return
                elif kind=="spr":
                    if parts[2]=="M":
                        # Explicitly choose the manual-payment currency only
                        # after picking the manual rail.
                        # The spb branch below reads the method catalog
                        # only once, then renders the normal currency chooser.
                        action="spb:"+invoice_token+":RUB:"+page_text
                        kind="spb"
                        parts=action.split(":")
                    elif parts[2]=="S":
                        if fixed_project_id is None:
                            # Telegram Stars must be received by the actual
                            # CENTRAL platform bot, never by tenant bots.
                            try:
                                await stars_ui.send_platform_stars_invoice(
                                    bot, factory, payer_telegram_id=user_id,
                                    invoice_id=invoice_id,
                                )
                            except Exception:
                                await bot.send_message(user_id,tr(
                                    "⚠️ ⭐ Оплата пока недоступна: "
                                    "курс выплат или счёт требуют проверки.",
                                    "⚠️ ⭐ Stars checkout is unavailable: "
                                    "the payout rate or invoice needs review.",
                                ))
                        else:
                            # Payer-only deep link keeps the same immutable
                            # invoice ID while opening @vimipay_bot privately.
                            central=get_settings().saas_central_bot_username
                            deep_link=(
                                "https://t.me/"+central+"?start=sa_"+invoice_token
                            )
                            await _show_saas_payment_step(
                                bot,query,tr(
                                    "⭐ <b>Оплата через Telegram Stars</b>\n"
                                    "Платёж принимает центральный ViMi Pay.",
                                    "⭐ <b>Pay with Telegram Stars</b>\n"
                                    "Payment is received by central ViMi Pay.",
                                ),[
                                    [InlineKeyboardButton(
                                        text=tr("⭐ Продолжить в ViMi Pay",
                                                "⭐ Continue in ViMi Pay"),
                                        url=deep_link,
                                    )],
                                    [_button(tr("⬅️ Способы оплаты",
                                                "⬅️ Payment methods"),
                                             "spm:"+invoice_token+":RUB:"
                                             +page_text)],
                                ],
                            )
                        await query.answer()
                        return
                    elif parts[2]=="C":
                        if fixed_project_id is None:
                            # Platform-owned Crypto Pay app, never a merchant
                            # credential or a customer bot's payment address.
                            try:
                                await gateway_ui.send_platform_crypto_checkout(
                                    bot, factory, payer_telegram_id=user_id,
                                    invoice_id=invoice_id,
                                )
                            except Exception:
                                await _show_saas_payment_step(
                                    bot, query,
                                    tr(
                                        "⚠️ 💎 Оплата Crypto Pay пока недоступна. "
                                        "Счёт не изменён; вернитесь к способам оплаты.",
                                        "⚠️ 💎 Crypto Pay payment is not available. "
                                        "The invoice is unchanged; choose another method.",
                                    ),
                                    [[_button(
                                        tr("⬅️ Способы оплаты", "⬅️ Payment methods"),
                                        "spm:"+invoice_token+":RUB:"+page_text,
                                    )]],
                                )
                        else:
                            central=get_settings().saas_central_bot_username
                            deep_link=(
                                "https://t.me/"+central+"?start=sc_"+invoice_token
                            )
                            await _show_saas_payment_step(
                                bot,query,tr(
                                    "💎 <b>Crypto Pay · ViMi Pay</b>\n"
                                    "Оплата открывается в центральном боте.",
                                    "💎 <b>Crypto Pay · ViMi Pay</b>\n"
                                    "Checkout opens in the central bot.",
                                ),[
                                    [InlineKeyboardButton(
                                        text=tr("💎 Продолжить в ViMi Pay",
                                                "💎 Continue in ViMi Pay"),
                                        url=deep_link,
                                    )],
                                    [_button(tr("⬅️ Способы оплаты",
                                                "⬅️ Payment methods"),
                                             "spm:"+invoice_token+":RUB:"
                                             +page_text)],
                                ],
                            )
                        await query.answer()
                        return
                    else:
                        # Lava.top collects the payer's actual email only in
                        # central @vimipay_bot; never in a tenant merchant bot.
                        if fixed_project_id is None:
                            try:
                                await lava_ui.send_platform_lava_email_prompt(
                                    bot, factory, payer_telegram_id=user_id,
                                    invoice_id=invoice_id,
                                )
                            except Exception:
                                await _show_saas_payment_step(
                                    bot,query,tr(
                                        "⚠️ 💳 Lava.top пока недоступна. "
                                        "Счёт не изменён; выберите другой способ.",
                                        "⚠️ 💳 Lava.top is not available. "
                                        "The invoice is unchanged; choose another method.",
                                    ),[[_button(
                                        tr("⬅️ Способы оплаты","⬅️ Payment methods"),
                                        "spm:"+invoice_token+":RUB:"+page_text,
                                    )]],
                                )
                        else:
                            central=get_settings().saas_central_bot_username
                            deep_link=(
                                "https://t.me/"+central+"?start=sl_"+invoice_token
                            )
                            await _show_saas_payment_step(
                                bot,query,tr(
                                    "💳 <b>Lava.top · ViMi Pay</b>\n"
                                    "Продолжите в центральном боте: email "
                                    "нужен только для создания ссылки на оплату.",
                                    "💳 <b>Lava.top · ViMi Pay</b>\n"
                                    "Continue in the central bot: your email "
                                    "is needed only to create a payment link.",
                                ),[
                                    [InlineKeyboardButton(
                                        text=tr("💳 Продолжить в ViMi Pay",
                                                "💳 Continue in ViMi Pay"),
                                        url=deep_link,
                                    )],
                                    [_button(tr("⬅️ Способы оплаты",
                                                "⬅️ Payment methods"),
                                             "spm:"+invoice_token+":RUB:"
                                             +page_text)],
                                ],
                            )
                        await query.answer()
                        return
                if kind=="spb":
                    methods=await saas_catalog_for_invoice(
                        factory,actor_id=actor_id,invoice_id=invoice_id,
                        project_id=fixed_project_id,
                    )
                    if not methods:
                        await _show_saas_payment_step(
                            bot,query,tr(
                                "⚠️ Полуавтоматическая оплата не настроена.",
                                "⚠️ Manual payment is not configured.",
                            ),[[_button(tr("⬅️ Способы оплаты",
                                         "⬅️ Payment methods"),
                                         "spm:"+invoice_token+":RUB:"+page_text)]],
                        )
                        await query.answer()
                        return
                    currency=parts[2]
                    available=sorted({str(m["currency"]) for m in methods},
                        key=lambda c: ("RUB","USD","EUR","UAH","BYN").index(c))
                    if currency not in available:
                        currency=available[0]
                    rows=[]
                    currencies=[_button(
                        ("✅ " if c==currency else "")
                        + _saas_currency_label(c),
                        "spb:"+invoice_token+":"+c+":"+page_text,
                        "success" if c==currency else "primary",
                    ) for c in available]
                    rows.extend(currencies[i:i+3] for i in
                                range(0,len(currencies),3))
                    for method in methods:
                        if method["currency"]!=currency:
                            continue
                        rows.append([_button(
                            "💳 "+_saas_method_label(
                                method["name"],currency,
                            )[:40],
                            "spv:"+invoice_token+":"+encode_uuid(
                                UUID(str(method["id"])))+":"+currency
                            +":"+page_text,"success",
                        )])
                    rows.append([_button(
                        tr("⬅️ Способы оплаты", "⬅️ Payment methods"),
                        "spm:"+invoice_token+":RUB:"+page_text,
                    )])
                    await _show_saas_payment_step(
                        bot, query,
                        tr(
                            "💳 <b>Оплата ViMi Pay</b>\n"
                            "Осталось оплатить: ",
                            "💳 <b>ViMi Pay payment</b>\n"
                            "Amount remaining: ",
                        )
                        +"<b>"+format(remaining,".2f")+" ₽</b>\n\n"
                        +tr("Валюта: ","Currency: ")
                        +escape(_saas_currency_label(currency))+"\n"
                        +tr("Выберите способ оплаты:",
                            "Choose a payment method:"),
                        rows,
                    )
                else:
                    methods=await saas_catalog_for_invoice(
                        factory,actor_id=actor_id,invoice_id=invoice_id,
                        project_id=fixed_project_id,
                    )
                    method_id=decode_uuid(parts[2])
                    currency=parts[3]
                    chosen=next((m for m in methods if (
                        m["id"]==str(method_id)
                        and m["currency"]==currency)),None)
                    if chosen is None:
                        raise ValueError(tr(
                            "Способ оплаты был выключен. Выберите другой.",
                            "This payment method is no longer active.",
                        ))
                    method_label=escape(_saas_method_label(
                        chosen["name"],currency,
                    ))
                    # Full invoice payment only. Legacy 25/50% callbacks
                    # are rejected, never replayed into new bank attempts.
                    if kind=="spc" and parts[4]!="F":
                        raise ValueError(tr(
                            "Частичная оплата отменена.",
                            "Partial payments are no longer supported.",
                        ))
                    attempt=await saas_v3_create_attempt(
                        factory,actor_id=actor_id,invoice_id=invoice_id,
                        method_id=method_id,rub=remaining,currency=currency,
                        project_id=fixed_project_id,
                    )
                    # Exactly the existing proof workflow: reply to this
                    # message with a photo or document. The payment
                    # remains unconfirmed until operator bank review.
                    issued_prompt = await bot.send_message(
                        user_id,
                        tr(
                            "🧾 <b>ViMi Pay · Оплата счёта</b>\n\n",
                            "🧾 <b>ViMi Pay · Invoice payment</b>\n\n",
                        )
                        +"💳 <b>"+method_label+"</b>\n"
                        +tr("💰 Перевести: ","💰 Transfer: ")
                        +"<b>"+escape(str(attempt["amount"])+" "
                                      +str(attempt["currency"]))+"</b>"
                        +(
                            "\n"+tr("💱 Курс ЦБ: ","💱 CBR quote: ")
                            +escape(str(attempt["rub_per_unit"]))
                            if attempt["currency"]!="RUB" else ""
                        )
                        +"\n\n<pre>"+escape(str(attempt["instructions"]))
                        +"</pre>\n\n"
                        +tr(
                            "После перевода ответьте <b>на это сообщение</b> "
                            "фото или документом чека. Подтверждение "
                            "происходит только после проверки поступления "
                            "владельцем ViMi Pay.",
                            "After payment, <b>reply to this message</b> "
                            "with a receipt photo/document. ViMi Pay "
                            "confirms only after bank verification.",
                        )
                        ,
                        parse_mode="HTML",
                        reply_markup=_markup([[_button(
                            tr("⬅️ К счёту","⬅️ Invoice"),
                            "saasinvoices:0",
                        )]]),
                    )
                    try:
                        await saas_bind_payment_prompt(
                            factory, actor_id=actor_id,
                            attempt_id=UUID(str(attempt["id"])),
                            source_bot_id=bot.id, source_chat_id=user_id,
                            source_message_id=issued_prompt.message_id,
                            project_id=fixed_project_id,
                        )
                    except Exception:
                        # Do not leave an apparently usable instruction that
                        # cannot accept a receipt after a process restart.
                        try:
                            await bot.delete_message(
                                user_id, issued_prompt.message_id,
                            )
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "Unbound ViMi Pay payment prompt cannot be deleted"
                            )
                        raise RuntimeError("ViMi Pay payment instruction unavailable") from None
                await query.answer()
                return
            if action == "saasprofile":
                await show_saas_service_profile(bot, user_id, actor_id)
                await query.answer()
                return
            if action.startswith("saasprice:"):
                if fixed_project_id is None:
                    await query.answer(tr(
                        "Откройте 🧾 Счёт своего бота",
                        "Open your bot's 🧾 Bill",
                    ), show_alert=True)
                    return
                chosen_price = action.split(":", 1)[1]
                if chosen_price not in ("RUB", "USD"):
                    raise ValueError("Invalid service billing profile")
                previous_profile = await saas_price_profile(
                    factory, actor_id=actor_id, project_id=fixed_project_id,
                )
                was_initial = previous_profile.get("currency_selected") is False
                try:
                    changed = await saas_price_profile(
                        factory, actor_id=actor_id,
                        currency=chosen_price, project_id=fixed_project_id,
                    )
                except DBAPIError as exc:
                    # Connected-bot profile changes are locked only while
                    # that bot owes a bill; unrelated bots never mix debts.
                    # No raw database error is displayed to the merchant.
                    cause = str(exc.orig)
                    if (
                        getattr(exc.orig, "sqlstate", None) == "23514"
                        and "Settle outstanding service invoice" in cause
                    ):
                        await query.answer(tr(
                            "⛔ Сначала погасите неоплаченные счета ViMi Pay. "
                            "После этого можно сменить валюту обслуживания.",
                            "⛔ Settle outstanding ViMi Pay invoices first. "
                            "Then you can change the service currency.",
                        ), show_alert=True)
                        return
                    raise
                if str(changed["currency"]) != chosen_price:
                    raise RuntimeError("Future ViMi Pay tariff did not persist")
                if was_initial:
                    await show_dashboard(
                        bot, user_id, actor_id, fixed_project_id,
                    )
                else:
                    await show_saas_service_profile(bot, user_id, actor_id)
                await query.answer(tr(
                    "✅ Валюта обслуживания выбрана.",
                    "✅ Service currency selected.",
                ) if was_initial else tr(
                    "✅ Валюта будущих счетов изменена.",
                    "✅ Future invoice currency updated.",
                ))
                return
            if action.startswith("saaspaychoices:"):
                # Old Telegram keyboard: no longer allow splitting one bill
                # into individual charges. Return to the owner's bill.
                await show_saas_statements(
                    bot, user_id, actor_id, fixed_project_id,
                )
                await query.answer()
                return
            if action.startswith("saasbotbill:"):
                if fixed_project_id is not None:
                    raise PermissionError("Per-bot bill link belongs to central ViMi Pay")
                target_id = decode_uuid(action.split(":", 1)[1])
                await show_central_bot_bill(
                    bot, user_id, actor_id, target_id,
                )
                await query.answer()
                return
            if action.startswith("saasnearest:"):
                if fixed_project_id is not None:
                    raise PermissionError("Nearest account invoice belongs to central ViMi Pay")
                invoice_id = decode_uuid(action.split(":", 1)[1])
                # The invoice can be on any page and pages change as bills
                # arrive. The owner-scoped DB lookup resolves it at click time.
                invoice_page = await owner_invoice_statement_page(
                    factory, actor_id=actor_id, invoice_id=invoice_id,
                )
                await show_saas_issued_invoice(
                    bot, user_id, actor_id, invoice_id, page=invoice_page,
                )
                await query.answer()
                return
            if action.startswith("saasaccountlist:"):
                if fixed_project_id is not None:
                    raise PermissionError("Account invoices belong to central ViMi Pay")
                page_text = action.split(":", 1)[1]
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Invalid invoice page")
                await show_account_invoice_list(
                    bot, user_id, actor_id, page=int(page_text),
                )
                await query.answer()
                return
            if action.startswith("saasinvoices:"):
                # Historical debts belong to the Telegram account, even after
                # its last project has been transferred away.
                page_text = action.split(":", 1)[1]
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Invalid platform invoice page")
                await show_saas_statements(
                    bot, user_id, actor_id, fixed_project_id,
                    page=int(page_text),
                )
                await query.answer()
                return
            if action.startswith("sl:"):
                # Short callback binds the displayed day/rate/amount to this
                # one invoice. PostgreSQL revalidates under the invoice lock.
                fields = action.split(":")
                if len(fields) != 6:
                    raise ValueError("Invalid guarded invoice settlement")
                _, token, page_text, currency, day_token, fingerprint = fields
                if (
                    not page_text.isascii() or not page_text.isdecimal()
                    or int(page_text) > 100000
                    or currency not in ("RUB", "USD", "EUR", "UAH", "BYN")
                    or len(day_token) != 8 or not day_token.isascii()
                    or not day_token.isdecimal()
                    or len(fingerprint) != 12
                    or any(ch not in "0123456789abcdef" for ch in fingerprint)
                ):
                    raise ValueError("Invalid settlement callback")
                invoice_id = decode_uuid(token)
                invoice_page = int(page_text)
                suffix = token + ":" + page_text
                try:
                    quoted = await preview_saas_invoice_currency(
                        factory, actor_user_id=actor_id,
                        invoice_id=invoice_id, currency=currency,
                    )
                    if (
                        not quoted["quote_ready"]
                        or quoted["quote_day"].replace("-", "") != day_token
                        or quote_fingerprint(quoted) != fingerprint
                    ):
                        raise ValueError("Displayed quote expired")
                    await lock_saas_invoice_currency_expected(
                        factory, actor_user_id=actor_id,
                        invoice_id=invoice_id, currency=currency,
                        quote_day=datetime.strptime(day_token, "%Y%m%d").date(),
                        rub_per_unit=Decimal(str(quoted["rub_per_unit"])),
                        expected_amount=Decimal(str(quoted["amount"])),
                    )
                except Exception:
                    await bot.send_message(
                        user_id,
                        tr(
                            "⚠️ Условия расчёта изменились или счёт недоступен. "
                            "Откройте его снова и проверьте актуальный курс "
                            "перед фиксацией.",
                            "⚠️ Settlement terms changed or the invoice is "
                            "unavailable. Reopen it and review the updated "
                            "rate before locking.",
                        ),
                        reply_markup=_markup([[_button(
                            tr("⬅️ К счёту", "⬅️ Invoice"),
                            "saasbill:" + suffix,
                        )]]),
                    )
                else:
                    await show_saas_issued_invoice(
                        bot, user_id, actor_id, invoice_id, page=invoice_page,
                    )
                await query.answer()
                return
            if action.startswith((
                "saasbill:", "saasbillchoose:", "saasbillquote:",
            )):
                parts = action.split(":")
                kind = parts[0]
                if len(parts) not in (3, 4):
                    raise ValueError("Invalid billing action")
                if kind == "saasbillquote" and len(parts) != 4:
                    raise ValueError("Invalid settlement action")
                if kind in ("saasbill", "saasbillchoose") and len(parts) != 3:
                    raise ValueError("Invalid invoice action")
                invoice_id = decode_uuid(parts[1])
                page_value = parts[2]
                if (
                    not page_value.isascii() or not page_value.isdecimal()
                    or int(page_value) > 100000
                ):
                    raise ValueError("Invalid invoice page")
                invoice_page = int(page_value)
                if kind == "saasbill":
                    await show_saas_issued_invoice(
                        bot, user_id, actor_id, invoice_id, page=invoice_page,
                    )
                elif kind == "saasbillchoose":
                    # This step does not write an FX quote or change an invoice.
                    snapshot = await saas_invoice_currency_snapshot(
                        factory, actor_user_id=actor_id, invoice_id=invoice_id,
                    )
                    if snapshot.get("locked"):
                        raise ValueError(tr(
                            "Валюта уже зафиксирована.", "Currency already locked.",
                        ))
                    suffix = encode_uuid(invoice_id) + ":" + str(invoice_page)
                    await bot.send_message(
                        user_id,
                        tr("<b>💱 Выберите валюту выставленного счёта</b>\n"
                           "Перед фиксацией покажем курс ЦБ и окончательную сумму.",
                           "<b>💱 Choose issued invoice currency</b>\n"
                           "Review the official rate and amount before locking."),
                        parse_mode="HTML",
                        reply_markup=_markup([
                            [_button("RUB ₽", "saasbillquote:" + suffix + ":RUB"),
                             _button("USD $", "saasbillquote:" + suffix + ":USD")],
                            [_button("EUR €", "saasbillquote:" + suffix + ":EUR"),
                             _button("UAH ₴", "saasbillquote:" + suffix + ":UAH")],
                            [_button("BYN Br", "saasbillquote:" + suffix + ":BYN")],
                            [_button(tr("⬅️ Счёт", "⬅️ Invoice"),
                                     "saasbill:" + suffix)],
                        ]),
                    )
                else:
                    currency = parts[3]
                    if currency not in ("RUB", "USD", "EUR", "UAH", "BYN"):
                        raise ValueError("Invalid payment currency")
                    suffix = encode_uuid(invoice_id) + ":" + str(invoice_page)
                    quote = await preview_saas_invoice_currency(
                        factory, actor_user_id=actor_id,
                        invoice_id=invoice_id, currency=currency,
                    )
                    rows = []
                    if quote["quote_ready"]:
                        # Keep under Telegram's 64-byte callback limit.
                        # The 12-hex token covers the exact rate and amount
                        # shown on the Rich card, not merely its currency.
                        lock_data = (
                            "sl:" + suffix + ":" + currency + ":"
                            + quote["quote_day"].replace("-", "") + ":"
                            + quote_fingerprint(quote)
                        )
                        rows.append([_button(
                            tr("🔒 Подтвердить и зафиксировать",
                               "🔒 Confirm and lock"),
                            lock_data, "success",
                        )])
                    rows.append([_button(
                        tr("⬅️ Валюты", "⬅️ Currencies"),
                        "saasbillchoose:" + suffix,
                    )])
                    await send_owner_rich(
                        bot, chat_id=user_id,
                        rich_html=issued_quote_rich(quote),
                        classic_html=issued_quote_classic(quote),
                        reply_markup=_markup(rows),
                    )
                await query.answer()
                return
            project_id = await get_selected_project(
                factory, actor_id=actor_id,
                bot_scope_id=bot.id if fixed_project_id else 0,
                fixed_project_id=fixed_project_id,
            )
            if project_id is None:
                raise ValueError(tr("Сначала создайте или выберите проект",
                                    "Create or select a connection first"))
            if action == "modmanage" or action.startswith("modmanage:"):
                page = action.split(":", 1)[1] if ":" in action else "0"
                if not page.isascii() or not page.isdecimal():
                    raise ValueError("Неверная страница")
                await show_moderators(
                    bot, user_id, actor_id, project_id, page=int(page),
                )
            elif action == "modadd":
                await list_project_moderators(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                await prompt(
                    bot, user_id, actor_id, step="moderator_add",
                    text=tr(
                        "🛡 <b>Назначение модератора</b>\n\n"
                        "Модератор должен открыть этот бот и отправить /mod, "
                        "чтобы узнать свой Telegram ID.\n"
                        "Пришлите сюда числовой Telegram ID сотрудника.",
                        "🛡 <b>Add moderator</b>\n\n"
                        "The moderator should open this bot and send /mod "
                        "to see their Telegram ID.\n"
                        "Send the employee's numeric Telegram ID.",
                    ),
                )
            elif action.startswith(("modremoveask:", "modremoveok:")):
                verb, value = action.split(":", 1)
                if not value.isascii() or not value.isdecimal():
                    raise ValueError("Неверный Telegram ID")
                target = int(value)
                await list_project_moderators(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                if verb == "modremoveask":
                    await bot.send_message(
                        user_id,
                        tr("Отозвать права модератора у ID ",
                           "Revoke moderator access for ID ")
                        + str(target) + "?",
                        reply_markup=_markup([
                            [_button(tr("✅ Отозвать", "✅ Revoke"),
                                     "modremoveok:" + value, "danger")],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "modmanage")],
                        ]),
                    )
                else:
                    changed = await set_project_moderator(
                        factory, actor_id=actor_id, project_id=project_id,
                        telegram_id=target, enabled=False,
                    )
                    await bot.send_message(
                        user_id,
                        tr("✅ Права отозваны.", "✅ Access revoked.") if changed
                        else tr("Уже не модератор.", "Already not a moderator."),
                    )
                    await show_moderators(bot, user_id, actor_id, project_id)
            elif action == "subscribers":
                await show_subscribers(bot, user_id, actor_id, project_id)
            elif action.startswith("sublist:"):
                _, status, page_text = action.split(":", 2)
                if (
                    status not in SUBSCRIBER_FILTERS
                    or not page_text.isascii()
                    or not page_text.isdecimal()
                ):
                    raise ValueError("Некорректный фильтр подписчиков")
                await show_subscribers(
                    bot, user_id, actor_id, project_id,
                    status=status, page=int(page_text),
                )
            elif action.startswith("subfind:"):
                _, page_text, search = action.split(":", 2)
                if not page_text.isascii() or not page_text.isdecimal() or len(search) > 32:
                    raise ValueError("Некорректный поиск подписчика")
                await show_subscribers(
                    bot, user_id, actor_id, project_id,
                    page=int(page_text), search=search,
                )
            elif action == "subsearch":
                await prompt(
                    bot, user_id, actor_id, step="subscriber_search",
                    text=tr(
                        "🔎 <b>Найти подписчика</b>\n\n"
                        "Пришлите @username или числовой Telegram ID. "
                        "Поиск ограничен покупателями этого бота.",
                        "🔎 <b>Find a subscriber</b>\n\n"
                        "Send @username or numeric Telegram ID. "
                        "Only customers of this bot are searchable.",
                    ),
                )
            elif action.startswith("subscriber:"):
                cid = decode_uuid(action.split(":", 1)[1])
                await show_subscriber_card(
                    bot, user_id, actor_id, project_id, cid,
                )
            elif action.startswith("subinvoices:"):
                _, cid, page_text = action.split(":", 2)
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Недопустимая страница счетов")
                await show_subscriber_invoices(
                    bot, user_id, actor_id, project_id,
                    decode_uuid(cid), page=int(page_text),
                )
            elif action.startswith("subhistory:"):
                _, cid, page_text = action.split(":", 2)
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Недопустимая страница покупок")
                await show_subscriber_card(
                    bot, user_id, actor_id, project_id,
                    decode_uuid(cid), history_page=int(page_text),
                )
            elif action.startswith("subgrantpage:"):
                _, cid, page_text = action.split(":", 2)
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Недопустимая страница прав доступа")
                await show_subscriber_card(
                    bot, user_id, actor_id, project_id,
                    decode_uuid(cid), grant_page=int(page_text),
                )
            elif action.startswith("submod:"):
                customer_id = decode_uuid(action.split(":", 1)[1])
                detail = await subscriber_detail(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=customer_id,
                )
                if not detail.owner_only_actions:
                    raise PermissionError("Только владелец управляет модераторами")
                cid = encode_uuid(customer_id)
                await bot.send_message(
                    user_id,
                    tr(
                        "🛡 <b>Роль модератора</b>\n\n"
                        "Покупатель получит доступ к переписке и блокировке "
                        "спама, а также просмотру заявок без сумм и чеков. "
                        "Оплаты, тарифы и владение ботом недоступны.\n"
                        "Выберите действие для Telegram ID ",
                        "🛡 <b>Moderator role</b>\n\n"
                        "The customer may message buyers, ban spam and view "
                        "redacted requests. Payments, plans and bot ownership "
                        "remain unavailable.\nChoose action for Telegram ID ",
                    ) + str(detail.telegram_id),
                    parse_mode="HTML", reply_markup=_markup([
                        [_button(tr("✅ Назначить", "✅ Appoint"),
                                 "submodconfirm:" + cid + ":1", "success")],
                        [_button(tr("🗑 Отозвать", "🗑 Revoke"),
                                 "submodconfirm:" + cid + ":0", "danger")],
                        [_button(tr("⬅️ Карточка", "⬅️ Customer card"),
                                 "subscriber:" + cid)],
                    ]),
                )
            elif action.startswith("submodconfirm:"):
                _, cid, flag = action.split(":", 2)
                if flag not in ("0", "1"):
                    raise ValueError("Неверное действие")
                customer_id = decode_uuid(cid)
                detail = await subscriber_detail(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=customer_id,
                )
                if not detail.owner_only_actions:
                    raise PermissionError("Только владелец управляет модераторами")
                changed = await set_project_moderator(
                    factory, actor_id=actor_id, project_id=project_id,
                    telegram_id=detail.telegram_id, enabled=flag == "1",
                )
                await bot.send_message(user_id, (
                    tr("✅ Права изменены.", "✅ Permissions changed.") if changed
                    else tr("Изменений нет.", "No changes.")
                ))
                await show_subscriber_card(
                    bot, user_id, actor_id, project_id, customer_id,
                )
            elif action.startswith("submsg:"):
                cid = decode_uuid(action.split(":", 1)[1])
                detail = await subscriber_detail(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=cid,
                )
                if not detail.owner_only_actions:
                    raise PermissionError("Сообщения покупателям отправляет владелец")
                await prompt(
                    bot, user_id, actor_id,
                    step="subscriber_message",
                    payload={"customer_id": cid.hex},
                    text=tr(
                        "✉️ <b>Сообщение покупателю</b>\n\n"
                        "Отправьте текст, эмодзи или Telegram-ссылку. "
                        "Сообщение будет отправлено только этому покупателю.",
                        "✉️ <b>Message customer</b>\n\n"
                        "Send text, emoji or Telegram links. "
                        "Only this buyer receives it.",
                    ),
                )
            elif action.startswith("subactivate:"):
                parts = action.split(":")
                if len(parts) not in (2, 3):
                    raise ValueError("Неверная страница тарифов")
                page_text = parts[2] if len(parts) == 3 else "0"
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Неверная страница тарифов")
                await show_manual_plan_picker(
                    bot, user_id, actor_id, project_id,
                    decode_uuid(parts[1]), page=int(page_text),
                )
            elif action.startswith(("subactask:", "subactok:")):
                verb, cid, pid = action.split(":", 2)
                customer_id, plan_id = decode_uuid(cid), decode_uuid(pid)
                if verb == "subactask":
                    detail = await subscriber_detail(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=customer_id,
                    )
                    if not detail.owner_only_actions:
                        raise PermissionError("Только владелец может выдавать тариф")
                    async with tenant_session_scope(
                        factory, TenantContext(
                            user_id=actor_id, project_id=project_id,
                        ),
                    ) as session:
                        plan = await session.scalar(
                            select(Plan).where(
                                Plan.project_id == project_id,
                                Plan.id == plan_id, Plan.status == "active",
                            )
                        )
                    if plan is None:
                        raise ValueError("Тариф больше недоступен")
                    await bot.send_message(
                        user_id,
                        tr(
                            "⚠️ <b>Бесплатная активация</b>\n",
                            "⚠️ <b>Complimentary activation</b>\n",
                        ) + escape(plan.name) + " · "
                        + format_period(plan.duration_value, plan.duration_unit)
                        + tr(
                            "\n\nПодтвердите выдачу. Это НЕ оплата. "
                            "Права появятся в карточке, а финансовая история "
                            "и оплаченные подписки не изменятся.",
                            "\n\nConfirm grant. This is NOT payment. "
                            "Access will appear in the card without changing "
                            "approved payments or paid subscriptions.",
                        ),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(
                                tr("✅ Активировать", "✅ Activate"),
                                "subactok:" + cid + ":" + pid, "success",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "subscriber:" + cid)],
                        ]),
                    )
                else:
                    await activate_manual_plan(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=customer_id, plan_id=plan_id,
                    )
                    await bot.send_message(
                        user_id,
                        tr("✅ Тариф активирован без оплаты.",
                           "✅ Plan activated without payment."),
                    )
                    await show_subscriber_card(
                        bot, user_id, actor_id, project_id, customer_id,
                    )
            elif action.startswith("subcancel:"):
                chunks = action.split(":")
                if len(chunks) not in (2, 3, 4):
                    raise ValueError("Некорректная страница аннулирования")
                grant_page = chunks[3] if len(chunks) == 4 else (
                    chunks[2] if len(chunks) == 3 else "0"
                )
                paid_page = chunks[2] if len(chunks) == 4 else "0"
                if any(
                    not part.isascii() or not part.isdecimal()
                    for part in (grant_page, paid_page)
                ):
                    raise ValueError("Некорректная страница аннулирования")
                await show_customer_cancel_picker(
                    bot, user_id, actor_id, project_id,
                    decode_uuid(chunks[1]), page=int(grant_page),
                    paid_page=int(paid_page),
                )
            elif action.startswith(("subendask:", "subendok:")):
                verb, cid, sid = action.split(":", 2)
                customer_id, subscription_id = decode_uuid(cid), decode_uuid(sid)
                sub = await get_active_customer_subscription(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=customer_id, subscription_id=subscription_id,
                )
                if verb == "subendask":
                    await bot.send_message(
                        user_id,
                        tr(
                            "⚠️ <b>Досрочно завершить оплаченную подписку?</b>\n\n",
                            "⚠️ <b>End paid subscription early?</b>\n\n",
                        ) + escape(sub.plan_name) + tr(
                            "\nЭто может лишить покупателя уже оплаченного "
                            "доступа. Согласуйте возврат отдельно. "
                            "Подтверждённые платежи не удаляются.",
                            "\nThis may revoke prepaid access. Handle any "
                            "refund separately. Confirmed payments remain.",
                        ),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(
                                tr("🗑 Завершить подписку", "🗑 End subscription"),
                                "subendok:" + cid + ":" + sid, "danger",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "subscriber:" + cid)],
                        ]),
                    )
                else:
                    result = await end_owner_subscription(
                        factory, actor_id=actor_id, project_id=project_id,
                        subscription_id=subscription_id,
                    )
                    if result.telegram_id is not None and result.changed:
                        try:
                            await bot.send_message(
                                result.telegram_id, result.message,
                            )
                        except AiogramError:
                            pass
                    await bot.send_message(
                        user_id,
                        tr("✅ Подписка завершена; история оплат сохранена.",
                           "✅ Subscription ended; payment history preserved."),
                    )
                    await show_subscriber_card(
                        bot, user_id, actor_id, project_id, customer_id,
                    )
            elif action.startswith(("sra:", "srk:")):
                verb, cid, source, page_text = action.split(":", 3)
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Некорректная страница аннулирования")
                page = int(page_text)
                customer_id, activation_id = decode_uuid(cid), decode_uuid(source)
                detail = await subscriber_detail(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=customer_id, grant_page=page,
                )
                if not detail.owner_only_actions or activation_id not in {
                    g.source_id for g in detail.grants
                    if g.manual_plan_id is not None
                }:
                    raise PermissionError("Бесплатная активация недоступна")
                if verb == "sra":
                    await bot.send_message(
                        user_id, tr(
                            "⚠️ Отозвать эту бесплатную активацию? "
                            "Оплаченные права и платежи сохраняются.",
                            "⚠️ Revoke this complimentary plan? "
                            "Paid rights and payment records remain.",
                        ), reply_markup=_markup([
                            [_button(
                                tr("🗑 Подтвердить", "🗑 Confirm"),
                                "srk:" + cid + ":" + source + ":" + page_text,
                                "danger",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "subscriber:" + cid)],
                        ]),
                    )
                else:
                    await revoke_manual_plan(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=customer_id, activation_id=activation_id,
                    )
                    await show_subscriber_card(
                        bot, user_id, actor_id, project_id, customer_id,
                    )
            elif action.startswith(("subbanask:", "subbanok:")):
                verb, cid, flag = action.split(":", 2)
                if flag not in ("0", "1"):
                    raise ValueError("Неверный режим блокировки")
                customer_id, banned = decode_uuid(cid), flag == "1"
                detail = await subscriber_detail(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=customer_id,
                )
                if not detail.owner_only_actions:
                    raise PermissionError("Только владелец может блокировать")
                if verb == "subbanask":
                    await bot.send_message(
                        user_id,
                        tr(
                            "🚫 <b>Заблокировать покупателя?</b>\n"
                            "Новые счета и отправка чеков будут запрещены. "
                            "Ранее оплаченные права и платежи не стираются.",
                            "🚫 <b>Ban customer?</b>\n"
                            "New invoices and receipt submissions will be blocked. "
                            "Existing paid grants and payments remain.",
                        ) if banned else tr(
                            "✅ Разблокировать покупателя? Новые покупки снова доступны.",
                            "✅ Unban customer? New purchases will be allowed again.",
                        ),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(
                                tr("✅ Подтвердить", "✅ Confirm"),
                                "subbanok:" + cid + ":" + flag,
                                "danger" if banned else "success",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "subscriber:" + cid)],
                        ]),
                    )
                else:
                    await set_customer_ban(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=customer_id, banned=banned,
                    )
                    await show_subscriber_card(
                        bot, user_id, actor_id, project_id, customer_id,
                    )
            elif action.startswith("subgrantstart:"):
                await show_manual_resource_picker(
                    bot, user_id, actor_id, project_id,
                    decode_uuid(action.split(":", 1)[1]),
                )
            elif action.startswith("subres:"):
                _, cid, page_text = action.split(":", 2)
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Недопустимая страница каналов")
                await show_manual_resource_picker(
                    bot, user_id, actor_id, project_id,
                    decode_uuid(cid), page=int(page_text),
                )
            elif action.startswith("subtarget:"):
                _, cid, rid = action.split(":", 2)
                customer_id, resource_id = decode_uuid(cid), decode_uuid(rid)
                detail = await subscriber_detail(
                    factory, actor_id=actor_id, project_id=project_id,
                    customer_id=customer_id,
                )
                if not detail.owner_only_actions or resource_id not in {
                    ref for ref, _ in detail.resources
                }:
                    raise PermissionError("Канал больше недоступен для ручной выдачи")
                options = [
                    (tr("7 дней", "7 days"), 7),
                    (tr("30 дней", "30 days"), 30),
                    (tr("90 дней", "90 days"), 90),
                    (tr("Бессрочно", "Lifetime"), 0),
                ]
                rows = [[_button(
                    "🔑 " + label,
                    "subgrantask:" + cid + ":" + rid + ":" + str(days),
                )] for label, days in options]
                rows.append([_button(
                    tr("⬅️ К каналам", "⬅️ Back to channels"),
                    "subgrantstart:" + cid,
                )])
                await bot.send_message(
                    user_id,
                    tr(
                        "🔑 <b>Выберите срок ручного доступа</b>\n"
                        "Будет выдано отдельное право, без платежа.",
                        "🔑 <b>Choose the manual access duration</b>\n"
                        "A separate grant is created without payment.",
                    ),
                    parse_mode="HTML", reply_markup=_markup(rows),
                )
            elif action.startswith(("subgrantask:", "subgrantok:")):
                verb, cid, rid, days_text = action.split(":", 3)
                if not days_text.isascii() or not days_text.isdecimal():
                    raise ValueError("Неверный срок")
                days = int(days_text)
                if days not in MANUAL_DURATIONS:
                    raise ValueError("Недопустимый срок ручного доступа")
                customer_id, resource_id = decode_uuid(cid), decode_uuid(rid)
                if verb == "subgrantask":
                    detail = await subscriber_detail(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=customer_id,
                    )
                    resources = dict(detail.resources)
                    if not detail.owner_only_actions or resource_id not in resources:
                        raise PermissionError("Доступно только владельцу и активному каналу")
                    duration = (
                        tr("бессрочно", "lifetime") if days == 0
                        else str(days) + tr(" дней", " days")
                    )
                    await bot.send_message(
                        user_id,
                        tr("🔑 <b>Подтвердите ручную выдачу</b>\n\n",
                           "🔑 <b>Confirm manual grant</b>\n\n")
                        + escape(subscriber_label(
                            detail.telegram_id, detail.username,
                            detail.first_name,
                        ))
                        + "\n📣 " + escape(resources[resource_id])
                        + "\n⏳ " + duration + "\n\n"
                        + tr(
                            "Это отдельное бесплатное право. Оплаченные "
                            "подписки и финансовая история не меняются.",
                            "A separate complimentary grant. Paid "
                            "subscriptions and financial history stay intact.",
                        ),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(
                                tr("✅ Выдать доступ", "✅ Grant access"),
                                "subgrantok:" + cid + ":" + rid + ":"
                                + str(days), "success",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "subscriber:" + cid)],
                        ]),
                    )
                else:
                    await grant_manual_access(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=customer_id, resource_id=resource_id,
                        days=days,
                    )
                    await bot.send_message(
                        user_id, tr(
                            "✅ Ручное право сохранено. Выдачу доступа "
                            "в Telegram завершит работающий бот.",
                            "✅ Manual grant saved. The running bot will "
                            "complete Telegram access delivery.",
                        ),
                    )
                    await show_subscriber_card(
                        bot, user_id, actor_id, project_id, customer_id,
                    )
            elif action.startswith(("subrevoke:", "subrevok:")):
                verb, cid, gid, page_text = action.split(":", 3)
                if not page_text.isascii() or not page_text.isdecimal():
                    raise ValueError("Недопустимая страница ручных прав")
                grant_page = int(page_text)
                customer_id, grant_id = decode_uuid(cid), decode_uuid(gid)
                if verb == "subrevoke":
                    detail = await subscriber_detail(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=customer_id, grant_page=grant_page,
                    )
                    if not detail.owner_only_actions or grant_id not in {
                        g.id for g in detail.grants if g.source == "manual"
                    }:
                        raise PermissionError("Можно отзывать только собственные ручные права")
                    await bot.send_message(
                        user_id,
                        tr("⚠️ <b>Отозвать ручной доступ?</b>\n\n",
                           "⚠️ <b>Revoke manual access?</b>\n\n")
                        + escape(subscriber_label(
                            detail.telegram_id, detail.username,
                            detail.first_name,
                        ))
                        + "\n\n" + tr(
                            "Будет отозвано только выбранное ручное право. "
                            "Оплаченные подписки и служебные права сохраняются. "
                            "Если других прав нет — бот поставит отзыв доступа "
                            "в Telegram в очередь.",
                            "Only this manual grant is revoked. Paid "
                            "subscriptions and service rights remain. "
                            "If no other grants apply, Telegram revocation "
                            "will be queued.",
                        ),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(
                                tr("🗑 Да, отозвать", "🗑 Confirm revoke"),
                                "subrevok:" + cid + ":" + gid
                                + ":" + page_text, "danger",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "subscriber:" + cid)],
                        ]),
                    )
                else:
                    result = await revoke_manual_access(
                        factory, actor_id=actor_id, project_id=project_id,
                        customer_id=customer_id, grant_id=grant_id,
                    )
                    await bot.send_message(
                        user_id, tr(
                            "✅ Ручной доступ отозван." if result.changed
                            else "ℹ️ Это право уже было отозвано.",
                            "✅ Manual access revoked." if result.changed
                            else "ℹ️ This grant was already revoked.",
                        ),
                    )
                    await show_subscriber_card(
                        bot, user_id, actor_id, project_id, customer_id,
                        grant_page=grant_page,
                    )
            elif action == "statistics" or action.startswith("statistics:"):
                period = "7" if action == "statistics" else action.split(":", 1)[1]
                if period not in ("7", "30", "all"):
                    raise ValueError(tr("Неизвестный период", "Unknown period"))
                await show_statistics(bot, user_id, actor_id, project_id, period)
            elif action.startswith("invoice:"):
                request_id = decode_uuid(action.split(":", 1)[1])
                await show_invoice(bot, user_id, actor_id, project_id, request_id)
            elif action == "notifications":
                await show_notifications(bot, user_id, actor_id, project_id)
            elif action.startswith("notifyset:"):
                _, category, raw = action.split(":", 2)
                if (
                    raw not in ("0", "1")
                    or category not in ("invoice", "expiry", "access", "daily")
                ):
                    raise ValueError(tr("Некорректная настройка", "Invalid setting"))
                async with tenant_session_scope(
                    factory, TenantContext(user_id=actor_id, project_id=project_id),
                ) as session:
                    await set_owner_setting(
                        session, project_id=project_id, actor_id=actor_id,
                        category=category, enabled=raw == "1",
                    )
                await show_notifications(bot, user_id, actor_id, project_id)
            elif action == "settings":
                await show_settings(bot, user_id)
            elif action == "dashboard":
                await show_dashboard(bot, user_id, actor_id, project_id)
            elif action == "setupback2":
                if fixed_project_id != project_id:
                    raise PermissionError("Open the connected administrator bot")
                await show_channel_setup_step_two(
                    bot, user_id, actor_id, project_id,
                    source_message_id=getattr(query.message, "message_id", None),
                )
            elif action == "setupstep3":
                # Backward-compatible CTA and forward from stage 2/4.
                await show_payment_setup_step(
                    bot, user_id, actor_id, project_id,
                    source_message_id=getattr(query.message, "message_id", None),
                )
            elif action.startswith("connrename:"):
                target_id = UUID(hex=action.split(":", 1)[1])
                if target_id != project_id:
                    raise ValueError("Меню другого подключения. Откройте список заново.")
                await get_connection_disposition(
                    factory, actor_id=actor_id, project_id=target_id,
                )
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=target_id,
                )
                username = next((
                    name for _, name, status in data.bot_instances
                    if status == "registered"
                ), None)
                await prompt(
                    bot, user_id, actor_id, step="connection_rename",
                    text=rename_input_card(username=username, rich=False),
                    rich_html=rename_input_card(username=username, rich=True),
                    show_main_menu=False,
                )
            elif action.startswith(("connremove:", "connremoveok:",
                                    "connarchive:", "connarchiveok:")):
                verb, token = action.split(":", 1)
                target_id = UUID(hex=token)
                if target_id != project_id:
                    raise ValueError("Меню другого подключения. Откройте список заново.")
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=target_id,
                )
                if not data.can_delete_bot:
                    raise PermissionError("Действие доступно только владельцу подключения")
                if verb == "connremove":
                    disposition = await get_connection_disposition(
                        factory, actor_id=actor_id, project_id=target_id,
                    )
                    if disposition.has_bot:
                        raise ValueError(
                            "Сначала отключите бота через управление подключением."
                        )
                    if disposition.has_history:
                        raise ValueError(
                            "История платежей и доступов не подлежит удалению. "
                            "Для этого проекта используйте архив."
                        )
                    await send_owner_rich(
                        bot, chat_id=user_id,
                        rich_html=project_disposal_card(
                            name=data.project_name, archive=False, rich=True,
                        ),
                        classic_html=project_disposal_card(
                            name=data.project_name, archive=False, rich=False,
                        ),
                        reply_markup=_markup([
                            [_button(
                                tr("🗑 Да, удалить подключение",
                                   "🗑 Yes, delete connection"),
                                "connremoveok:" + target_id.hex, "danger",
                            )],
                            [_button(
                                tr("⬅️ Отмена", "⬅️ Cancel"),
                                "bots", "primary",
                            )],
                        ]),
                    )
                elif verb == "connremoveok":
                    await remove_unused_connection(
                        factory, actor_id=actor_id, project_id=target_id,
                    )
                    # One complete confirmation, no additional unsolicited
                    # connections list after a successful destructive action.
                    await send_owner_rich(
                        bot, chat_id=user_id,
                        rich_html=project_disposal_success(
                            name=data.project_name, archive=False, rich=True,
                        ),
                        classic_html=project_disposal_success(
                            name=data.project_name, archive=False, rich=False,
                        ),
                        reply_markup=_markup([[
                            _button(
                                tr("📂 Мои подключения", "📂 My connections"),
                                "projects", "primary",
                            ),
                            _button(
                                tr("🏠 Главное меню", "🏠 Main menu"),
                                "onboardhome", "primary",
                            ),
                        ]]),
                    )
                elif verb == "connarchive":
                    disposition = await get_connection_disposition(
                        factory, actor_id=actor_id, project_id=target_id,
                    )
                    if disposition.has_bot:
                        raise ValueError(tr(
                            "Сначала отключите бота в управлении подключением.",
                            "Disconnect the bot in connection management first.",
                        ))
                    await send_owner_rich(
                        bot, chat_id=user_id,
                        rich_html=project_disposal_card(
                            name=data.project_name, archive=True, rich=True,
                        ),
                        classic_html=project_disposal_card(
                            name=data.project_name, archive=True, rich=False,
                        ),
                        reply_markup=_markup([
                            [_button(
                                tr("📦 Да, архивировать", "📦 Yes, archive"),
                                "connarchiveok:" + target_id.hex, "danger",
                            )],
                            [_button(
                                tr("⬅️ Отмена", "⬅️ Cancel"),
                                "bots", "primary",
                            )],
                        ]),
                    )
                else:
                    await archive_owner_connection(
                        factory, actor_id=actor_id, project_id=target_id,
                    )
                    await send_owner_rich(
                        bot, chat_id=user_id,
                        rich_html=project_disposal_success(
                            name=data.project_name, archive=True, rich=True,
                        ),
                        classic_html=project_disposal_success(
                            name=data.project_name, archive=True, rich=False,
                        ),
                        reply_markup=_markup([[
                            _button(
                                tr("📦 Архив подключений",
                                   "📦 Archived connections"),
                                "archives", "primary",
                            ),
                            _button(
                                tr("🏠 Главное меню", "🏠 Main menu"),
                                "onboardhome", "primary",
                            ),
                        ]]),
                    )
            elif action == "network":
                await show_network_profile(bot, user_id, actor_id, project_id)
            elif action == "networkask":
                await bot.send_message(
                    user_id,
                    tr(
                        "📁 <b>Сохранить настройки для новых ботов?</b>\n\n"
                        "Копируются тарифы, сроки, цены, способы оплаты и валюты. "
                        "Тарифы будут черновиками, оплата выключена.\n\n"
                        "Выберите, копировать ли также банковские реквизиты. "
                        "Автоприменение к новым ботам включится сразу.",
                        "📁 <b>Save settings for future bots?</b>\n\n"
                        "Plans, durations, prices, methods and currencies are copied. "
                        "Plans remain drafts; payments are disabled.\n\n"
                        "Choose whether to include bank details. "
                        "Auto-apply to future bots will be enabled.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("✅ С реквизитами", "✅ Include bank details"),
                            "networksave:yes", "success",
                        )],
                        [_button(
                            tr("✅ Без реквизитов", "✅ Without bank details"),
                            "networksave:no",
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"), "network")],
                    ]),
                )
            elif action.startswith("networksave:"):
                choice = action.split(":", 1)[1]
                if choice not in ("yes", "no"):
                    raise ValueError("Некорректный выбор профиля")
                saved = await save_owner_network_profile(
                    factory, actor_id=actor_id, source_project_id=project_id,
                    copy_instructions=(choice == "yes"),
                )
                await bot.send_message(
                    user_id,
                    tr("✅ Профиль сохранён: ", "✅ Profile saved: ")
                    + str(saved.plan_count)
                    + tr(" тарифов и ", " plans and ")
                    + str(saved.method_count)
                    + tr(" способов оплаты.", " payment methods."),
                )
                await show_network_profile(bot, user_id, actor_id, project_id)
            elif action in ("networkoff", "networkon"):
                await set_network_auto_apply(
                    factory, actor_id=actor_id, enabled=action == "networkon",
                )
                await show_network_profile(bot, user_id, actor_id, project_id)
            elif action == "reviews":
                await show_reviews(bot, user_id, actor_id, project_id)
            elif action == "preview":
                await show_guest_preview(bot, user_id, actor_id, project_id)
            elif action.startswith("pvcat:"):
                _, currency, page_token = action.split(":", 2)
                if not currency.isascii() or not currency.isalnum() or (
                    not page_token.isascii() or not page_token.isdecimal()
                    or int(page_token) > 10000
                ):
                    raise ValueError("Недопустимая страница")
                await show_guest_preview(
                    bot, user_id, actor_id, project_id,
                    currency=currency, page=int(page_token),
                    message_id=query.message.message_id,
                )
            elif action.startswith("pvoffer:"):
                _, price_token, page_token = action.split(":", 2)
                if not page_token.isascii() or not page_token.isdecimal() or (
                    int(page_token) > 10000
                ):
                    raise ValueError("Недопустимая страница")
                await show_guest_preview(
                    bot, user_id, actor_id, project_id,
                    price_id=decode_uuid(price_token), page=int(page_token),
                    message_id=query.message.message_id,
                )
            elif action.startswith("pvmethod:"):
                _, price_token, method_token, page_token = action.split(":", 3)
                if not page_token.isascii() or not page_token.isdecimal() or (
                    int(page_token) > 10000
                ):
                    raise ValueError("Недопустимая страница")
                await show_guest_preview(
                    bot, user_id, actor_id, project_id,
                    price_id=decode_uuid(price_token),
                    method_id=decode_uuid(method_token), page=int(page_token),
                    message_id=query.message.message_id,
                )
            elif action.startswith("pvi:"):
                _, price_token, method_token, page_token, part_token = (
                    action.split(":", 4)
                )
                if not all(
                    part.isascii() and part.isdecimal()
                    for part in (page_token, part_token)
                ) or int(page_token) > 10000 or int(part_token) > 1000:
                    raise ValueError("Недопустимая страница")
                await show_guest_preview(
                    bot, user_id, actor_id, project_id,
                    price_id=decode_uuid(price_token),
                    method_id=decode_uuid(method_token), page=int(page_token),
                    instruction_page=int(part_token),
                    message_id=query.message.message_id,
                )
            elif action.startswith("drop:"):
                _, project_ref, instance_ref = action.split(":", 2)
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr("Меню другого проекта. Откройте «Мои боты» снова.",
                                        "Another project menu. Reopen My bots."))
                instance_id = decode_uuid(instance_ref)
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                name = next((
                    username for bot_id, username, status in data.bot_instances
                    if bot_id == instance_id and status != "deleting"
                ), None)
                if name is None or not data.can_delete_bot:
                    raise PermissionError(tr("Этот бот не доступен для удаления",
                                            "This bot cannot be removed."))
                blockers = (await list_owner_obligations(
                    factory, actor_id=actor_id, project_id=project_id,
                )).blockers
                # Never show a destructive confirmation when the current
                # owner-scoped snapshot still has protected buyer obligations.
                rows = []
                if not blockers.has_blockers:
                    rows.append([_button(
                        tr("🗑 Да, отключить бота",
                           "🗑 Yes, disconnect bot"),
                        "dropok:" + project_ref + ":" + instance_ref,
                        "danger",
                    )])
                else:
                    rows.append([_button(
                        tr("👥 Покупатели и доступы",
                           "👥 Customers and access"),
                        "oblist", "primary",
                    )])
                rows.append([_button(
                    tr("⬅️ Отмена", "⬅️ Cancel"), "bots", "primary",
                )])
                await send_owner_rich(
                    bot, chat_id=user_id,
                    rich_html=disconnect_card(
                        username=name, blockers=blockers, rich=True,
                    ),
                    classic_html=disconnect_card(
                        username=name, blockers=blockers, rich=False,
                    ),
                    reply_markup=_markup(rows),
                )
            elif action.startswith("dropok:"):
                _, project_ref, instance_ref = action.split(":", 2)
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr("Проект изменился. Откройте «Мои боты» снова.",
                                        "Project changed. Reopen My bots."))
                try:
                    requested = await request_bot_retirement(
                        factory, actor_id=actor_id,
                        project_id=project_id,
                        bot_instance_id=decode_uuid(instance_ref),
                    )
                except ValueError as exc:
                    # Include the entire safety explanation; Telegram alert
                    # callbacks are limited in length and would truncate it.
                    await bot.send_message(user_id, tr(
                        "⚠️ " + str(exc),
                        "⚠️ Bot removal is blocked by outstanding obligations. "
                        "Review and resolve each obligation below.",
                    ))
                    await show_obligations(bot, user_id, actor_id, project_id)
                    await query.answer(tr("Удаление пока недоступно",
                                          "Bot removal is blocked"), show_alert=True)
                    return
                await bot.send_message(
                    user_id,
                    (
                        tr(
                            "✅ Удаление запрошено. Бот прекратит обработку покупателей, "
                            "а после безопасной остановки ViMi Pay удалит его "
                            "данные подключения.\n\n"
                            "Нажмите «🔄 Обновить статус» в разделе «Мои боты». "
                            "Когда бот исчезнет из списка, его можно подключить "
                            "заново или создать новый проект с нуля.",
                            "✅ Bot removal requested. Buyer processing will stop, "
                            "then ViMi Pay will safely remove the connection.\n\n"
                            "Use Refresh status in My bots. Once removed, "
                            "you can reconnect it or start a new project.",
                        ) if requested else tr(
                            "Этот бот уже не подключён. Обновите список.",
                            "This bot is already disconnected. Refresh the list.",
                        )
                    ),
                )
                await show_bots(bot, user_id, actor_id, project_id)
            elif action.startswith(("orphanask:", "orphanok:")):
                kind, project_ref, instance_ref = action.split(":", 2)
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr(
                        "Меню другого подключения. Откройте «Мои боты» снова.",
                        "Different connection. Reopen My bots.",
                    ))
                instance_id = decode_uuid(instance_ref)
                overview = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                if (
                    not overview.can_delete_bot
                    or instance_id not in {
                        ident for ident, _, status in overview.bot_instances
                        if status != "deleting"
                    }
                ):
                    raise PermissionError(tr(
                        "Отключение доступно только владельцу действующего подключения",
                        "Only the owner may retire the connected bot",
                    ))
                if kind == "orphanask":
                    snapshot = await list_owner_obligations(
                        factory, actor_id=actor_id, project_id=project_id,
                    )
                    blockers = snapshot.blockers
                    if (
                        not blockers.access_actions
                        or any((
                            blockers.requests, blockers.subscriptions,
                            blockers.grants, blockers.access, blockers.join_tickets,
                        ))
                    ):
                        raise ValueError(tr(
                            "Есть другие обязательства. Обновите список.",
                            "Other obligations remain. Refresh the list.",
                        ))
                    await bot.send_message(
                        user_id,
                        tr(
                            "⚠️ <b>Отключить недоступного бота?</b>\n\n"
                            "Используйте только если бот окончательно удалён "
                            "в BotFather или больше не может выполнять команды.\n\n"
                            "Незавершённые команды будут отмечены как "
                            "<b>прекращённые без подтверждения выполнения</b>, "
                            "а не успешно обработанные. "
                            "ViMi Pay НЕ сможет подтвердить фактический доступ "
                            "пользователей к каналам. Действующие оплаты, "
                            "подписки и права доступа не затрагиваются. "
                            "История останется для архива.\n\n"
                            "Продолжить?",
                            "⚠️ <b>Retire unreachable bot?</b>\n\n"
                            "Use only if the bot was permanently deleted "
                            "in BotFather or can no longer process commands.\n\n"
                            "Pending commands will be recorded as "
                            "<b>abandoned without execution confirmation</b>, "
                            "never as successful. "
                            "ViMi Pay CANNOT confirm actual channel membership. "
                            "Active payments, subscriptions and grants are "
                            "not changed. History remains for archiving.\n\n"
                            "Continue?",
                        ),
                        parse_mode="HTML",
                        reply_markup=_markup([
                            [_button(
                                tr("🗑 Подтвердить отключение",
                                   "🗑 Confirm retirement"),
                                "orphanok:" + project_ref + ":" + instance_ref,
                                "danger",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"), "oblist")],
                        ]),
                    )
                else:
                    ended = await retire_unreachable_bot_with_orphan_actions(
                        factory, actor_id=actor_id, project_id=project_id,
                        bot_instance_id=instance_id,
                    )
                    await bot.send_message(
                        user_id, tr(
                            "✅ Отключение запущено. Незавершённых команд "
                            "прекращено: ",
                            "✅ Retirement started. Pending commands retired: ",
                        ) + str(ended) + tr(
                            ".\nФинансовая история и неподтверждённое "
                            "состояние Telegram-доступа сохранены. "
                            "После остановки бота подключение с историей "
                            "можно переместить в архив.",
                            ".\nFinancial history and unverified Telegram "
                            "access state are preserved. After the bot stops, "
                            "the connection can be archived.",
                        ),
                    )
                    await show_bots(bot, user_id, actor_id, project_id)
            elif action == "oblist" or action.startswith("oblist:"):
                section = (action.split(":", 1)[1]
                           if action.startswith("oblist:") else "overview")
                await show_obligations(
                    bot, user_id, actor_id, project_id, section=section,
                )
            elif action.startswith("obpage:"):
                fields = action.split(":")
                if len(fields) == 3:
                    _, project_ref, page_number = fields
                    section = "overview"  # Stale old keyboard: go to summary.
                elif len(fields) == 4:
                    _, project_ref, section, page_number = fields
                else:
                    raise ValueError(tr("Недопустимая страница", "Invalid page"))
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr("Открыто меню другого проекта",
                                        "Another project menu is open."))
                if not page_number.isascii() or not page_number.isdecimal():
                    raise ValueError(tr("Недопустимая страница", "Invalid page"))
                await show_obligations(
                    bot, user_id, actor_id, project_id,
                    page=int(page_number), section=section,
                )
            elif action.startswith(("ob:", "obok:")):
                parts = action.split(":")
                if len(parts) not in (4, 5) or parts[1] not in (
                    "pay", "sub", "grant", "sync"
                ):
                    raise ValueError(tr("Неизвестное действие", "Unknown action"))
                verb, kind, project_ref, object_ref = parts[:4]
                page = int(parts[4]) if len(parts) == 5 else 0
                if decode_uuid(project_ref) != project_id:
                    raise ValueError(tr("Открыто меню другого проекта. Обновите обязательства.",
                                        "Another project menu. Refresh obligations."))
                object_id = decode_uuid(object_ref)
                snapshot = await list_owner_obligations(
                    factory, actor_id=actor_id, project_id=project_id,
                    page=page,
                )
                active_ids = {
                    "pay": {item.id for item in snapshot.payments},
                    "sub": {item.id for item in snapshot.subscriptions},
                    "grant": {item.id for item in snapshot.grants
                              if item.source not in ("payment", "stars_payment")},
                    "sync": {item.id for item in snapshot.access},
                }
                if verb == "ob":
                    if object_id not in active_ids[kind]:
                        raise ValueError(tr("Действие уже неактуально. Обновите обязательства.",
                                        "Action is stale. Refresh obligations."))
                    warnings = {
                        "pay": tr(
                            "Проверьте поступление денег в банке. "
                            "Отклонение или отмена заявки не возвращает деньги.",
                            "Verify the bank transfer. Rejecting or closing "
                            "a payment request does not refund money.",
                        ),
                        "sub": tr(
                            "Вы досрочно закрываете оплаченный срок! "
                            "Возврат денег ViMi Pay не выполняет. "
                            "Урегулируйте вопрос с покупателем самостоятельно.",
                            "You are ending a paid term early. ViMi Pay "
                            "does not issue refunds. Resolve this with the buyer.",
                        ),
                        "grant": tr(
                            "Это отдельное право доступа. "
                            "Проверьте, не является ли оно служебным или бессрочным.",
                            "This is a separate access grant. Check whether it "
                            "is permanent or used for account management.",
                        ),
                        "sync": tr(
                            "ViMi Pay перепроверит права и отправит действие "
                            "в Telegram. Доступ не будет удалён при других действующих правах.",
                            "ViMi Pay will recalculate access and send the action "
                            "to Telegram. Other valid grants still preserve access.",
                        ),
                    }
                    await bot.send_message(
                        user_id, tr("⚠️ <b>Подтвердите действие</b>\n\n",
                                   "⚠️ <b>Confirm this action</b>\n\n")
                        + escape(warnings[kind]),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(
                                tr("✅ Подтверждаю действие",
                                   "✅ Confirm action"),
                                "obok:" + kind + ":" + project_ref + ":" + object_ref
                                + (":" + str(page) if page else ""),
                                "danger" if kind != "sync" else "success",
                            )],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "obpage:" + project_ref + ":"
                                     + kind + ":" + str(page))],
                        ]),
                    )
                else:
                    if object_id not in active_ids[kind]:
                        raise ValueError(tr("Действие уже выполнено или устарело",
                                        "Action was completed or has expired."))
                    if kind == "pay":
                        change = await close_owner_payment(
                            factory, actor_id=actor_id, project_id=project_id,
                            request_id=object_id,
                        )
                    elif kind == "sub":
                        change = await end_owner_subscription(
                            factory, actor_id=actor_id, project_id=project_id,
                            subscription_id=object_id,
                        )
                    elif kind == "grant":
                        change = await revoke_owner_grant(
                            factory, actor_id=actor_id, project_id=project_id,
                            grant_id=object_id,
                        )
                    else:
                        message = await retry_owner_access(
                            factory, actor_id=actor_id, project_id=project_id,
                            state_id=object_id,
                        )
                        await bot.send_message(user_id, "🔄 " + obligation_message(message))
                        change = None
                    if change is not None:
                        await bot.send_message(
                            user_id, "✅ " + (
                                tr("Состояние сохранено", "Change saved")
                                if change.changed else obligation_message(change.message)
                            ),
                        )
                        await notify_buyer(bot, user_id, actor_id, project_id, change)
                    await show_obligations(
                        bot, user_id, actor_id, project_id,
                        page=page, section=kind,
                    )
            elif action == "bots":
                await show_bots(bot, user_id, actor_id, project_id)
            elif action == "connect":
                if get_settings().bot_token_encryption_key is None:
                    raise ValueError(tr("Подключение ботов временно недоступно",
                                        "Bot connections are temporarily unavailable"))
                overview = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                username = next((
                    name for _, name, status in overview.bot_instances
                    if status == "registered"
                ), None)
                await prompt(
                    bot, user_id, actor_id, step="bot_token",
                    text=token_input_card(username=username, rich=False),
                    rich_html=token_input_card(username=username, rich=True),
                    show_main_menu=False,
                )
            elif action == "resources":
                await show_resources(
                    bot, user_id, actor_id, project_id,
                    source_message_id=getattr(query.message, "message_id", None)
                    if fixed_project_id is not None else None,
                )
            elif action.startswith("resourceview:"):
                resource_id = decode_uuid(action.split(":", 1)[1])
                await show_resource_card(
                    bot, user_id, actor_id, project_id, resource_id,
                )
            elif action.startswith(("resremoveask:", "resremoveok:")):
                verb, token = action.split(":", 1)
                resource_id = decode_uuid(token)
                metrics = await read_channel_insights(
                    factory, actor_id=actor_id, project_id=project_id,
                    resource_id=resource_id,
                )
                if metrics.paid_orders:
                    raise ValueError(tr(
                        "Удаление запрещено: есть подтверждённые покупки.",
                        "Cannot delete: confirmed purchases exist.",
                    ))
                if verb == "resremoveask":
                    await bot.send_message(
                        user_id,
                        tr("🗑 <b>Удалить канал из ViMi Pay?</b>\n",
                           "🗑 <b>Remove channel from ViMi Pay?</b>\n")
                        + "<b>" + escape(metrics.name) + "</b>\n\n"
                        + tr(
                            "Канал исчезнет из панели управления, "
                            "а связанные тарифы — из новых продаж. "
                            "Неоплаченные счета сохранятся в истории и "
                            "не блокируют удаление. Отправленные чеки "
                            "по-прежнему можно проверить; доступ тех, "
                            "кто оплатил до удаления, не нарушается. "
                            "Сам канал в Telegram не удаляется.",
                            "The channel will disappear from the management "
                            "panel and linked plans from new sales. "
                            "Unpaid invoices stay in history and do not block "
                            "removal. Submitted receipts remain reviewable; "
                            "already-paid access stays protected. "
                            "The Telegram channel itself is not deleted.",
                        ),
                        parse_mode="HTML", reply_markup=_markup([
                            [_button(tr("🗑 Да, удалить", "🗑 Confirm removal"),
                                     "resremoveok:" + token, "danger")],
                            [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                     "resourceview:" + token)],
                        ]),
                    )
                else:
                    name, hidden_plans = await remove_channel_without_paid_purchases(
                        factory, actor_id=actor_id, project_id=project_id,
                        resource_id=resource_id,
                    )
                    await bot.send_message(
                        user_id,
                        tr("✅ Канал удалён из управления: ",
                           "✅ Channel removed from management: ")
                        + escape(name) + "\n"
                        + tr("Скрыто тарифов: ", "Plans removed from sale: ")
                        + str(hidden_plans),
                        parse_mode="HTML",
                    )
                    await show_resources(bot, user_id, actor_id, project_id)
            elif action.startswith("pick:"):
                kind = action.split(":", 1)[1]
                if kind not in ("channel", "group"):
                    raise ValueError(tr("Неизвестный тип чата", "Unsupported chat type"))
                request_id = randbelow(2**31 - 2) + 1
                guided = await guided_channel_selection(actor_id, project_id)
                generation = await set_flow(
                    factory, actor_id=actor_id, step="chat_picker",
                    payload={
                        "request_id": request_id, "kind": kind,
                        "guided": guided,
                    },
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                # request_chat can only be shown on a native reply keyboard;
                # Rich inline buttons cannot invoke Telegram's picker.
                # Use one fully formatted Telegram HTML message with
                # exactly two native actions: Select chat and Back.
                picker_message = await bot.send_message(
                    user_id,
                    picker_classic(
                        kind, await connected_bot_handle(actor_id, project_id),
                    ),
                    parse_mode="HTML",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[
                            [KeyboardButton(
                                text=(
                                    tr("📢 Выбрать канал", "📢 Select channel")
                                    if kind == "channel" else
                                    tr("👥 Выбрать группу", "👥 Select group")
                                ),
                                request_chat=KeyboardButtonRequestChat(
                                    request_id=request_id,
                                    chat_is_channel=(kind == "channel"),
                                    request_title=True,
                                ),
                            )],
                            [KeyboardButton(text=tr("⬅️ Назад", "⬅️ Back"))],
                        ],
                        resize_keyboard=True,
                        one_time_keyboard=True,
                    ),
                )
                try:
                    # Bind the bot-owned picker message to this exact flow.
                    await bind_prompt(
                        factory, actor_id=actor_id, step="chat_picker",
                        message_id=picker_message.message_id,
                        generation=generation, bot_scope_id=bot.id,
                    )
                except ValueError:
                    # A newer flow replaced it before binding; clean up
                    # only the stale instruction message, not the new flow.
                    try:
                        await bot.delete_message(
                            user_id, picker_message.message_id,
                        )
                    except (AiogramError, TypeError):
                        pass
                    raise
            elif action == "resource:manual":
                username = await connected_bot_handle(actor_id, project_id)
                await prompt(
                    bot, user_id, actor_id, step="resource_reference",
                    text=manual_classic(username),
                    rich_html=manual_rich(username),
                    payload={
                        "guided": await guided_channel_selection(
                            actor_id, project_id,
                        ),
                    },
                    show_main_menu=False,
                )
            elif action == "plans":
                await show_plans(bot, user_id, actor_id, project_id)
            elif action.startswith(("bulks:", "bulkok:")):
                parts = action.split(":")
                verb = parts[0]
                if (
                    len(parts) != (4 if verb == "bulks" else 5)
                    or parts[1] not in ("plan", "method")
                    or parts[2] not in ("on", "off")
                    or decode_uuid(parts[3]) != project_id
                ):
                    raise ValueError(tr(
                        "Меню другого подключения. Откройте раздел заново.",
                        "Different connection menu. Reopen this section.",
                    ))
                kind, enable = parts[1], parts[2] == "on"
                state = await read_bulk_sales_state(
                    factory, actor_id=actor_id, project_id=project_id,
                    kind=kind,
                )
                if not state.available or state.enable_action != enable:
                    raise ValueError(tr(
                        "Настройки изменились. Откройте раздел заново.",
                        "Settings changed. Reopen this section.",
                    ))
                if verb == "bulks":
                    total = state.paused if enable else state.active
                    subject = (
                        tr("все тарифы", "all plans") if kind == "plan"
                        else tr("все способы оплаты", "all payment methods")
                    )
                    action_label = (
                        tr("включить", "enable") if enable
                        else tr("выключить", "disable")
                    )
                    explanation = tr(
                        "Черновики не публикуются автоматически. "
                        "Для включения все архивные тарифы должны иметь "
                        "действующий канал, цену и оплату. "
                        "Если хотя бы один не готов, изменения отменяются.\n\n",
                        "Drafts are never published automatically. "
                        "Every archived plan needs an active channel, price "
                        "and payment method. If any is unready, nothing changes.\n\n",
                    ) if kind == "plan" and enable else ""
                    await bot.send_message(
                        user_id,
                        tr("⚙️ <b>Массовое управление</b>\n\n",
                           "⚙️ <b>Bulk management</b>\n\n")
                        + action_label.capitalize() + " " + subject + "?\n"
                        + tr("Будет изменено: ", "Affected: ") + str(total)
                        + ".\n\n" + explanation
                        + tr(
                            "Изменятся только новые продажи. "
                            "Действующие подписки, заявки и история платежей "
                            "сохранятся.",
                            "Only future purchases are affected. "
                            "Existing subscriptions, pending requests and "
                            "payment history stay unchanged.",
                        ),
                        parse_mode="HTML",
                        reply_markup=_markup([
                            [_button(
                                tr("✅ Подтвердить", "✅ Confirm"),
                                "bulkok:" + kind + ":"
                                + ("on:" if enable else "off:")
                                + encode_uuid(project_id) + ":" + state.revision,
                                "success" if enable else "danger",
                            )],
                            [_button(
                                tr("⬅️ Отмена", "⬅️ Cancel"),
                                "plans" if kind == "plan" else "methods",
                            )],
                        ]),
                    )
                else:
                    changed = await switch_all_sales(
                        factory, actor_id=actor_id, project_id=project_id,
                        kind=kind, enable=enable, expected_revision=parts[4],
                    )
                    await bot.send_message(
                        user_id,
                        tr("✅ Изменено: ", "✅ Updated: ") + str(changed),
                    )
                    if kind == "plan":
                        await show_plans(bot, user_id, actor_id, project_id)
                    else:
                        await show_methods(bot, user_id, actor_id, project_id)
            elif action == "newplan":
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                if not any(status == "active" for _, _, status in data.resources):
                    raise ValueError(tr("Сначала подключите действующий канал или группу",
                                        "Connect an active channel or group first"))
                generation = await set_flow(
                    factory, actor_id=actor_id, step="plan_duration",
                    payload={"name": ""}, project_id=fixed_project_id,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                await show_duration_picker(bot, user_id, generation)
            elif action.startswith("flowback:"):
                # A stale keyboard must never reset a newer wizard.
                _, target, generation = action.split(":", 2)
                if target not in ("plans", "duration"):
                    raise ValueError(tr("Шаг настройки устарел",
                                        "This setup step has expired"))
                flow = await consume_button_flow(
                    factory, actor_id=actor_id,
                    expected_step=(
                        "plan_duration" if target == "plans" else "plan_resource"
                    ),
                    generation=generation,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                    discard=(target == "plans"),
                )
                if flow.project_id != project_id:
                    raise ValueError(tr(
                        "Проект изменился. Повторите действие.",
                        "Project changed. Please retry.",
                    ))
                if target == "plans":
                    # The button step and generation are already invalidated
                    # atomically by consume_button_flow(discard=True).
                    await show_plans(bot, user_id, actor_id, project_id)
                else:
                    fresh = await advance_flow(
                        factory, actor_id=actor_id, source=flow,
                        step="plan_duration",
                        payload={"name": flow.payload["name"]},
                        bot_scope_id=bot.id if fixed_project_id else 0,
                    )
                    await show_duration_picker(bot, user_id, fresh)
            elif action.startswith("planname:"):
                generation = action.split(":", 1)[1]
                flow = await consume_button_flow(
                    factory, actor_id=actor_id, expected_step="plan_duration",
                    generation=generation,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                if flow.project_id != project_id:
                    raise ValueError("Проект изменился. Повторите действие.")
                await prompt(
                    bot, user_id, actor_id, step="plan_name", source_flow=flow,
                    text=tr(
                        "✏️ Введите название тарифа (2–120 символов). "
                        "Для тарифа без названия вернитесь назад и "
                        "выберите срок.",
                        "✏️ Enter an optional plan name (2–120 characters). "
                        "For an unnamed plan, go Back and choose its duration.",
                    ),
                )
            elif action.startswith("duration:"):
                _, key, generation = action.split(":", 2)
                if key not in DURATIONS:
                    raise ValueError(tr("Неподдерживаемый срок", "Unsupported duration"))
                flow = await consume_button_flow(
                    factory, actor_id=actor_id,
                    expected_step="plan_duration", generation=generation,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                if flow.project_id != project_id:
                    raise ValueError(tr("Проект изменился. Повторите действие.",
                                            "Project changed. Please retry."))
                value, unit, _ = DURATIONS[key]
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id
                )
                resources = [
                    rid for rid, _, status in data.resources if status == "active"
                ]
                if not resources:
                    raise ValueError(tr("Сначала подключите канал", "Connect a channel first"))
                if len(resources) == 1:
                    plan_id = await create_owner_plan(
                        factory, actor_id=actor_id, project_id=project_id,
                        name=flow.payload["name"], duration_value=value,
                        duration_unit=unit, resource_id=resources[0],
                    )
                    await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
                else:
                    generation = await advance_flow(
                        factory, actor_id=actor_id, source=flow,
                        step="plan_resource",
                        payload={"name": flow.payload["name"], "value": value, "unit": unit},
                        bot_scope_id=bot.id if fixed_project_id else 0,
                    )
                    rows = [
                        [_button(name[:45], "use:" + rid.hex + ":" + generation)]
                        for rid, name, status in data.resources if status == "active"
                    ]
                    await bot.send_message(
                        user_id, tr("📢 Какой канал откроет этот тариф?",
                                     "📢 Which channel does this plan unlock?"),
                        reply_markup=_markup(
                            rows + [
                                [_button(
                                    tr("⬅️ Выбор срока", "⬅️ Choose duration"),
                                    "flowback:duration:" + generation,
                                )],
                                [_button("✖️ Отмена", "cancel")],
                            ],
                        ),
                    )
            elif action.startswith("use:"):
                _, resource_hex, generation = action.split(":", 2)
                flow = await consume_button_flow(
                    factory, actor_id=actor_id,
                    expected_step="plan_resource", generation=generation,
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                if flow.project_id != project_id:
                    raise ValueError(tr("Проект изменился. Повторите действие.",
                                            "Project changed. Please retry."))
                plan_id = await create_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    name=flow.payload["name"], duration_value=flow.payload["value"],
                    duration_unit=flow.payload["unit"],
                    resource_id=UUID(hex=resource_hex),
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("plan:"):
                await show_plan_card(
                    bot, user_id, actor_id, project_id,
                    UUID(hex=action.split(":", 1)[1]),
                )
            elif action.startswith("plrename:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                data = await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                warning = tr(
                    "\n⚠️ Новое название также будет видно в истории покупок.",
                    "\n⚠️ The new name will also appear in purchase history.",
                ) if data.has_history else ""
                await prompt(
                    bot, user_id, actor_id, step="plan_rename",
                    payload={"plan_id": plan_id.hex},
                    text=tr(
                        "✏️ <b>Переименовать тариф</b>\n\n"
                        "Введите новое название (2–120 символов).",
                        "✏️ <b>Rename plan</b>\n\n"
                        "Enter the new name (2–120 characters).",
                    ) + warning,
                )
            elif action.startswith("plduration:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                await show_plan_duration_editor(
                    bot, user_id, actor_id, project_id, plan_id,
                )
            elif action.startswith("plsetperiod:"):
                _, token, key = action.split(":", 2)
                if key not in DURATIONS:
                    raise ValueError(tr("Недопустимый срок", "Invalid duration"))
                plan_id = decode_uuid(token)
                value, unit, _ = DURATIONS[key]
                await edit_owner_draft_duration(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, duration_value=value, duration_unit=unit,
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("plresource:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                await show_plan_resource_editor(
                    bot, user_id, actor_id, project_id, plan_id,
                )
            elif action.startswith("plsetres:"):
                _, plan_token, resource_token = action.split(":", 2)
                plan_id = decode_uuid(plan_token)
                await edit_owner_draft_resource(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, resource_id=decode_uuid(resource_token),
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("plarchive:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                info = await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                if info.status != "active":
                    raise ValueError("Снять с продажи можно только активный тариф")
                await bot.send_message(
                    user_id,
                    tr(
                        "⏸️ <b>Снять тариф с продажи?</b>\n\n"
                        "Покупатели больше не смогут выбрать его для новых заявок. "
                        "Существующие подписки, уже созданные заявки и "
                        "история платежей останутся без изменений.",
                        "⏸️ <b>Stop selling this plan?</b>\n\n"
                        "Buyers will no longer be able to select it for new "
                        "requests. Existing subscriptions, payment requests "
                        "and payment history will remain unchanged.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("⏸️ Да, снять с продажи", "⏸️ Confirm stop"),
                            "plarchok:" + encode_uuid(plan_id) + ":" + info.revision,
                            "danger",
                        )],
                        [_button(
                            tr("⬅️ Оставить тариф", "⬅️ Keep plan"),
                            "plan:" + plan_id.hex,
                        )],
                    ]),
                )
            elif action.startswith("plarchok:"):
                _, token, revision = action.split(":", 2)
                plan_id = decode_uuid(token)
                await archive_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, expected_revision=revision,
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("plrestore:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                info = await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                if info.status != "archived":
                    raise ValueError("Тариф изменился. Откройте карточку заново.")
                await bot.send_message(
                    user_id,
                    tr(
                        "▶️ <b>Вернуть тариф в продажу?</b>\n\n"
                        "Он снова появится в каталоге, если цены и оплата настроены.",
                        "▶️ <b>Resume selling this plan?</b>\n\n"
                        "It will reappear in the catalog if prices and payments are ready.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("✅ Возобновить продажи", "✅ Resume sales"),
                            "plrestoreok:" + encode_uuid(plan_id) + ":" + info.revision,
                            "success",
                        )],
                        [_button(
                            tr("⬅️ Оставить в архиве", "⬅️ Keep archived"),
                            "plan:" + plan_id.hex,
                        )],
                    ]),
                )
            elif action.startswith("plrestoreok:"):
                _, token, revision = action.split(":", 2)
                plan_id = decode_uuid(token)
                await publish_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, expected_status="archived",
                    expected_revision=revision,
                )
                await show_plan_card(bot, user_id, actor_id, project_id, plan_id)
            elif action.startswith("pldelete:"):
                plan_id = decode_uuid(action.split(":", 1)[1])
                info = await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                if info.has_purchase:
                    raise ValueError(tr(
                        "У тарифа есть подтверждённые покупки. "
                        "Удаление запрещено: доступ покупателей должен сохраниться.",
                        "This plan has confirmed purchases. "
                        "Removal is blocked to preserve buyer access.",
                    ))
                status_code = {"draft": "d", "active": "a", "archived": "r"}[
                    info.status
                ]
                await bot.send_message(
                    user_id,
                    tr(
                        "🗑 <b>Удалить тариф?</b>\n\n",
                        "🗑 <b>Delete this plan?</b>\n\n",
                    )
                    + "<b>" + escape(info.name) + "</b>\n\n"
                    + tr(
                        "При отсутствии счетов тариф удаляется полностью. "
                        "Неоплаченные счета не блокируют удаление: тариф "
                        "скрывается из каталога, но история сохраняется. "
                        "Подтверждённые покупки блокируют удаление.",
                        "Plans with no invoices are deleted permanently. "
                        "Unpaid invoices don't block removal: the plan is "
                        "hidden while invoices stay in history. "
                        "Confirmed purchases block deletion.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("🗑 Да, удалить тариф", "🗑 Yes, delete plan"),
                            "pldelok:" + encode_uuid(plan_id) + ":" + status_code
                            + ":" + info.revision,
                            "danger",
                        )],
                        [_button(
                            tr("⬅️ Отмена", "⬅️ Cancel"),
                            "plan:" + plan_id.hex,
                        )],
                    ]),
                )
            elif action.startswith("pldelok:"):
                _, token, code, revision = action.split(":", 3)
                expected = {"d": "draft", "a": "active", "r": "archived"}.get(
                    code
                )
                if expected is None:
                    raise ValueError("Тариф изменился. Откройте карточку заново.")
                await delete_unused_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=decode_uuid(token), expected_status=expected,
                    expected_revision=revision,
                )
                await bot.send_message(
                    user_id,
                    tr("✅ Тариф удалён.", "✅ Plan deleted."),
                )
                await show_plans(bot, user_id, actor_id, project_id)
            elif action.startswith("currencyadd:"):
                parts = action.split(":")
                if len(parts) < 2 or len(parts) > 3 or parts[1] not in (
                    "price", "method", "attach",
                ):
                    raise ValueError("Некорректный режим выбора валюты")
                mode = parts[1]
                if (mode == "method" and len(parts) != 2) or (
                    mode != "method" and len(parts) != 3
                ):
                    raise ValueError("Некорректная карточка валюты")
                target = parts[2] if len(parts) == 3 else None
                if mode == "price":
                    await get_owner_plan_info(
                        factory, actor_id=actor_id, project_id=project_id,
                        plan_id=UUID(hex=target),
                    )
                if mode == "attach":
                    await get_owner_method_info(
                        factory, actor_id=actor_id, project_id=project_id,
                        method_id=UUID(hex=target),
                    )
                await prompt(
                    bot, user_id, actor_id, step="currency_custom_code",
                    payload={"mode": mode, "target": target},
                    text=tr(
                        "💱 <b>Добавить валюту</b>\n\n"
                        "Введите обычный код валюты, например UAH, BYN, "
                        "GBP, CNY или JPY. ⭐ Telegram Stars и 💎 Crypto Pay "
                        "настраиваются в разделе «Способы оплаты».\n\n"
                        "Цена и способ оплаты настраиваются отдельно.",
                        "💱 <b>Add currency</b>\n\n"
                        "Enter a fiat currency code, such as UAH, BYN, "
                        "GBP, CNY or JPY. ⭐ Telegram Stars and 💎 Crypto Pay "
                        "are configured in Payment methods.\n\n"
                        "Prices and payment instructions are configured separately.",
                    ),
                )
            elif action.startswith("price:"):
                _, plan_hex, currency = action.split(":")
                # Existing Telegram messages keep their old callback data.
                # Route obsolete Stars/crypto price buttons to their actual
                # payment-rail settings instead of failing or recreating
                # standalone crypto tariffs.
                if currency == "XTR":
                    await show_stars_payments(
                        bot, user_id, actor_id, project_id,
                    )
                    await query.answer()
                    return
                if currency in ("TON", "USDT"):
                    await show_crypto_pay(
                        bot, user_id, actor_id, project_id,
                    )
                    await query.answer()
                    return
                if currency not in await owner_available_currencies(
                    factory, actor_id=actor_id, project_id=project_id,
                    fiat_only=True,
                ):
                    raise ValueError(tr("Неизвестная валюта", "Unsupported currency"))
                await prompt(
                    bot, user_id, actor_id, step="price_amount",
                    payload={"plan_id": plan_hex, "currency": currency},
                    text=tr(
                        f"💰 Укажите цену в {display_currency(currency)} (например, 499 или 9.99):",
                        f"💰 Enter the price in {display_currency(currency)} (e.g. 499 or 9.99):",
                    ),
                )
            elif action.startswith("publish:"):
                # Old pre-management publish buttons are intentionally stale:
                # publication requires the draft's current plan revision.
                _, token, revision = action.split(":", 2)
                plan_id = decode_uuid(token)
                try:
                    name = await publish_owner_plan(
                        factory, actor_id=actor_id, project_id=project_id,
                        plan_id=plan_id, expected_status="draft",
                        expected_revision=revision,
                    )
                except ValueError as exc:
                    if "Добавьте действующий способ оплаты" not in str(exc):
                        raise
                    await bot.send_message(
                        user_id,
                        tr(
                            "💳 <b>Остался один шаг</b>\n\n"
                            "Добавьте способ оплаты, который поддерживает "
                            "валюту тарифа. Черновик и цены сохранены.",
                            "💳 <b>One step left</b>\n\n"
                            "Add a payment method supporting this plan's "
                            "currency. Your draft and prices are saved.",
                        ),
                        parse_mode="HTML",
                        reply_markup=_markup([
                            [_button(
                                "➕ Настроить способ оплаты",
                                "newmethod", "success",
                            )],
                            [_button("⬅️ Вернуться к тарифу", "plan:" + plan_id.hex)],
                        ]),
                    )
                    await query.answer()
                    return
                await bot.send_message(
                    user_id, tr(f"✅ Тариф «{escape(name)}» опубликован.",
                                 f"✅ Plan “{escape(name)}” published."),
                    parse_mode="HTML",
                )
                await show_plans(bot, user_id, actor_id, project_id)
            elif action in ("methods", "methods:guided"):
                await show_methods(
                    bot, user_id, actor_id, project_id,
                    guided=True if action == "methods:guided" else None,
                    source_message_id=getattr(query.message, "message_id", None)
                    if fixed_project_id is not None else None,
                )
            elif action == "starsnew":
                overview = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                if overview.plans:
                    await show_stars_payments(
                        bot, user_id, actor_id, project_id,
                    )
                else:
                    await bot.send_message(
                        user_id,
                        tr(
                            "⭐ <b>Создать тариф на 1 месяц?</b>\n\n"
                            "После ввода вашей цены в звёздах ViMi Pay "
                            "создаст тариф, привяжет его к каналу "
                            "и включит для новых покупок. "
                            "До ввода цены ничего не создаётся.",
                            "⭐ <b>Create a 1-month plan?</b>\n\n"
                            "Once you enter your Stars price, ViMi Pay "
                            "will create the plan, link it to a channel, "
                            "and enable new purchases. Nothing is "
                            "created until you enter the price.",
                        ),
                        parse_mode="HTML",
                        reply_markup=_markup([
                            [_button(
                                tr("✅ Продолжить", "✅ Continue"),
                                "starsnewok", "success",
                            )],
                            [_button(
                                tr("⬅️ Отмена", "⬅️ Cancel"), "starspage:0",
                            )],
                        ]),
                    )
            elif action == "starsnewok" or action.startswith("starsnewres:"):
                overview = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                if overview.plans:
                    await show_stars_payments(
                        bot, user_id, actor_id, project_id,
                    )
                else:
                    resources = [
                        (rid, name) for rid, name, status in overview.resources
                        if status == "active"
                    ]
                    if not resources:
                        await bot.send_message(
                            user_id, tr(
                                "📢 Сначала подключите канал для этого тарифа.",
                                "📢 Connect a channel before creating a plan.",
                            ),
                            reply_markup=_markup([[_button(
                                tr("📢 Подключить канал", "📢 Connect channel"),
                                "resources", "success",
                            )], [_button(
                                tr("⬅️ Назад", "⬅️ Back"), "starspage:0",
                            )]]),
                        )
                    elif action == "starsnewok" and len(resources) > 1:
                        await bot.send_message(
                            user_id,
                            tr("📢 Какой канал откроет тариф на 1 месяц?",
                               "📢 Which channel will the 1-month plan unlock?"),
                            reply_markup=_markup([
                                [_button(
                                    "📢 " + name[:38], "starsnewres:" + rid.hex,
                                )] for rid, name in resources
                            ] + [[_button(
                                tr("⬅️ Назад", "⬅️ Back"), "starspage:0",
                            )]]),
                        )
                    else:
                        selected = (
                            UUID(hex=action.split(":", 1)[1])
                            if action.startswith("starsnewres:")
                            else resources[0][0]
                        )
                        if selected not in {rid for rid, _ in resources}:
                            raise ValueError(tr(
                                "Канал изменился. Выберите действующий канал.",
                                "Channel changed. Select an active channel.",
                            ))
                        await prompt_first_stars_month(
                            bot, user_id, actor_id, selected,
                        )
            elif action.startswith("starspage:"):
                page_token = action.split(":", 1)[1]
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid Stars page")
                await show_stars_payments(
                    bot, user_id, actor_id, project_id, page=int(page_token),
                )
            elif action.startswith("starsprice:"):
                _, plan_hex, page_token = action.split(":", 2)
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid Stars price page")
                plan_id = UUID(hex=plan_hex)
                await get_owner_plan_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                )
                await prompt(
                    bot, user_id, actor_id, step="price_amount",
                    payload={
                        "plan_id": plan_id.hex, "currency": "XTR",
                        "stars_page": int(page_token),
                    },
                    text=tr(
                        "⭐ <b>Цена Telegram Stars</b>\n\n"
                        "Введите целое число звёзд от 1 до 250000. "
                        "Оно будет списано Telegram при покупке.",
                        "⭐ <b>Telegram Stars price</b>\n\n"
                        "Enter a whole number of Stars from 1 to 250000. "
                        "Telegram will charge this amount for the purchase.",
                    ),
                )
            elif action.startswith("starsoff:"):
                _, plan_hex, page_token = action.split(":", 2)
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid Stars page")
                await disable_owner_stars_price(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=UUID(hex=plan_hex),
                )
                await show_stars_payments(
                    bot, user_id, actor_id, project_id,
                    page=int(page_token),
                )
            elif action.startswith(("starsnative:", "starsnativeok:",
                                    "starsnativeoff:")):
                part, plan_hex, page_token = action.split(":", 2)
                if not page_token.isascii() or not page_token.isdecimal():
                    raise ValueError("Invalid Stars page")
                page = int(page_token)
                if not 0 <= page <= 10000:
                    raise ValueError("Invalid Stars page")
                plan_id = UUID(hex=plan_hex)
                target = (
                    await read_native_stars_target(
                        factory, actor_id=actor_id, project_id=project_id,
                        plan_id=plan_id, bot_id=bot.id,
                    )
                    if part != "starsnativeoff" else None
                )
                if part == "starsnative":
                    if target.active:
                        await show_stars_payments(
                            bot, user_id, actor_id, project_id, page=page,
                        )
                    else:
                        await bot.send_message(
                            user_id,
                            tr(
                                "⭐ <b>Платная ссылка Telegram-канала</b>\n\n"
                                "Подписка: <b>30 дней</b>, "
                                "автоматическое продление.\n"
                                "Цена: <b>",
                                "⭐ <b>Telegram channel paid invitation</b>\n\n"
                                "Subscription: <b>30 days</b>, "
                                "automatic renewal.\n"
                                "Price: <b>",
                            ) + str(target.amount) + " ⭐</b>\n\n"
                            + tr(
                                "Звёзды получит канал, а не бот. "
                                "Telegram управляет выдачей доступа и продлениями. "
                                "ViMi Pay пока не видит транзакции баланса канала "
                                "и не включает их в подтверждённую выручку и 5%.\n\n"
                                "Подтвердить создание ссылки?",
                                "Stars will go to the channel, not the bot. "
                                "Telegram manages access and renewals. "
                                "ViMi Pay cannot yet verify channel transactions "
                                "and will not report them as confirmed 5% revenue.\n\n"
                                "Create the paid link?",
                            ),
                            parse_mode="HTML",
                            reply_markup=_markup([
                                [_button(
                                    tr("✅ Создать ссылку", "✅ Create link"),
                                    "starsnativeok:" + plan_id.hex + ":" + str(page),
                                    "success",
                                )],
                                [_button(
                                    tr("⬅️ Отмена", "⬅️ Cancel"),
                                    "starspage:" + str(page),
                                )],
                            ]),
                        )
                elif part == "starsnativeok":
                    if target.active:
                        await show_stars_payments(
                            bot, user_id, actor_id, project_id, page=page,
                        )
                    else:
                        chat = await bot.get_chat(target.chat_id)
                        if getattr(chat, "type", None) != "channel":
                            raise ValueError("Telegram target is not a channel")
                        if getattr(chat, "username", None):
                            raise ValueError("Telegram channel must be private")
                        admin = await bot.get_chat_member(target.chat_id, bot.id)
                        if (
                            admin.status not in ("administrator", "creator")
                            or (admin.status != "creator"
                                and not getattr(admin, "can_invite_users", False))
                        ):
                            raise ValueError("Bot needs channel invite administrator rights")
                        invite = await bot.create_chat_subscription_invite_link(
                            chat_id=target.chat_id,
                            name="ViMi Pay",
                            subscription_period=NATIVE_PERIOD_SECONDS,
                            subscription_price=target.amount,
                        )
                        if (
                            invite.subscription_price != target.amount
                            or invite.subscription_period != NATIVE_PERIOD_SECONDS
                        ):
                            try:
                                await bot.revoke_chat_invite_link(
                                    target.chat_id, invite.invite_link,
                                )
                            except AiogramError:
                                logging.exception("Native channel link cleanup failed")
                            raise ValueError("Telegram returned unexpected invite pricing")
                        try:
                            await activate_native_stars_link(
                                factory, actor_id=actor_id, project_id=project_id,
                                plan_id=plan_id, bot_id=bot.id,
                                expected_price_id=target.price_id,
                                invite_link=invite.invite_link,
                            )
                        except Exception:
                            try:
                                await bot.revoke_chat_invite_link(
                                    target.chat_id, invite.invite_link,
                                )
                            except AiogramError:
                                logging.exception("Native channel link cleanup failed")
                            raise
                        await show_stars_payments(
                            bot, user_id, actor_id, project_id, page=page,
                        )
                else:
                    old = await deactivate_native_stars_link(
                        factory, actor_id=actor_id, project_id=project_id,
                        plan_id=plan_id, bot_id=bot.id,
                    )
                    if old is not None:
                        chat_id, url = old
                        try:
                            await bot.revoke_chat_invite_link(
                                chat_id=chat_id, invite_link=url,
                            )
                        except AiogramError:
                            await bot.send_message(
                                user_id, tr(
                                    "⚠️ Новые продажи через ссылку в ViMi Pay выключены, "
                                    "но Telegram не подтвердил отзыв ссылки. "
                                    "Отзовите её вручную в канале:",
                                    "⚠️ New link sales are disabled in ViMi Pay, "
                                    "but Telegram did not confirm revocation. "
                                    "Revoke this invitation manually:",
                                ) + "\n" + escape(url), parse_mode="HTML",
                            )
                    await show_stars_payments(
                        bot, user_id, actor_id, project_id, page=page,
                    )
            elif action == "cryptopay":
                await show_crypto_pay(bot, user_id, actor_id, project_id)
            elif action == "cryptonet":
                if fixed_project_id != project_id:
                    raise PermissionError("Open the connected administrator bot")
                # An actual provider token is requested only after owner
                # chooses the correct API environment.
                await read_crypto_pay_connection(
                    factory, project_id=project_id,
                    bot_id=bot.id, actor_id=actor_id,
                )
                await bot.send_message(
                    user_id,
                    tr("💎 <b>Выберите сеть Crypto Pay</b>",
                       "💎 <b>Choose Crypto Pay environment</b>"),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button("💎 Crypto Pay · @CryptoBot",
                                 "cryptonet:mainnet", "success")],
                        [_button("🧪 Testnet · @CryptoTestnetBot",
                                 "cryptonet:testnet")],
                        [_button(tr("⬅️ Назад", "⬅️ Back"), "cryptopay")],
                    ]),
                )
            elif action.startswith("cryptonet:"):
                env = action.split(":", 1)[1]
                if env not in ("mainnet", "testnet"):
                    raise ValueError("Invalid Crypto Pay environment")
                if fixed_project_id != project_id:
                    raise PermissionError("Open the connected administrator bot")
                await read_crypto_pay_connection(
                    factory, project_id=project_id,
                    bot_id=bot.id, actor_id=actor_id,
                )
                await prompt(
                    bot, user_id, actor_id,
                    step="crypto_pay_token", payload={"environment": env},
                    text=tr(
                        "🔐 <b>Отправьте API-токен вашего приложения Crypto Pay.</b>\n"
                        "Сообщение будет удалено до проверки. Никому не передавайте "
                        "токен. При ошибке удаления токен придётся сменить.\n"
                        "Для выхода нажмите «В меню».",
                        "🔐 <b>Send your Crypto Pay app API token.</b>\n"
                        "Your message will be deleted before verification. "
                        "Never share the token. If deletion fails, rotate it.\n"
                        "Tap Main menu to exit.",
                    ),
                )
                remember_crypto_pay_owner(
                    project_id=project_id, bot_id=bot.id,
                    actor_id=actor_id, telegram_user_id=user_id,
                )
            elif action == "cryptoffask":
                if fixed_project_id != project_id:
                    raise PermissionError("Open the connected administrator bot")
                await read_crypto_pay_connection(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=actor_id,
                )
                await bot.send_message(
                    user_id,
                    tr(
                        "⏸️ Отключить Crypto Pay для новых счетов? "
                        "Старые счета и зашифрованные ключи останутся в истории.",
                        "⏸️ Disable new Crypto Pay invoices? Existing invoices "
                        "and encrypted credentials will remain for reconciliation.",
                    ),
                    reply_markup=_markup([
                        [_button(tr("✅ Подтвердить", "✅ Confirm"),
                                 "cryptoffok", "danger")],
                        [_button(tr("⬅️ Назад", "⬅️ Back"), "cryptopay")],
                    ]),
                )
            elif action == "cryptoffok":
                if fixed_project_id != project_id:
                    raise PermissionError("Open the connected administrator bot")
                await disconnect_crypto_pay(
                    factory, project_id=project_id, bot_id=bot.id,
                    actor_id=actor_id,
                )
                await show_crypto_pay(bot, user_id, actor_id, project_id)
            elif action.startswith(("methodpage:", "methodwizardpage:")):
                page = int(action.split(":", 1)[1])
                await show_methods(
                    bot, user_id, actor_id, project_id, page=page,
                    guided=True if action.startswith("methodwizardpage:") else None,
                    source_message_id=getattr(query.message, "message_id", None)
                    if fixed_project_id is not None else None,
                )
            elif action == "newmethod":
                await currency_picker(
                    bot, user_id, actor_id, project_id=project_id, mode="method",
                )
            elif action.startswith("method:"):
                await show_method_card(
                    bot, user_id, actor_id, project_id,
                    UUID(hex=action.split(":", 1)[1]),
                )
            elif action.startswith("methodlink:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                method = await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                codes = await owner_available_currencies(
                    factory, actor_id=actor_id, project_id=project_id,
                    fiat_only=True,
                )
                rows = [
                    [_button(
                        "➕ " + display_currency(code),
                        "methodattach:" + method_id.hex + ":" + code,
                    )] for code in codes if code not in method.currencies
                ]
                rows.extend([
                    [_button(
                        "➖ " + display_currency(code),
                        "methodremoveask:" + encode_uuid(method_id) + ":" + code,
                        "danger",
                    )] for code in method.currencies
                    if len(method.currencies) > 1
                ])
                rows.append([_button(
                    tr("➕ Другая валюта", "➕ Another currency"),
                    "currencyadd:attach:" + method_id.hex,
                )])
                if not rows:
                    await bot.send_message(
                        user_id, tr("Все доступные валюты уже подключены.",
                                     "All available currencies are already enabled."),
                        reply_markup=_markup([[_button(
                            tr("⬅️ К способу оплаты", "⬅️ Payment method"),
                            "method:" + method_id.hex,
                        )]]),
                    )
                else:
                    rows.append([
                        _button("⬅️ Вернуться", "method:" + method_id.hex),
                    ])
                    await bot.send_message(
                        user_id,
                        "🌐 <b>" + escape(method.name) + "</b>\n\n"
                        + tr("Выберите дополнительную валюту. "
                             "Текущие подключения сохранятся.",
                             "Select another currency. "
                             "Existing currencies will remain enabled."),
                        parse_mode="HTML", reply_markup=_markup(rows),
                    )
            elif action.startswith("methodattach:"):
                _, method_hex, currency = action.split(":", 2)
                added = await attach_owner_method_currency(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=UUID(hex=method_hex), currency_code=currency,
                )
                await bot.send_message(
                    user_id,
                    tr("✅ Валюта подключена", "✅ Currency enabled") if added
                    else tr("Эта валюта уже подключена.",
                            "This currency is already enabled."),
                )
                await show_method_card(
                    bot, user_id, actor_id, project_id, UUID(hex=method_hex),
                )
            elif action.startswith("methodrename:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                await prompt(
                    bot, user_id, actor_id, step="method_rename",
                    payload={"method_id": method_id.hex},
                    text=tr(
                        "✏️ <b>Новое название способа оплаты</b>\n"
                        "От 2 до 120 символов.",
                        "✏️ <b>Rename payment method</b>\n"
                        "Enter 2–120 characters.",
                    ),
                )
            elif action.startswith("methodedit:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                await prompt(
                    bot, user_id, actor_id, step="method_edit",
                    payload={"method_id": method_id.hex},
                    text=tr(
                        "📝 <b>Новые реквизиты и инструкция</b>\n"
                        "Допускаются {amount}, {currency}, {plan}, {period}.\n"
                        "Новые инструкции увидят покупатели при открытии оплаты.",
                        "📝 <b>New payment instructions</b>\n"
                        "Allowed: {amount}, {currency}, {plan}, {period}.\n"
                        "Buyers will see the new text when opening payment.",
                    ),
                )
            elif action.startswith("methodremoveask:"):
                _, token, code = action.split(":", 2)
                method_id = decode_uuid(token)
                method = await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                if code not in method.currencies or len(method.currencies) < 2:
                    raise ValueError(tr(
                        "Эту валюту отключить нельзя.",
                        "This currency cannot be detached.",
                    ))
                await bot.send_message(
                    user_id,
                    tr("➖ Отключить валюту ", "➖ Detach currency ") + code
                    + tr(" от способа «", " from “")
                    + escape(method.name) + tr("»?", "”?"),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("➖ Да, отключить валюту", "➖ Confirm detach"),
                            "methodremove:" + token + ":" + code + ":" + method.revision,
                            "danger",
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "method:" + method_id.hex)],
                    ]),
                )
            elif action.startswith("methodremove:"):
                _, token, code, revision = action.split(":", 3)
                method_id = decode_uuid(token)
                await detach_owner_method_currency(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, currency_code=code,
                    expected_revision=revision,
                )
                await bot.send_message(
                    user_id, tr("✅ Валюта отключена.", "✅ Currency detached."),
                )
                await show_method_card(bot, user_id, actor_id, project_id, method_id)
            elif action.startswith("methoddeleteask:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                method = await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                if method.has_history:
                    raise ValueError(tr(
                        "Есть история покупок. Отключите способ оплаты.",
                        "Purchase history exists. Disable the method instead.",
                    ))
                await bot.send_message(
                    user_id,
                    tr("🗑 <b>Удалить способ оплаты навсегда?</b>\n\n",
                       "🗑 <b>Permanently delete payment method?</b>\n\n")
                    + escape(method.name)
                    + tr(
                        "\n\nНастройки и валюты будут удалены. "
                        "Удаление возможно только без истории заявок и платежей.",
                        "\n\nSettings and currency links will be deleted. "
                        "Only methods with no request or payment history can be removed.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("🗑 Да, удалить", "🗑 Confirm deletion"),
                            "methoddelete:" + encode_uuid(method_id) + ":" + method.revision,
                            "danger",
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "method:" + method_id.hex)],
                    ]),
                )
            elif action.startswith("methoddelete:"):
                _, token, revision = action.split(":", 2)
                method_id = decode_uuid(token)
                await delete_unused_owner_method(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, expected_revision=revision,
                )
                await bot.send_message(
                    user_id, tr("✅ Способ оплаты удалён.", "✅ Payment method deleted."),
                )
                await show_methods(bot, user_id, actor_id, project_id)
            elif action.startswith("methodconfirmoff:"):
                method_id = UUID(hex=action.split(":", 1)[1])
                method = await get_owner_method_info(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id,
                )
                if method.status != "active":
                    raise ValueError(tr(
                        "Способ уже отключён.", "Method is already disabled.",
                    ))
                await bot.send_message(
                    user_id,
                    tr(
                        "⏸️ <b>Отключить способ оплаты?</b>\n\n"
                        "Он исчезнет из новых покупок. Уже полученные "
                        "заявки и история платежей сохранятся.",
                        "⏸️ <b>Disable this payment method?</b>\n\n"
                        "It will no longer be offered for new purchases. "
                        "Existing payment requests and history are preserved.",
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup([
                        [_button(
                            tr("⏸️ Подтвердить отключение", "⏸️ Confirm disable"),
                            "methodoff:" + encode_uuid(method_id) + ":" + method.revision,
                            "danger",
                        )],
                        [_button(tr("⬅️ Отмена", "⬅️ Cancel"),
                                 "method:" + method_id.hex)],
                    ]),
                )
            elif action.startswith(("methodoff:", "methodon:")):
                is_active = action.startswith("methodon:")
                _, token, revision = action.split(":", 2)
                method_id = decode_uuid(token)
                await change_owner_method_status_guarded(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, enabled=is_active,
                    expected_revision=revision,
                )
                await show_method_card(bot, user_id, actor_id, project_id, method_id)
            elif action.startswith("methodcurrency:"):
                code = action.split(":", 1)[1]
                if code not in await owner_available_currencies(
                    factory, actor_id=actor_id, project_id=project_id,
                ):
                    raise ValueError(tr("Неизвестная валюта", "Unsupported currency"))
                await prompt(
                    bot, user_id, actor_id, step="method_name",
                    payload={"currency": code},
                    text=tr(
                        f"💳 Название способа оплаты для {code}:\n"
                        "Например: СБП или Банковский перевод.",
                        f"💳 Payment method name for {code}:\n"
                        "For example: Bank transfer.",
                    ),
                )
            elif action == "timezone":
                rows = [
                    [_button(zone, "tz:" + zone + ":" + project_id.hex)]
                    for zone in (
                        "Europe/Moscow", "Europe/Amsterdam",
                        "Europe/London", "Asia/Bangkok", "Asia/Dubai"
                    )
                ]
                rows.append([_button("✍️ Указать другой", "tzcustom")])
                rows.append([_button(
                    tr("⬅️ Настройки", "⬅️ Settings"), "settings",
                )])
                await bot.send_message(
                    user_id, tr("🌍 Выберите часовой пояс:", "🌍 Choose a timezone:"),
                    reply_markup=_markup(rows),
                )
            elif action == "tzcustom":
                await prompt(
                    bot, user_id, actor_id, step="timezone",
                    text=tr(
                        "🌍 Отправьте часовой пояс IANA, например Europe/Moscow.",
                        "🌍 Send an IANA timezone, for example Europe/London.",
                    ),
                )
            elif action.startswith("tz:"):
                _, value, origin_hex = action.split(":", 2)
                if UUID(hex=origin_hex) != project_id:
                    raise ValueError(tr(
                        "Это меню другого проекта. Откройте настройки заново.",
                        "This menu belongs to another project. Reopen settings.",
                    ))
                await change_project_timezone(
                    factory, project_id=project_id,
                    actor_telegram_user_id=user_id, timezone_name=value,
                )
                await show_settings(bot, user_id)
            elif action == "readiness":
                data = await setup_overview(
                    factory, actor_id=actor_id, project_id=project_id,
                )
                next_step = _next_setup_step(data)
                rows = []
                if next_step is not None:
                    rows.append([_button(
                        next_step[0], next_step[1], "success",
                    )])
                rows.append([_button(
                    tr("⬅️ Настройки", "⬅️ Settings"), "settings",
                )])
                await bot.send_message(
                    user_id,
                    tr("📋 <b>Готовность к продажам</b>\n\n",
                       "📋 <b>Sales readiness</b>\n\n")
                    + "\n".join(_progress(data))
                    + "\n\n🤖 " + escape(owner_runtime_label(data.bot_runtime_label))
                    + "\n\n"
                    + (
                        tr("✅ Тарифы доступны для покупки.",
                           "✅ Plans are available to buyers.")
                        if data.sales_configured
                        else tr("Бот уже может принимать настройки. "
                                "Покупатели увидят тарифы после завершения шагов.",
                                "The bot is ready for configuration. "
                                "Buyers will see plans after setup is complete.")
                    ),
                    parse_mode="HTML",
                    reply_markup=_markup(rows),
                )
            elif action == "help":
                await show_help(bot, user_id)
            else:
                raise ValueError(tr("Неизвестное действие", "Unknown action"))
        except (ValueError, PermissionError) as exc:
            detail = (
                obligation_error(str(exc))
                if action.startswith(("ob:", "obok:", "obpage:"))
                else owner_error(str(exc))
            )
            await query.answer(detail[:180], show_alert=True)
            return
        # Method submenus still send their own cards; retire the originating
        # selection card after the new one exists. Do not apply this to
        # payment mutations, receipts, or arbitrary admin callbacks.
        if action in {"newmethod", "cryptopay"} or action.startswith((
            "starspage:", "method:",
        )):
            old_id = getattr(query.message, "message_id", None)
            if old_id is not None and fixed_project_id is not None:
                try:
                    await bot.delete_message(user_id, old_id)
                except AiogramError:
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=user_id, message_id=old_id,
                            reply_markup=None,
                        )
                    except AiogramError:
                        logging.getLogger(__name__).warning(
                            "Unable to retire previous payment-setup card"
                        )
        await query.answer()

    @router.message(F.managed_bot_created)
    async def managed_bot_created_message(message: Message, bot: Bot) -> None:
        """Only a Telegram service message may offer an owner-visible candidate."""
        if fixed_project_id is not None or not _private(message):
            return
        if message.from_user is None or message.managed_bot_created is None:
            return
        actor_id = await actor(message.from_user)
        bot_user = message.managed_bot_created.bot_user
        generation = await stage_managed_bot_candidate(
            factory, actor_id=actor_id, managed_bot_id=bot_user.id,
        )
        if generation is None:
            return
        # Telegram's service message has no request_id; explicit confirmation
        # prevents an old event from silently consuming a newer owner request.
        label = ("@" + bot_user.username) if bot_user.username else str(bot_user.id)
        async with recipient_language(factory, message.from_user.id):
            await bot.send_message(
                message.chat.id,
                tr(
                    "🤖 <b>Telegram создал бота:</b> ",
                    "🤖 <b>Telegram created a bot:</b> ",
                ) + escape(label) + "\n\n" + tr(
                    "Подтвердите подключение именно этого бота к ViMi Pay. "
                    "Если это старое уведомление, отмените запрос.",
                    "Confirm this exact bot for ViMi Pay. "
                    "If this is an old notification, cancel the request.",
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_button(
                        tr("✅ Подключить этого бота", "✅ Connect this bot"),
                        "managedconfirm:" + str(bot_user.id) + ":" + generation,
                        "success",
                    )],
                    [_button(tr("⬅️ Мои подключения", "⬅️ My connections"), "projects")],
                ]),
            )

    @router.message(F.chat_shared)
    async def shared_chat_handler(message: Message, bot: Bot) -> None:
        """Native chat selector: a single outcome, never a second directory."""
        if not _private(message) or fixed_project_id is None:
            return
        actor_id = await actor(message.from_user)
        if not await can_manage_project(
            factory, actor_id=actor_id, project_id=fixed_project_id,
        ):
            return
        kind = "channel"
        problem = "permissions"
        guided = False
        flow: SetupFlow | None = None
        try:
            flow = await consume_button_flow(
                factory, actor_id=actor_id, expected_step="chat_picker",
                request_id=message.chat_shared.request_id,
                bot_scope_id=bot.id,
                discard=True,
            )
            if flow.project_id != fixed_project_id:
                raise ValueError(tr(
                    "Настройка другого бота недоступна",
                    "You cannot change another bot's settings",
                ))
            kind = flow.payload.get("kind", "channel")
            guided = flow.payload.get("guided") is True
            chat = await verify_channel_and_add(
                factory, actor_id=actor_id,
                actor_telegram_id=message.from_user.id,
                project_id=flow.project_id,
                chat_reference=message.chat_shared.chat_id,
            )
            success = True
        except (ValueError, PermissionError) as exc:
            success = False
            chat = None
            problem = connection_problem_code(exc)
        # The native instructions and Telegram's chat_shared service
        # event are separate messages. Remove both when allowed.
        await delete_native_chat_picker_card(bot, message.chat.id, flow)
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except (AiogramError, TypeError):
            pass
        await show_connection_result(
            bot, message.chat.id, actor_id, fixed_project_id,
            success=success, kind=kind,
            resource_name=chat.name if chat is not None else None,
            problem=problem, guided=guided,
        )

    async def send_connected_bot(
        bot: Bot, user_id: int, actor_id: UUID,
        project_id: UUID, username: str,
    ) -> None:
        imported = await get_project_import_status(
            factory, actor_id=actor_id, project_id=project_id,
        )
        rows = [
            [InlineKeyboardButton(
                text=tr("🚀 Перейти к своему боту", "🚀 Open my bot"),
                url="https://t.me/" + username,
                style="success",
            )],
            [_button(
                tr("⚙️ Управление подключением", "⚙️ Manage connection"),
                # Selecting the exact newly registered project is essential:
                # other existing owner connections may be currently selected.
                "select:" + project_id.hex, "primary",
            )],
        ]
        await send_owner_rich(
            bot, chat_id=user_id,
            rich_html=connection_success_rich(
                username=username, imported=imported,
            ),
            classic_html=connection_success_classic(
                username=username, imported=imported,
            ),
            reply_markup=_markup(rows),
        )
        # Do NOT immediately open management after a new connection.
        # The owner continues onboarding inside their bot; management is
        # available only if they explicitly choose the second button.

    async def finish_bot_connection(
        bot: Bot, user_id: int, actor_id: UUID, project_id: UUID, token: str,
    ) -> None:
        before = await setup_overview(
            factory, actor_id=actor_id, project_id=project_id,
        )
        was_connected = any(
            status == "registered" for _, status in before.bots
        )
        result = await register_customer_bot(
            factory, project_id=project_id,
            actor_telegram_user_id=user_id, token=token,
            encryption_key=get_settings().bot_token_encryption_key,
            central_bot_id=bot.id,
        )
        await set_default_project_name_from_bot(
            factory, actor_id=actor_id, project_id=project_id,
            username=result.username,
        )
        if was_connected:
            # Rotating a token is maintenance, not first-time onboarding.
            await show_bots(bot, user_id, actor_id, project_id)
        else:
            await send_connected_bot(
                bot, user_id, actor_id, project_id, result.username,
            )

    @router.message(SetupReplyFilter(factory, fixed_project_id))
    async def owner_reply_handler(message: Message, bot: Bot) -> None:
        if not _private(message) or message.text is None:
            return
        origin = message.reply_to_message
        if origin is not None and (
            origin.from_user is None or origin.from_user.id != bot.id
        ):
            return
        actor_id = await actor(message.from_user)
        text = (message.text or "").strip()
        flow = await consume_flow(
            factory, actor_id=actor_id,
            reply_message_id=origin.message_id if origin is not None else None,
            bot_scope_id=bot.id if fixed_project_id else 0,
        )
        if flow is None:
            return
        # Always delete BotFather credential messages before validating or storing
        # anything, even if the credential was typed incorrectly.
        unexpected_secret = bool(
            flow.step not in ("bot_token", "new_bot_token", "crypto_pay_token")
            and TOKEN_PATTERN.fullmatch(text)
        )
        if flow.step in (
            "bot_token", "new_bot_token", "crypto_pay_token",
        ) or unexpected_secret:
            try:
                deleted = await bot.delete_message(
                    message.chat.id, message.message_id
                )
            except AiogramError:
                deleted = False
            if not deleted:
                await bot.send_message(
                    message.chat.id,
                    tr("⚠️ Не удалось удалить секретное сообщение. Смените токен "
                       "в @CryptoBot или BotFather и попробуйте снова.",
                       "⚠️ Could not delete the secret message. Rotate the token "
                       "in @CryptoBot or BotFather before retrying."),
                )
                return
            if unexpected_secret:
                await bot.send_message(
                    message.chat.id,
                    tr("⚠️ Вы прислали токен вместо запрошенных данных. "
                       "Для безопасности смените токен в BotFather и повторите шаг.",
                       "⚠️ You sent a bot token instead of the requested information. "
                       "Rotate your token in BotFather and retry."),
                )
                return
        # Old central setup prompts remain in Telegram after a deployment.
        # Do not permit completing tenant settings via stale central messages.
        if fixed_project_id is None and flow.step not in (
            "new_bot_token", "bot_token", "connection_rename",
        ):
            await bot.send_message(
                message.chat.id,
                tr("Настройка перенесена в вашего бота. "
                   "Откройте его через «Мои боты».",
                   "Setup has moved to your connected bot. "
                   "Open it from My bots."),
            )
            return
        if fixed_project_id is not None and flow.step in (
            "new_bot_token", "bot_token", "project_name", "connection_rename",
        ):
            await bot.send_message(
                message.chat.id,
                tr("Подключение токена доступно только в @vimipay_bot.",
                   "Bot tokens can only be connected through @vimipay_bot."),
            )
            return
        try:
            if flow.step == "crypto_pay_token":
                if (
                    fixed_project_id is None or flow.project_id != fixed_project_id
                    or flow.payload.get("environment") not in ("mainnet", "testnet")
                ):
                    raise PermissionError("Crypto Pay setup belongs to a connected bot")
                await connect_crypto_pay(
                    factory, project_id=fixed_project_id, bot_id=bot.id,
                    actor_id=actor_id, api_token=text,
                    encryption_key=get_settings().bot_token_encryption_key,
                    testnet=flow.payload["environment"] == "testnet",
                )
                # Only a non-secret success notification; do not echo the token.
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Приложение Crypto Pay проверено и защищённо подключено.",
                       "✅ Crypto Pay app verified and securely connected."),
                )
                await show_crypto_pay(
                    bot, message.chat.id, actor_id, fixed_project_id,
                )
            elif flow.step == "moderator_add":
                if fixed_project_id is None or flow.project_id != fixed_project_id:
                    raise PermissionError("Нельзя назначать модератора другого бота")
                if (
                    not text.isascii() or not text.isdecimal()
                    or len(text) > 19
                ):
                    raise ValueError("Нужен числовой Telegram ID")
                changed = await set_project_moderator(
                    factory, actor_id=actor_id, project_id=fixed_project_id,
                    telegram_id=int(text), enabled=True,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Модератор назначен.", "✅ Moderator added.") if changed
                    else tr("Уже назначен.", "Already assigned."),
                )
                await show_moderators(
                    bot, message.chat.id, actor_id, fixed_project_id,
                )
                return
            if flow.step == "subscriber_search":
                needle = text.removeprefix("@").strip()
                if (
                    not 2 <= len(needle) <= 32
                    or not needle.isascii()
                    or not all(ch.isalnum() or ch == "_" for ch in needle)
                ):
                    raise ValueError(
                        "Пришлите @username или Telegram ID (2–32 символа)"
                    )
                if fixed_project_id is None:
                    raise PermissionError("Поиск подписчиков доступен в подключённом боте")
                if flow.project_id != fixed_project_id:
                    raise PermissionError("Поиск другого бота запрещён")
                await show_subscribers(
                    bot, message.chat.id, actor_id, fixed_project_id,
                    search=needle,
                )
                return
            if flow.step == "subscriber_message":
                if fixed_project_id is None or flow.project_id != fixed_project_id:
                    raise PermissionError("Сообщение другого проекта отправлять нельзя")
                if not 1 <= len(text) <= 3500:
                    raise ValueError("Текст сообщения должен содержать 1–3500 символов")
                customer_id = UUID(hex=flow.payload["customer_id"])
                detail = await subscriber_detail(
                    factory, actor_id=actor_id, project_id=fixed_project_id,
                    customer_id=customer_id,
                )
                if not detail.owner_only_actions:
                    raise PermissionError("Отправлять сообщения может только владелец")
                try:
                    await send_to_customer(bot, detail.telegram_id, text)
                except AiogramError:
                    await bot.send_message(
                        message.chat.id,
                        tr("⚠️ Сообщение не доставлено. Возможно, покупатель "
                           "заблокировал бота.",
                           "⚠️ Not delivered. The customer may have blocked the bot."),
                    )
                else:
                    await bot.send_message(
                        message.chat.id,
                        tr("✅ Сообщение отправлено покупателю.",
                           "✅ Message sent to customer."),
                    )
                await show_subscriber_card(
                    bot, message.chat.id, actor_id,
                    fixed_project_id, customer_id,
                )
                return
            if flow.step == "new_bot_token":
                if not TOKEN_PATTERN.fullmatch(text):
                    raise ValueError(
                        tr("Неверный формат токена. Получите токен в @BotFather.",
                           "Invalid token format. Get a token from @BotFather.")
                    )
                project_id, registration = await register_new_owner_bot(
                    factory, actor_id=actor_id,
                    actor_telegram_user_id=message.from_user.id,
                    token=text,
                    encryption_key=get_settings().bot_token_encryption_key,
                    central_bot_id=bot.id,
                )
                await send_connected_bot(
                    bot, message.chat.id, actor_id,
                    project_id, registration.username,
                )
                return
            if flow.step == "project_name":
                await create_owner_project(factory, actor_id=actor_id, name=text)
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Проект создан. Часовой пояс — Москва (можно изменить).",
                       "✅ Project created. Default timezone: Moscow (editable)."),
                )
                await show_home(bot, message.chat.id, actor_id)
                return
            project_id = flow.project_id
            if project_id is None or (
                fixed_project_id is not None and project_id != fixed_project_id
            ):
                raise ValueError(tr("Настройка другого бота недоступна",
                                    "You cannot change another bot's settings"))
            if flow.step == "connection_rename":
                if fixed_project_id is not None:
                    raise PermissionError("Переименование доступно в центральном боте")
                current = await get_selected_project(factory, actor_id=actor_id)
                if current != project_id:
                    raise ValueError("Подключение изменилось. Откройте список заново.")
                renamed = await rename_owner_connection(
                    factory, actor_id=actor_id, project_id=project_id,
                    name=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Подключение переименовано: ",
                       "✅ Connection renamed: ") + escape(renamed),
                    parse_mode="HTML",
                )
                await show_bots(bot, message.chat.id, actor_id, project_id)
            elif flow.step == "bot_token":
                if not TOKEN_PATTERN.fullmatch(text):
                    raise ValueError(tr("Неверный формат токена. Получите его в BotFather.",
                                        "Invalid token format. Get it from BotFather."))
                await finish_bot_connection(
                    bot, message.chat.id, actor_id, project_id, text,
                )
            elif flow.step == "resource_reference":
                reference: str | int = int(text) if text.lstrip("-").isdigit() else text
                resource = await verify_channel_and_add(
                    factory, actor_id=actor_id,
                    actor_telegram_id=message.from_user.id,
                    project_id=project_id, chat_reference=reference,
                )
                resource_kind = (
                    "group" if "group" in str(resource.resource_type).lower()
                    else "channel"
                )
                await show_connection_result(
                    bot, message.chat.id, actor_id, project_id,
                    success=True, kind=resource_kind,
                    resource_name=resource.name,
                    guided=flow.payload.get("guided") is True,
                )
            elif flow.step == "plan_name":
                if not 2 <= len(text) <= 120:
                    raise ValueError(tr("Название тарифа: от 2 до 120 символов",
                                        "Plan name must be 2–120 characters"))
                generation = await advance_flow(
                    factory, actor_id=actor_id, source=flow,
                    step="plan_duration", payload={"name": text},
                    bot_scope_id=bot.id if fixed_project_id else 0,
                )
                await show_duration_picker(
                    bot, message.chat.id, generation,
                )
            elif flow.step == "plan_rename":
                plan_id = UUID(hex=flow.payload["plan_id"])
                name = await rename_owner_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id, name=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr(
                        f"✅ Тариф переименован в «{escape(name)}».",
                        f"✅ Plan renamed to “{escape(name)}”.",
                    ),
                    parse_mode="HTML",
                )
                await show_plan_card(
                    bot, message.chat.id, actor_id, project_id, plan_id,
                )
            elif flow.step == "currency_custom_code":
                code = await enable_owner_currency(
                    factory, actor_id=actor_id, project_id=project_id,
                    code=text, fiat_only=True,
                )
                mode = flow.payload.get("mode")
                target = flow.payload.get("target")
                if mode == "price":
                    plan_id = UUID(hex=target)
                    await prompt(
                        bot, message.chat.id, actor_id, step="price_amount",
                        source_flow=flow,
                        payload={"plan_id": plan_id.hex, "currency": code},
                        text=tr(
                            f"💰 Введите цену в {code} (например, 499 или 9.99):",
                            f"💰 Enter the price in {code} (e.g. 499 or 9.99):",
                        ),
                    )
                elif mode == "method":
                    await prompt(
                        bot, message.chat.id, actor_id, step="method_name",
                        source_flow=flow,
                        payload={"currency": code},
                        text=tr(
                            f"💳 Название способа оплаты для {code}:\n"
                            "Например: Перевод на карту или Банковский перевод.",
                            f"💳 Name a payment method for {code}:\n"
                            "For example: Bank transfer.",
                        ),
                    )
                elif mode == "attach":
                    method_id = UUID(hex=target)
                    await attach_owner_method_currency(
                        factory, actor_id=actor_id, project_id=project_id,
                        method_id=method_id, currency_code=code,
                    )
                    await bot.send_message(
                        message.chat.id,
                        tr("✅ Валюта добавлена к способу оплаты.",
                           "✅ Currency added to payment method."),
                    )
                    await show_method_card(
                        bot, message.chat.id, actor_id, project_id, method_id,
                    )
                else:
                    raise ValueError("Шаг выбора валюты устарел")
            elif flow.step == "stars_month_price":
                valid_stars = (
                    text.isascii() and text.isdecimal()
                    and 1 <= int(text) <= 250000
                )
                if not valid_stars:
                    await prompt_first_stars_month(
                        bot, message.chat.id, actor_id,
                        UUID(hex=flow.payload["resource_id"]),
                        source_flow=flow, retry=True,
                    )
                    return
                plan_id = await create_one_month_stars_plan(
                    factory, actor_id=actor_id, project_id=project_id,
                    resource_id=UUID(hex=flow.payload["resource_id"]),
                    stars_amount=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr(
                        "✅ <b>Тариф на 1 месяц опубликован!</b>\n⭐ Цена: ",
                        "✅ <b>1-month plan published!</b>\n⭐ Price: ",
                    ) + escape(text.strip()) + " ⭐",
                    parse_mode="HTML",
                )
                await show_stars_payments(
                    bot, message.chat.id, actor_id, project_id,
                )
            elif flow.step == "price_amount":
                plan_id = UUID(hex=flow.payload["plan_id"])
                await save_owner_price(
                    factory, actor_id=actor_id, project_id=project_id,
                    plan_id=plan_id,
                    currency_code=flow.payload["currency"], amount=text,
                )
                await bot.send_message(message.chat.id, tr("✅ Цена сохранена.", "✅ Price saved."))
                if flow.payload["currency"] == "XTR":
                    await show_stars_payments(
                        bot, message.chat.id, actor_id, project_id,
                        page=int(flow.payload.get("stars_page", 0)),
                    )
                else:
                    await show_plan_card(
                        bot, message.chat.id, actor_id, project_id, plan_id,
                    )
            elif flow.step == "method_rename":
                method_id = UUID(hex=flow.payload["method_id"])
                name = await rename_owner_method(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, name=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Способ переименован.", "✅ Payment method renamed."),
                )
                await show_method_card(
                    bot, message.chat.id, actor_id, project_id, method_id,
                )
            elif flow.step == "method_edit":
                method_id = UUID(hex=flow.payload["method_id"])
                await edit_owner_method_instructions(
                    factory, actor_id=actor_id, project_id=project_id,
                    method_id=method_id, instruction=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Реквизиты обновлены.", "✅ Instructions updated."),
                )
                await show_method_card(
                    bot, message.chat.id, actor_id, project_id, method_id,
                )
            elif flow.step == "method_name":
                if not 2 <= len(text) <= 120:
                    raise ValueError(tr("Название способа оплаты: от 2 до 120 символов",
                                        "Payment method name must be 2–120 characters"))
                await prompt(
                    bot, message.chat.id, actor_id,
                    step="method_instruction",
                    source_flow=flow,
                    payload={"currency": flow.payload["currency"], "name": text},
                    text=(
                        tr(
                            "💳 Отправьте инструкцию покупателю: куда и как переводить "
                            "деньги.\n\nМожно использовать {amount}, {currency}, "
                            "{plan}, {period}.\n"
                            "Например: Переведите {amount} {currency} по СБП на ...",
                            "💳 Send the instructions that buyers should follow.\n\n"
                            "You can use {amount}, {currency}, {plan}, {period}.\n"
                            "For example: Transfer {amount} {currency} to ...",
                        )
                    ),
                )
            elif flow.step == "method_instruction":
                await create_owner_payment_method(
                    factory, actor_id=actor_id, project_id=project_id,
                    name=flow.payload["name"],
                    currency_code=flow.payload["currency"], instruction=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Способ оплаты добавлен.", "✅ Payment method added."),
                )
                await show_methods(bot, message.chat.id, actor_id, project_id)
            elif flow.step == "timezone":
                await change_project_timezone(
                    factory, project_id=project_id,
                    actor_telegram_user_id=message.from_user.id, timezone_name=text,
                )
                await bot.send_message(
                    message.chat.id,
                    tr("✅ Часовой пояс обновлён.", "✅ Timezone updated."),
                )
                await show_dashboard(bot, message.chat.id, actor_id, project_id)
        except (ValueError, PermissionError, RuntimeError) as exc:
            if flow.step == "resource_reference" and flow.project_id is not None:
                # One compact Rich error with a working Back, not a generic
                # technical warning followed by the entire admin directory.
                await show_connection_result(
                    bot, message.chat.id, actor_id, flow.project_id,
                    success=False, kind="chat",
                    problem=connection_problem_code(exc),
                    guided=flow.payload.get("guided") is True,
                )
                return
            destination = {
                "plan_name": "plans",
                "stars_month_price": "starspage:0",
                "plan_duration": "plans",
                "plan_resource": "plans",
                "resource_reference": "resources",
                "method_name": "methods",
                "method_instruction": "methods",
                "method_rename": "methods",
                "method_edit": "methods",
                "timezone": "dashboard",
                "new_bot_token": "projects",
                "bot_token": "bots",
                "connection_rename": "bots",
                "currency_custom_code": "methods",
                "subscriber_search": "subscribers",
                "subscriber_message": "subscribers",
                "moderator_add": "modmanage",
            }.get(flow.step, "dashboard")
            if flow.step in ("method_rename", "method_edit"):
                method_hex = flow.payload.get("method_id")
                try:
                    destination = "method:" + UUID(hex=method_hex).hex
                except (TypeError, ValueError, AttributeError):
                    pass
            if flow.step == "price_amount" and flow.payload.get("currency") == "XTR":
                destination = "starspage:" + str(flow.payload.get("stars_page", 0))
            if flow.step in ("price_amount", "plan_rename") and (
                flow.payload.get("currency") != "XTR"
            ):
                plan_hex = flow.payload.get("plan_id")
                try:
                    destination = "plan:" + UUID(hex=plan_hex).hex
                except (TypeError, ValueError, AttributeError):
                    destination = "plans"
            await bot.send_message(
                message.chat.id,
                "⚠️ " + owner_error(str(exc))[:220]
                + tr("\nИсправьте данные и повторите действие.",
                     "\nCorrect the input and try again."),
                reply_markup=_markup([[_button(
                    tr("⬅️ Вернуться в раздел", "⬅️ Back to section"),
                    destination,
                )]]),
            )

    @router.message(StrayBotTokenFilter())
    async def stray_bot_token_handler(message: Message, bot: Bot) -> None:
        # An expired/replaced prompt must never leave a credential in chat.
        try:
            removed = await bot.delete_message(
                message.chat.id, message.message_id,
            )
        except AiogramError:
            removed = False
        if not removed:
            await bot.send_message(
                message.chat.id,
                tr("⚠️ Не удалось удалить токен. Удалите сообщение и "
                   "смените токен в @BotFather.",
                   "⚠️ Could not delete the token. Delete the message and "
                   "rotate your token in @BotFather."),
            )
            return
        await bot.send_message(
            message.chat.id,
            tr("🔐 Токен удалён из чата. Для подключения "
               "откройте @vimipay_bot → Подключить бота.",
               "🔐 Token removed from this chat. "
               "To connect your bot, open @vimipay_bot → Connect bot."),
        )

    if language_menu_renderers is not None:
        async def show_main_menu_after_language(bot: Bot, user: User) -> bool:
            actor_id = await actor(user)
            if fixed_project_id is not None and not await can_manage_project(
                factory, actor_id=actor_id, project_id=fixed_project_id,
            ):
                # Buyers must receive the real buyer storefront, not admin UI.
                return False
            await cancel_owner_flow(
                factory, actor_id=actor_id,
                bot_scope_id=bot.id if fixed_project_id else 0,
            )
            await show_home(bot, user.id, actor_id)
            return True

        language_menu_renderers.append(show_main_menu_after_language)

    return router
