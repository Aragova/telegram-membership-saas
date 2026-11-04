"""Launch the self-service customer bot supervisor."""

import asyncio

from telegram_membership_saas.customer_supervisor import run_customer_supervisor
from telegram_membership_saas.termination_signals import run_with_termination_signals


def main() -> None:
    asyncio.run(run_with_termination_signals(run_customer_supervisor()))


if __name__ == "__main__":
    main()
