"""Receipt bytes are re-uploaded using the original bot, never its file_id.

Pure in-memory Telegram adapter tests: no network, DB or real banking data.
"""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.types import BufferedInputFile

from telegram_membership_saas.modules.platform_saas_proof_media import (
    reupload_proof_media,
)


@pytest.mark.asyncio
async def test_photo_reuploads_source_bytes_to_central_bot():
    data = b"\xff\xd8fake-photo"
    source = SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(
            file_path="photos/receipt.jpg", file_size=len(data),
        )),
        download_file=AsyncMock(return_value=BytesIO(data)),
    )
    central = SimpleNamespace(
        send_photo=AsyncMock(), send_document=AsyncMock(),
    )
    await reupload_proof_media(
        source, central, operator_id=12345,
        source_file_id="original-bot-only-id",
        file_type="photo", attempt_id=uuid4(), caption="Check funds in bank",
    )
    central.send_photo.assert_awaited_once()
    call = central.send_photo.await_args
    assert call.args[0] == 12345
    assert isinstance(call.args[1], BufferedInputFile)
    assert call.args[1].data == data
    assert call.args[1] != "original-bot-only-id"
    central.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_preserves_safe_extension_without_bot_scoped_file_id():
    source = SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(
            file_path="documents/receipt.pdf", file_size=12,
        )),
        download_file=AsyncMock(return_value=BytesIO(b"TEST-PDF")),
    )
    central = SimpleNamespace(
        send_photo=AsyncMock(), send_document=AsyncMock(),
    )
    await reupload_proof_media(
        source, central, operator_id=12345,
        source_file_id="document-bot-only-id", file_type="document",
        attempt_id=uuid4(), caption="Proof for operator",
    )
    media = central.send_document.await_args.args[1]
    assert isinstance(media, BufferedInputFile)
    assert media.filename.endswith(".pdf")
    assert media.data == b"TEST-PDF"
    central.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_telegram_proof_fails_without_download_or_delivery():
    source = SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(
            file_path="documents/huge.pdf", file_size=20 * 1024 * 1024 + 1,
        )),
        download_file=AsyncMock(),
    )
    central = SimpleNamespace(
        send_photo=AsyncMock(), send_document=AsyncMock(),
    )
    with pytest.raises(ValueError, match="transfer limit"):
        await reupload_proof_media(
            source, central, operator_id=12345,
            source_file_id="large-source-id", file_type="document",
            attempt_id=uuid4(), caption="Proof",
        )
    source.download_file.assert_not_awaited()
    central.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_recipient_and_file_type_fail_closed():
    source = SimpleNamespace(get_file=AsyncMock())
    central = SimpleNamespace(send_photo=AsyncMock(), send_document=AsyncMock())
    for operator_id, kind in [(0, "photo"), (12345, "video")]:
        with pytest.raises(ValueError):
            await reupload_proof_media(
                source, central, operator_id=operator_id,
                source_file_id="fake", file_type=kind,
                attempt_id=uuid4(), caption="Proof",
            )
    source.get_file.assert_not_awaited()
