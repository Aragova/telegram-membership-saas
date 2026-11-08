"""Unified Telegram Rich Messages transport for all interactive ViMi Pay screens.

Rich rendering is enabled by default. It changes presentation, never the
callback keyboard, database state, receipts, or owner-written instructions.
The public Bot API accepts rich_message in editMessageText (Bot API 10.1+),
so navigation can update a single card instead of stacking new messages.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup, InputRichMessage

from telegram_membership_saas.config import get_settings

log = logging.getLogger(__name__)

_skip_rich: ContextVar[bool] = ContextVar("skip_rich", default=False)
_HEADING = re.compile(r"^\s*(?:<b>|<strong>)?.{1,120}$")


@contextmanager
def classic_messages():
    """Use normal Telegram HTML for a compatibility fallback, without retrying Rich."""
    token = _skip_rich.set(True)
    try:
        yield
    finally:
        _skip_rich.reset(token)


def rich_enabled() -> bool:
    """Master switch; one setting covers central, admin and buyer interfaces."""
    return bool(getattr(get_settings(), "rich_messages_enabled", False))


def to_rich_html(legacy_html: str) -> str:
    """Promote a trusted, already HTML-escaped legacy screen to Rich HTML.

    Do not rewrite the body. In particular, user-authored payment instructions,
    banking references, links and emojis remain byte-for-byte unchanged.
    Convert only the first short line to a native Rich heading.
    """
    if not legacy_html or "<h1>" in legacy_html or "<h2>" in legacy_html:
        return legacy_html
    lines = legacy_html.split("\n", 1)
    first = lines[0].strip()
    if (
        not first
        or len(first) > 120
        or "<" in first and not any(tag in first for tag in ("<b>", "<strong>"))
        or not _HEADING.fullmatch(first)
    ):
        return legacy_html
    title = "<h3>" + first + "</h3>"
    return title + ("\n" + lines[1] if len(lines) == 2 else "")


def _eligible(text: str, kwargs: dict) -> bool:
    """Only convert inline *UI* messages, never receipts or text-entry prompts."""
    return (
        rich_enabled()
        and not _skip_rich.get()
        and isinstance(kwargs.get("reply_markup"), InlineKeyboardMarkup)
        and kwargs.get("parse_mode") == "HTML"
        and isinstance(text, str)
        and bool(text.strip())
        and len(text.encode("utf-8")) < 32768
        and not kwargs.get("entities")
        and not kwargs.get("reply_parameters")
        and not kwargs.get("business_connection_id")
    )


def _supported_send_arguments(kwargs: dict) -> dict:
    """Keep message metadata in Rich delivery (never silently drop metadata)."""
    allowed = {
        "message_thread_id", "direct_messages_topic_id", "disable_notification",
        "protect_content", "allow_paid_broadcast", "message_effect_id",
        "suggested_post_parameters", "reply_markup",
    }
    return {key: value for key, value in kwargs.items()
            if key in allowed and value is not None}


class RichUiBot(Bot):
    """Rich-capable Bot used by central and every connected customer process.

    Unsupported Rich formatting falls back to the original HTML message. Only
    Telegram's explicit parsing/method rejections trigger a retry; transport
    errors can have delivered a message and must NOT be retried.
    """

    async def send_message(self, chat_id, text, **kwargs):
        if _eligible(text, kwargs):
            # Other sendMessage-only arguments may carry semantic intent.
            accepted = {
                "parse_mode", "reply_markup", "message_thread_id",
                "direct_messages_topic_id", "disable_notification",
                "protect_content", "allow_paid_broadcast", "message_effect_id",
                "suggested_post_parameters",
            }
            if not (set(kwargs) - accepted):
                try:
                    return await self.send_rich_message(
                        chat_id=chat_id,
                        rich_message=InputRichMessage(html=to_rich_html(text)),
                        **_supported_send_arguments(kwargs),
                    )
                except TelegramBadRequest as exc:
                    log.info("Rich UI send unsupported, using HTML: %s", exc)
                except TelegramNotFound as exc:
                    if "method not found" not in str(exc).lower():
                        raise
                    log.info("Telegram sendRichMessage unavailable, using HTML")
        return await super().send_message(chat_id, text, **kwargs)

    async def edit_message_text(self, text, **kwargs):
        if _eligible(text, kwargs):
            # editMessageText supports the 'rich_message' field since 10.1.
            # Validate installed aiogram schema before attempting to use it.
            supported = "rich_message" in EditMessageText.model_fields
            accepted = {
                "parse_mode", "reply_markup", "chat_id", "message_id",
                "inline_message_id", "business_connection_id",
            }
            if supported and not (set(kwargs) - accepted):
                try:
                    return await self(EditMessageText(
                        text=None,
                        rich_message=InputRichMessage(html=to_rich_html(text)),
                        chat_id=kwargs.get("chat_id"),
                        message_id=kwargs.get("message_id"),
                        inline_message_id=kwargs.get("inline_message_id"),
                        reply_markup=kwargs.get("reply_markup"),
                    ))
                except TelegramBadRequest as exc:
                    lower = str(exc).lower()
                    if "message is not modified" in lower:
                        raise
                    if "message to edit not found" in lower:
                        raise
                    log.info("Rich UI edit unsupported, using HTML: %s", exc)
                except TelegramNotFound as exc:
                    if "method not found" not in str(exc).lower():
                        raise
                    log.info("Telegram Rich edit unavailable, using HTML")
        return await super().edit_message_text(text, **kwargs)
