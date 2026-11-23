import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import ARRAY, Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, UniqueConstraint, create_engine
from sqlalchemy.dialects.postgresql import JSONB

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg:///nebula")
STORAGE = Path(os.getenv("FILE_ROOT", "data"))
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
metadata = MetaData()


def table(name, *columns):
    return Table(name, metadata, Column("id", String, primary_key=True), *columns)


orgs = table("organizations", Column("name", String, nullable=False))
users = table(
    "users", Column("org", ForeignKey(orgs.c.id), nullable=False),
    Column("username", String, unique=True, nullable=False),
    Column("roles", ARRAY(String), nullable=False, server_default="{}"),
    Column("enabled", Boolean, nullable=False, server_default="true"),
    *(Column(field, String, nullable=False, server_default="") for field in ("entrance", "floor", "apartment", "phone")),
    *(Column(field, String, nullable=False) for field in ("password", "role", "name", "address")),
)
sessions = table(
    "sessions", Column("user_id", ForeignKey(users.c.id), nullable=False),
    Column("expires", DateTime(timezone=True), nullable=False),
    Column("active_role", String),
)
attempts = table(
    "login_attempts", Column("count", Integer, nullable=False),
    Column("expires", DateTime(timezone=True), nullable=False),
)
contractors = table("contractors", Column("org", ForeignKey(orgs.c.id), nullable=False), Column("data", JSONB, nullable=False))
requests = table(
    "requests", Column("org", ForeignKey(orgs.c.id), nullable=False, index=True),
    Column("author", ForeignKey(users.c.id), nullable=False),
    Column("key", String, nullable=False), Column("fingerprint", String, nullable=False),
    Column("data", JSONB, nullable=False), UniqueConstraint("author", "key"),
)
files = table(
    "files", Column("owner", ForeignKey(users.c.id), nullable=False),
    Column("request_id", ForeignKey(requests.c.id), index=True),
    Column("purpose", String), Column("version", Integer),
    Column("data", JSONB, nullable=False),
)
approvals = table(
    "approvals", Column("request_id", ForeignKey(requests.c.id), nullable=False, index=True),
    Column("org", ForeignKey(orgs.c.id), nullable=False, index=True),
    Column("kind", String, nullable=False), Column("version", Integer, nullable=False),
    Column("data", JSONB, nullable=False), UniqueConstraint("request_id", "kind", "version"),
)
jobs = table(
    "jobs", Column("request_id", ForeignKey(requests.c.id), nullable=False, index=True),
    Column("kind", String, nullable=False), Column("target", String, nullable=False),
    Column("generation", Integer, nullable=False), Column("attempt", Integer, nullable=False),
    Column("state", String, nullable=False),
)


model_usage = table(
    "model_usage", Column("org", ForeignKey(orgs.c.id), nullable=False),
    *(Column(field, String, nullable=False) for field in ("kind", "model", "outcome")),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("latency_ms", Integer, nullable=False),
    *(Column(field, Integer) for field in ("prompt_tokens", "completion_tokens", "total_tokens")),
)


def uid():
    return str(uuid4())


def now():
    return datetime.now(UTC)


def stamp():
    return now().isoformat()


def connection():
    with engine.begin() as conn:
        yield conn
