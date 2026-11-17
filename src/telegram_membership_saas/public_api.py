"""Stage 4: isolated public ViMi Pay HTTP process.

A deliberately small, fail-closed ASGI entrypoint. Do not reuse the
development FastAPI app or attach billing, webhook and Telegram routes
until those routes have their own authenticated/verified release gates.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(
    title="ViMi Pay Public API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    debug=False,
)


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    # Process liveness only, not database, billing or Telegram readiness.
    # Keep the public response free of environment and system metadata.
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
