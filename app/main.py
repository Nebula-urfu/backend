import asyncio
import logging
from time import monotonic
from uuid import uuid4
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select

from . import admin, approvals, auth, documents, procurement, requests
from .ai import worker
from .auth import Conn
from .errors import Problem
from .logging_config import configure, event, request_id
from .views import ProblemView


@asynccontextmanager
async def lifespan(app):
    configure()
    event("application.started")
    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        event("application.stopped")


app = FastAPI(title="Помощник УК по закупкам", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def private_api(request, call_next):
    correlation = uuid4().hex
    context = request_id.set(correlation)
    started = monotonic()
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            event("http.unhandled_error", logging.ERROR, error_type=type(exc).__name__)
            response = JSONResponse({"status": 500, "title": "Внутренняя ошибка сервера"},
                                    status_code=500, media_type="application/problem+json")
        response.headers["X-Request-ID"] = correlation
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        status = response.status_code
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        if request.url.path == "/healthz" and status == 200:
            level = logging.DEBUG
        route = request.scope.get("route")
        event("http.request", level, method=request.method, route=getattr(route, "path", "<unmatched>"),
              status=status, problem_code=getattr(request.state, "problem_code", None), duration_ms=round((monotonic() - started) * 1000, 1))
        return response
    finally:
        request_id.reset(context)


@app.exception_handler(Problem)
async def problem(request: Request, exc: Problem):
    request.state.problem_code = exc.code
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


for router in (auth.router, admin.router, requests.router, documents.router, procurement.router, approvals.router):
    app.include_router(router, prefix="/api/v1", responses={
        code: {"model": ProblemView, "description": title, "content": {"application/problem+json": {}}}
        for code, title in {400: "Invalid input", 401: "Authentication required", 403: "Forbidden",
                            404: "Not found", 409: "Conflict", 429: "Too many login attempts"}.items()
    })


def export_openapi():
    from fastapi.openapi.utils import get_openapi

    if app.openapi_schema is None:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        for path in schema["paths"].values():
            for operation in path.values():
                responses = operation.get("responses", {})
                responses.pop("422", None)
                for response in responses.values():
                    content = response.get("content", {})
                    if "application/problem+json" in content and "application/json" in content:
                        content["application/problem+json"] = content.pop("application/json")
        app.openapi_schema = schema
    return app.openapi_schema


app.openapi = export_openapi
