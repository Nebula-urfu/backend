import re

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .auth import Conn, User, allow
from .db import templates
from .errors import version
from .logging_config import event

router = APIRouter(prefix="/admin/templates", tags=["administration"])
TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
COMMON = {"address", "entrance", "request_id", "tz_version"}
LETTER = COMMON | {"draft", "draft_subject", "staff_name", "staff_phone"}
CONTRACT = COMMON | {"contractor", "price", "days", "warranty"}
ALLOWED = {
    "letterSubject": LETTER, "letterBody": LETTER,
    "specIntro": COMMON, "quoteFormIntro": COMMON,
    "contractTerms": CONTRACT, "protocolIntro": COMMON | {"nmc"},
}
DEFAULTS = {
    "letterSubject": "{{draft_subject}}",
    "letterBody": "{{draft}}",
    "specIntro": "Шаблон пилота. Требует проверки перед использованием.",
    "quoteFormIntro": "Шаблон пилота",
    "contractTerms": "Работы и приёмка по приложенному техническому заданию. Реквизиты и условия оплаты подлежат заполнению.",
    "protocolIntro": "Шаблон пилота",
}


class TemplateFields(BaseModel):
    letterSubject: str = Field(min_length=1, max_length=250)
    letterBody: str = Field(min_length=1, max_length=4000)
    specIntro: str = Field(min_length=1, max_length=4000)
    quoteFormIntro: str = Field(min_length=1, max_length=4000)
    contractTerms: str = Field(min_length=1, max_length=4000)
    protocolIntro: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def valid_variables(self):
        for key, allowed in ALLOWED.items():
            value = getattr(self, key).strip()
            unknown = set(TOKEN.findall(value)) - allowed
            remaining = TOKEN.sub("", value)
            if not value or unknown or "{{" in remaining or "}}" in remaining:
                raise ValueError(f"Недопустимые переменные или пустое поле: {key}")
            if any(ord(c) < 32 and c not in "\n\t\r" for c in value):
                raise ValueError(f"Недопустимый символ: {key}")
            if key == "letterSubject" and ("\n" in value or "\r" in value):
                raise ValueError("Тема письма должна занимать одну строку")
            setattr(self, key, value)
        return self


class TemplateUpdate(TemplateFields):
    revision: int = Field(ge=0)


class TemplateView(BaseModel):
    revision: int
    values: TemplateFields
    defaults: TemplateFields
    variables: dict[str, list[str]]


def current(conn, org, lock=False):
    query = select(templates).where(templates.c.org == org)
    row = conn.execute(query.with_for_update() if lock else query).first()
    return (row.revision, row.data) if row else (0, DEFAULTS)


def template_view(revision, values):
    return {"revision": revision, "values": values, "defaults": DEFAULTS,
            "variables": {key: sorted(variables) for key, variables in ALLOWED.items()}}


@router.get("", response_model=TemplateView)
def get_templates(user: User, conn: Conn):
    allow(user, "ADMIN")
    return template_view(*current(conn, user.org))


@router.put("", response_model=TemplateView)
def save_templates(body: TemplateUpdate, user: User, conn: Conn):
    allow(user, "ADMIN")
    revision, _ = current(conn, user.org, lock=True)
    version(revision, body.revision)
    values = body.model_dump(exclude={"revision"})
    if revision:
        conn.execute(update(templates).where(templates.c.org == user.org).values(revision=revision + 1, data=values))
    else:
        created = conn.execute(insert(templates).values(org=user.org, revision=1, data=values)
                               .on_conflict_do_nothing().returning(templates.c.revision)).scalar()
        version(created, 1)
    event("templates.saved", organization_id=user.org, revision=revision + 1)
    return template_view(revision + 1, values)


def variables_for(row):
    return dict(address=row.data["address"], entrance=row.data["entrance"], request_id=row.id,
                tz_version=row.data["tz"]["version"])


def render(text, values):
    return TOKEN.sub(lambda match: str(values.get(match[1], "")), text)
