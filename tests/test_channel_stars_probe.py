"""Synthetic evidence only: no Telegram credentials or network access."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

path = Path(__file__).resolve().parents[1] / "ops" / "audit" / "channel_stars_probe.py"
spec = importlib.util.spec_from_file_location("channel_stars_probe", path)
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def row(identifier, **kwargs):
    return SimpleNamespace(id=identifier, subscription_period=probe.PERIOD, **kwargs)


def page(rows, next_offset=None):
    return SimpleNamespace(history=rows, next_offset=next_offset)


async def test_pagination_dedup_and_refunds_never_authorize_commission():
    fetch = AsyncMock(side_effect=[
        page([row("sale"), row("sale", pending=True), row("sale", failed=True)], "n"),
        page([row("sale"), row("renew", bot_payload=b"private"),
              row("sale", refund=True)]),
    ])
    result = await probe.inspect_history(fetch, "sale", "renew")
    assert result == {
        "pages": 2,
        "complete": True,
        "subscription_candidates": 3,
        "sale_seen": True,
        "renewal_seen": True,
        "sale_refund_seen": True,
        "renewal_refund_seen": False,
        "candidate_payload_seen": True,
        "commission_authorized": False,
    }
    assert [call.args for call in fetch.await_args_list] == [("",), ("n",)]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("cursor", ["again", 123])
async def test_invalid_or_repeated_cursor_is_incomplete(cursor):
    fetch = AsyncMock(side_effect=[page([], "again"), page([], cursor)])
    report = await probe.inspect_history(fetch, "sale", "renew")
    assert report["pages"] == 2
    assert not report["complete"]
    assert not report["commission_authorized"]


async def test_non_subscription_and_empty_history_are_not_sales():
    fetch = AsyncMock(return_value=page([SimpleNamespace(id="sale", subscription_period=None)]))
    result = await probe.inspect_history(fetch, "sale", "renew")
    assert result["complete"]
    assert not result["sale_seen"]
    assert result["subscription_candidates"] == 0
    fetch.return_value = page(None)
    assert (await probe.inspect_history(fetch, "sale", "renew"))["complete"]


async def test_bounded_pages():
    fetch = AsyncMock(side_effect=[page([], str(i)) for i in range(probe.MAX_PAGES)])
    result = await probe.inspect_history(fetch, "sale", "renew")
    assert result["pages"] == probe.MAX_PAGES
    assert not result["complete"]


def test_noninteractive_input_rejected(monkeypatch):
    monkeypatch.setattr(probe.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(ValueError, match="private terminal"):
        probe._read_inputs()


@pytest.mark.parametrize("complete,code", [(True, 0), (False, 2)])
def test_cli_reports_only_safe_summary(monkeypatch, capsys, complete, code):
    monkeypatch.setattr(probe, "_read_inputs", lambda: ())
    monkeypatch.setattr(probe, "run_live", AsyncMock(return_value={
        "complete": complete, "commission_authorized": False,
    }))
    monkeypatch.setattr(probe.logging, "disable", lambda _: None)
    assert probe.main() == code
    assert json.loads(capsys.readouterr().out) == {
        "status": "read_ok", "complete": complete, "commission_authorized": False,
    }


def test_cli_does_not_leak_exception(monkeypatch, capsys):
    monkeypatch.setattr(probe, "_read_inputs", lambda: ())
    monkeypatch.setattr(probe, "run_live", AsyncMock(side_effect=RuntimeError("private data")))
    monkeypatch.setattr(probe.logging, "disable", lambda _: None)
    assert probe.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "probe_failed", "commission_authorized": False,
    }
