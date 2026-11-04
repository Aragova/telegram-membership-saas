"""Automatic Telegram-language persistence must not override manual choice."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram_membership_saas.localization import read_language


def _factory(*, explicit=None, detected=None):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        side_effect=[
            SimpleNamespace(first=lambda: (explicit, detected)),
            SimpleNamespace(),
        ]
    )
    session.commit = AsyncMock()
    return MagicMock(return_value=session), session


async def test_manual_choice_overrides_telegram_and_never_rewritten():
    factory, session = _factory(explicit="ru", detected="en")
    assert await read_language(factory, 123, "en-GB", remember_hint=True) == "ru"
    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()


async def test_incoming_language_hint_is_saved_only_when_changed():
    factory, session = _factory(explicit=None, detected="ru")
    assert await read_language(factory, 123, "en-US", remember_hint=True) == "en"
    assert session.execute.await_count == 2
    session.commit.assert_awaited_once()


async def test_outbound_notification_uses_last_observed_language():
    factory, session = _factory(explicit=None, detected="en")
    assert await read_language(factory, 123, "ru") == "en"
    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()
