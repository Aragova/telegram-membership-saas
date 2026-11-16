# ViMi Pay — защищённые резервные копии и восстановление

> **Статус:** план и opt-in implementation, НЕ включены на VM; отдельные ключи, GCS bucket и systemd timer ещё не подтверждены. Никаких автоматических изменений staging.

## Что проверено оператором 15 ноября 2026

На VM `fast-vm` PostgreSQL ViMi Pay расположен в отдельном named Docker volume `telegram-membership-saas-staging_postgres_data`, PG 16.15. Измерено 86 ГБ свободного места, backup timers/cron для проекта в стандартных местах не обнаружены; `dpkg-db-backup.timer` не является бэкапом PostgreSQL. **Это не исключает snapshots на уровне GCE**.

Создан ручной архив `/var/backups/vimi-pay/membership-20261115T155206Z.dump` (~4,9 МиБ): `pg_restore` прочитал архив, SHA-256 совпала. **Успешно выполнено полноценное восстановление в временном контейнере** `postgres:16-alpine --network none`: 51 ботов, 24 финансовых счёта, 743 пользователя; журнал завершился `RESTORE_OK`. Временная база удалена; боевые контейнеры и БД не изменились. Это проверка **базовой базы**, не полного восстановления VM, ключей или внешних API.

## Реальная проверка GCE перед облачными бэкапами, 15 ноября 2026

Оператор выполнил read-only запрос к GCE metadata: VM `fast-vm`, project
`project-4b9dfa7c-54f8-443e-86a`, zone `europe-west4-b`; прикреплён
**стандартный Compute Engine service account**, не специально выделенный
для резервных копий. Установлены `gcloud` и `curl`, **не установлены**
`age` и `age-keygen`. OAuth scopes VM включают
`https://www.googleapis.com/auth/devstorage.read_only`, но **не включают**
`devstorage.read_write` или `cloud-platform`. Поэтому загрузка в GCS
с текущими VM credentials **не будет работать даже при IAM
`roles/storage.objectCreator`**.

**Блокирующая мера перед любым пробным upload:** выбрать модель выдачи
права записи, учитывая что на этой VM также работает другой ViMi сервис.
Предпочтительно провести IAM-аудит текущего SA и зависимостей двух сервисов,
подготовить отдельный user-managed SA с минимальными необходимыми правами,
проверить план миграции. Google Cloud требует **остановить VM перед
изменением прикреплённого SA или OAuth scopes**. Это означает downtime
обоих ViMi сервисов и может изменить внешний ephemeral IPv4. **Не
выполнять без подтверждённого статического IP, backup/rollback и
согласованного окна обслуживания**. Если остановка VM неприемлема,
использовать отдельный изолированный uploader со своей IAM-identity,
спроектировав безопасный канал передачи encrypted archive. Не
расширять scopes существующего default SA вслепую: при широких
существующих IAM roles это может непреднамеренно открыть новые привилегии.

Официальные источники:
- https://docs.cloud.google.com/compute/docs/access/service-accounts
- https://docs.cloud.google.com/compute/docs/instances/change-service-account

**До устранения этого блокера:** не устанавливать/включать таймер,
не объявлять GCS backup рабочим. Локальный архив и изолированный restore
продолжают оставаться единственной подтверждённой проверкой.

## Cloud Run URL diagnostics (16 ноября 2026)

Оператор подтвердил: private Cloud Run `vimi-pay-backup-broker` развернулся со статусом Ready и с корректным writer SA, а default Compute SA VM получил `roles/run.invoker` только на него. Пробы `GET /healthz` (даже с корректным ID-token), возвращали Google frontend HTML `404` без GET request-логов. Это **не доказывает** неисправность маршрутизации: Cloud Run резервирует некоторые пути, оканчивающиеся на `z`. Поэтому endpoint изменён на `GET /health`; для проверки уже развёрнутой ревизии до её обновления допустим только безопасный `POST /session` с `{}`, который должен ответить `400` с JSON `invalid request` и не должен создавать объект или upload-session. Официально: https://docs.cloud.google.com/run/docs/known-issues . Полный E2E пока не выполнен.

## Проверка безостановочной авторизации и архитектура Cloud Run, 16 ноября 2026

Оператор подтвердил на `fast-vm`: запрос GCE metadata
`.../service-accounts/default/identity?audience=https://example.com`
вернул HTTP **200**. Это доказывает, что VM умеет получать Google ID-токен
для указанной аудитории, но **не** является подтверждением разрешения
Cloud Run Invoker и **не** даёт доступ к GCS. Во время настройки оператор
создал приватный EU bucket `vimi-pay-backups-633224371599` с Soft Delete,
выбрал Lifecycle Delete по age 90 дней и создал сервисный аккаунт
`vimi-pay-backup-writer` без JSON-ключей. Выдача этому аккаунту роли
`Storage Object Creator` на bucket инициирована через Cloud Console;
состояние после сохранения IAM policy и lifecycle необходимо проверить
перед тестом.

**Выбран вариант без остановки VM:** вместо прямых записей из VM в GCS
используем **IAM-закрытый Cloud Run session broker** (см.
`ops/backup/session_broker/`). Обязательно при развёртывании:

- Cloud Run service identity: новый `vimi-pay-backup-writer`, только
  `roles/storage.objectCreator` на выделенном bucket, без
  Project Editor/Storage Admin.
- Выдать существующему прикреплённому Compute Engine default SA
  `633224371599-compute@developer.gserviceaccount.com` **только
  `roles/run.invoker` на конкретный Cloud Run service**. Не назначать
  на проект, не включать `allUsers`, не создавать SA keys.
- Broker развёртывается в `europe-west4`, требует аутентификацию
  `--no-allow-unauthenticated`, запущен с
  `VIMI_BACKUP_GCS_BUCKET=vimi-pay-backups-633224371599`. Проверить
  отсутствие публичных IAM binding и допустимый входящий HTTPS.
- VM получает **ID-токен для URL реального Cloud Run service** (не
  `example.com`), передаёт его в приватный `POST /session`. Broker,
  использующий отдельную identity с правом записи, создаёт GCS
  **resumable upload session** для одного из трёх строго разрешённых
  файлов внутри уникального каталога `vimi-pay/postgres/UTC-UUID/`.
  Устанавливается `ifGenerationMatch=0`. Broker возвращает session URI.
- Этот session URI — **bearer-секрет** и никогда не должен попадать в
  командную строку, журналы, bash trace, browser, чат или GitHub.
  `ops/backup/session_upload.py` держит URI только в памяти Python и
  загружает зашифрованные файлы напрямую в GCS через HTTPS. Даже большой
  архив не проходит через Cloud Run: лимит 32 MiB на HTTP/1 запросы
  Cloud Run здесь не применяется к содержимому архива.
- `vimi-pay-gcs-backup.sh` создаёт два зашифрованных файла и в конце
  контрольный `manifest.sha256`. Если любой из трёх uploads не завершился,
  маркер завершённой копии не публикуется. Перезапуск по расписанию
  создаст новый prefix. Существующий uploader **пока не реализует**
  возобновление после обрыва потоковой передачи в одной сессии;
  для больших архивов надо добавить контроль сетевых ошибок и
  возобновление по подтверждённому byte offset. Не утверждать, что
  механизм испытан на больших объёмах.

**Важно:** `Cloud Run` сервис пока отсутствует, backend и VM-клиент
не развернуты, код не подвергался живому E2E через GCS. Новый код в
черновом PR #172 — архитектурная заготовка, **не готовое расписание**.
Перед запуском необходимы независимое ревью кода, CI, приватный Cloud Run
и проверка настоящей загрузки и восстановления в изолированную БД.

Документация Google:
- https://docs.cloud.google.com/run/docs/authenticating/service-to-service
- https://docs.cloud.google.com/storage/docs/resumable-uploads
- https://docs.cloud.google.com/storage/docs/performing-resumable-uploads

## Чего нет в обычном `pg_dump`

- Кластерные роли, grants и настройки PostgreSQL: для них необходим отдельно `pg_dumpall --globals-only`.
- Ключи `TMS_BOT_TOKEN_ENCRYPTION_KEY`, токены самого центрального бота, секреты провайдеров и конфигурация VM/Nginx: хранить **отдельно** в защищённом менеджере секретов/офлайн-хранилище. Нельзя класть секреты в git и в обычный архив; шифрование поля токена в БД **без ключа** не позволяет восстановить запуск.
- Files/media, хранящиеся вне базы, включая внешние Telegram file_id / объектное хранилище: провести инвентаризацию отдельно.
- Google Compute Engine persistent disk snapshots; GCS-архив **не заменяет** снимки системы и конфигурации.

## Целевое решение: age + закрытый Cloud Run + GCS

Все `ops/backup/` файлы **отключены по умолчанию**.

1. `vimi-pay-gcs-backup.sh` шифрует `pg_dump -Fc -Z 6` и
   `pg_dumpall --globals-only` публичным age recipient, не записывая
   plaintext dump на локальный диск. Приватная age identity хранится
   только в независимом безопасном хранилище.
2. Root-only конфигурация `/etc/vimi-pay/backup.env` включает
   `VIMI_BACKUP_SESSION_SERVICE_URL` (HTTPS URL приватного Cloud Run),
   `VIMI_BACKUP_AGE_RECIPIENT` и имя Docker PostgreSQL контейнера.
   На VM нужны `age`, `python3`, `docker`, `flock`,
   `sha256sum`; для загрузки **не нужны GCS write OAuth scope**,
   `gcloud auth print-access-token` или JSON-ключи.
3. На Cloud Run `session_broker/server.py` создаёт GCS resumable
   sessions под отдельным `vimi-pay-backup-writer` с bucket-level
   `roles/storage.objectCreator`. VM вызывает broker по IAM ID-токену.
4. Скрипт передаёт в GCS только **зашифрованные** байты. Каждый запуск
   использует уникальный UTC+UUID prefix; `manifest.sha256`
   публикуется **последним**.
5. На bucket обязательно действует lifecycle rule
   `ops/backup/gcs-lifecycle-90days.json`: root object
   `{"rule":[{"action":{"type":"Delete"},"condition":{"age":90}}]}`.
   Она очищает как завершённые, так и незавершённые prefixes.
   Soft Delete — 7 дней; без Object Versioning; необратимый
   Bucket Lock не нужен. Проверить фактически **сохранённое** правило.
6. `vimi-pay-gcs-backup.timer` — ежедневно в 03:20 UTC + до 20 минут
   случайной задержки. Merge PR **не устанавливает** unit на VM
   и не запускает таймер.

## Чек-лист выпуска, обязательный перед включением

- [ ] Точный HEAD CI и независимое security-review зелёные.
- [ ] Bucket проверен: регион EU, закрыт от публики, uniform access,
  lifecycle Delete Age=90 и soft-delete 7 дней действуют.
- [ ] Проверены permissions нового writer SA (Object Creator только
  bucket) и `roles/run.invoker` старого default SA только для брокера.
- [ ] Собрать проверенный образ из
  `ops/backup/session_broker/Dockerfile`, развернуть в Cloud Run
  `europe-west4`, запретить unauthenticated invocations,
  назначить writer SA и bucket env; проверить, что без токена сервис
  отвечает 401/403, а с ID-токеном проходит только разрешённый вызов.
- [ ] На VM установить доверенный `age`; сгенерировать age PRIVATE
  identity **вне VM**, сохранить в двух независимых защищённых местах,
  передать на VM **только публичный recipient**.
- [ ] Root-owned: `/usr/local/sbin/vimi-pay-gcs-backup` (0750),
  `/usr/local/libexec/vimi-pay/session_upload.py` (0640),
  `/etc/vimi-pay/backup.env` (0600), systemd units. Предварительно
  `bash -n`, `python3 -m py_compile`, `systemd-analyze verify`.
- [ ] Запустить **один** ручной тест: `sudo systemctl start
  vimi-pay-gcs-backup.service`. Убедиться в `BACKUP_UPLOADED`,
  не публикуя токены, приватные ключи и сессионные URI.
- [ ] Отдельным recovery-аккаунтом с правом **только чтения** получить
  все три объекта завершённого prefix, проверить SHA-256, расшифровать
  и **восстановить БД и roles в изолированной среде**.
- [ ] Только после подтверждения восстановимости включить
  `sudo systemctl enable --now vimi-pay-gcs-backup.timer`,
  alert по отсутствию успешных копий >26 часов, записать RTO/RPO.
- [ ] Проверить доступность отдельных disk snapshots GCE и
  восстановление конфигурации VM и ключей.

**Запрещено:** переносить приватный age key или SA JSON key на VM,
давать `roles/storage.objectAdmin` на bucket, менять OAuth scopes
`fast-vm`, включать Cloud Run `allUsers` или выкладывать
resumable upload session URI в логи.

## Аварийное восстановление: общий порядок

1. Изолировать новый PostgreSQL от production Telegram-ботов. **Не запускать одновременно polling двух копий ботов на одном токене**.
2. Независимым recovery identity с `storage.objects.get` скачать ровно один complete prefix (где есть `manifest.sha256`) на доверенную восстановительную машину.
3. `sha256sum -c manifest.sha256` на двух зашифрованных age-объектах. Затем расшифровать **офлайн** с `age -d -i /защищённый/путь/identity` в изолированное временное хранилище, недоступное другим пользователям.
4. В **новом** PostgreSQL 16 восстановить кластерные роли через `psql` от superuser; роли могут конфликтовать с bootstrap superuser, подготовить кластер без повторных существующих ролей. Отдельно создать `membership`, восстановить `pg_restore --exit-on-error` (применять role/ownership только в изолированном окружении).
5. Проверить реальные RLS, миграционный head, все финансовые таблицы, idempotency, count, cron/outbox, Telegram secrets. Только после проверки и отдельного одобрения маршрутизировать клиентский трафик.
6. Зафиксировать время создания последнего успешного бэкапа и измеренное время восстановления. Ни статус `BACKUP_UPLOADED`, ни проверка checksum **не доказывают**, что можно развернуть production; проверяет только restore drill.

## Диагностическая проверка GCP без секретов

Оператор может проверить инструменты и metadata через:

```bash
command -v gcloud || true
command -v age || true
curl -fsS -H 'Metadata-Flavor: Google' \
  --connect-timeout 2 --max-time 4 \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email' || true
curl -fsS -H 'Metadata-Flavor: Google' \
  --connect-timeout 2 --max-time 4 \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/scopes' || true
```

**Никогда** не запрашивать и не показывать `.../token`, `identity`, приватные `*.key`, `.env`, docker environment или credentials JSON.

## Риски и ограничения

Доступность Telegram Bot API не гарантируется от бэкапов. `pg_dump` во время работы допускает запись приложения, но это снимок БД на конкретный момент, не point-in-time recovery. При одном бэкапе в сутки теоретический RPO составляет до ~24 часов плюс задержка; для финансовой платформы в будущем следует оценить архивирование WAL / PITR. Целевые RTO/RPO считаются **не достигнутыми**, пока не проверены развертывание roles, секретов, VM, доступ к платежным источникам и наблюдаемость.
