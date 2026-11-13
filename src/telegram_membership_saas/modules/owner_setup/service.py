"""Tenant-safe owner setup operations for each connected bot and central registrar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from uuid import UUID

from aiogram import Bot
from cryptography.fernet import InvalidToken
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from telegram_membership_saas.config import get_settings
from telegram_membership_saas.db import apply_tenant_context, tenant_session_scope
from telegram_membership_saas.modules.bots import BotInstance
from telegram_membership_saas.modules.bots.health_service import (
    public_health_label,
    read_bot_health,
)
from telegram_membership_saas.modules.bots.service import (
    BotRegistration,
    decrypt_token,
    encrypt_token,
    verify_telegram_bot,
)
from telegram_membership_saas.modules.identity import TelegramUser
from telegram_membership_saas.modules.owner_setup.currencies import (
    PRIMARY_CURRENCIES,
    list_owner_currencies,
)
from telegram_membership_saas.modules.owner_setup.models import OwnerSetupSession
from telegram_membership_saas.modules.owner_setup.network_profiles import (
    bind_imported_drafts_on_first_channel,
    import_profile_into_new_project,
)
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    create_manual_payment_method,
)
from telegram_membership_saas.modules.plans import Plan, PlanResource
from telegram_membership_saas.modules.pricing import (
    PlanPrice,
    PlanPriceStatus,
    replace_active_plan_price,
)
from telegram_membership_saas.modules.projects import (
    Permission,
    Project,
    ProjectMember,
    ProjectMemberRole,
    ProjectStatus,
    has_permission,
)
from telegram_membership_saas.modules.resources import Resource, ResourceStatus, ResourceType
from telegram_membership_saas.modules.telegram_admin.service import _require_project_manager
from telegram_membership_saas.modules.telegram_commerce.service import (
    default_period_plan_name,
    list_checkout_options,
)
from telegram_membership_saas.project_worker_lock import checkout_retirement_lock
from telegram_membership_saas.tenant_context import TenantAccessDenied, TenantContext

FLOW_TTL = timedelta(minutes=15)
# Backwards-compatible default names; all configured currencies are
# validated against the active global registry and project selection.
ALLOWED_CURRENCIES = PRIMARY_CURRENCIES


@dataclass(frozen=True, slots=True)
class SetupFlow:
    step: str
    project_id: UUID | None
    payload: dict
    prompt_message_id: int | None
    generation: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerHome:
    projects: tuple[tuple[UUID, str], ...]
    selected: UUID | None
    archived_count: int = 0


@dataclass(frozen=True, slots=True)
class SetupOverview:
    project_id: UUID
    project_name: str
    timezone: str
    bots: tuple[tuple[str, str], ...]
    resources: tuple[tuple[UUID, str, str], ...]
    plans: tuple[tuple[UUID, str, str], ...]
    methods: tuple[tuple[UUID, str, str], ...]
    bot_runtime_label: str = "⚪ Ожидает запуска"
    live_bot_username: str | None = None
    sales_configured: bool = False
    bot_instances: tuple[tuple[UUID, str, str], ...] = ()
    can_delete_bot: bool = False


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


async def _current_session(
    session: AsyncSession, actor_id: UUID, bot_scope_id: int = 0,
) -> OwnerSetupSession:
    state = await session.get(
        OwnerSetupSession, (actor_id, bot_scope_id), with_for_update=True,
    )
    if state is None:
        state = OwnerSetupSession(
            user_id=actor_id, bot_scope_id=bot_scope_id, payload={},
        )
        session.add(state)
        await session.flush()
    return state


async def owner_home(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, bot_scope_id: int = 0,
    fixed_project_id: UUID | None = None,
) -> OwnerHome:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        visible = list(
            (await session.scalars(
                select(Project).order_by(Project.created_at, Project.id)
            )).all()
        )
        roles = dict((await session.execute(
            select(ProjectMember.project_id, ProjectMember.role).where(
                ProjectMember.user_id == actor_id
            )
        )).all())
        projects = [
            p for p in visible if p.status == ProjectStatus.ACTIVE.value
            and (
                p.owner_user_id == actor_id
                or (
                    p.id in roles and has_permission(
                        ProjectMemberRole(roles[p.id]), Permission.PROJECT_MANAGE
                    )
                )
            )
        ]
        state = await _current_session(session, actor_id, bot_scope_id)
        ids = {p.id for p in projects}
        if fixed_project_id is not None and fixed_project_id not in ids:
            raise PermissionError("Нет прав на управление этим ботом")
        selected = state.selected_project_id
        expected = (
            fixed_project_id if fixed_project_id is not None
            else selected if selected in ids
            else projects[0].id if len(projects) == 1 else None
        )
        if selected != expected:
            selected = expected
            state.selected_project_id = selected
            state.step = None
            state.prompt_message_id = None
            state.generation = None
            state.payload = {}
        return OwnerHome(
            projects=tuple((p.id, p.name) for p in projects),
            selected=selected,
            archived_count=sum(
                1 for p in visible
                if p.owner_user_id == actor_id
                and p.status == ProjectStatus.ARCHIVED.value
            ),
        )


async def can_manage_project(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID,
) -> bool:
    """Authorize tenant owner/manager without modifying central wizard state."""
    try:
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id, project_id=project_id)
        ) as session:
            await _require_project_manager(
                session, project_id=project_id, actor_user_id=actor_id,
            )
            return True
    except (PermissionError, TenantAccessDenied):
        return False


async def choose_project(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    project_id: UUID, bot_scope_id: int = 0,
) -> None:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        state = await _current_session(session, actor_id, bot_scope_id)
        state.selected_project_id = project_id
        state.step = None
        state.payload = {}
        state.prompt_message_id = None
        state.generation = None


async def back_owner_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, bot_scope_id: int = 0,
) -> SetupFlow | None:
    """Atomically exit one in-progress wizard and return its prior context.

    Back is navigation only: no plan/payment/subscription data is touched.
    The old generation and ForceReply become invalid immediately.
    """
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        state = await session.get(
            OwnerSetupSession, (actor_id, bot_scope_id), with_for_update=True,
        )
        if state is None:
            return None
        current = SetupFlow(
            step=state.step, project_id=state.selected_project_id,
            payload=dict(state.payload or {}),
            prompt_message_id=state.prompt_message_id,
            generation=state.generation,
        ) if state.step and state.expires_at > datetime.now(UTC) else None
        state.step = None
        state.payload = {}
        state.prompt_message_id = None
        state.generation = None
        state.expires_at = datetime.now(UTC)
        return current


async def cancel_owner_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, bot_scope_id: int = 0,
) -> bool:
    """Invalidate the exact bot-scoped pending wizard without touching project data."""
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id),
    ) as session:
        state = await session.get(
            OwnerSetupSession, (actor_id, bot_scope_id), with_for_update=True,
        )
        if state is None:
            return False
        # A consumed step may have an in-flight continuation. Its generation
        # must be invalidated too, or advance_flow() could resurrect the wizard.
        was_active = bool(state.step or state.generation or state.prompt_message_id)
        state.step = None
        state.payload = {}
        state.prompt_message_id = None
        state.generation = None
        state.expires_at = datetime.now(UTC)
        return was_active


async def set_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    step: str,
    payload: dict | None = None,
    project_id: UUID | None = None,
    bot_scope_id: int = 0,
) -> str:
    if len(step) > 48:
        raise ValueError("Invalid flow")
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id, bot_scope_id)
        chosen = project_id if project_id is not None else state.selected_project_id
        if chosen is not None:
            await _require_project_manager(
                session, project_id=chosen, actor_user_id=actor_id
            )
        elif step not in ("project_name", "new_bot_token", "managed_bot_create"):
            raise PermissionError("Сначала подключите бота")
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
    bot_scope_id: int = 0,
) -> None:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id, bot_scope_id)
        if state.step != step or state.generation != generation:
            raise ValueError("Этот шаг уже заменён другим запросом")
        state.prompt_message_id = message_id


async def is_expected_prompt(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, reply_message_id: int | None,
    bot_scope_id: int = 0,
) -> bool:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await session.get(OwnerSetupSession, (actor_id, bot_scope_id))
        return bool(
            state is not None
            and state.step
            and state.prompt_message_id is not None
            and (
                reply_message_id is None
                or state.prompt_message_id == reply_message_id
            )
            and state.expires_at > datetime.now(UTC)
        )


async def consume_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    reply_message_id: int | None,
    bot_scope_id: int = 0,
) -> SetupFlow | None:
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id, bot_scope_id)
        if (
            not state.step
            or state.prompt_message_id is None
            or (reply_message_id is not None
                and state.prompt_message_id != reply_message_id)
            or state.expires_at <= datetime.now(UTC)
        ):
            return None
        saved = SetupFlow(
            step=state.step,
            project_id=state.selected_project_id,
            payload=state.payload or {},
            prompt_message_id=state.prompt_message_id,
            generation=state.generation,
        )
        state.step = None
        state.prompt_message_id = None
        state.payload = {}
        return saved


async def consume_button_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    expected_step: str,
    request_id: int | None = None,
    generation: str | None = None,
    bot_scope_id: int = 0,
    discard: bool = False,
) -> SetupFlow:
    """Consume a button step; optionally discard it atomically on Back.

    A terminal Back must invalidate generation under the SAME row lock that
    verified it. A second unconditional cancel could wipe a newer wizard.
    """
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id, bot_scope_id)
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
            generation=state.generation,
        )
        state.step = None
        state.payload = {}
        state.prompt_message_id = None
        if discard:
            state.generation = None
            state.expires_at = datetime.now(UTC)
        return flow


async def advance_flow(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    source: SetupFlow,
    step: str,
    payload: dict,
    bot_scope_id: int = 0,
) -> str:
    """Continue only the exact consumed flow and project, atomically."""
    if source.generation is None:
        raise ValueError("Шаг настройки устарел")
    async with tenant_session_scope(factory, TenantContext(user_id=actor_id)) as session:
        state = await _current_session(session, actor_id, bot_scope_id)
        if (
            state.step is not None
            or state.selected_project_id != source.project_id
            or state.generation != source.generation
        ):
            raise ValueError("Проект или шаг изменился. Начните действие заново.")
        if source.project_id is not None:
            await _require_project_manager(
                session, project_id=source.project_id, actor_user_id=actor_id
            )
        state.step = step
        state.payload = payload
        state.prompt_message_id = None
        state.generation = token_urlsafe(12)
        state.expires_at = datetime.now(UTC) + FLOW_TTL
        return state.generation


async def get_selected_project(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, bot_scope_id: int = 0,
    fixed_project_id: UUID | None = None,
) -> UUID | None:
    home = await owner_home(
        factory, actor_id=actor_id, bot_scope_id=bot_scope_id,
        fixed_project_id=fixed_project_id,
    )
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
        state.prompt_message_id = None
        state.generation = None
        state.payload = {}
        return project.id


async def register_new_owner_bot(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID,
    actor_telegram_user_id: int,
    token: str,
    encryption_key: SecretStr | None,
    central_bot_id: int,
    expected_bot_id: int | None = None,
    managed_creation_generation: str | None = None,
) -> tuple[UUID, BotRegistration]:
    """Verify first; commit new project and unique bot together, or neither."""
    verified = await verify_telegram_bot(token)
    if expected_bot_id is not None and verified.telegram_bot_id != expected_bot_id:
        raise ValueError("Managed bot identity does not match Telegram token")
    if verified.telegram_bot_id == central_bot_id:
        raise ValueError("Нельзя подключить управляющий бот ViMi Pay")
    encrypted = encrypt_token(token, key=encryption_key)
    try:
        async with tenant_session_scope(
            factory, TenantContext(user_id=actor_id)
        ) as session:
            actual_id = await session.scalar(
                select(TelegramUser.telegram_user_id).where(
                    TelegramUser.id == actor_id,
                )
            )
            if actual_id != actor_telegram_user_id:
                raise PermissionError("Не удалось подтвердить владельца бота")
            # Managed creation is authorized and consumed atomically together
            # with BotInstance+Project. Any failure rolls back the entire
            # transaction and preserves the candidate for another attempt.
            if managed_creation_generation is not None:
                state = await _current_session(session, actor_id, 0)
                if (
                    state.step != "managed_bot_create"
                    or state.generation != managed_creation_generation
                    or state.expires_at <= datetime.now(UTC)
                    or (state.payload or {}).get("candidate_bot_id")
                    != expected_bot_id
                ):
                    raise ValueError("Managed creation request expired or replaced")
            project = Project(
                owner_user_id=actor_id, name="@" + verified.username,
                timezone="Europe/Moscow",
            )
            session.add(project)
            await session.flush()
            # Project exists in this transaction: bind tenant RLS before
            # writing the credential, while keeping the insert atomic.
            await apply_tenant_context(
                session, TenantContext(user_id=actor_id, project_id=project.id),
            )
            session.add(BotInstance(
                project_id=project.id,
                telegram_bot_id=verified.telegram_bot_id,
                username=verified.username,
                display_name=verified.display_name,
                token_ciphertext=encrypted,
                status="registered",
            ))
            await session.flush()
            # The new project is empty and isolated from all other bots.
            # A failed template import rolls back only copied commercial
            # configuration and never blocks successful bot registration.
            await import_profile_into_new_project(
                session, actor_id=actor_id, project_id=project.id,
            )
            state = await _current_session(session, actor_id, 0)
            state.selected_project_id = project.id
            state.step = None
            state.prompt_message_id = None
            state.generation = None
            state.payload = {}
            return project.id, BotRegistration(
                telegram_bot_id=verified.telegram_bot_id,
                username=verified.username, updated=False,
            )
    except IntegrityError:
        # Global bot ID uniqueness prevents a bot being claimed twice.
        # The same DB rollback also removes the newly created project.
        raise ValueError("Этот Telegram-бот уже подключён в другом проекте") from None
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "P6711":
            raise ValueError(
                "Подключение нового бота временно ограничено: у владельца "
                "есть просроченный счёт ViMi Pay. Действующие боты и "
                "подписчики продолжают работать. / New bot connections "
                "are paused until the owner's overdue invoice is paid."
            ) from None
        if getattr(exc.orig, "sqlstate", None) == "P6703":
            raise ValueError(
                "Во время бесплатного ознакомления доступен только один бот "
                "(72 часа). / Only one bot is allowed during the 72-hour trial."
            ) from None
        if getattr(exc.orig, "sqlstate", None) == "P6701":
            raise ValueError(
                "Достигнут установленный лимит подключённых ботов. "
                "Удалите ненужное подключение или обратитесь в поддержку ViMi Pay."
            ) from None
        raise


async def set_default_project_name_from_bot(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, username: str,
) -> None:
    """Name an automatically created project after its verified Telegram bot."""
    if not username or len(username) > 32:
        raise ValueError("Некорректное имя бота")
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
        )
        project = await session.scalar(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        if project is not None and project.name == "Подключение бота":
            project.name = "@" + username


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
            Resource.project_id == project_id,
            Resource.removed_at.is_(None),
        ))).all())
        plans = list((await session.scalars(select(Plan).where(
            Plan.project_id == project_id,
            Plan.removed_at.is_(None),
        ))).all())
        methods = list((await session.scalars(select(PaymentMethod).where(
            PaymentMethod.project_id == project_id
        ))).all())
        sales_configured = bool(await list_checkout_options(
            session, project_id=project_id
        ))
        health = await read_bot_health(session, project_id=project_id)
        eligible_bots = [b for b in bots if b.status == "registered"]
        runtime_label, live = (
            public_health_label(
                health, telegram_bot_id=eligible_bots[0].telegram_bot_id
            )
            if len(eligible_bots) == 1
            else ("⚪ Не подключён", False)
        )
        return SetupOverview(
            bot_runtime_label=(
                "🟡 Бот работает, но тарифы или оплата не настроены"
                if live and not sales_configured else runtime_label
            ),
            live_bot_username=(
                eligible_bots[0].username if live and sales_configured else None
            ),
            sales_configured=sales_configured,
            project_id=project_id,
            project_name=project.name,
            timezone=project.timezone,
            bots=tuple((b.username, b.status) for b in bots),
            resources=tuple((r.id, r.name, r.status) for r in resources),
            plans=tuple((p.id, p.name, p.status) for p in plans),
            methods=tuple((m.id, m.name, m.status) for m in methods),
            bot_instances=tuple((b.id, b.username, b.status) for b in bots),
            can_delete_bot=project.owner_user_id == actor_id,
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
        # Serialize verified-channel insertion and the first-resource decision
        # for a project. Concurrent channel selections cannot accidentally
        # bind imported drafts to TWO different private channels.
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id,
            lock=True,
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
                name=(chat.title or "Канал")[:120],
            )
            session.add(resource)
        else:
            resource.status = ResourceStatus.ACTIVE.value
            resource.removed_at = None
        try:
            await session.flush()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "P6711":
                raise ValueError(
                    "Добавление нового канала временно ограничено из-за "
                    "просроченного счёта ViMi Pay. Действующие каналы и "
                    "подписчики продолжают работать. / New channel connections "
                    "are paused until the owner's overdue invoice is paid."
                ) from None
            if getattr(exc.orig, "sqlstate", None) == "P6501":
                raise ValueError(
                    "Этот канал уже использовался в другом вашем подключении. "
                    "Откройте прежнее подключение в @vimipay_bot → Мои подключения "
                    "(при необходимости восстановите его из архива) и "
                    "подключите нового бота там. Финансовая история сохранится."
                ) from None
            if getattr(exc.orig, "sqlstate", None) == "P6702":
                raise ValueError(
                    "У этого канала есть финансовая история в удалённом подключении. "
                    "Обратитесь в поддержку ViMi Pay для безопасного восстановления."
                ) from None
            raise
        # Only projects seeded from a network profile receive automatic
        # linking. The FIRST verified channel binds unassigned draft plans;
        # no existing published plan or other project's resource changes.
        await bind_imported_drafts_on_first_channel(
            session, actor_id=actor_id, project_id=project_id,
            resource_id=resource.id,
        )
        return resource


async def create_owner_plan(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID, name: str,
    duration_value: int | None, duration_unit: str,
    resource_id: UUID,
) -> UUID:
    title = name.strip()
    if title and not 2 <= len(title) <= 120:
        raise ValueError("Название тарифа: от 2 до 120 символов")
    if duration_unit not in ("day", "week", "month", "year", "lifetime"):
        raise ValueError("Неподдерживаемая длительность")
    if (duration_unit == "lifetime") != (duration_value is None):
        raise ValueError("Укажите корректный срок тарифа")
    if duration_value is not None and not 1 <= duration_value <= 365:
        raise ValueError("Неверный срок тарифа")
    if not title:
        # Existing DB/payment snapshots require a nonempty title. The buyer
        # storefront suppresses the generated period-only name.
        title = default_period_plan_name(duration_value, duration_unit)
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        resource = await session.scalar(select(Resource.id).where(
            Resource.id == resource_id, Resource.project_id == project_id,
            Resource.status == ResourceStatus.ACTIVE.value,
            Resource.removed_at.is_(None),
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
        # Editing a price invalidates earlier permanent-delete confirmations.
        await session.execute(
            update(Plan).where(
                Plan.id == plan_id, Plan.project_id == project_id,
            ).values(updated_at=func.clock_timestamp())
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
    expected_status: str = "draft",
    expected_revision: str | None = None,
) -> str:
    async with tenant_session_scope(
        factory, TenantContext(user_id=actor_id, project_id=project_id)
    ) as session:
        await checkout_retirement_lock(session, project_id)
        await _require_project_manager(
            session, project_id=project_id, actor_user_id=actor_id
        )
        plan = await session.scalar(select(Plan).where(
            Plan.id == plan_id, Plan.project_id == project_id
        ).with_for_update())
        if plan is None or plan.removed_at is not None:
            raise ValueError("Тариф не принадлежит выбранному проекту")
        if plan.status != expected_status:
            raise ValueError("Тариф изменился. Откройте карточку заново.")
        if expected_revision is not None:
            from telegram_membership_saas.modules.owner_setup.plan_management import (
                plan_revision,
            )
            if plan_revision(plan) != expected_revision:
                raise ValueError("Тариф изменился. Откройте карточку заново.")
        linked = await session.scalar(select(PlanResource.resource_id).join(
            Resource, PlanResource.resource_id == Resource.id
        ).where(
            PlanResource.project_id == project_id, PlanResource.plan_id == plan_id,
            Resource.project_id == project_id,
            Resource.status == ResourceStatus.ACTIVE.value,
            Resource.removed_at.is_(None),
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
        # Telegram Stars is an in-app native payment rail. It must never
        # require invented bank details or a manual payment method.
        if available is None and "XTR" not in priced:
            raise ValueError(
                "Добавьте действующий способ оплаты для валюты тарифа"
            )
        plan.status = "active"
        await session.flush()
        return plan.name


async def owner_available_currencies(
    factory: async_sessionmaker[AsyncSession], *,
    actor_id: UUID, project_id: UUID | None = None,
    fiat_only: bool = False,
) -> tuple[str, ...]:
    return await list_owner_currencies(
        factory, actor_id=actor_id, project_id=project_id,
        fiat_only=fiat_only,
    )
