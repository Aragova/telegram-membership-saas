from fastapi.testclient import TestClient

from telegram_membership_saas.api import create_app
from telegram_membership_saas.config import Settings


def test_health_endpoint() -> None:
    settings = Settings(environment="test", _env_file=None)
    client = TestClient(create_app(settings))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test"}
