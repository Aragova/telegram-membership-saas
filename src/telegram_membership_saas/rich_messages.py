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
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardMarkup

try:
    from aiogram.types import InputRichMessage
except ImportError:  # Older aiogram versions still allowed by pyproject.toml.
    InputRichMessage = None

from telegram_membership_saas.config import get_settings

log = logging.getLogger(__name__)

_skip_rich: ContextVar[bool] = ContextVar("skip_rich", default=False)
_NATIVE_REPLY_MARKERS = (
    # These are authenticated using parent.text in the support reply filters.
    "💬 ViMi Pay · Покупатель ", "💬 ViMi Pay · Customer ",
    "✉️ ViMi Pay · Сообщение администратора",
    "✉️ ViMi Pay · Message from admin",
    "✍️ ViMi Pay · Ответ администратору",
    "✍️ ViMi Pay · Reply to admin",
)
_HEADING = re.compile(r"^\s*.{1,120}$")
_ROW = re.compile(r"^([^:\n]{2,65}?):\s+(.+)$")
_STATISTICS = (
    "ViMi Pay · Платформа", "ViMi Pay · Platform",
    "Статистика · ViMi Pay", "Statistics · ViMi Pay",
    "Мои покупки", "My purchases",
    "Статистика бота", "Bot statistics",
    "ViMi Pay · Счета", "ViMi Pay · Invoices",
    "Мониторинг · ViMi Pay", "Monitoring · ViMi Pay",
)


def _tableize_metrics(lines: list[str]) -> list[str]:
    """Only first-party statistical summaries, never owner payment instructions."""
    result: list[str] = []
    i = 0
    while i < len(lines):
        group: list[tuple[str, str]] = []
        while i < len(lines):
            # Bullet turnover rows contain an internal colon in "(operations: N)".
            # They are display lines, not label/value metric rows.
            if lines[i].lstrip().startswith(("• ", "- ", "· ")):
                break
            match = _ROW.fullmatch(lines[i])
            if match is None:
                break
            group.append((match.group(1), match.group(2)))
            i += 1
        if len(group) >= 2:
            result.append("<table bordered compact>")
            for label, value in group:
                result.append(
                    "<tr><th>" + label + "</th><td>" + value + "</td></tr>"
                )
            result.append("</table>")
        else:
            result.extend(label + ": " + value for label, value in group)
        if i < len(lines):
            result.append(lines[i])
            i += 1
    return result


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


_BLOCK_SPLIT = re.compile(
    r"(<pre\b[^>]*>.*?</pre>|<table\b[^>]*>.*?</table>|"
    r"<details\b[^>]*>.*?</details>|<blockquote\b[^>]*>.*?</blockquote>|"
    r"<ul\b[^>]*>.*?</ul>|<ol\b[^>]*>.*?</ol>|"
    r"<h[1-6]\b[^>]*>.*?</h[1-6]>)",
    flags=re.IGNORECASE | re.DOTALL,
)
_BLOCK_START = re.compile(
    r"^<(pre|table|details|blockquote|ul|ol|h[1-6])\b", re.IGNORECASE,
)


def _rich_paragraphs(body: str) -> list[str]:
    """Keep existing visible line breaks; Rich HTML collapses raw newlines.

    Preserve true Rich block tags and preformatted code, wrapping ordinary
    legacy HTML into paragraphs separated with explicit <br> elements.
    """
    blocks = []
    for chunk in _BLOCK_SPLIT.split(body):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        if _BLOCK_START.match(chunk):
            blocks.append(chunk)
        else:
            blocks.append("<p>" + chunk.replace("\n", "<br>") + "</p>")
    return blocks


def to_rich_html(legacy_html: str) -> str:
    """Render legacy HTML as actual Rich blocks without collapsing bank details.

    Owner-authored payment wording and banking fields are not modified.
    Only the presentation adds headings, paragraph and explicit line breaks.
    """
    if not legacy_html:
        return legacy_html
    lines = legacy_html.split("\n", 1)
    first = lines[0].strip()
    has_heading = (
        bool(first)
        and len(first) <= 120
        and ("<" not in first or any(
            tag in first for tag in ("<b>", "<strong>")
        ))
        and bool(_HEADING.fullmatch(first))
    )
    if has_heading:
        header = "<h3>" + first + "</h3>"
        body = lines[1] if len(lines) == 2 else ""
    else:
        header = ""
        body = legacy_html

    if has_heading and any(marker in first for marker in _STATISTICS):
        body = "\n".join(_tableize_metrics(body.split("\n")))

    blocks = _rich_paragraphs(body)
    return "\n".join(([header] if header else []) + blocks)


def _eligible(text: str, kwargs: dict) -> bool:
    """Only convert inline *UI* messages, never receipts or text-entry prompts."""
    return (
        rich_enabled()
        and InputRichMessage is not None
        and not _skip_rich.get()
        and isinstance(text, str)
        and not text.startswith(_NATIVE_REPLY_MARKERS)
        and isinstance(kwargs.get("reply_markup"), InlineKeyboardMarkup)
        and kwargs.get("parse_mode") in ("HTML", None)
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
                        rich_message=InputRichMessage(html=to_rich_html(
                            text if kwargs.get("parse_mode") == "HTML" else escape(text),
                        )),
                        **_supported_send_arguments(kwargs),
                    )
                except TelegramBadRequest as exc:
                    log.info("Rich UI send unsupported, using HTML: %s", exc)
                except TelegramNotFound as exc:
                    if "method not found" not in str(exc).lower():
                        raise
                    log.info("Telegram sendRichMessage unavailable, using HTML")
        return await super().send_message(chat_id, text, **kwargs)

    async def edit_message_text(self, text=None, **kwargs):
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
                        rich_message=InputRichMessage(html=to_rich_html(
                            text if kwargs.get("parse_mode") == "HTML" else escape(text),
                        )),
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
