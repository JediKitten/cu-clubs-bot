# API клубных ботов v1

Обновлено 15 ноября 2026, схема до `0012`.
Начни с [подробной инструкции лидера и разработчика](api-guide-for-club-leaders.md):
получение ключа, адрес, запуск клиента, привязка студента, встречи, импорт,
webhooks и ошибки. Здесь — краткий контракт и точный перечень маршрутов.

Префикс `/api/v1`, JSON UTF-8, даты ISO 8601 с часовым поясом. Схема запросов,
ответов и обязательных заголовков: `/openapi.json`, интерактивно `/docs`.
Запускаемый пример: [club_api_client.py](../examples/club_api_client.py).

## Ключ и согласие студента

Лидер открывает «Мои клубы → клуб → Настройки клуба → Интеграции → Создать ключ».
Ключ выдаётся отдельным файлом; API хранит только хеш. Передавать
`Authorization: Bearer cck_…`. Scopes: events:read, events:write,
registration:self, attendance:write, roster:read, changes:read.
Ключ ограничен своим клубом и принятыми коллаборациями, даже если выдавший его
администратор имеет права на другие клубы. Отзыв ключа и роли выдавшего его
лидера проверяется при каждом запросе. Личные записи лидера не раскрываются.

`POST /links` с `telegram_id` возвращает id, verification_uri, exchange_code,
expires_at. Человек открывает ссылку, подтверждает почту при необходимости и
**отдельно** разрешает подключение. ID и ключ клуба сами по себе не дают согласия.
В явно включённом тестовом режиме вместо почты достаточно выбора категории
студент/сотрудник **и заполнения ФИО с логином в Тайме**; абитуриент/внешний остаётся гостевым QR-путём. Самовыбор не
изменяет `verified_at`. `GET /me` возвращает фактический `verified` и дополнительный
`declared_category` (student/staff/applicant/external/null), `time_login` может быть null.
`POST /links/{id}/exchange` с exchange_code выдаёт user_token на 90 дней по умолчанию.
Передавать его в `X-User-Token` вместе с ключом клуба. Код одноразовый, привязан
к клиенту и человеку, истекает через 10 минут. Привязка отзывается через
«Личный кабинет → Подключённые боты»; сама верификация никого не регистрирует.

## Ресурсы

| Операции | Scope |
|---|---|
| GET /clubs/{id}, /events?from=…&to=…&club_id=…, /events/{id} | events:read |
| POST /events; PATCH /events/{id}; POST /events/{id}/publish, /invite, /cancel | events:write; If-Match для существующей встречи |
| POST /links, /links/{id}/exchange; GET /me | registration:self; /me требует X-User-Token |
| PUT/DELETE /events/{id}/registrations/me; GET /events/{id}/participants/me | registration:self + X-User-Token |
| GET /events/{id}/participants?format=json\|xlsx&all_participants=true | roster:read |
| POST /events/{id}/attendance | attendance:write; ФИО/логин/необязательный тег; только студент |
| POST /events/{id}/attendance-imports | attendance:write; multipart поле file, .xlsx ≤10MB; только предпросмотр |
| POST /attendance-imports/{id}/resolve, /confirm | attendance:write; текущая version; resolve: номера rows для явного принятия конфликтов |
| GET /changes?cursor=…&limit=… | changes:read; порядок commit, только свой клуб |
| GET/POST /webhook-endpoints; DELETE /webhook-endpoints/{id} | changes:read |
| GET /webhook-deliveries; POST /webhook-deliveries/{id}/retry | changes:read; ручной повтор с прежним delivery_id |

Каждая изменяющая операция требует Idempotency-Key (1–128 печатных ASCII).
Повтор того же запроса возвращает прежний ответ, другое тело/пользователь/If-Match
с тем же ключом — 409. Кэш с секретами зашифрован, не хранит их открытым текстом.
Текущая версия встречи возвращается в ETag; передавать `If-Match: "3"`.
Старая версия — 412, отсутствие If-Match — 428. Ошибки содержат error.code,
error.message, при валидации error.field, а также request_id. 401 — ключ/токен,
403 — права/scope, 404 — недоступный объект, 409 — конфликт/места/закрытая запись,
422 — данные, 429 — лимит (120/мин по умолчанию, заголовок Retry-After).
JSON ограничен 256KB, multipart 10MB плюс небольшая служебная часть.

## Подписанные webhooks

Только публичный HTTPS:443, без credentials/redirects. Все DNS-адреса проверяются,
соединение закрепляется за проверенным IP с TLS/SNI исходного имени. Private,
loopback, служебные адреса и DNS rebinding не дают доступ к внутренней сети.
Секрет подписи возвращается при создании и его идемпотентных повторах; далее
API его не раскрывает, фоновой доставке он доступен в зашифрованном виде.

Типы: event.published/updated/cancelled, registration.changed, attendance.changed.
Тело содержит delivery_id, id изменения, type, event_id, version, occurred_at.
Имена людей, почта, roster и ключи в feed не попадают.
HMAC-SHA256 от `timestamp + "." + raw_body` находится в
`X-CU-Clubs-Signature: v1=<hex>`, timestamp — X-CU-Clubs-Timestamp,
ID — X-CU-Clubs-Delivery. Проверять исходные байты, давность ≤300 секунд и
constant-time сравнение; verifier есть в примере клиента. Дедуплицировать ID:
timeout может последовать уже после принятия сообщения. Повторы используют
тот же ID; физически однократная доставка не обещается. Пропуски восстанавливать
через /changes, ошибки и ручной повтор доступны через delivery API.

## Тестовый сервер

API слушает **127.0.0.1:8310 на сервере**. Серверным клубным ботам доступен этот
адрес; для внешних клиентов нужен отдельный HTTPS reverse proxy. Сейчас внешний
TLS-маршрут не настроен и порт не открыт на все интерфейсы. Для проверки:
`ssh -L 8310:127.0.0.1:8310 kir@87.120.84.226`, затем localhost:8310.
Чужие reverse proxy routes при выпуске не меняются.
SMTP отложен: нового студента нельзя фиктивно подтвердить ради API. Исключение
тестового лидера не подтверждает студенческую почту. Временный отдельный флаг
`CU_CLUBS_TEST_SELF_REGISTRATION_ENABLED=true` разрешён только при environment=test
и допускает заявленные студент/сотрудник к явной привязке без отметки верификации.
После выключения флага возобновляются строгие проверки даже для ранее выданных токенов.
Перенос в self-service cu3rd начат. Запрошен `https://clubs-api.cu3rd.ru`, но до приёмки DNS, сертификата и восстановления базы этот адрес не считается доступным. Состояние — в [инструкции переноса](self-service-migration.md).
При восстановлении сохранять
тот же VERIFICATION_SECRET для encrypted inbox, idempotency и webhook secrets.

## Точный перечень маршрутов

Секция ниже проверяется CI по декораторам API. После изменения маршрутов:
`python scripts/check_api_docs.py --update`, затем смысловая сверка обеих
инструкций и примера клиента. Эта проверка не заменяет сверку полей и прав.

<!-- api-routes:start -->
| Метод | Путь |
|---|---|
| `POST` | `/api/v1/attendance-imports/{batch_id}/confirm` |
| `POST` | `/api/v1/attendance-imports/{batch_id}/resolve` |
| `GET` | `/api/v1/changes` |
| `GET` | `/api/v1/clubs/{club_id}` |
| `GET` | `/api/v1/events` |
| `POST` | `/api/v1/events` |
| `GET` | `/api/v1/events/{public_id}` |
| `PATCH` | `/api/v1/events/{public_id}` |
| `POST` | `/api/v1/events/{public_id}/attendance` |
| `POST` | `/api/v1/events/{public_id}/attendance-imports` |
| `POST` | `/api/v1/events/{public_id}/cancel` |
| `POST` | `/api/v1/events/{public_id}/invite` |
| `GET` | `/api/v1/events/{public_id}/participants` |
| `GET` | `/api/v1/events/{public_id}/participants/me` |
| `POST` | `/api/v1/events/{public_id}/publish` |
| `DELETE` | `/api/v1/events/{public_id}/registrations/me` |
| `PUT` | `/api/v1/events/{public_id}/registrations/me` |
| `POST` | `/api/v1/links` |
| `POST` | `/api/v1/links/{link_id}/exchange` |
| `GET` | `/api/v1/me` |
| `GET` | `/api/v1/webhook-deliveries` |
| `POST` | `/api/v1/webhook-deliveries/{delivery_id}/retry` |
| `GET` | `/api/v1/webhook-endpoints` |
| `POST` | `/api/v1/webhook-endpoints` |
| `DELETE` | `/api/v1/webhook-endpoints/{endpoint_id}` |
<!-- api-routes:end -->
