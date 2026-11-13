"""UI invariant: payment callbacks fit Telegram's 64-byte hard limit."""
from uuid import uuid4

import pytest

from telegram_membership_saas.modules.owner_setup.handlers import (
    _decode_saas_page,
    _encode_saas_page,
)
from telegram_membership_saas.modules.telegram_commerce.service import encode_uuid


@pytest.mark.parametrize("page", [0, 1, 9, 10, 99, 100, 1000, 100000])
def test_platform_payment_method_selector_callbacks_fit_telegram(page):
    invoice = encode_uuid(uuid4())
    method = encode_uuid(uuid4())
    compact_page = _encode_saas_page(page)
    assert _decode_saas_page(compact_page) == page
    for kind, payload in (
        ("spm", f"{invoice}:RUB:{compact_page}"),
        ("spv", f"{invoice}:{method}:RUB:{compact_page}"),
        ("spc", f"{invoice}:{method}:RUB:F:{compact_page}"),
    ):
        callback = "own:" + kind + ":" + payload
        assert len(callback.encode("utf-8")) <= 64


def test_reject_forged_or_out_of_range_page_tokens():
    for token in ("", "-1", "1:", "255t", "100000", "💎"):
        with pytest.raises(ValueError):
            _decode_saas_page(token)
