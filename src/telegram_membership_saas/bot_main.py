import asyncio

from telegram_membership_saas.telegram_runtime import run_telegram_bot


def main() -> None:
    asyncio.run(run_telegram_bot())


if __name__ == "__main__":
    main()
