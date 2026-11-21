# ViMi Pay owner setup: channel/group step 2/4

Approved Telegram screen UX (21 November 2026), for the project bot
(`ViMi Pay Channel`) during initial setup, Russian and English.

## Four connected screens

1. **📢 My channels and groups**: an authentic connected-resource counter,
   short copy, three existing add actions (channel, group, manual ID) and
   **⬅️ Back**. No shortcut to an unavailable admin dashboard. Back goes to
   the existing dashboard callback which automatically renders setup step 1/4
   until onboarding is complete; for configured projects it opens the normal
   dashboard. Connected resource rows and management callbacks are unchanged.
2. **📢 Connect channel**: step 2/4, exact registered @bot, permissions to
   invite and ban members, and Telegram's native `request_chat` chooser for
   channels. Native buttons: Select channel, Back.
3. **👥 Connect group**: same, with the native `request_chat` group chooser,
   which must keep `chat_is_channel=False`. Buttons: Select group, Back.
4. **✍️ Connect by ID**: rich and classic HTML, guidance to send numeric
   `-100…` ID or public `@username` in the current chat, exact connected
   bot, access requirements, examples and only a generation-bound Back button.
   No Main menu button.

No extra Help/Language/Cancel/Admin button is added to these screens. The
`resource_reference` flow keeps existing tenant and Telegram verification.

## Telegram technical constraint

A real system chat chooser is available through a **reply keyboard**
`KeyboardButtonRequestChat`, not a Rich inline button. Its one message
is formatted in Telegram HTML and its two buttons are native Telegram
controls (Telegram owns their actual colors). The directory and manual
ID form use true Rich rendering and classic HTML fallback.

The native Back button atomically discards **only** a current owner-scoped
`chat_picker` step and removes its reply keyboard. An old Back cannot
cancel a different newer input form. The existing `chat_shared` request ID
check and `verify_channel_and_add` still verify the selected chat and the
Telegram admin permissions `can_invite_users`, `can_restrict_members`,
and the requesting owner role.

## Regression checks

Both languages, HTML escaping, native channel-vs-group request constraints,
one active Back, real selected bot username, category routes, new-vs-expired
native picker, manual prompt generation and no bypass of setup or existing
financial/access controls. No database changes.
