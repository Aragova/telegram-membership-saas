# ViMi Pay — Rich UI standard (RU + EN)

This is the presentation contract for **new or redesigned screens** in the
central bot and customer/admin bots. A screen is not considered finished merely
because its handler and database actions work.

## Messages

- Use one purposeful screen for each step. A completed step must not
  automatically open a second management menu if the owner has not requested it.
- Prefer Telegram Rich HTML via the existing supported sender and **always**
  provide a complete readable classic HTML fallback.
- Use a clear heading, short **bold** action names, *italic* supporting
  explanations, semantic emojis and a short "what to do next" section when
  there is an obvious follow-up. Do not generate empty cards or lengthy
  repetitions of the same information.
- Tell the user **where** to act: in the current ViMi Pay chat, @BotFather,
  or inside their connected bot. Never refer vaguely to "enter the token"
  without identifying the chat/input.
- Display truthful operational states (connected is not the same as running;
  running is not the same as sales ready). Do not leak internal UUIDs,
  tokens, diagnostics, or untranslated technical statuses.
- Provide complete RU and EN texts, including buttons, errors and fallback
  copy. Escape any owner-entered name or Telegram username inserted into HTML.

## Inline buttons

- Arrange related actions **two per row** when both labels remain readable.
  A single full-width action is appropriate for longer, critical, navigation,
  or destructive controls. Do not cram more actions into a row just to satisfy
  a numeric target.
- **Green / success:** primary forward actions such as open/continue and a
  contextual check when it is part of the main workflow.
- **Blue / primary:** settings, editing, additional services, back and other
  navigation.
- **Red / danger:** disconnect, delete, archive or other destructive
  operations. Isolate destructive controls in their own row and keep the
  existing confirmation/authorization flow.
- Buttons must represent working actions with the correct project/bot scope;
  do not add placeholders, duplicate navigations or testing-only buttons.

## Engineering acceptance

- Confirm Rich and classic rendering, RU/EN, keyboard closure, valid and stale
  callbacks, privilege boundaries, and no unintended repeat messages.
- Preserve account invoices, checkout flows, token redaction and protected
  retirement semantics when changing onboarding/management presentation.
- Open one focused PR, run CI and request review. No merge or deployment without
  explicit owner approval.
