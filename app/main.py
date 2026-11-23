import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select

from . import approvals, auth, documents, procurement, requests
from .ai import worker
from .auth import Conn
from .errors import Problem


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Помощник УК по закупкам", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def private_api(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
    return response


@app.exception_handler(Problem)
async def problem(request: Request, exc: Problem):
    return JSONResponse({"status": exc.status, "code": exc.code, "title": exc.title},
                        status_code=exc.status, media_type="application/problem+json")


@app.exception_handler(RequestValidationError)
async def validation(request: Request, exc: RequestValidationError):
    errors = [{"field": ".".join(map(str, error["loc"])), "message": error["msg"]} for error in exc.errors()]
    return JSONResponse({"status": 400, "code": "VALIDATION_ERROR", "title": "Проверьте поля", "errors": errors},
                        status_code=400, media_type="application/problem+json")


@app.get("/healthz")
def health(conn: Conn):
    conn.execute(select(1))
    return {"status": "ok"}


for router in (auth.router, requests.router, documents.router, procurement.router, approvals.router):
    app.include_router(router, prefix="/api/v1")
