import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id = ContextVar("request_id", default=None)
log = logging.getLogger("nebula")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        if correlation := request_id.get():
            payload["request_id"] = correlation
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    log.handlers[:] = [handler]
    log.setLevel(level)
    log.propagate = False
    logging.getLogger("uvicorn.access").disabled = True


def event(name, level=logging.INFO, **fields):
    log.log(level, name, extra={"fields": fields})


def error_fields(exc):
    from pathlib import Path
    from traceback import extract_tb

    return {
        "error_type": type(exc).__name__,
        "frames": [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in extract_tb(exc.__traceback__, limit=-8)
        ],
    }
