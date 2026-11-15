# ViMi Pay — защищённые резервные копии и восстановление

> **Статус:** план и opt-in implementation, НЕ включены на VM; отдельные ключи, GCS bucket и systemd timer ещё не подтверждены. Никаких автоматических изменений staging.

## Что проверено оператором 15 ноября 2026

На VM `fast-vm` PostgreSQL ViMi Pay расположен в отдельном named Docker volume `telegram-membership-saas-staging_postgres_data`, PG 16.15. Измерено 86 ГБ свободного места, backup timers/cron для проекта в стандартных местах не обнаружены; `dpkg-db-backup.timer` не является бэкапом PostgreSQL. **Это не исключает snapshots на уровне GCE**.

Создан ручной архив `/var/backups/vimi-pay/membership-20261115T155206Z.dump` (~4,9 МиБ): `pg_restore` прочитал архив, SHA-256 совпала. **Успешно выполнено полноценное восстановление в временном контейнере** `postgres:16-alpine --network none`: 51 ботов, 24 финансовых счёта, 743 пользователя; журнал завершился `RESTORE_OK`. Временная база удалена; боевые контейнеры и БД не изменились. Это проверка **базовой базы**, не полного восстановления VM, ключей или внешних API.

## Чего нет в обычном `pg_dump`

- Кластерные роли, grants и настройки PostgreSQL: для них необходим отдельно `pg_dumpall --globals-only`.
- Ключи `TMS_BOT_TOKEN_ENCRYPTION_KEY`, токены самого центрального бота, секреты провайдеров и конфигурация VM/Nginx: хранить **отдельно** в защищённом менеджере секретов/офлайн-хранилище. Нельзя класть секреты в git и в обычный архив; шифрование поля токена в БД **без ключа** не позволяет восстановить запуск.
- Files/media, хранящиеся вне базы, включая внешние Telegram file_id / объектное хранилище: провести инвентаризацию отдельно.
- Google Compute Engine persistent disk snapshots; GCS-архив **не заменяет** снимки системы и конфигурации.

## Целевое решение: age + GCS

Файлы в `ops/backup/` — **отключённая по умолчанию** подготовка автоматической выгрузки:

1. Root-only `/etc/vimi-pay/backup.env`, `chmod 0600`, указывает имя отдельного bucket, **публичный** recipient `age1...`, имя контейнера PostgreSQL; секретной половины age на VM нет.
2. `vimi-pay-gcs-backup.sh` захватывает `flock`, потоково шифрует `pg_dump -Fc -Z 6` и `pg_dumpall --globals-only`. Незашифрованные дампы **не записываются на диск**.
3. Сервисный аккаунт VM с `roles/storage.objectCreator` **на отдельном bucket**, без objectAdmin/admin/delete. На VM требуется `gcloud` (для короткоживущего GCE OAuth bearer token), `curl` и корректные OAuth access scopes; изменить прикреплённый сервисный аккаунт без предварительной проверки других ViMi сервисов нельзя.
4. Каждый запуск создаёт уникальный prefix `gs://BUCKET/vimi-pay/postgres/UTC-UUID/` и грузит два age-архива. Файл `manifest.sha256` выгружается **последним**: только prefix с manifest считается завершённым.
5. Для сохранения **write-only** IAM вместо `gcloud storage cp` в object path используется прямой JSON API `objects.insert` c точно закодированным именем и `ifGenerationMatch=0`. Это защищает от перезаписи и не требует `storage.objects.list`. Короткоживущий токен не выводится в лог или argv. Bucket-level retention и soft-delete настраиваются отдельно. **Нельзя включать необратимый Bucket Lock** без отдельного осознанного разрешения.
6. Root-owned `vimi-pay-gcs-backup.timer`: ежедневно 03:20 UTC + случайные 0–20 минут, `Persistent=true`. Юнит не устанавливается, не включается и не запускается при merge PR.

**Запрещено:** использовать пользовательский Google Cloud credential JSON в репозитории, хранить age private identity на VM с бэкапом, давать скрипту `storage.objectAdmin`, публиковать `pg_dumpall` в логах, использовать GCS bucket без проверенной политики доступа и хранения.

## Пререквизиты перед включением (только оператор)

- [ ] Определить фактический GCP project ID, текущий прикреплённый VM service account, scopes и текущий набор IAM прав. Не менять его до аудита ViMi Autoposting.
- [ ] Создать отдельный закрытый Cloud Storage bucket в выбранном регионе, выключить публичный доступ, включить uniform bucket-level access; утвердить retention и soft-delete. Для этого сценария **не требуется Object Versioning**: каждый бэкап имеет уникальные имена и не перезаписывается. Рекомендуемый стартовый baseline: защита от удаления 30 дней, Lifecycle Delete через 90 дней, soft delete 7 дней, без versioning. Проверить стоимость и правила хранения данных до включения.
- [ ] **Обязательная lifecycle-cleanup политика:** на выделенный bucket ввести `ops/backup/gcs-lifecycle-90days.json` (пример, удаляет все объекты после возраста 90 дней, включая незавершённые префиксы без manifest). Консоль GCS → bucket → Lifecycle → Add rule → Delete → Age 90 days; либо администратор отдельно применяет `gcloud storage buckets update gs://BUCKET --lifecycle-file=ops/backup/gcs-lifecycle-90days.json`. Команда **заменяет** текущие правила bucket, поэтому применять её только после проверки, что bucket выделенный и пустой, и после подтверждения владельца. Нельзя включать таймер без этой проверки. Retention не должен превышать период планового удаления; 30-дневная retention совместима с 90-дневной очисткой, а soft-delete может добавить дни фактического хранения.
- [ ] Выдать сервисному аккаунту **только** `roles/storage.objectCreator` на bucket (с учётом scopes).
- [ ] Сгенерировать ключи age **на доверенной офлайн-машине** (`age-keygen -o /secure/offline/identity`; `age-keygen -y /secure/offline/identity` выдаёт только публичный recipient). Сохранить private identity отдельно минимум в двух защищённых местах; на VM перенести только `age1...` recipient. Один private identity на десятилетия не следует оставлять без процедуры ротации.
- [ ] Установить `gcloud`, `age` и `curl` на VM из доверенных источников (проверить версии), убедиться что `docker`, `flock`, `sha256sum` доступны.
- [ ] Разместить скрипт в `/usr/local/sbin/vimi-pay-gcs-backup` (root:root, 0750), config `/etc/vimi-pay/backup.env` (root:root, 0600) и два unit-файла в `/etc/systemd/system/`. Все shell/units сперва `bash -n` / `systemd-analyze verify`.
- [ ] **Один ручной тест** `sudo systemctl start vimi-pay-gcs-backup.service`, проверить `journalctl -u ...` на `BACKUP_UPLOADED`. Отдельным restore-аккаунтом (read only) скачать 3 объекта, проверить SHA-256 и расшифровать age в изолированном окружении, выполнить реальное восстановление и проверить содержимое.
- [ ] Только после успешного восстановления **и проверки действующего lifecycle Delete ≥90 days**: `sudo systemctl enable --now vimi-pay-gcs-backup.timer`, настроить alert если не было `BACKUP_UPLOADED` в последние 26 часов; документировать RPO/RTO.
- [ ] Подтвердить отдельные snapshots Compute Engine для VM и данных; настроить расписание и тест восстановления системы.

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
