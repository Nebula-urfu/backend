FROM python:3.14.10-slim-bookworm AS build
RUN pip install --no-cache-dir uv==0.13.9
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
FROM python:3.14.10-slim-bookworm
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends catdoc poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 10001 nebula && useradd -u 10001 -g nebula nebula && install -d -o nebula -g nebula -m 0700 /data/files
COPY --from=build /app/.venv /app/.venv
COPY app app
COPY migrations migrations
COPY alembic.ini .
COPY scripts/seed_smoke.py scripts/scenario.py scripts/
ENV PATH=/app/.venv/bin:$PATH FILE_ROOT=/data/files PYTHONUNBUFFERED=1
USER nebula
CMD ["sh", "-ec", "alembic upgrade head; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
