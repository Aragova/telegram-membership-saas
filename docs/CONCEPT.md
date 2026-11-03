# Product Concept v1.4

## 1. Назначение

SaaS-сервис для монетизации закрытых Telegram-каналов и групп.

Владелец создаёт проект, подключает ресурс, создаёт тарифы, задаёт сроки, валюты и цены, создаёт способы оплаты и получает заявки от покупателей. После ручного подтверждения оплаты система автоматически управляет подпиской и доступом.

## 2. Основной сценарий владельца

1. Создать Project.
2. Подключить Telegram Channel/Group.
3. Создать Plan.
4. Задать Duration.
5. Добавить Currency + Price пары.
6. Создать ManualSettlement payment method.
7. Опубликовать тариф.
8. Получать Payment Requests.
9. Проверять реальное поступление денег в своём банке.
10. Approve/Reject.
11. Система сама управляет дальнейшим доступом.

## 3. Основной сценарий покупателя

1. Открыть bot.
2. Выбрать Plan.
3. Выбрать Currency.
4. Выбрать совместимый Payment Method.
5. Получить инструкцию владельца.
6. Оплатить напрямую владельцу.
7. Отправить screenshot/receipt в Telegram.
8. Дождаться решения администратора.
9. После approve получить доступ.
10. Смотреть срок своей подписки и продлевать её.

## 4. Главное меню владельца

- Тарифы
- Каналы / группы
- Оплата
- Заявки
- Подписчики
- Статистика
- Уведомления
- Настройки
- Посмотреть как клиент

Будущие разделы: промокоды, рефералы, рассылки, дополнительные администраторы, web dashboard.

## 5. Plans

Plan содержит имя, срок действия, связанные Resources, Prices и Promotions.

Пример карточки:

- Тариф: 1 месяц
- Срок: 1 MONTH
- RUB — 666
- USD — 10
- EUR — 9
- Акция: нет
- Доступ: Premium Channel

## 6. Currency model

У тарифа нет primary currency и base price. Все валюты равноправны.

Пример:

- RUB — 666
- USD — 10
- EUR — 9
- USDT — 10

Раздел «Валюта» позволяет добавить/удалить currency. При добавлении currency сразу задаётся price.

Раздел «Цена» показывает все существующие currency+price пары и позволяет изменить цену любой валюты.

Currency и Payment Method — разные понятия:

- USDT = currency asset;
- USDT TRC20 = payment method;
- RUB = currency asset;
- Сбербанк / СБП / T-Bank = payment methods.

## 7. Currency Registry

CurrencyAsset должен содержать как минимум:

- code;
- type: FIAT/CRYPTO;
- name;
- symbol;
- precision;
- status.

Не использовать float для денег.

## 8. Pricing

PlanPrice должен поддерживать versioning. UI может показывать простое изменение цены, но история старых покупок не должна переписываться.

PaymentRequest фиксирует pricing snapshot:

- currency;
- regular_amount;
- discount_amount;
- final_amount;
- promotion_id при наличии.

## 9. Duration

Хранить value + unit, а не считать месяц 30 днями.

Примеры:

- 7 DAY;
- 1 MONTH;
- 3 MONTH;
- 6 MONTH;
- 1 YEAR;
- LIFETIME.

## 10. Promotions

MVP допускает:

- процентную скидку;
- ручную акционную цену для каждой валюты.

Promotion имеет name, starts_at, ends_at, status и работает в timezone проекта.

Сумма заявки snapshot-ится: завершение акции не меняет уже созданную PaymentRequest.

## 11. Payment Methods

Payment Method задаёт владелец проекта.

Основной MVP provider: `ManualSettlement`.

Примеры method names:

- Сбербанк;
- СБП;
- T-Bank;
- Revolut;
- PayPal;
- USDT TRC20;
- BTC;
- TON.

Payment Method связывается с одной или несколькими currencies.

## 12. ManualSettlement

В интерфейсе: «Полуавтоматический».

Flow:

- пользователь выбирает plan/currency/method;
- bot показывает instruction template;
- пользователь оплачивает вне системы;
- пользователь отправляет screenshot/receipt;
- bot пересылает Telegram-сообщение/файл admin;
- admin открывает свой bank/payment app и проверяет реальное поступление;
- admin нажимает Approve или Reject.

Система не должна хранить или анализировать файл.

Не делать OCR, AI receipt analysis, bank detection, transaction parsing, duplicate receipt detection, fraud scoring, S3 archive или ProofAsset.

## 13. PaymentRequest

До approve существует только PaymentRequest.

Статусы:

- WAITING_CONFIRMATION;
- WAITING_REVIEW;
- APPROVED;
- REJECTED;
- CANCELLED;
- EXPIRED.

В ней хранится бизнес-состояние и pricing snapshot, но не файл подтверждения.

## 14. Approval

Approve должен быть atomic и idempotent.

Если два администратора нажали approve одновременно, только один transition `WAITING_REVIEW -> APPROVED` успешен. Второй получает «заявка уже обработана».

Повторный callback не должен создавать второй Payment или продлевать подписку повторно.

## 15. Payment

Payment создаётся только после успешного approve или автоматического подтверждения будущим payment provider.

## 16. Subscription

Subscription — коммерческий lifecycle тарифа пользователя.

Если активная subscription продлевается, новый срок считается от текущего current_period_end, а не от today.

## 17. Entitlement Grants

Каждое независимое основание доступа хранится отдельно.

Источники:

- PAYMENT;
- MANUAL;
- OWNER_SERVICE;
- PROMOCODE;
- TRIAL;
- REFERRAL;
- IMPORT;
- COMPENSATION.

Истечение одного grant не означает revoke, пока существует другой active grant.

## 18. Owner / Service Access

Владелец сети ботов не должен покупать собственные тарифы или подтверждать себе оплату по каждому каналу.

Платформа должна поддерживать служебные Telegram-аккаунты владельца:

- можно привязать один или несколько owner/service accounts;
- service account может автоматически получать доступ ко всем существующим проектам владельца;
- можно включить автоматическое применение ко всем будущим проектам;
- постоянный доступ создаётся напрямую как `EntitlementGrant(source=OWNER_SERVICE, valid_until=NULL)`;
- для него не создаются PaymentRequest, Payment и Subscription;
- он не учитывается как платная продажа, revenue, conversion или paid subscriber;
- service access можно отозвать отдельно, не затрагивая management role;
- управление проектом и доступ к контенту — независимые права.

Для обычных пользователей Manual grant также может быть временным или постоянным, но относится только к выбранному scope, а не автоматически ко всей сети владельца.

Это не отдельный коммерческий Plan и не «бесплатный тариф».

## 19. Effective Access

Effective Access вычисляется из всех активных grants на resource.

- есть хотя бы один активный grant -> desired_access = true;
- нет активных grants -> desired_access = false.

`valid_until = NULL` означает бессрочный grant до явного revoke.

## 20. Access lifecycle

После approve:

Payment -> Subscription -> Entitlement Grants -> Effective Access -> Outbox -> Worker -> Telegram Adapter.

Для OWNER_SERVICE:

Owner Service Assignment -> Entitlement Grants -> Effective Access -> Outbox -> Worker -> Telegram Adapter.

После истечения subscription:

Expire соответствующий grant -> Recalculate Effective Access -> Revoke только если активных grants больше нет.

## 21. Safe join

Предпочтительно использовать временные invite links / join requests и сверять Telegram user ID перед approve join request.

## 22. Reconciliation

Периодически сравнивать desired access в БД с фактическим Telegram state.

Расхождения должны восстанавливаться через retry/reconciliation.

## 23. Subscribers

Админ должен видеть Active / Expiring Soon / Expired, поиск пользователя, историю покупок и возможность manual grant/revoke.

Служебные owner/service accounts должны отображаться отдельно от платных подписчиков.

## 24. TimeZone

Default нового проекта: `Europe/Moscow`.

Владелец может изменить timezone проекта.

Все timestamps в БД — UTC. UI, promotions, calendar duration, reminders, daily statistics — в timezone проекта.

## 25. Localization

Строки должны быть отделены от handlers через keys. MVP может быть RU-only, но архитектура должна допускать project default language, supported languages и user language.

## 26. Future expansion

Архитектура должна позволять добавить без перестройки core:

- Telegram Stars;
- Stripe;
- YooKassa;
- PayPal;
- crypto providers;
- recurring subscriptions;
- web dashboard;
- REST API;
- white-label bots;
- Managed Bots;
- multi-resource bundles;
- Discord/web access;
- promo codes;
- trials;
- referrals;
- analytics.

## 27. MVP success criterion

MVP считается успешным, когда стабильно работает end-to-end цепочка:

Create Project -> Connect Resource -> Create Plan -> Add Currencies/Prices -> Create ManualSettlement -> Customer selects/buys -> Sends confirmation -> Admin verifies bank -> Approves -> Payment -> Subscription -> Grants -> Access -> Renewal -> Expiration -> Effective access recalculation -> Revoke when no grants remain.

Отдельный acceptance для владельца сети:

Link owner/service Telegram account -> mark `ALL_OWNER_PROJECTS` -> create future project/resource -> owner service grant appears without payment/subscription -> access is granted automatically -> commercial statistics remain unchanged.