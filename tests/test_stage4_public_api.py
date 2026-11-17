"""Stage 4 must stay closed until the separately reviewed Nginx cutover."""

from pathlib import Path

from fastapi.testclient import TestClient

from telegram_membership_saas.api import create_app
from telegram_membership_saas.config import Settings
from telegram_membership_saas.public_api import app


REPO = Path(__file__).resolve().parents[1]


def test_public_api_only_exposes_an_anonymous_process_healthcheck() -> None:
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert "environment" not in response.json()


def test_public_api_does_not_expose_docs_or_business_routes() -> None:
    client = TestClient(app)

    for url in (
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/payments",
        "/subscriptions",
        "/webhooks/telegram",
    ):
        assert client.get(url).status_code == 404, url

    assert client.post("/health").status_code == 405
    assert len([r for r in app.routes if getattr(r, "path", "") == "/health"]) == 1


def test_public_api_compose_never_exposes_a_public_host_port() -> None:
    source = (REPO / "ops" / "public-api.compose.yaml").read_text(encoding="utf-8")
    assert "127.0.0.1:18080:8000" in source
    assert '"8000:8000"' not in source
    assert '- "0.0.0.0:18080:8000"' not in source
    assert "telegram_membership_saas.public_api:app" in source
    assert "no-new-privileges:true" in source
    assert "read_only: true" in source
    assert "TMS_TELEGRAM_BOT_TOKEN" not in source
    assert "depends_on:" not in source
    assert "network_mode: host" not in source
    assert "profiles:" not in source
    assert "bot:" not in source
    assert "customer-supervisor:" not in source


def test_existing_development_api_is_unchanged() -> None:
    """The public entrypoint must not replace the existing dev/staging API."""
    test_app = create_app(Settings(environment="test", _env_file=None))
    response = TestClient(test_app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test"}
