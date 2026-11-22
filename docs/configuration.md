# Переменные окружения

Сверено со всеми полями `src/cu_clubs/config.py` и `.env.example`,
22 ноября 2026. Префикс приложения — `CU_CLUBS_`; Pydantic читает окружение
и `.env`, пустые значения игнорируются. Таблица описывает **defaults кода**,
не значения или секреты работающего сервера. [Live-настройки](handover.md).

Локальный `.env.example` содержит каждое поле Settings. Не заменяйте им
серверный `.env`. `POSTGRES_USER/PASSWORD/DB` в конце примера — переменные
Compose, не дополнительные поля Settings. На сервере Compose собирает URL
из POSTGRES_PASSWORD; не выводите итоговый `docker compose config` в общий отчёт.

| Переменная | Тип | Default кода |
|---|---|---|
| `CU_CLUBS_ENVIRONMENT` | `Literal['local', 'test', 'production']` | 'local' |
| `CU_CLUBS_HOST` | `str` | '127.0.0.1' |
| `CU_CLUBS_PORT` | `int` | 8000 |
| `CU_CLUBS_DATABASE_URL` | `SecretStr` | локальный PostgreSQL URL из .env.example |
| `CU_CLUBS_SELF_SERVICE_POOLER` | `bool` | false |
| `CU_CLUBS_READINESS_TIMEOUT_SECONDS` | `float` | 3.0 |
| `CU_CLUBS_TIMEZONE` | `str` | 'Europe/Moscow' |
| `CU_CLUBS_BOOTSTRAP_ADMIN_IDS` | `list[PositiveInt]` | [] |
| `CU_CLUBS_BOOTSTRAP_LEADER_TELEGRAM_ID` | `PositiveInt \| None` | не задано |
| `CU_CLUBS_TEST_ALLOW_UNVERIFIED_LEADERS` | `bool` | false |
| `CU_CLUBS_TEST_SELF_REGISTRATION_ENABLED` | `bool` | false |
| `CU_CLUBS_YANDEX_AUTH_ENABLED` | `bool` | false |
| `CU_CLUBS_YANDEX_CLIENT_ID` | `str \| None` | не задано |
| `CU_CLUBS_YANDEX_CLIENT_SECRET` | `SecretStr \| None` | не задано |
| `CU_CLUBS_YANDEX_REDIRECT_URI` | `str \| None` | не задано |
| `CU_CLUBS_EXTERNAL_DELIVERY_ENABLED` | `bool` | false |
| `CU_CLUBS_TELEGRAM_BOT_TOKEN` | `SecretStr \| None` | не задано |
| `CU_CLUBS_TELEGRAM_WEBHOOK_SECRET` | `SecretStr \| None` | не задано |
| `CU_CLUBS_TELEGRAM_BOT_USERNAME` | `str \| None` | не задано |
| `CU_CLUBS_TELEGRAM_WEBHOOK_URL` | `str \| None` | не задано |
| `CU_CLUBS_TELEGRAM_TRANSPORT` | `Literal['webhook', 'polling']` | 'webhook' |
| `CU_CLUBS_SMTP_HOST` | `str \| None` | не задано |
| `CU_CLUBS_SMTP_PORT` | `int` | 587 |
| `CU_CLUBS_SMTP_USERNAME` | `str \| None` | не задано |
| `CU_CLUBS_SMTP_PASSWORD` | `SecretStr \| None` | не задано |
| `CU_CLUBS_SMTP_SENDER` | `str \| None` | не задано |
| `CU_CLUBS_SMTP_USE_SSL` | `bool` | false |
| `CU_CLUBS_SMTP_STARTTLS` | `bool` | true |
| `CU_CLUBS_VERIFICATION_SECRET` | `SecretStr \| None` | не задано |
| `CU_CLUBS_GOOGLE_AUTH_MODE` | `Literal['oauth', 'service_account']` | 'oauth' |
| `CU_CLUBS_GOOGLE_CREDENTIALS_PATH` | `Path \| None` | не задано |
| `CU_CLUBS_GOOGLE_SPREADSHEET_ID` | `str \| None` | не задано |
| `CU_CLUBS_GOOGLE_SHEET_NAME` | `str` | 'Calendar 2026' |
| `CU_CLUBS_CALENDAR_SYNC_ENABLED` | `bool` | false |
| `CU_CLUBS_CALENDAR_ALLOW_WRITES` | `bool` | false |
| `CU_CLUBS_CALENDAR_TEST_SPREADSHEET_ID` | `str \| None` | не задано |
| `CU_CLUBS_CALENDAR_SYNC_INTERVAL_SECONDS` | `int` | 60 |
| `CU_CLUBS_CALENDAR_SYNC_BATCH_SIZE` | `int` | 10 |
| `CU_CLUBS_CALENDAR_DIGEST_COLOR` | `str` | '#FF00FF' |
| `CU_CLUBS_API_ENABLED` | `bool` | true |
| `CU_CLUBS_API_RATE_LIMIT_PER_MINUTE` | `int` | 120 |
| `CU_CLUBS_API_GRANT_TTL_DAYS` | `int` | 90 |
| `CU_CLUBS_BACKUP_RECIPIENT_TELEGRAM_ID` | `PositiveInt \| None` | не задано |
| `CU_CLUBS_BACKUP_CRON` | `str` | '0 3 * * *' |
| `CU_CLUBS_BACKUP_DIRECTORY` | `Path` | Path('backups') |
| `CU_CLUBS_BACKUPS_ENABLED` | `bool` | false |

## Условия включения

- `ENVIRONMENT`: local/test/production. `TEST_SELF_REGISTRATION_ENABLED` и
  `TEST_ALLOW_UNVERIFIED_LEADERS` разрешены только в test; выбор категории
  и назначение лидера не подтверждают университетскую личность.
- `YANDEX_AUTH_ENABLED`: нужны client ID, secret, verification secret длиной
  от 32 символов, bot username и HTTPS callback с точным путём
  `/auth/yandex/callback`, без query/fragment/username. Приложение запрашивает
  только login:email. [Порядок настройки](yandex-login.md).
- `EXTERNAL_DELIVERY_ENABLED`: нужны токен выбранного Telegram-бота, его username
  и webhook secret от 32 символов даже при polling. Worker сверяет токен
  с username. Само включение флага не настраивает webhook и не выдаёт роли.
- `VERIFICATION_SECRET`: сохранять при релизе и восстановлении. Это не
  webhook secret; замена влияет на шифрованный inbox и подписанные проверки.
- Sheets: для записи нужны enable + allow_writes, test environment и точное
  совпадение target ID с разрешённым test ID. JSON credentials хранятся вне Git.
  [Календарный контракт](calendar-contract.md).
- `CALENDAR_DIGEST_COLOR` объявлен и валидируется, но текущий адаптер
  использует фиксированный `#FF00FF`; смена переменной пока не меняет поведение.
- `BACKUPS_ENABLED`: нужны включённая внешняя доставка и numeric получатель.
  `BACKUP_CRON` поддерживает только ежедневное `minute hour * * *`, время
  интерпретируется в TIMEZONE. `backup --force` не является CLI-опцией;
  команда `cu-clubs backup` сама вызывает force, сохраняя дневную дедупликацию.
- SMTP включается заполнением параметров отправителя, а не отдельным флагом.
  Локальное наличие кода не подтверждает работающий почтовый транспорт.
- `SELF_SERVICE_POOLER` нужен отдельному transaction-pooler окружению. В
  текущей установке false; не включать как способ «починить» обычную БД.

## Что не хранить в документации

Значения Telegram/Yandex/SMTP ключей, verification secret, рабочий database URL,
Google JSON/refresh token и runtime-env.json передаются отдельным защищённым
каналом. В .env.example и этой таблице рабочих значений нет. Числовые admin IDs,
получатель копий и ID таблицы должны быть подтверждены владельцем при передаче.
