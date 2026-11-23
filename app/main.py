import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select

from . import admin, approvals, auth, documents, procurement, requests
from .ai import worker
from .auth import Conn
from .errors import Problem
from .views import ProblemView


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
