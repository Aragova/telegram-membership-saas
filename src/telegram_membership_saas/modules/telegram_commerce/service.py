from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_membership_saas.modules.identity import ProjectCustomer, TelegramUser
from telegram_membership_saas.modules.payments import (
    PaymentMethod,
    PaymentMethodCurrency,
    PaymentMethodStatus,
    PaymentRequest,
    PaymentRequestStatus,
    approve_payment_request,
    cancel_payment_request,
    create_payment_request,
    get_or_create_project_customer,
    mark_payment_request_submitted,
    reject_payment_request,
    render_manual_instruction,
)
from telegram_membership_saas.modules.payments.request_service import require_payment_permission
from telegram_membership_saas.modules.plans import DurationUnit, Plan, PlanStatus
from telegram_membership_saas.modules.pricing import (
    CurrencyAsset,
    CurrencyAssetStatus,
    PlanPrice,
    PlanPriceStatus,
)
from telegram_membership_saas.modules.projects import (
    Permission,
    Project,
    ProjectMember,
    ProjectStatus,
)


@dataclass(frozen=True, slots=True)
class CheckoutOption:
    plan_id: UUID
    plan_name: str
    duration_value: int | None
    duration_unit: str
    plan_price_id: UUID
    currency_code: str
    amount: Decimal
    currency_precision: int
    payment_method_id: UUID
    payment_method_name: str


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    request: PaymentRequest
    option: CheckoutOption
    instruction: str


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    request: PaymentRequest
    item: ReviewItem
    reviewer_telegram_ids: tuple[int, ...]
    newly_submitted: bool


@dataclass(frozen=True, slots=True)
class ReviewItem:
    request_id: UUID
    customer_telegram_user_id: int
    customer_username: str | None
    plan_name: str
    amount: Decimal
    currency_code: str
    payment_method_name: str
    status: str


@dataclass(frozen=True, slots=True)
class ReviewResult:
    item: ReviewItem
    changed: bool


def encode_uuid(value: UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode("ascii").rstrip("=")


def decode_uuid(value: str) -> UUID:
    if len(value) != 22:
        raise ValueError("Invalid compact UUID")
    try:
        raw = base64.urlsafe_b64decode(value + "==")
    except Exception as exc:
        raise ValueError("Invalid compact UUID") from exc
    if len(raw) != 16:
        raise ValueError("Invalid compact UUID")
    return UUID(bytes=raw)


def format_amount(amount: Decimal, precision: int) -> str:
    if precision < 0 or precision > 18:
        raise ValueError("Currency precision must be between 0 and 18")
    with localcontext() as context:
        context.prec = 50
        quantized = amount.quantize(Decimal(1).scaleb(-precision))
    text = f"{quantized:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_period(duration_value: int | None, duration_unit: str) -> str:
    if duration_unit == DurationUnit.LIFETIME.value:
        return "навсегда"
    if duration_value is None or duration_value <= 0:
        raise ValueError("Finite plan duration must be positive")

    labels = {
        DurationUnit.DAY.value: ("день", "дня", "дней"),
        DurationUnit.WEEK.value: ("неделя", "недели", "недель"),
        DurationUnit.MONTH.value: ("месяц", "месяца", "месяцев"),
        DurationUnit.YEAR.value: ("год", "года", "лет"),
    }
    forms = labels.get(duration_unit)
    if forms is None:
        raise ValueError("Unsupported plan duration unit")

    remainder100 = duration_value % 100
    remainder10 = duration_value % 10
    if 11 <= remainder100 <= 14:
        form = forms[2]
    elif remainder10 == 1:
        form = forms[0]
    elif 2 <= remainder10 <= 4:
        form = forms[1]
    else:
        form = forms[2]
    return f"{duration_value} {form}"


async def upsert_telegram_user(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    username: str | None,
    first_name: str | None,
) -> TelegramUser:
    normalized_username = username.strip() if username else None
    normalized_first_name = first_name.strip() if first_name else None
    update_values: dict[str, str] = {}
    if normalized_username:
        update_values["username"] = normalized_username
    if normalized_first_name:
        update_values["first_name"] = normalized_first_name

    statement = insert(TelegramUser).values(
        telegram_user_id=telegram_user_id,
        username=normalized_username or None,
        first_name=normalized_first_name or None,
    )
    if update_values:
        statement = statement.on_conflict_do_update(
            index_elements=[TelegramUser.telegram_user_id],
            set_=update_values,
        )
    else:
        statement = statement.on_conflict_do_nothing(
            index_elements=[TelegramUser.telegram_user_id]
        )
    statement = statement.returning(TelegramUser.id)
    user_id = await session.scalar(statement)
    if user_id is None:
        user_id = await session.scalar(
            select(TelegramUser.id).where(
                TelegramUser.telegram_user_id == telegram_user_id
            )
        )
    if user_id is None:
        raise RuntimeError("Telegram user could not be upserted")
    user = await session.scalar(select(TelegramUser).where(TelegramUser.id == user_id))
    if user is None:
        raise RuntimeError("Telegram user disappeared after upsert")
    return user


async def _require_active_project(session: AsyncSession, project_id: UUID) -> Project:
    project = await session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.status == ProjectStatus.ACTIVE.value,
        )
    )
    if project is None:
        raise ValueError("Project is not active or accessible")
    return project


def _catalog_statement(project_id: UUID):
    return (
        select(
            Plan,
            PlanPrice,
            CurrencyAsset.precision,
            PaymentMethod,
        )
        .join(
            PlanPrice,
            and_(
                PlanPrice.plan_id == Plan.id,
                PlanPrice.project_id == Plan.project_id,
            ),
        )
        .join(CurrencyAsset, CurrencyAsset.code == PlanPrice.currency_code)
        .join(
            PaymentMethodCurrency,
            and_(
                PaymentMethodCurrency.project_id == Plan.project_id,
                PaymentMethodCurrency.currency_code == PlanPrice.currency_code,
            ),
        )
        .join(
            PaymentMethod,
            and_(
                PaymentMethod.id == PaymentMethodCurrency.payment_method_id,
                PaymentMethod.project_id == PaymentMethodCurrency.project_id,
            ),
        )
        .where(
            Plan.project_id == project_id,
            Plan.status == PlanStatus.ACTIVE.value,
            PlanPrice.status == PlanPriceStatus.ACTIVE.value,
            CurrencyAsset.status == CurrencyAssetStatus.ACTIVE.value,
            PaymentMethod.status == PaymentMethodStatus.ACTIVE.value,
        )
        .order_by(
            Plan.sort_order,
            Plan.name,
            PlanPrice.currency_code,
            PaymentMethod.sort_order,
            PaymentMethod.name,
        )
    )


def _option_from_row(row) -> CheckoutOption:
    plan, price, precision, method = row
    return CheckoutOption(
        plan_id=plan.id,
        plan_name=plan.name,
        duration_value=plan.duration_value,
        duration_unit=plan.duration_unit,
        plan_price_id=price.id,
        currency_code=price.currency_code,
        amount=price.amount,
        currency_precision=precision,
        payment_method_id=method.id,
        payment_method_name=method.name,
    )


async def list_checkout_options(
    session: AsyncSession,
    *,
    project_id: UUID,
) -> tuple[CheckoutOption, ...]:
    await _require_active_project(session, project_id)
    rows = (await session.execute(_catalog_statement(project_id))).all()
    return tuple(_option_from_row(row) for row in rows)


async def _load_checkout_option(
    session: AsyncSession,
    *,
    project_id: UUID,
    plan_price_id: UUID,
    payment_method_id: UUID,
) -> CheckoutOption:
    row = (
        await session.execute(
            _catalog_statement(project_id).where(
                PlanPrice.id == plan_price_id,
                PaymentMethod.id == payment_method_id,
            )
        )
    ).first()
    if row is None:
        raise ValueError("Selected plan price/payment method is no longer available")
    return _option_from_row(row)


async def create_checkout_for_telegram_user(
    session: AsyncSession,
    *,
    project_id: UUID,
    telegram_user_id: int,
    username: str | None,
    first_name: str | None,
    plan_price_id: UUID,
    payment_method_id: UUID,
) -> CheckoutResult:
    await _require_active_project(session, project_id)
    option = await _load_checkout_option(
        session,
        project_id=project_id,
        plan_price_id=plan_price_id,
        payment_method_id=payment_method_id,
    )
    user = await upsert_telegram_user(
        session,
        telegram_user_id=telegram_user_id,
        username=username,
        first_name=first_name,
    )
    customer = await get_or_create_project_customer(
        session,
        project_id=project_id,
        user_id=user.id,
    )
    request = await create_payment_request(
        session,
        project_id=project_id,
        customer_id=customer.id,
        plan_id=option.plan_id,
        plan_price_id=option.plan_price_id,
        payment_method_id=option.payment_method_id,
    )
    method = await session.scalar(
        select(PaymentMethod).where(
            PaymentMethod.id == option.payment_method_id,
            PaymentMethod.project_id == project_id,
        )
    )
    if method is None:
        raise RuntimeError("Payment method disappeared while checkout was being created")

    instruction = render_manual_instruction(
        method.instruction_template,
        amount=format_amount(request.final_amount, option.currency_precision),
        currency=request.currency_code,
        plan=option.plan_name,
        period=format_period(request.duration_value, request.duration_unit),
    )
    return CheckoutResult(request=request, option=option, instruction=instruction)


async def _customer_for_telegram_user(
    session: AsyncSession,
    *,
    project_id: UUID,
    telegram_user_id: int,
) -> ProjectCustomer | None:
    return await session.scalar(
        select(ProjectCustomer)
        .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
        .where(
            ProjectCustomer.project_id == project_id,
            TelegramUser.telegram_user_id == telegram_user_id,
        )
    )


async def _reviewer_telegram_ids(
    session: AsyncSession,
    *,
    project_id: UUID,
) -> tuple[int, ...]:
    project = await _require_active_project(session, project_id)
    owner_telegram_id = await session.scalar(
        select(TelegramUser.telegram_user_id).where(TelegramUser.id == project.owner_user_id)
    )
    member_ids = tuple(
        (
            await session.scalars(
                select(TelegramUser.telegram_user_id)
                .join(ProjectMember, ProjectMember.user_id == TelegramUser.id)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.role.in_(("admin", "payment_manager")),
                )
                .order_by(TelegramUser.telegram_user_id)
            )
        ).all()
    )
    ordered = []
    if owner_telegram_id is not None:
        ordered.append(owner_telegram_id)
    ordered.extend(member_ids)
    return tuple(dict.fromkeys(ordered))


async def require_awaiting_receipt_for_telegram_user(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
    telegram_user_id: int,
) -> None:
    """Verify a receipt prompt belongs to the buyer without submitting the request."""
    await _require_active_project(session, project_id)
    customer = await _customer_for_telegram_user(
        session,
        project_id=project_id,
        telegram_user_id=telegram_user_id,
    )
    if customer is None:
        raise PermissionError("Payment request does not belong to this Telegram user")
    request = await session.scalar(
        select(PaymentRequest).where(
            PaymentRequest.id == request_id,
            PaymentRequest.project_id == project_id,
        )
    )
    if request is None or request.customer_id != customer.id:
        raise PermissionError("Payment request does not belong to this Telegram user")
    if request.status != PaymentRequestStatus.WAITING_CONFIRMATION.value:
        raise ValueError("Заявка уже отправлена или закрыта")


async def submit_checkout_for_telegram_user(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
    telegram_user_id: int,
) -> SubmissionResult:
    customer = await _customer_for_telegram_user(
        session,
        project_id=project_id,
        telegram_user_id=telegram_user_id,
    )
    if customer is None:
        raise PermissionError("Payment request does not belong to this Telegram user")

    request = await session.scalar(
        select(PaymentRequest)
        .where(
            PaymentRequest.id == request_id,
            PaymentRequest.project_id == project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if request is None or request.customer_id != customer.id:
        raise PermissionError("Payment request does not belong to this Telegram user")

    newly_submitted = request.status == PaymentRequestStatus.WAITING_CONFIRMATION.value
    submitted = await mark_payment_request_submitted(
        session,
        project_id=project_id,
        request_id=request_id,
    )
    item = await _load_review_item(
        session,
        project_id=project_id,
        request_id=submitted.id,
    )
    return SubmissionResult(
        request=submitted,
        item=item,
        reviewer_telegram_ids=await _reviewer_telegram_ids(session, project_id=project_id),
        newly_submitted=newly_submitted,
    )


async def cancel_checkout_for_telegram_user(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
    telegram_user_id: int,
) -> PaymentRequest:
    customer = await _customer_for_telegram_user(
        session,
        project_id=project_id,
        telegram_user_id=telegram_user_id,
    )
    if customer is None:
        raise PermissionError("Payment request does not belong to this Telegram user")
    return await cancel_payment_request(
        session,
        project_id=project_id,
        request_id=request_id,
        customer_id=customer.id,
    )


async def _load_review_item(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
) -> ReviewItem:
    row = (
        await session.execute(
            select(
                PaymentRequest,
                TelegramUser.telegram_user_id,
                TelegramUser.username,
                Plan.name,
                PaymentMethod.name,
            )
            .join(ProjectCustomer, ProjectCustomer.id == PaymentRequest.customer_id)
            .join(TelegramUser, TelegramUser.id == ProjectCustomer.user_id)
            .join(
                Plan,
                and_(
                    Plan.id == PaymentRequest.plan_id,
                    Plan.project_id == PaymentRequest.project_id,
                ),
            )
            .join(
                PaymentMethod,
                and_(
                    PaymentMethod.id == PaymentRequest.payment_method_id,
                    PaymentMethod.project_id == PaymentRequest.project_id,
                ),
            )
            .where(
                PaymentRequest.id == request_id,
                PaymentRequest.project_id == project_id,
            )
        )
    ).first()
    if row is None:
        raise ValueError("Payment request is not accessible in the current project scope")
    request, telegram_user_id, username, plan_name, method_name = row
    return ReviewItem(
        request_id=request.id,
        customer_telegram_user_id=telegram_user_id,
        customer_username=username,
        plan_name=plan_name,
        amount=request.final_amount,
        currency_code=request.currency_code,
        payment_method_name=method_name,
        status=request.status,
    )


async def list_pending_reviews_for_actor(
    session: AsyncSession,
    *,
    project_id: UUID,
    actor_telegram_user_id: int,
    actor_username: str | None = None,
    actor_first_name: str | None = None,
    limit: int = 20,
) -> tuple[ReviewItem, ...]:
    if limit <= 0 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    actor = await upsert_telegram_user(
        session,
        telegram_user_id=actor_telegram_user_id,
        username=actor_username,
        first_name=actor_first_name,
    )
    await require_payment_permission(
        session,
        project_id=project_id,
        actor_user_id=actor.id,
        permission=Permission.PAYMENT_VIEW,
    )
    ids = tuple(
        (
            await session.scalars(
                select(PaymentRequest.id)
                .where(
                    PaymentRequest.project_id == project_id,
                    PaymentRequest.status == PaymentRequestStatus.WAITING_REVIEW.value,
                )
                .order_by(PaymentRequest.submitted_at, PaymentRequest.id)
                .limit(limit)
            )
        ).all()
    )
    items = []
    for request_id in ids:
        items.append(
            await _load_review_item(
                session,
                project_id=project_id,
                request_id=request_id,
            )
        )
    return tuple(items)


async def review_payment_request_from_telegram(
    session: AsyncSession,
    *,
    project_id: UUID,
    request_id: UUID,
    actor_telegram_user_id: int,
    approve: bool,
    actor_username: str | None = None,
    actor_first_name: str | None = None,
) -> ReviewResult:
    actor = await upsert_telegram_user(
        session,
        telegram_user_id=actor_telegram_user_id,
        username=actor_username,
        first_name=actor_first_name,
    )
    permission = Permission.PAYMENT_APPROVE if approve else Permission.PAYMENT_REJECT
    await require_payment_permission(
        session,
        project_id=project_id,
        actor_user_id=actor.id,
        permission=permission,
    )

    locked_request = await session.scalar(
        select(PaymentRequest)
        .where(
            PaymentRequest.id == request_id,
            PaymentRequest.project_id == project_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if locked_request is None:
        raise ValueError("Payment request is not accessible in the current project scope")
    before = locked_request.status

    if approve:
        result = await approve_payment_request(
            session,
            project_id=project_id,
            request_id=request_id,
            reviewer_user_id=actor.id,
        )
        request = result.request
        changed = before != PaymentRequestStatus.APPROVED.value
    else:
        request = await reject_payment_request(
            session,
            project_id=project_id,
            request_id=request_id,
            reviewer_user_id=actor.id,
        )
        changed = before != PaymentRequestStatus.REJECTED.value

    item = await _load_review_item(
        session,
        project_id=project_id,
        request_id=request.id,
    )
    return ReviewResult(item=item, changed=changed)
