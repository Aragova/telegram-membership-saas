# Payment Methods and Manual Settlement

This document defines the first payment-provider slice.

## Product rule

A `PaymentMethod` describes how a Project owner receives money. In the first release the only implemented provider is `MANUAL_SETTLEMENT`.

The provider type is architectural. The owner-facing method name is arbitrary, for example:

- Сбербанк;
- СБП;
- T-Bank;
- Revolut;
- PayPal;
- USDT TRC20;
- USDT TON.

No bank integration is implied by these names.

## Currency versus payment method

Currency/asset and payment method/network are separate concepts.

Examples:

```text
USDT             -> CurrencyAsset
USDT TRC20       -> PaymentMethod
USDT TON         -> PaymentMethod
USDT ERC20       -> future PaymentMethod
```

A payment method links to one or more supported `CurrencyAsset` rows. It is invalid for a method to have zero supported currencies.

The buyer flow later becomes:

```text
Plan
-> equal currency/price pair
-> compatible payment method
-> payment instruction
```

If there is only one compatible currency or method, the UI may skip the redundant selection screen.

## Manual settlement

For `MANUAL_SETTLEMENT` the buyer pays outside our system using the owner's instructions.

The system does not:

- connect to the owner's bank;
- parse bank statements;
- OCR screenshots or receipts;
- validate a receipt image;
- store a proof archive;
- create a Payment before an administrator approves the request.

Proof handling belongs to the later PaymentRequest Telegram flow: the buyer's Telegram photo/screenshot/document is forwarded or copied to the project administrator, who checks the actual bank balance and then approves or rejects the request.

## Instruction template

The owner writes arbitrary instruction text. Four safe placeholders are available:

- `{amount}`;
- `{currency}`;
- `{plan}`;
- `{period}`.

Unknown fields, Python conversions and format specs are rejected. Literal braces can be escaped as `{{` and `}}`.

Example:

```text
Переведите {amount} {currency} по указанным реквизитам.
Тариф: {plan}
Период: {period}
После оплаты отправьте подтверждение в этот чат.
```

## Tenant isolation

`payment_methods` and `payment_method_currencies` both carry `project_id` and use forced PostgreSQL RLS.

The currency link also uses a composite foreign key to `(payment_methods.id, payment_methods.project_id)`, so a currency row cannot be attached to a method from another Project by supplying mismatched tenant identifiers.

`currency_assets` remains global reference data.

## Future providers

The provider enum is intentionally separate from the display name so later adapters can add providers such as Telegram Stars, Stripe, YooKassa, PayPal or a crypto processor without changing the meaning of the current manual-settlement methods.

The current database check allows only `manual_settlement`; a future provider is introduced by an explicit migration rather than by arbitrary strings.
