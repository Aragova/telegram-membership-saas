"""Sensitive owner token setup must take precedence over stale file editors."""
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.file_sales import handlers as file_handlers
from telegram_membership_saas.modules.file_sales import manual_handlers


@pytest.mark.parametrize(
    "module,filter_class",
    [
        (file_handlers, file_handlers.FileEditorFilter),
        (manual_handlers, manual_handlers.ManualPriceFilter),
    ],
)
async def test_active_owner_secret_prompt_cannot_reach_file_editor(
    monkeypatch, module, filter_class,
):
    actor = uuid4()
    pending = {15: {"stage": "description", "expires": monotonic() + 300}}
    fake_factory = object()
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=15, username="owner", first_name="Owner"),
        text="12345:merchant-secret-plaintext",
        successful_payment=None, refunded_payment=None,
    )
    bot = SimpleNamespace(id=4321)
    actor_mock = AsyncMock(return_value=actor)
    prompt_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "ensure_telegram_actor", actor_mock)
    monkeypatch.setattr(module, "is_expected_prompt", prompt_mock)

    filter_ = filter_class(pending, fake_factory)
    assert await filter_(message, bot) is False
    assert 15 not in pending
    prompt_mock.assert_awaited_once_with(
        fake_factory, actor_id=actor, bot_scope_id=4321,
        reply_message_id=None,
    )


@pytest.mark.parametrize(
    "module,filter_class",
    [
        (file_handlers, file_handlers.FileEditorFilter),
        (manual_handlers, manual_handlers.ManualPriceFilter),
    ],
)
async def test_file_editor_still_accepts_messages_without_owner_prompt(
    monkeypatch, module, filter_class,
):
    pending = {15: {"stage": "description", "expires": monotonic() + 300}}
    monkeypatch.setattr(module, "ensure_telegram_actor", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(module, "is_expected_prompt", AsyncMock(return_value=False))
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=15, username=None, first_name=None),
        text="Normal file description",
        successful_payment=None, refunded_payment=None,
    )
    assert await filter_class(pending, object())(
        message, SimpleNamespace(id=4321),
    )
    assert 15 in pending
