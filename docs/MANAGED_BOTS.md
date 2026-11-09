# Managed Bots in ViMi Pay (Telegram Bot API 9.6+)

## Purpose and security model

ViMi Pay central manager: `@vimipay_bot`. A channel owner may **create a
new bot without sending the bot token in chat**, using Telegram's native
Managed Bot creation dialog. The bot remains owned by that Telegram user.
The central manager obtains its token using the HTTP Bot API
`getManagedBotToken`. This is **not** an MTProto user login and does not
ask users for passwords, phone numbers or auth codes.

Official Telegram references:

- https://core.telegram.org/bots/features#managed-bots
- https://core.telegram.org/bots/api#keyboardbuttonrequestmanagedbot
- https://core.telegram.org/bots/api#managedbotupdated
- https://core.telegram.org/bots/api#getmanagedbottoken
- https://core.telegram.org/bots/api#replacemanagedbottoken

The feature requires **Bot Management Mode** to be enabled for
`@vimipay_bot` in **@BotFather Mini App**. The central bot's
`getMe.can_manage_bots` gate prevents a confusing unsupported dialog.

## Owner experience

1. Central ViMi Pay → **✨ Создать бота / Create bot**.
2. Telegram shows a native `request_managed_bot` button in the private
   chat. The owner sets the new bot name and username and approves creation.
3. Telegram sends `managed_bot` update and/or `managed_bot_created`
   service message. The same handler accepts either and deduplicates them
   using the **15-minute, owner-scoped wizard row lock**.
4. ViMi Pay checks the creator's Telegram ID, fetches the token through
   the Bot API, verifies the exact created bot ID via `getMe`, then calls
   the existing atomic project+bot registration service.
5. The token is stored **only encrypted** in `bot_instances` using the
   existing `TMS_BOT_TOKEN_ENCRYPTION_KEY`. The message is never shown or
   logged. The bot starts with independent, inactive draft commercial
   configuration and can be configured **inside the created bot**.
6. The old **🔑 Через BotFather / Via BotFather** token path is still
   supported and all existing customer bots are unchanged.

## Token synchronization (explicit)

On an existing connection: **🔄 Managed Bot: синхронизировать токен**.
The central manager only fetches a token for a **registered bot in a
project owned by the user pressing the button**, verifies the actual bot
identity and uses the existing owner-checked credential update function.
If token was unchanged, no DB change or worker restart is made.
**Never invoke** `replaceManagedBotToken` automatically: it would revoke
a working token and could interrupt existing sales/access.
Unsolicited `managed_bot` update events do **not** rotate credentials.
If Telegram rotated the token, the owner uses this button to synchronize it.

No extra tables, migrations, data transfers, cross-tenant permissions, or
per-process bot limits are added by this feature. It does not
automatically transfer a created bot into any existing channel.

## Testing & rollout

CI covers:

- Native managed-bot keyboard and manager permission gate.
- Separate `managed_bot` and service-message event handlers.
- Owner-authorized event handling and duplicate delivery safety.
- Real PostgreSQL RLS atomic project+bot registration.
- Encrypted token only; strict bot identity check; no cross-user linking.
- Explicit token synchronization and rejected cross-owner requests.

A **real Telegram end-to-end test** additionally requires a central bot
with Bot Management Mode enabled and an account that creates a **new test
managed bot**. The API's creation dialog cannot be reproduced by CI mocks.
After PR approval and staging deployment:

1. Enable Bot Management Mode for `@vimipay_bot` in @BotFather.
2. From one test Telegram account, open central bot and press Create bot.
3. Use the native Telegram dialog to create a new test bot, confirm the
   bot appears under the same owner's connections and opens normally.
4. Check no token was posted in chat; configure that bot internally.
5. Check duplicate Telegram updates didn't create multiple projects.
6. Test the explicit token-sync action (without revoking a token).
7. Confirm existing bots and all payment/receipt flows are unchanged.

**Never test** by calling `replaceManagedBotToken` against a live
customer bot. No secret, QR-code, password or bot token should be sent
in the ChatGPT conversation.
