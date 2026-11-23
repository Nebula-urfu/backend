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
