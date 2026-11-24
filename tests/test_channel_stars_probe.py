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
        "reason": "provider_or_runtime_error",
    }


def _fake_telethon(monkeypatch, *, bot=True, bot_id=77, broadcast=True, channel_id=88):
    client = AsyncMock()
    client.get_me.return_value = SimpleNamespace(bot=bot, id=bot_id)
    client.get_entity.return_value = SimpleNamespace(id=channel_id, broadcast=broadcast)
    client.return_value = page([row("sale")])
    peer = SimpleNamespace()
    session = object()
    client_args = []
    request_args = []

    def make_client(*args, **kwargs):
        client_args.append((args, kwargs))
        return client

    def make_peer(*args):
        assert args == (88, 99)
        return peer

    def make_request(**kwargs):
        request_args.append(kwargs)
        return object()

    modules = {
        "telethon": SimpleNamespace(TelegramClient=make_client),
        "telethon.sessions": SimpleNamespace(MemorySession=lambda: session),
        "telethon.tl.functions.payments": SimpleNamespace(
            GetStarsTransactionsRequest=make_request,
        ),
        "telethon.tl.types": SimpleNamespace(InputPeerChannel=make_peer),
    }
    for name, module in modules.items():
        monkeypatch.setitem(probe.sys.modules, name, module)
    return client, peer, session, client_args, request_args


async def test_live_read_uses_exact_bot_and_peer_and_disconnects(monkeypatch):
    client, peer, session, args, requests = _fake_telethon(monkeypatch)
    report = await probe.run_live(1, "api", "77:fake", 88, 99, "sale", "")
    assert report["sale_seen"]
    assert not report["commission_authorized"]
    assert args == [((session, 1, "api"), {
        "receive_updates": False, "request_retries": 1, "connection_retries": 0,
        "flood_sleep_threshold": 0, "auto_reconnect": False,
    })]
    client.connect.assert_awaited_once()
    client.sign_in.assert_awaited_once_with(bot_token="77:fake")
    client.get_entity.assert_awaited_once_with(peer)
    assert requests == [{"peer": peer, "offset": "", "limit": probe.PAGE_SIZE}]
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("changes", [
    {"bot": False}, {"bot_id": 78}, {"broadcast": False}, {"channel_id": 89},
])
async def test_live_identity_mismatch_never_reads_transactions(monkeypatch, changes):
    client, _, _, _, requests = _fake_telethon(monkeypatch, **changes)
    with pytest.raises(ValueError, match="identity mismatch|broadcast channel"):
        await probe.run_live(1, "api", "77:fake", 88, 99, "sale", "")
    assert not requests
    client.disconnect.assert_awaited_once()


async def test_live_rpc_failure_disconnects(monkeypatch):
    client, _, _, _, _ = _fake_telethon(monkeypatch)
    client.side_effect = RuntimeError("provider failure")
    with pytest.raises(RuntimeError, match="provider failure"):
        await probe.run_live(1, "api", "77:fake", 88, 99, "sale", "")
    client.disconnect.assert_awaited_once()


def _inputs(monkeypatch, values):
    monkeypatch.setattr(probe.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    answers = iter(values)
    monkeypatch.setattr(probe.getpass, "getpass", lambda _: next(answers))


def test_hidden_inputs(monkeypatch):
    _inputs(monkeypatch, ["ISOLATED TEST", "1", "api", "77:fake", "88", "99", "sale", ""])
    assert probe._read_inputs() == (1, "api", "77:fake", 88, 99, "sale", "")


@pytest.mark.parametrize("values", [
    ["no"],
    ["ISOLATED TEST", "0", "api", "77:fake", "88", "99", "sale", ""],
    ["ISOLATED TEST", "1", "api", "77:fake", "0", "99", "sale", ""],
    ["ISOLATED TEST", "1", "", "77:fake", "88", "99", "sale", ""],
    ["ISOLATED TEST", "1", "api", "", "88", "99", "sale", ""],
    ["ISOLATED TEST", "1", "api", "77:fake", "88", "99", "", ""],
    ["ISOLATED TEST", "1", "api", "77:fake", "88", "99", "sale", "sale"],
])
def test_invalid_hidden_inputs(monkeypatch, values):
    _inputs(monkeypatch, values)
    with pytest.raises(ValueError):
        probe._read_inputs()


@pytest.mark.parametrize("malformed", [
    None, SimpleNamespace(), SimpleNamespace(history=[]),
    SimpleNamespace(next_offset=None), SimpleNamespace(history="invalid", next_offset=None),
])
async def test_malformed_page_is_never_reported_complete(malformed):
    fetch = AsyncMock(return_value=malformed)
    result = await probe.inspect_history(fetch, "sale", "renew")
    assert not result["complete"]
    assert not result["commission_authorized"]
    assert result["pages"] == 0


@pytest.mark.parametrize("entity", [None, SimpleNamespace(broadcast=True)])
async def test_missing_channel_identity_is_rejected(monkeypatch, entity):
    client, _, _, _, requests = _fake_telethon(monkeypatch)
    client.get_entity.return_value = entity
    with pytest.raises(ValueError, match="broadcast channel"):
        await probe.run_live(1, "api", "77:fake", 88, 99, "sale", "")
    assert not requests
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("token", ["77", "77:", "abc:fake", "0:fake", "１２:fake", ":fake"])
def test_malformed_token_rejected_at_input(monkeypatch, token):
    _inputs(monkeypatch, ["ISOLATED TEST", "1", "api", token, "88", "99", "sale", ""])
    with pytest.raises(ValueError, match="Invalid bot token format"):
        probe._read_inputs()


@pytest.mark.parametrize("identifier", [None, "", 123])
async def test_malformed_candidate_id_leaves_scan_incomplete(identifier):
    fetch = AsyncMock(return_value=page([row(identifier)]))
    result = await probe.inspect_history(fetch, "sale", "")
    assert not result["complete"]
    assert not result["commission_authorized"]
    assert not result["renewal_seen"]


@pytest.mark.parametrize("error,reason", [
    (KeyboardInterrupt(), "interrupted"),
    (probe.ProbeValidationError("secret"), "invalid_input_or_identity"),
    (TimeoutError("secret"), "timeout"),
    (ImportError("secret"), "dependency_unavailable"),
    (OSError("secret"), "connection_error"),
    (type("ChatAdminRequiredError", (Exception,), {})("secret"), "peer_access_denied"),
    (type("FloodWaitError", (Exception,), {})("secret"), "rate_limited"),
    (ValueError("secret"), "provider_or_runtime_error"),
])
def test_safe_failure_reasons(error, reason):
    assert probe._failure_reason(error) == reason
