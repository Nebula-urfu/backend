#!/usr/bin/env bash
set -euo pipefail
tmp=$(mktemp -d)
trap 'if [[ -n ${server:-} ]]; then kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true; fi; pg_ctl -D "$tmp/pg" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT
mkdir "$tmp/socket"
initdb -D "$tmp/pg" -A trust --no-locale -E UTF8 >/dev/null
pg_ctl -D "$tmp/pg" -l "$tmp/postgres.log" -o "-k $tmp/socket -h ''" -w start >/dev/null
createdb -h "$tmp/socket" nebula
export DATABASE_URL="postgresql+psycopg:///nebula?host=$tmp/socket" FILE_ROOT="$tmp/files" SECURE_COOKIES=false
export SMOKE_BASE=http://127.0.0.1:18490
unset OPENAI_API_KEY OPENAI_BASE_URL
uv run --locked alembic upgrade head
uv run --locked alembic downgrade base
uv run --locked alembic upgrade head
uv run --locked python -m scripts.seed_smoke
uv run --locked uvicorn app.main:app --port 18490 >"$tmp/api.log" 2>&1 &
server=$!
for _ in $(seq 1 60); do
  if curl -fsS "$SMOKE_BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep .25
done
uv run --locked python -m scripts.scenario || { cat "$tmp/api.log"; exit 1; }
kill "$server"
wait "$server" || true
server=
uv run --locked pytest -q
