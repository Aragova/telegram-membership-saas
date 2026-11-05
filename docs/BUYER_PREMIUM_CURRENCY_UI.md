# Buyer storefront — premium currency selection (Telegram-native)

## Why

The original buyer UI grouped currencies four per row and used a faint
monochrome ✓ symbol. With RUB, USD, EUR, UAH and BYN enabled, UAH was alone
on the second row despite all five fitting the intended reference layout.
The copy also duplicated a name such as "1 месяц · 1 месяц".

## UI contract

- **Five currencies per row**, in the familiar order RUB, USD, EUR, UAH, BYN.
  Buttons are deliberately compact: 🇷🇺RUB, 🇺🇸USD, 🇪🇺EUR, 🇺🇦UAH, 🇧🇾BYN.
- The currently selected currency shows a **bright green dot** and requests
  Telegram's **green success button style**. If an older Telegram client
  ignores color styles, the explicit dot still shows the selection.
- With more than five enabled currencies, rows are split into groups of five.
  No artificial placeholders, currencies without eligible payment methods,
  currency conversions, or inactive options are introduced.
- Other common fiat codes use appropriate flags; supported crypto codes
  use neutral symbols. Unmapped asset codes retain readable labels with a
  generic 🌐 icon rather than a misleading national flag.
- Buyer catalog copy is welcoming and concise, with a clear currency,
  channel name, price, and what happens after picking a plan.
  An automatically generated name identical to the subscription duration
  is shown once, not repeated.
- The offer detail retains the original payment method selection and
  the verified price/plan ID in the callback.
- The **read-only owner guest preview** uses the same renderer, with
  callbacks rewritten to the owner preview namespace, never checkout.
- The output is Russian/English localized; all source strings are escaped
  as HTML where appropriate.

**Telegram controls final button pixel widths and appearance.** Bot code
controls row groupings, labels, and supported button styles, but cannot
override client fonts, theme or platform-specific rendering.

No new migrations, no pricing changes, no bot registration or other native
Telegram menu commands. Buyer pricing and checkout remain the source of
truth, only the visible storefront changed.

## Deployment

After review/merge, staging requires only rebuilding **customer-supervisor**
using existing `compose.light100.yaml`; no PostgreSQL migration and no
restart of central `bot` or PostgreSQL. Verify a customer bot offering the
five currencies and another with fewer/extra currencies, and use
**Смотреть гостем** if desired. Never touch production.
