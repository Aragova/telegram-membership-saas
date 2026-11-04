import asyncio

from telegram_membership_saas.telegram_runtime import run_telegram_bot
from telegram_membership_saas.termination_signals import run_with_termination_signals


def main() -> None:
    asyncio.run(run_with_termination_signals(run_telegram_bot()))


if __name__ == "__main__":
    main()
