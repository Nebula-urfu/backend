#!/usr/bin/env bash
set -euo pipefail
: "${BACKEND_IMAGE:?}" "${FRONTEND_IMAGE:?}"
export COMPOSE_PROJECT_NAME="nebula-smoke-$$"
export POSTGRES_PASSWORD="smoke-$RANDOM-$RANDOM"
export OPENAI_API_KEY= OPENAI_BASE_URL= SECURE_COOKIES=false
export HTTP_BIND=127.0.0.1:18080 HTTPS_BIND=127.0.0.1:18443 SITE_ADDRESS=http://localhost
trap 'docker compose down -v --remove-orphans' EXIT
docker compose up -d --wait --pull missing
docker compose exec -T backend python -m scripts.seed_smoke
curl -fsS http://localhost:18080/login >/dev/null
id=$(docker compose exec -T backend python -c 'from urllib.request import urlopen; assert urlopen("http://proxy/healthz").status == 200' &&
    docker compose exec -T -e SMOKE_BASE=http://proxy backend python -m scripts.scenario)
docker compose restart backend postgres
docker compose up -d --wait
docker compose exec -T -e SMOKE_BASE=http://proxy backend python -c '
import sys
from scripts.scenario import call, login
staff = login("staff")
path = "/requests/" + sys.argv[1]
assert call(staff, "GET", path).json()["status"] == "COMPLETED"
assert call(staff, "GET", path + "/package/zip").content.startswith(b"PK")
' "$id"
