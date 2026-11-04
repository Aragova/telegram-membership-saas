"""One-time, explicit platform owner provisioning using migration DB rights.

Run only on staging/production from a trusted administrator terminal after
setting TMS_PLATFORM_OWNER_TELEGRAM_ID and TMS_MIGRATION_DATABASE_URL.
The Telegram bot runtime itself cannot promote any platform operator.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from telegram_membership_saas.config import get_settings


async def bootstrap_platform_owner() -> None:
    settings = get_settings()
    owner = settings.platform_owner_telegram_id
    if owner is None:
        raise RuntimeError("Set TMS_PLATFORM_OWNER_TELEGRAM_ID before bootstrapping")
    admin_url = settings.migration_database_url
    if not admin_url or admin_url == settings.database_url:
        raise RuntimeError(
            "Set a distinct TMS_MIGRATION_DATABASE_URL with database-owner privileges"
        )

    runtime_role = make_url(settings.database_url).username
    if not runtime_role:
        raise RuntimeError("Runtime database role is missing")
    engine = create_async_engine(admin_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            ids = (await connection.execute(text(
                "SELECT telegram_user_id FROM platform_operators "
                "WHERE role = 'owner' FOR UPDATE"
            ))).scalars().all()
            if any(existing != owner for existing in ids):
                raise RuntimeError(
                    "Platform already has a different owner; "
                    "owner transfer requires a separate audited migration"
                )
            await connection.execute(text(
                "INSERT INTO platform_operators (telegram_user_id, role) "
                "VALUES (:owner, 'owner') "
                "ON CONFLICT (telegram_user_id) DO NOTHING"
            ), {"owner": owner})
            # Identifier quote prevents the database username being SQL.
            quoted_role = connection.dialect.identifier_preparer.quote(runtime_role)
            await connection.execute(text(
                "GRANT EXECUTE ON FUNCTION "
                "public.platform_report_snapshot(timestamptz, integer, integer) "
                f"TO {quoted_role}"
            ))
            await connection.execute(text(
                "GRANT EXECUTE ON FUNCTION "
                "public.platform_usage_snapshot(timestamptz, integer, integer) "
                f"TO {quoted_role}"
            ))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(bootstrap_platform_owner())
    print("Platform owner access provisioned; no tenant data changed.")


if __name__ == "__main__":
    main()
