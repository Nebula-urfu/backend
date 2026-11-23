from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from .auth import Conn, PASSWORDS, Role, User, allow, assigned_roles
from .db import jobs, model_usage, now, requests, sessions, uid, users
from .errors import Problem, invalid, missing
from .requests import page

router = APIRouter(prefix="/admin", tags=["administration"])
Username = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=2, max_length=128, pattern=r"^[a-z0-9_.-]+$")]
Password = Annotated[str, Field(min_length=12, max_length=256)]


class AccountInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=300)
    roles: list[Role] = Field(min_length=1, max_length=4)
    enabled: bool = True

    @model_validator(mode="after")
    def check_account(self):
        self.name = self.name.strip()
        self.address = self.address.strip()
        if not self.name or len(set(self.roles)) != len(self.roles):
            raise ValueError("Укажите имя и уникальные роли")
        if "RESIDENT" in self.roles and not self.address:
            raise ValueError("Укажите адрес жителя")
        return self


class AccountCreate(AccountInput):
    username: Username
    password: Password


class AccountUpdate(AccountInput):
    password: Password | None = None


class Account(AccountInput):
    userId: str
    username: str


class AccountPage(BaseModel):
    items: list[Account]
    nextCursor: str | None


def public(row):
    return dict(userId=row.id, username=row.username, name=row.name, address=row.address,
                roles=assigned_roles(row), enabled=row.enabled)


@router.get("/users", response_model=AccountPage)
def list_users(user: User, conn: Conn,
               query: str = Query("", max_length=128), offset: int = Query(0, ge=0)):
    allow(user, "ADMIN")
    statement = select(users).where(users.c.org == user.org)
    if query.strip():
        term = query.strip()
        statement = statement.where(users.c.username.icontains(term, autoescape=True) | users.c.name.icontains(term, autoescape=True))
    rows = conn.execute(statement.order_by(users.c.username).offset(offset).limit(101)).all()
    return {"items": [public(row) for row in rows[:100]], "nextCursor": str(offset + 100) if len(rows) > 100 else None}


@router.post("/users", status_code=201, response_model=Account)
def create_user(body: AccountCreate, user: User, conn: Conn):
    allow(user, "ADMIN")
    values = body.model_dump(exclude={"password"})
    row = conn.execute(insert(users).values(
        **values, id=uid(), org=user.org, role=body.roles[0], password=PASSWORDS.hash(body.password),
    ).on_conflict_do_nothing(index_elements=["username"]).returning(users)).first()
    if row is None:
        raise Problem(409, "USERNAME_UNAVAILABLE", "Этот логин недоступен")
    return public(row)


@router.put("/users/{ident}", response_model=Account)
def edit_user(ident: str, body: AccountUpdate, user: User, conn: Conn):
    allow(user, "ADMIN")
    if ident == user.id:
        raise invalid("Изменение своего аккаунта через панель недоступно")
    row = conn.execute(select(users).where(users.c.id == ident, users.c.org == user.org).with_for_update()).first()
    if not row:
        raise missing()
    values = body.model_dump(exclude={"password"})
    values["role"] = row.role if row.role in body.roles else body.roles[0]
    if body.password:
        values["password"] = PASSWORDS.hash(body.password)
    updated = conn.execute(update(users).where(users.c.id == ident).values(**values).returning(users)).one()
    if body.password or body.enabled != row.enabled or set(body.roles) != set(assigned_roles(row)):
        conn.execute(delete(sessions).where(sessions.c.user_id == ident))
    return public(updated)


class Metric(BaseModel):
    label: str
    count: int


class ProviderStat(BaseModel):
    model: str
    calls: int
    successful: int
    reported: int
    promptTokens: int
    completionTokens: int
    totalTokens: int
    averageLatencyMs: int


class Stats(BaseModel):
    days: int
    requests: list[Metric]
    daily: list[Metric]
    steps: list[Metric]
    urgency: list[Metric]
    jobs: list[Metric]
    model: list[ProviderStat]


@router.get("/stats", response_model=Stats)
def stats(user: User, conn: Conn, days: int = Query(30, ge=1, le=365)):
    allow(user, "ADMIN")
    since = (now() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    request_filter = (requests.c.org == user.org, requests.c.data["createdAt"].astext >= since.isoformat())

    def counts(label):
        statement = select(label.label("label"), func.count().label("count")).where(*request_filter)
        return [dict(row._mapping) for row in conn.execute(statement.group_by(label).order_by(label))]

    current_step = case(
        *[(requests.c.data[key]["state"].astext != "APPROVED", label) for key, label in (
            ("triage", "TRIAGE"), ("tz", "TZ"), ("nmc", "QUOTES_NMC"), ("tender", "TENDER"), ("package", "PACKAGE"),
        )], else_="COMPLETED",
    )
    usage = model_usage.c
    providers = conn.execute(select(
        usage.model, func.count().label("calls"),
        func.count().filter(usage.outcome == "ok").label("successful"),
        func.count(usage.total_tokens).label("reported"),
        func.coalesce(func.sum(usage.prompt_tokens), 0).label("promptTokens"),
        func.coalesce(func.sum(usage.completion_tokens), 0).label("completionTokens"),
        func.coalesce(func.sum(usage.total_tokens), 0).label("totalTokens"),
        func.round(func.avg(usage.latency_ms)).label("averageLatencyMs"),
    ).where(usage.org == user.org, usage.recorded_at >= since).group_by(usage.model).order_by(usage.model))
    job_counts = conn.execute(select(jobs.c.state.label("label"), func.count().label("count"))
        .join(requests, jobs.c.request_id == requests.c.id).where(*request_filter)
        .group_by(jobs.c.state).order_by(jobs.c.state))
    urgency = func.coalesce(requests.c.data["triage"]["confirmed"]["urgency"].astext,
                            requests.c.data["triage"]["proposal"]["urgency"].astext, "UNSET")
    daily_counts = {row["label"]: row["count"] for row in counts(func.substr(requests.c.data["createdAt"].astext, 1, 10))}
    return dict(days=days, requests=counts(requests.c.data["status"].astext),
                daily=[{"label": label, "count": daily_counts.get(label, 0)}
                       for i in range(days) if (label := (since + timedelta(days=i)).date().isoformat())],
                steps=counts(current_step), urgency=counts(urgency), jobs=[dict(row._mapping) for row in job_counts], model=[dict(row._mapping) for row in providers])
