import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id = ContextVar("request_id", default=None)
log = logging.getLogger("nebula")


def event_fields(record):
    fields = {}
    if correlation := request_id.get():
        fields["request_id"] = correlation
    fields.update(getattr(record, "fields", {}))
    return fields


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update(event_fields(record))
        return json.dumps(payload, ensure_ascii=False, default=str)


class DevFormatter(logging.Formatter):
    LEVEL_COLORS = {
        "DEBUG": 36, "INFO": 32, "WARNING": 33, "ERROR": 31, "CRITICAL": 35,
    }

    def __init__(self, color):
        super().__init__()
        self.color = color

    def style(self, value, code):
        return f"\x1b[{code}m{value}\x1b[0m" if self.color else value

    def format(self, record):
        timestamp = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S.%f")[:-3] + "Z"
        level = self.style(f"{record.levelname:<8}", self.LEVEL_COLORS.get(record.levelname, 37))
        suffix = []
        for key, value in event_fields(record).items():
            if value is not None:
                literal = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
                suffix.append(f"{self.style(key, 36)}={literal}")
        line = f"{self.style(timestamp, 90)}  {level}  {self.style(record.getMessage(), 1)}"
        return "  ".join([line, *suffix])


def configure():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    mode = os.getenv("LOG_FORMAT", "json").lower()
    if mode not in {"json", "dev"}:
        raise ValueError("LOG_FORMAT must be json or dev")
    color = os.getenv("LOG_COLOR", "always").lower()
    if color not in {"always", "auto", "never"}:
        raise ValueError("LOG_COLOR must be always, auto or never")
    colored = "NO_COLOR" not in os.environ and (color == "always" or (color == "auto" and sys.stdout.isatty()))
    formatter = DevFormatter(colored) if mode == "dev" else JsonFormatter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
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
