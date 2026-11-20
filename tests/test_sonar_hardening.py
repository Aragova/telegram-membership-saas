"""Regression contracts for Sonar-driven changes that must not change bot UX."""

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.engine import make_url

from telegram_membership_saas.config import Settings
from telegram_membership_saas.modules.owner_setup.dashboard_ui import _channel_rows
from telegram_membership_saas.rich_messages import _metric_parts, _tableize_metrics


def test_default_db_settings_never_embed_password(monkeypatch) -> None:
    monkeypatch.delenv("TMS_DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert make_url(settings.database_url).password is None

    # An explicit private configuration still has precedence in real runtimes.
    configured = "postgresql+asyncpg://runtime:secret@database:5432/membership"
    monkeypatch.setenv("TMS_DATABASE_URL", configured)
    assert Settings(_env_file=None).database_url == configured


def test_rich_statistics_parser_preserves_values_spacing_and_bullets() -> None:
    assert _metric_parts("Sales:  42  ") == ("Sales", "42  ")
    assert _metric_parts("AA:  ") == ("AA", " ")
    assert _metric_parts("AA: ") is None
    assert _metric_parts("A: 5") is None
    assert _metric_parts("Long:42") is None
    assert _metric_parts("a" * 66 + ": 5") is None
    assert _tableize_metrics([
        "Paid: 12", "Orders: 3", "• RUB: (operations: 5)",
    ]) == [
        "<table bordered compact>",
        "<tr><th>Paid</th><td>12</td></tr>",
        "<tr><th>Orders</th><td>3</td></tr>",
        "</table>",
        "• RUB: (operations: 5)",
    ]


def test_dashboard_intentionally_has_one_or_two_finance_rows() -> None:
    # Telegram Stars may pay directly to the channel or to a separate bot.
    # This is the existing product requirement, not a malformed tuple.
    channel = SimpleNamespace(
        owner_deleted=False, owner_username="kostik",
        owner_user_id=123456, owner_label="Owner",
        stars_recipient="channel",
    )
    one_row = _channel_rows(channel)
    assert len(one_row) == 1
    assert "@kostik" in one_row[0][1]

    channel.stars_recipient = "bot"
    two_rows = _channel_rows(channel)
    assert len(two_rows) == 2
    assert "@kostik" in two_rows[0][1]
    assert "🤖" in two_rows[1][1]


def test_pinned_python_312_runtime_matches_dev_dependency_resolutions() -> None:
    root = Path(__file__).resolve().parents[1]

    def load_pins(filename: str) -> dict[str, str]:
        pins = {}
        for line in (root / filename).read_text(encoding="utf-8").splitlines():
            if not line or line[0].isspace() or line.startswith("#"):
                continue
            package, equals, version = line.partition("==")
            assert equals and package and version, f"Unpinned dependency: {line}"
            assert package not in pins
            pins[package] = version
        return pins

    runtime = load_pins("requirements-runtime.lock")
    dev = load_pins("requirements-dev.lock")
    assert len(runtime) >= 30
    assert all(dev.get(name) == version for name, version in runtime.items())
    assert {"aiogram", "asyncpg", "fastapi", "cryptography"} <= set(runtime)
