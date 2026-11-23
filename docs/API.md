# HTTP API

На одном origin с Nuxt используется `/api/v1`. При прямой интеграции, например локальной, базовый адрес — `http://127.0.0.1:8000/api/v1`. Источник контракта — `/openapi.json` FastAPI, копия версионируется в корне backend. Учётная запись определяет УК, роль и адрес. Браузерный клиент размещайте на одном origin. CORS для сторонних сайтов выключен.

```sh
base=http://127.0.0.1:8000/api/v1
# login.json содержит username и password. cookie.jar содержит секрет сессии.
umask 077
curl -fsS -c cookie.jar -H 'X-Requested-With: Nebula' \
  -H 'Content-Type: application/json' --data-binary @login.json "$base/session/login"
# Сохраните csrfToken из ответа в переменную csrf или запросите GET /session с cookie.
curl -fsS -b cookie.jar -H "X-CSRF-Token: $csrf" -F 'file=@roof.png' "$base/files"
curl -fsS -b cookie.jar -H "X-CSRF-Token: $csrf" -H 'Idempotency-Key: stable-client-key' \
  -H 'Content-Type: application/json' --data-binary @request.json "$base/requests"
curl -fsS -b cookie.jar -o roof.png "$base/files/$file_id/download"
curl -fsS -b cookie.jar -o package.zip "$base/requests/$request_id/package/zip"
curl -fsS -b cookie.jar -X DELETE -H "X-CSRF-Token: $csrf" "$base/session"
```

Cookie `nebula_session` выдаётся на 12 часов с `HttpOnly` и `SameSite=Strict`. В HTTPS используйте `SECURE_COOKIES=true`. Все изменяющие вызовы с сессией требуют `X-CSRF-Token`. Логин требует `X-Requested-With: Nebula`. При истечении сессии повторите вход. Создание заявки требует заголовок `Idempotency-Key`, при повторах используйте те же идентификаторы уже загруженных фото и то же тело.

Ошибки возвращают `application/problem+json` с `status`, `code`, `title` и, для ошибок полей, массивом `errors`. 400 — неверные данные, 401 — сессия или вход, 403 — роль или CSRF, 404 — объект вне области доступа, 409 — устаревшая версия или текущий этап, 429 — ограничение входа. Запросы записи ТЗ и КП передают `baseVersion`, отправка на проверку — `version`, решение — `subjectVersion`. Опрос выполняется через GET заявки и соответствующего шага.

Отличия от демо: реальные cookie-сессии, `/files`, `/contractors`, `/requests/{id}/package/zip`, ручное подтверждение КП и фактическая генерация офисных файлов. Эндпоинты `/dev` удалены. Отклонённая версия остаётся снимком в журнале. Для повторного согласования сохраните новую версию или пересчитайте НМЦ.
