from fastapi import FastAPI
from pydantic import BaseModel

from telegram_membership_saas.config import Settings, get_settings


class HealthResponse(BaseModel):
    status: str
    environment: str


def create_app(settings: Settings | None = None) -> FastAPI:
    current = settings or get_settings()
    app = FastAPI(title=current.app_name)

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", environment=current.environment)

    return app
