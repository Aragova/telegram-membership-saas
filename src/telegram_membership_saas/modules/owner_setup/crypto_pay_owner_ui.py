"""Owner-facing Crypto Pay onboarding copy (Rich + classic, RU/EN).

These are presentation-only screens. Credential handling, live provider calls,
sales and secure prompt generation remain in their existing services.
"""
from __future__ import annotations

from html import escape

from telegram_membership_saas.localization import tr


def _environment(environment: str) -> str:
    return (
        tr("реальные оплаты", "real payments")
        if environment == "mainnet"
        else tr("тестовый режим", "test mode")
    )


def crypto_overview_rich(app: object | None) -> str:
    status = (
        "🟢 <b>" + tr("ПОДКЛЮЧЕНО", "CONNECTED") + "</b>"
        + "<br>" + escape(str(app.app_label))
        + " · " + _environment(str(app.environment))
        if app is not None else
        "🔴 <b>" + tr("НЕ ПОДКЛЮЧЕНО", "NOT CONNECTED") + "</b>"
    )
    return "\n".join([
        "<h3>💎 Crypto Pay</h3>",
        "<p>" + status + "</p>",
        "<p>" + tr(
            "Принимайте оплату подписок и файлов в криптовалюте. "
            "Деньги поступают <b>напрямую на ваш Crypto Pay</b>.",
            "Accept crypto payments for subscriptions and files. "
            "Funds go <b>directly to your own Crypto Pay</b>.",
        ) + "</p>",
        "<h4>" + tr("🔐 КАК ПОДКЛЮЧИТЬ?", "🔐 HOW TO CONNECT") + "</h4>",
        "<p>" + tr(
            "1️⃣ Откройте <b>@CryptoBot</b> в Telegram.<br>"
            "2️⃣ Перейдите в <b>Crypto Pay → Создать приложение</b>.<br>"
            "3️⃣ Скопируйте <b>API-токен</b> и отправьте его в ViMi Pay.",
            "1️⃣ Open <b>@CryptoBot</b> in Telegram.<br>"
            "2️⃣ Go to <b>Crypto Pay → Create App</b>.<br>"
            "3️⃣ Copy the <b>API token</b> and send it to ViMi Pay.",
        ) + "</p>",
        "<h4>" + tr(
            "🧪 ХОТИТЕ СНАЧАЛА ПРОВЕРИТЬ?",
            "🧪 WANT TO TEST IT FIRST?",
        ) + "</h4>",
        "<p>" + tr(
            "Есть тестовый режим <b>без реальных денег</b>.",
            "Use the test environment <b>without real money</b>.",
        ) + "</p>",
        "<p>⚠️ <b>" + tr("ВАЖНО", "IMPORTANT") + "</b><br>" + tr(
            "Само подключение токена не запускает продажи. "
            "Для покупок также нужны настроенные тарифы "
            "и автоматическая выдача подписок или файлов.",
            "Connecting a token alone does not start sales. "
            "Buyers also need configured plans and automatic "
            "subscription or file delivery.",
        ) + "</p>",
    ])


def crypto_overview_classic(app: object | None) -> str:
    status = (
        "🟢 <b>" + tr("ПОДКЛЮЧЕНО", "CONNECTED") + "</b>"
        + "\n" + escape(str(app.app_label))
        + " · " + _environment(str(app.environment))
        if app is not None else
        "🔴 <b>" + tr("НЕ ПОДКЛЮЧЕНО", "NOT CONNECTED") + "</b>"
    )
    return "\n\n".join([
        "💎 <b>Crypto Pay</b>\n" + status,
        tr(
            "Принимайте оплату подписок и файлов в криптовалюте. "
            "Деньги поступают <b>напрямую на ваш Crypto Pay</b>.",
            "Accept crypto payments for subscriptions and files. "
            "Funds go <b>directly to your own Crypto Pay</b>.",
        ),
        "<b>" + tr("🔐 КАК ПОДКЛЮЧИТЬ?", "🔐 HOW TO CONNECT") + "</b>\n"
        + tr(
            "1️⃣ Откройте <b>@CryptoBot</b>.\n"
            "2️⃣ Crypto Pay → <b>Создать приложение</b>.\n"
            "3️⃣ Скопируйте <b>API-токен</b> и отправьте его в ViMi Pay.",
            "1️⃣ Open <b>@CryptoBot</b>.\n"
            "2️⃣ Crypto Pay → <b>Create App</b>.\n"
            "3️⃣ Copy the <b>API token</b> and send it to ViMi Pay.",
        ),
        "<b>" + tr(
            "🧪 ХОТИТЕ СНАЧАЛА ПРОВЕРИТЬ?",
            "🧪 WANT TO TEST IT FIRST?",
        ) + "</b>\n" + tr(
            "Доступен тестовый режим без реальных денег.",
            "A test environment is available without real money.",
        ),
        "⚠️ <b>" + tr("ВАЖНО", "IMPORTANT") + "</b>\n" + tr(
            "Подключение токена само по себе не запускает продажи. "
            "Настройте тарифы и автоматическую выдачу подписок или файлов.",
            "Connecting a token alone does not start sales. "
            "Configure plans and automatic subscription or file delivery.",
        ),
    ])


def crypto_network_rich() -> str:
    return "\n".join([
        "<h3>" + tr(
            "💎 Выберите режим Crypto Pay",
            "💎 Choose a Crypto Pay environment",
        ) + "</h3>",
        "<p>" + tr(
            "Crypto Pay поддерживает <b>два режима</b>. "
            "Выберите подходящий для вашей задачи.",
            "Crypto Pay has <b>two environments</b>. "
            "Choose the one you need.",
        ) + "</p>",
        "<h4>" + tr("💰 РЕАЛЬНЫЕ ОПЛАТЫ", "💰 REAL PAYMENTS") + "</h4>",
        "<p>🟢 <b>@CryptoBot</b><br>" + tr(
            "Для настоящих продаж. Покупатели оплачивают подписки "
            "и файлы <b>реальной криптовалютой</b>.",
            "For live sales. Buyers pay for subscriptions and files "
            "<b>with real cryptocurrency</b>.",
        ) + "</p>",
        "<h4>" + tr("🧪 ТЕСТОВЫЙ РЕЖИМ", "🧪 TEST MODE") + "</h4>",
        "<p>🔵 <b>@CryptoTestnetBot</b><br>" + tr(
            "Для проверки платежей. Создавайте тестовые счета "
            "<b>без списания реальных денег</b>.",
            "For checking payment flows. Create test invoices "
            "<b>without spending real money</b>.",
        ) + "</p>",
        "<p>💡 <b>" + tr("ОБРАТИТЕ ВНИМАНИЕ", "PLEASE NOTE") + "</b><br>" + tr(
            "Для каждого режима нужен <b>свой API-токен</b>. "
            "Токены реальной и тестовой сети не взаимозаменяемы.",
            "Each environment needs <b>its own API token</b>. "
            "Live and test tokens are not interchangeable.",
        ) + "</p>",
    ])


def crypto_network_classic() -> str:
    return "\n\n".join([
        tr(
            "💎 <b>Выберите режим Crypto Pay</b>",
            "💎 <b>Choose a Crypto Pay environment</b>",
        ),
        tr(
            "Crypto Pay поддерживает <b>два режима</b>. "
            "Выберите подходящий для вашей задачи.",
            "Crypto Pay has <b>two environments</b>. Choose the one you need.",
        ),
        "<b>" + tr("💰 РЕАЛЬНЫЕ ОПЛАТЫ", "💰 REAL PAYMENTS") + "</b>\n"
        + "🟢 <b>@CryptoBot</b>\n" + tr(
            "Для настоящих продаж. Покупатели платят реальной криптовалютой.",
            "For live sales. Buyers pay with real cryptocurrency.",
        ),
        "<b>" + tr("🧪 ТЕСТОВЫЙ РЕЖИМ", "🧪 TEST MODE") + "</b>\n"
        + "🔵 <b>@CryptoTestnetBot</b>\n" + tr(
            "Для проверки платежей и счетов без реальных денег.",
            "For testing payments and invoices without real money.",
        ),
        "💡 <b>" + tr("ОБРАТИТЕ ВНИМАНИЕ", "PLEASE NOTE") + "</b>\n"
        + tr(
            "Для каждого режима нужен <b>свой API-токен</b>. "
            "Токены не взаимозаменяемы.",
            "Each environment needs <b>its own API token</b>. "
            "The tokens are not interchangeable.",
        ),
    ])


def crypto_token_rich(environment: str) -> str:
    provider = "@CryptoBot" if environment == "mainnet" else "@CryptoTestnetBot"
    heading = (
        tr("🟢 РЕАЛЬНЫЕ ОПЛАТЫ", "🟢 REAL PAYMENTS")
        if environment == "mainnet"
        else tr("🔵 ТЕСТОВЫЙ РЕЖИМ", "🔵 TEST MODE")
    )
    return "\n".join([
        "<h3>" + tr("🔐 Подключение Crypto Pay", "🔐 Connect Crypto Pay") + "</h3>",
        "<p><b>" + heading + "</b><br>🤖 "
        + tr("Бот:", "Bot:") + " <b>" + provider + "</b></p>",
        "<h4>" + tr("🔑 ОТПРАВЬТЕ API-ТОКЕН", "🔑 SEND YOUR API TOKEN") + "</h4>",
        "<p>" + tr(
            "1️⃣ Откройте <b>", "1️⃣ Open <b>",
        ) + provider + "</b>" + tr(
            " и раздел <b>Crypto Pay</b>.<br>"
            "2️⃣ Выберите приложение и скопируйте <b>API-токен</b>.<br>"
            "3️⃣ ➡️ Вставьте токен <b>ПРЯМО СЮДА</b>, "
            "в этот чат, отдельным сообщением.",
            " and go to <b>Crypto Pay</b>.<br>"
            "2️⃣ Select your app and copy its <b>API token</b>.<br>"
            "3️⃣ ➡️ Paste the token <b>RIGHT HERE</b>, "
            "in this chat as a separate message.",
        ) + "</p>",
        "<h4>" + tr("🛡 БЕЗОПАСНОСТЬ", "🛡 SECURITY") + "</h4>",
        "<p>" + tr(
            "🔒 Сообщение с токеном будет удалено <b>до проверки</b>.<br>"
            "⚠️ Никому не передавайте токен. Если он стал известен "
            "посторонним, замените его в Crypto Pay.",
            "🔒 Your token message is deleted <b>before verification</b>.<br>"
            "⚠️ Never share it. If it is exposed, rotate the token in Crypto Pay.",
        ) + "</p>",
        "<p><i>" + tr(
            "Для отмены подключения нажмите «Назад».",
            "To cancel setup, tap Back.",
        ) + "</i></p>",
    ])


def crypto_token_classic(environment: str) -> str:
    provider = "@CryptoBot" if environment == "mainnet" else "@CryptoTestnetBot"
    heading = (
        tr("🟢 РЕАЛЬНЫЕ ОПЛАТЫ", "🟢 REAL PAYMENTS")
        if environment == "mainnet"
        else tr("🔵 ТЕСТОВЫЙ РЕЖИМ", "🔵 TEST MODE")
    )
    return "\n\n".join([
        tr("🔐 <b>Подключение Crypto Pay</b>", "🔐 <b>Connect Crypto Pay</b>"),
        "<b>" + heading + "</b>\n🤖 " + tr("Бот: ", "Bot: ") + "<b>"
        + provider + "</b>",
        "<b>" + tr("🔑 ОТПРАВЬТЕ API-ТОКЕН", "🔑 SEND YOUR API TOKEN") + "</b>\n"
        + tr("1️⃣ Откройте ", "1️⃣ Open ") + provider + tr(
            " → Crypto Pay.\n"
            "2️⃣ Выберите приложение и скопируйте <b>API-токен</b>.\n"
            "3️⃣ ➡️ Отправьте токен <b>ПРЯМО СЮДА</b>, отдельным сообщением.",
            " → Crypto Pay.\n"
            "2️⃣ Choose your app and copy its <b>API token</b>.\n"
            "3️⃣ ➡️ Send the token <b>RIGHT HERE</b> as a separate message.",
        ),
        "<b>" + tr("🛡 БЕЗОПАСНОСТЬ", "🛡 SECURITY") + "</b>\n" + tr(
            "🔒 Сообщение будет удалено до проверки.\n"
            "⚠️ Никому не передавайте токен. Если он раскрыт, "
            "смените его в Crypto Pay.",
            "🔒 The message is deleted before verification.\n"
            "⚠️ Never share the token. Rotate it if exposed.",
        ),
        "<i>" + tr(
            "Для отмены подключения нажмите «Назад».",
            "To cancel setup, tap Back.",
        ) + "</i>",
    ])
