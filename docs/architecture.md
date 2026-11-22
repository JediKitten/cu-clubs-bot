# Архитектура и карта кода

Сверено с кодом `b6d4531`, 22 ноября 2026. Состояние процессов и внешних
доступов — в [передаче](handover.md), продуктовые границы — в [статусе](project-status.md).

## Процессы и хранение

Python 3.12, FastAPI, aiogram 3, SQLAlchemy async + psycopg, PostgreSQL 17,
Alembic; зависимости зафиксированы в `uv.lock`. Redis не требуется.
API и worker используют одну БД. Внешний транспорт действующего бота — polling.
API отдельно обслуживает HTTP, включая Яндекс callback и клубный API.

Telegram updates сначала сохраняются в `telegram_updates` с уникальным
update_id и зашифрованным payload, затем обрабатываются worker. Conversation
и текущий UIScreen сохраняются в БД. Per-chat блокировки защищают обновление
экрана. Новое меню после копии определяется по доставленному `backup.ready`
с message_id больше текущего экрана; документ копии не включается в очистку.

Outbox хранит задания отправки. Захват использует lease и `SKIP LOCKED`;
истёкшая lease исходящей отправки переводится в `uncertain`, чтобы не повторять
возможную уже состоявшуюся доставку. Это не гарантия exactly-once Telegram.
Уведомления/опросы могут ждать завершения активного диалога. В текущей установке
один worker; масштабирование нескольких worker требует отдельной проверки.

Worker также обслуживает календарь, приглашения опросов, уведомления, API
webhooks и резервные копии. Heartbeat — `/tmp/cu-clubs-worker-heartbeat`.
Readiness API проверяет БД и не подтверждает SMTP, Google или Яндекс.

## Где менять поведение

Пути в таблице относительно `src/cu_clubs/`.

| Область | Основные модули |
|---|---|
| Запуск/настройки/HTTP | `__main__.py`, `config.py`, `app.py`, `db.py` |
| Telegram и экран | `telegram.py`, `screens.py`, `ui_common.py`, `worker.py` |
| Клубы, роли, карточка | `services.py`, `access.py`, `club_card_ui.py`, `club_edit_ui.py` |
| Идентичность и слияние | `identity.py`, `identity_merge.py`, `identity_ui.py`, `category_ui.py` |
| Яндекс | `yandex_auth.py`, `yandex_routes.py`, `yandex_ui.py`, `logging_privacy.py` |
| Встречи/шаблоны | `events.py`, `event_ui.py`, `event_schedule_ui.py`, `template_ui.py` |
| Личный календарь/ICS | `user_calendar.py`, `calendar_user_ui.py` |
| Google Sheets | `calendar.py`, `calendar_ui.py`, `integrations/sheets.py`, `integrations/google_oauth.py` |
| Посещение/XLSX | `attendance.py`, `attendance_ui.py`, `attendance_xlsx.py`, `telegram_files.py` |
| Опросы/аналитика | `surveys.py`, `survey_ui.py`, `analytics.py`, `analytics_ui.py`, `periods.py` |
| API и доставка | `api.py`, `api_service.py`, `api_security.py`, `api_delivery.py`, `api_ui.py` |
| Уведомления/дайджест | `notifications.py`, `notifications_ui.py`, `digest.py`, `digest_ui.py` |
| FAQ и media | `faq.py`, `faq_ui.py`, `faq_resources/ru.json`, `media.py`, `media/resources/manifest.json` |
| Почта/криптография | `integrations/mail.py`, `inbox_crypto.py` |

Бизнес-правила общие для Telegram/API: перенос проверки доступа только в UI
оставляет второй канал без защиты. Публичная команда клуба не даёт прав
организатора. Отметка посещения/импорт XLSX не подтверждает почту.

## Данные и миграции

Модели разделены на `models.py`, `identity_models.py`, `event_models.py`,
`calendar_models.py`, `attendance_models.py`, `survey_models.py`,
`notification_models.py`, `api_models.py`. Источник схемы — `migrations/versions/`;
текущий head и сервер — `20261116_0014` (Яндекс identity).

Основные группы: people/telegram_accounts, clubs/memberships/public_team,
события и регистрации, посещения, анкеты/ответы, calendar_bindings,
API clients/links/grants/idempotency/changes/webhooks, conversations/ui_screens,
telegram_updates/outbox/audit_log. Детальные поля сверяйте с моделями и миграциями.
Миграции выполняет `cu-clubs migrate`, не запуск API. `bootstrap` — отдельная
запись исходных ролей/клуба, не команда проверки состояния.

## Интеграции и границы

Яндекс использует одноразовые state/challenge, cookie, PKCE S256 и отдельное
подтверждение привязки в Telegram. Принимается только университетский default_email
и ожидаемый client_id. Токены провайдера не сохраняются. Подробности в
[yandex-login.md](yandex-login.md).

Для API используется отдельный ключ клиента и отдельное согласие пользователя;
Telegram ID сам по себе не заменяет это согласие. Есть scopes, отзыв,
лимиты, optimistic versions и идемпотентность. Реальный контракт и примеры —
[api-integration.md](api-integration.md), [руководство лидеров](api-guide-for-club-leaders.md).

Google credentials сохраняются в отдельном volume; обновление credentials не
равно замене БД. Текущий календарь — разрешённая тестовая копия. Figma-плагин
находится в `figma/digest/`, принимает экспортированный JSON и не является
серверным процессом или автоматической публикацией.

## Проверки и разработка

`tests/` покрывает домен, Telegram, интеграции, API и восстановление polling.
Интеграционные фикстуры требуют отдельную `TEST_DATABASE_URL`; без неё часть
набора пропускается. Они не предназначены для рабочей БД.
Документационный inventory проверяет маршруты `api.py`, не семантику прав.
Подходящие проверки выбираются по изменению; полный набор не нужно повторять
для правки текста. Правила совместимости и независимых review — в `AGENTS.md`.
