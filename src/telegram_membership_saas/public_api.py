"""Isolated ViMi Pay API: health plus opt-in read-only Lava redirects.

Never use the central bot's database credentials, payment methods, webhooks
or customer runtime here. Missing redirect credentials leave links closed.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.modules.platform_saas_lava_client import (
    LavaTopError,
    _checkout_link,
)
from telegram_membership_saas.modules.platform_saas_payment_redirect import (
    decode_lava_order,
)

NO_CACHE = {
    "Cache-Control": "no-store, max-age=0",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    database_url = os.environ.get("TMS_SHORTLINK_DATABASE_URL", "").strip()
    if database_url and not database_url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("Shortlink DB URL must use asyncpg")
    engine = (
        create_async_engine(
            database_url,
            echo=False,
            pool_size=2,
            max_overflow=0,
            pool_timeout=2,
            pool_pre_ping=True,
            connect_args={
                "server_settings": {
                    "statement_timeout": "2000",
                    "application_name": "vimi_pay_shortlink_redirect",
                },
            },
        )
        if database_url else None
    )
    app.state.shortlink_engine = engine
    try:
        yield
    finally:
        if engine is not None:
            await engine.dispose()


app = FastAPI(
    title="ViMi Pay Public API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    debug=False,
    lifespan=lifespan,
)


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


@app.get("/p/{token}", include_in_schema=False)
async def lava_checkout_redirect(token: str, request: Request):
    order_id = decode_lava_order(token)
    if order_id is None:
        return JSONResponse({"detail": "Not found"}, status_code=404, headers=NO_CACHE)

    # Tests can supply a narrow async lookup; production uses only the
    # dedicated SELECT-free, SECURITY DEFINER function and its read role.
    lookup = getattr(request.app.state, "shortlink_lookup", None)
    try:
        if lookup is not None:
            target = await lookup(order_id)
        else:
            engine = getattr(request.app.state, "shortlink_engine", None)
            if engine is None:
                return JSONResponse(
                    {"detail": "Not found"}, status_code=404, headers=NO_CACHE,
                )
            async with engine.connect() as connection:
                target = await connection.scalar(
                    text(
                        "SELECT public.platform_saas_lava_shortlink_target("
                        "CAST(:order_id AS uuid))"
                    ),
                    {"order_id": str(order_id)},
                )
    except Exception:
        # Never return DB errors, email, provider signatures or full URLs.
        return JSONResponse(
            {"detail": "Temporarily unavailable"},
            status_code=503, headers=NO_CACHE,
        )
    if not isinstance(target, str):
        return JSONResponse({"detail": "Not found"}, status_code=404, headers=NO_CACHE)
    try:
        approved = _checkout_link(target)
    except (LavaTopError, ValueError):
        return JSONResponse({"detail": "Not found"}, status_code=404, headers=NO_CACHE)
    return RedirectResponse(approved, status_code=302, headers=NO_CACHE)
