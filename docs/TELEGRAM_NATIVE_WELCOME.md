# Telegram 2026: native Welcome Messages in ViMi Pay

## Actual API capabilities

Telegram officially introduced native, new-member-only welcome packs for
channels/groups (August 2026). In the chat UI, **channel owner/admin** uses
**Chat profile → Edit → Welcome Message** to write one or more messages,
including Rich media. The rest of the group does not see these messages.

Telegram Bot API 10.3 exposes `can_send_welcome_messages` on administrator
rights, but **does not expose HTTP methods for reading, replacing or enabling
a chat's native welcome pack**. Do not invent a Bot API call or claim a saved
pack has been activated. Bot permission is informational: owners can configure
native welcome packs directly. Do not replace this with a public channel
message, unconsented DM, join auto-message or Telegram ephemeral response.
Ephemeral Messages remain a separate planned technology.

Official references:
- https://telegram.org/blog/welcome-messages-buttons-TG-13
- https://core.telegram.org/bots/api-changelog#august-24-2026
- https://core.telegram.org/bots/api

## ViMi Pay integration

The native-welcome setup assistant is shown only inside a connected bot's
**📢 Channels → 📨 Telegram welcomes** screen, never in the central connection
registrar or the buyer storefront.

- List paginated **active tenant resources** (channel and group) only.
- Resolve the chosen resource under the actor's project-manager permission
  and tenant scope, not by trusting callback IDs or the client.
- Read the bot's current `can_send_welcome_messages` right using
  `getChatMember`; distinguish granted, missing, unknown and non-admin.
  Never modify or promote administrators in this screen.
- Show an owner's Telegram-native editor path. Provide a starter text that
  can be copied (RU/EN, under Telegram's CopyTextButton length limit).
- The "Open chat" link is shown **only** when a safe, explicitly public
  `@username` is available; private channels require opening the chat
  manually. No fabricated private Telegram message links.
- No DB writes, migrations, credentials or deployment flags.

## Real Telegram staging acceptance

1. From a **connected bot** open Channels and select Telegram welcomes.
2. Select a pre-existing active channel, verify its name and bot welcome
   permission. Verify private channels don't get an unreliable URL.
3. Copy starter text; paste it into **channel profile → Edit → Welcome
   Message** and optionally add media or a second message. Save in Telegram.
4. Join that channel with a **new test subscriber** and confirm only they see
   the native pack. A previously joined subscriber should not see a new
   welcome pack retroactively.
5. Verify a bot without the extra permission is correctly marked as such.
   The owner should still be able to configure welcome directly.
6. Verify an unconnected channel and a callback copied from a different bot
   cannot view the other bot's channel ID or rights.
7. Verify normal subscription activation, payment and access flow unchanged.

This integration **does not assert** Telegram's configured welcome pack is
active until the owner tests it on a new member. Keep `compose.light100.yaml`
and all active connections unchanged.
