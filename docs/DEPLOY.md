# Деплой пилота

На Linux-сервере установите Docker Engine с Compose v2, создайте отдельный каталог с `compose.yaml`, `deploy/Caddyfile` и `.env`. В `.env` укажите фиксированные теги или OCI-дайджесты `BACKEND_IMAGE` и `FRONTEND_IMAGE`. Дайте оператору доступ к этим образам через реестр или передайте их с помощью `docker save` и `docker load`.

Укажите случайный пароль PostgreSQL из hex-символов, так как он входит в URL подключения. Установите для `.env` права 0600. Настройте DNS на адрес сервера, откройте TCP 80 и 443 и задайте `SITE_ADDRESS=procurement.example.org`, `HTTP_BIND=0.0.0.0:80`, `HTTPS_BIND=0.0.0.0:443`, `SECURE_COOKIES=true`. Caddy получит HTTPS-сертификат и сохранит его в томе. Порт БД остаётся во внутренней сети Compose. Префикс `/api/` передаётся в FastAPI целиком.

Запустите `docker compose up -d --wait`. Alembic выполняет миграции перед запуском API. Проверьте `docker compose ps`, `https://<домен>/healthz` и вход в интерфейс. Создайте УК, аккаунты и каталог подрядчиков командами из README. Для llama.cpp проверьте маршрут с сервера деплоя до `172.16.34.8:8080`. Все параметры модели задаются только контейнеру backend.

## Обновление и резервная копия

Остановите запись перед копированием: `docker compose stop backend`. Создайте dump и tar в одном окне обслуживания. Используйте каталог с ограниченными правами и храните материалы зашифрованными.

```sh
umask 077
docker compose exec -T postgres pg_dump -U nebula -Fc nebula > nebula.dump
docker compose run --rm -T --no-deps --entrypoint tar backend -C /data/files -cf - . > files.tar
docker compose start backend
```

Зафиксируйте старые ссылки на оба образа, версию миграции и соответствующую пару копий. Загрузите или скачайте оба новых образа, обновите ссылки в `.env`, выполните `docker compose up -d --wait`, проверьте состояние и вход. Для новых схем предварительно проверьте миграции на копии данных.

Для восстановления остановите backend и используйте чистые тома/новый Compose-проект с исходной парой образов. Сначала запустите postgres, восстановите dump командой `docker compose exec -T postgres pg_restore --exit-on-error -U nebula -d nebula < nebula.dump`. Восстановите файлы как пользователь приложения командой `docker compose run --rm -T --no-deps --entrypoint tar backend -C /data/files -xf - < files.tar`. Запустите весь проект и проверьте скачивание. При миграциях, меняющих данные, откатывайте оба образа вместе с согласованными копиями БД и файлов. Данные, записанные после копии, потребуют отдельной сверки.

## Журналы

FastAPI пишет структурированные JSON-события в stdout. `LOG_LEVEL` в окружении Compose по умолчанию `INFO`. Доступны также `DEBUG`, `WARNING`, `ERROR` и `CRITICAL`. Успешные проверки `/healthz` видны на `DEBUG`. Из ответа API возьмите заголовок `X-Request-ID` для поиска соответствующего HTTP-события. Фоновые операции ИИ можно искать по `job_id` и `procurement_id`.

```sh
docker compose logs -f --tail=100 backend
docker compose logs --since=1h backend | rg 'http.request|ai.job_result|approval.decided'
```

В журналы приложения попадают шаблоны маршрутов, коды ошибок, длительность вызовов, результаты заданий и доступные счётчики токенов. Тела запросов и ответов, параметры запроса, тексты исключений и учётные данные в события не записываются. Docker хранит до трёх файлов по 10 МиБ для каждого контейнера стека.

Для модели доступны события `ai.provider_started` (уровень DEBUG), `ai.provider_completed`, `ai.provider_failed` и `ai.validation_failed`. В ответе модели видны служебный идентификатор провайдера, причина завершения, флаг отказа, задержка и входные/выходные токены. Для ошибки SDK записываются категория и HTTP-статус, а для ошибки схемы — типы ошибок валидации. Для внутренних исключений журнал показывает файлы, строки и функции без вывода значений переменных. События `workflow.waiting`, `workflow.scope_changed` и `workflow.package_ready` объясняют, где остановилась заявка и какую версию пакета создала система.

```sh
LOG_LEVEL=DEBUG docker compose up -d --no-deps backend
docker compose logs -f --tail=150 backend
docker compose logs --since=1h backend | rg 'ai\.|workflow\.|file\.'
```

Эти события относятся к вызовам llama.cpp из backend. Внутренний журнал процесса llama.cpp находится на сервере модели. В событиях backend сохраняются служебные данные вызова. Тексты промптов, ответов модели, вложения и секреты туда не попадают.

### Режим dev

`LOG_FORMAT=dev` выводит одну компактную строку на событие. В строке видны время UTC, цветной уровень, название события и диагностические поля. `LOG_COLOR=always` сохраняет ANSI-цвета при просмотре журналов Docker, `auto` включает их только при записи в терминал, `never` отключает. По умолчанию сервер пишет JSON. Формат и уровень настраиваются независимо.

```sh
LOG_FORMAT=dev LOG_COLOR=always docker compose up -d --no-deps backend
docker compose logs -f --no-log-prefix --tail=150 backend
```
