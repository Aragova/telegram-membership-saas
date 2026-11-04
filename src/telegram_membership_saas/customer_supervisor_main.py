"""Launch the self-service customer bot supervisor."""

import asyncio

from telegram_membership_saas.customer_supervisor import run_customer_supervisor


def main() -> None:
    asyncio.run(run_customer_supervisor())


if __name__ == "__main__":
    main()
