"""Entrypoint for the project-scoped branded customer Telegram bot."""

import asyncio

from telegram_membership_saas.customer_runtime import run_customer_bot


def main() -> None:
    asyncio.run(run_customer_bot())


if __name__ == "__main__":
    main()
