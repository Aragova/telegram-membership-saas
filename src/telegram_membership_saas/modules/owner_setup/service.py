"""Tenant-safe, durable setup operations for the central ViMi Pay owner console."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from uuid import UUID

from aiogram import Bot
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.service import decrypt_token
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.owner_setup.models import OwnerSetupSession
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    create_manual_payment_method,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import (
    CurrencyAsset,
    PlanPrice,
    PlanPriceStatus,
    replace_active_plan_price,
)
from telegram_membership_saas.modules.projects import Project
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.tenant_context import TenantContext

FLOW_TTL = timedelta(minutes=15)
ALLOWED_CURRENCIES = ("RUB", "USD", "EUR", "USDT", "TON", "KZT", "AED")


@dataclass(frozen=True, slots=True)
class SetupFlow:
    step: str
    project_id: UUID | None
    payload: dict
    prompt_message_id: int | None


@dataclass(frozen=True, slots=True)
class OwnerHome:
    projects: tuple[tuple[UUID, str], ...]
    selected: UUID | None


@dataclass(frozen=True, slots=True)
class SetupOverview:
    project_id: UUID
    project_name: str
    timezone: str
    bots: tuple[tuple[str, str], ...]
    resources: tuple[tuple[UUID, str, str], ...]
    plans: tuple[tuple[UUID, str, str], ...]
    methods: tuple[tuple[UUID, str, str], ...]


async def ensure_telegram_actor(
    factory: async_sessionmaker[AsyncSession],
    *,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
) -> UUID:
    """Only global TelegramUser identity is handled outside a tenant scope."""
    if telegram_id <= 0:
        raise ValueError("Invalid Telegram identity")
    async with factory() as session:
        async with session.begin():
            result = await session.execute(
                insert(TelegramUser)
                .values(
                    telegram_user_id=telegram_id,
                    username=username,
                    first_name=first_name,
                )
                .on_conflict_do_update(
                    index_elements=[TelegramUser.telegram_user_id],
                    set_={"username": username, "first_name": first_name},
                )
                .returning(TelegramUser.id)
            )
            return result.scalar_one()


async def _current_session(session: AsyncSession, actor_id: UUID) -> OwnerSetupSession:
    state = await session.get(OwnerSetupSession, actor_id, with_for_update=True)
    if state is None:
        state = OwnerSetupSession(user_id=actor_id, payload={})
        session.add(state)
        await session.flush()
    return state


async def owner_home(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
) -> OwnerHome:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        projects = list(
            (await session.scalars(select(Project).order_by(Project.created_at, Project.id))).all()
        )
        state = await _current_session(session, actor_id)
        ids = {p.id for p in projects}
        selected = state.selected_project_id
        if selected not in ids:
            selected = projects[0].id if len(projects) == 1 else None
            state.selected_project_id = selected
            state.step = None
            state.prompt_message_id = None
            state.generation = None
            state.payload = {}
        return OwnerHome(
            projects=tuple((p.id, p.name) for p in projects),
            selected=selected,
        )


async def choose_project(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    project_id: UUID,
) -> None:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        state = await _current_session(session, actor_id)
        state.selected_project_id = project_id
        state.step = None
        state.payload = {}
        state.prompt_message_id = None
        state.generation = None


async def set_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    step: str,
    payload: dict | None = None,
    project_id: UUID | None = None,
) -> str:
    if len(step) > 48:
        raise ValueError("Invalid flow")
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id)
        chosen = project_id if project_id is not None else state.selected_project_id
        if chosen is not None:
            await _require_project_manager(
                session, project_id=chosen, actor_user_id=actor_id
            )
        elif step != "project_name":
            raise PermissionError("Сначала создайте проект")
        state.selected_project_id = chosen
        state.step = step
        state.payload = payload or {}
        state.prompt_message_id = None
        state.generation = token_urlsafe(12)
        state.expires_at = datetime.now(UTC) + FLOW_TTL
        return state.generation


async def bind_prompt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    step: str,
    message_id: int,
    generation: str,
) -> None:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id)
        if state.step != step or state.generation != generation:
            raise ValueError("Этот шаг уже заменён другим запросом")
        state.prompt_message_id = message_id


async def is_expected_prompt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, reply_message_id: int,
) -> bool:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await session.get(OwnerSetupSession, actor_id)
        return bool(
            state is not None
            and state.step
            and state.prompt_message_id == reply_message_id
            and state.expires_at > datetime.now(UTC)
        )


async def consume_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    reply_message_id: int | None,
) -> SetupFlow | None:
    if reply_message_id is None:
        return None
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id)
        if (
            not state.step or state.prompt_message_id != reply_message_id
            or state.expires_at <= datetime.now(UTC)
        ):
            return None
        saved = SetupFlow(
            step=state.step,
            project_id=state.selected_project_id,
            payload=state.payload or {},
            prompt_message_id=state.prompt_message_id,
        )
        state.step = None
        state.prompt_message_id = None
        state.generation = None
        state.payload = {}
        return saved


async def consume_button_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    expected_step: str,
    request_id: int | None = None,
    generation: str | None = None,
) -> SetupFlow:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id)
        if (
            state.step != expected_step
            or state.expires_at <= datetime.now(UTC)
            or (generation is not None and state.generation != generation)
            or (
                request_id is not None
                and state.payload.get("request_id") != request_id
            )
        ):
            raise ValueError("Шаг устарел. Откройте нужный раздел заново.")
        flow = SetupFlow(
            step=state.step,
            project_id=state.selected_project_id,
            payload=state.payload or {},
            prompt_message_id=state.prompt_message_id,
        )
        state.step = None
        state.payload = {}
        state.prompt_message_id = None
        state.generation = None
        return flow


async def get_selected_project(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
) -> UUID | None:
    home = await owner_home(factory, actor_id=actor_id)
    return home.selected


async def create_owner_project(
    factory: async_sessionmaker[AsyncSession], *, actor_id: UUID, name: str,
) -> UUID:
    title = name.strip()
    if not 2 <= len(title) <= 120:
        raise ValueError("Название проекта должно содержать от 2 до 120 символов")
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        project = Project(owner_user_id=actor_id, name=title, timezone="Europe/Moscow")
        session.add(project)
        await session.flush()
        state = await _current_session(session, actor_id)
        state.selected_project_id = project.id
        state.step = None
        state.payload = {}
        return project.id


async def setup_overview(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> SetupOverview:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        project = await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        bots = list((await session.scalars(select(BotInstance).where(
            BotInstance.project_id == project_id
        ))).all())
        resources = list((await session.scalars(select(Resource).where(
            Resource.project_id == project_id
        ))).all())
        plans = list((await session.scalars(select(Plan).where(
            Plan.project_id == project_id
        ))).all())
        methods = list((await session.scalars(select(PaymentMethod).where(
            PaymentMethod.project_id == project_id
        ))).all())
        return SetupOverview(
            project_id=project_id,
            project_name=project.name,
            timezone=project.timezone,
            bots=tuple((b.username, b.status) for b in bots),
            resources=tuple((r.id, r.name, r.status) for r in resources),
            plans=tuple((p.id, p.name, p.status) for p in plans),
            methods=tuple((m.id, m.name, m.status) for m in methods),
        )


async def verify_channel_and_add(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    actor_telegram_id: int,
    project_id: UUID,
    chat_reference: str | int,
) -> Resource:
    """Verify both the channel admin and the exact registered customer bot."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        bots = list((await session.scalars(select(BotInstance).where(
            BotInstance.project_id == project_id, BotInstance.status == "registered"
        ))).all())
        if len(bots) != 1:
            raise ValueError("Сначала подключите одного собственного бота")
        encrypted = bots[0].token_ciphertext
        expected_bot_id = bots[0].telegram_bot_id
    key = get_settings().bot_token_encryption_key
    try:
        token = decrypt_token(encrypted, key=key)
        bot = Bot(token=token)
    except (InvalidToken, ValueError, RuntimeError):
        raise ValueError("Подключённый бот требует повторной проверки токена") from None
    try:
        try:
            chat = await bot.get_chat(chat_id=chat_reference)
            chat_type = getattr(chat.type, "value", chat.type)
            if chat_type not in ("channel", "supergroup"):
                raise ValueError("Поддерживаются только каналы и супергруппы")
            identity = await bot.get_me()
            if identity.id != expected_bot_id:
                raise ValueError("Токен подключённого бота изменился")
            own = await bot.get_chat_member(chat_id=chat.id, user_id=expected_bot_id)
            owner = await bot.get_chat_member(
                chat_id=chat.id, user_id=actor_telegram_id
            )
            own_role = getattr(own.status, "value", own.status)
            owner_role = getattr(owner.status, "value", owner.status)
            if owner_role not in ("creator", "administrator"):
                raise ValueError("Вы должны быть администратором выбранного канала")
            if own_role != "creator" and (
                own_role != "administrator" or not own.can_invite_users
                or not own.can_restrict_members
            ):
                raise ValueError(
                    "Выдайте своему боту права добавления и блокировки пользователей"
                )
        except ValueError:
            raise
        except Exception:
            raise ValueError(
                "Не удалось проверить канал. Добавьте своего бота администратором "
                "и повторите выбор."
            ) from None
    finally:
        await bot.session.close()
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        external_id = str(chat.id)
        resource = await session.scalar(select(Resource).where(
            Resource.project_id == project_id, Resource.external_id == external_id
        ).with_for_update())
        if resource is None:
            resource = Resource(
                project_id=project_id,
                external_id=external_id,
                resource_type=(
                    ResourceType.TELEGRAM_CHANNEL.value if chat_type == "channel"
                    else ResourceType.TELEGRAM_GROUP.value
                ),
                name=chat.title or "Канал",
            )
            session.add(resource)
        else:
            resource.status = ResourceStatus.ACTIVE.value
        await session.flush()
        return resource


async def create_owner_plan(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, name: str,
    duration_value: int | None, duration_unit: str,
    resource_id: UUID,
) -> UUID:
    title = name.strip()
    if not 2 <= len(title) <= 120:
        raise ValueError("Название тарифа: от 2 до 120 символов")
    if duration_unit not in ("day", "week", "month", "year", "lifetime"):
        raise ValueError("Неподдерживаемая длительность")
    if (duration_unit == "lifetime") != (duration_value is None):
        raise ValueError("Укажите корректный срок тарифа")
    if duration_value is not None and not 1 <= duration_value <= 365:
        raise ValueError("Неверный срок тарифа")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        resource = await session.scalar(select(Resource.id).where(
            Resource.id == resource_id, Resource.project_id == project_id,
            Resource.status == ResourceStatus.ACTIVE.value
        ))
        if resource is None:
            raise ValueError("Выберите действующий канал или группу")
        plan = Plan(
            project_id=project_id, name=title,
            duration_value=duration_value, duration_unit=duration_unit,
            status="draft",
        )
        session.add(plan)
        await session.flush()
        session.add(PlanResource(
            project_id=project_id, plan_id=plan.id, resource_id=resource_id
        ))
        await session.flush()
        return plan.id


async def save_owner_price(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID,
    currency_code: str, amount: str,
) -> None:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        await replace_active_plan_price(
            session, project_id=project_id, plan_id=plan_id,
            currency_code=currency_code, amount=amount,
        )


async def create_owner_payment_method(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
    name: str, currency_code: str, instruction: str,
) -> None:
    if len(instruction) > 4000:
        raise ValueError("Инструкция слишком длинная (максимум 4000 символов)")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        await create_manual_payment_method(
            session, project_id=project_id, name=name,
            currency_codes=[currency_code], instruction_template=instruction,
        )


async def publish_owner_plan(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, plan_id: UUID,
) -> str:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        plan = await session.scalar(select(Plan).where(
            Plan.id == plan_id, Plan.project_id == project_id
        ).with_for_update())
        if plan is None:
            raise ValueError("Тариф не принадлежит выбранному проекту")
        linked = await session.scalar(select(PlanResource.resource_id).join(
            Resource, PlanResource.resource_id == Resource.id
        ).where(
            PlanResource.project_id == project_id, PlanResource.plan_id == plan_id,
            Resource.project_id == project_id,
            Resource.status == ResourceStatus.ACTIVE.value,
        ).limit(1))
        if linked is None:
            raise ValueError("Привяжите к тарифу действующий канал")
        priced = list((await session.scalars(select(PlanPrice.currency_code).where(
            PlanPrice.project_id == project_id, PlanPrice.plan_id == plan_id,
            PlanPrice.status == PlanPriceStatus.ACTIVE.value,
        ))).all())
        if not priced:
            raise ValueError("Сначала назначьте цену тарифа")
        available = await session.scalar(
            select(PaymentMethodCurrency.currency_code).join(
                PaymentMethod,
                PaymentMethod.id == PaymentMethodCurrency.payment_method_id,
            ).where(
                PaymentMethod.project_id == project_id,
                PaymentMethodCurrency.project_id == project_id,
                PaymentMethod.status == "active",
                PaymentMethodCurrency.currency_code.in_(priced),
            ).limit(1)
        )
        if available is None:
            raise ValueError(
                "Добавьте действующий способ оплаты для валюты тарифа"
            )
        plan.status = "active"
        await session.flush()
        return plan.name


async def owner_available_currencies(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
) -> tuple[str, ...]:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        registered = set((await session.scalars(
            select(CurrencyAsset.code).where(CurrencyAsset.status == "active")
        )).all())
        return tuple(x for x in ALLOWED_CURRENCIES if x in registered)
