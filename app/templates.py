from pathlib import Path
from typing import Literal

import re

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .auth import Conn, User, allow
from .db import engine, model_usage, now, templates, uid
from .errors import Problem, invalid, version
from .logging_config import event
from .office_text import MAX_BYTES, OFFICE_MIME, verify_office

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
    letterBody: str = Field(min_length=1, max_length=12000)
    specIntro: str = Field(min_length=1, max_length=12000)
    quoteFormIntro: str = Field(min_length=1, max_length=12000)
    contractTerms: str = Field(min_length=1, max_length=12000)
    protocolIntro: str = Field(min_length=1, max_length=12000)

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


TemplateKind = Literal["letter", "spec", "quote_form", "contract", "protocol"]
IMPORT_FIELDS = {
    "letter": "letterBody",
    "spec": "specIntro",
    "quote_form": "quoteFormIntro",
    "contract": "contractTerms",
    "protocol": "protocolIntro",
}
IMPORT_LABEL = {
    "letter": "тема и текст письма запроса коммерческого предложения",
    "spec": "вступительный блок технического задания",
    "quote_form": "указания по заполнению формы КП",
    "contract": "условия проекта договора",
    "protocol": "вступление и примечания к протоколу оценки",
}


class ImportedDraft(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    subject: str | None = Field(default=None, max_length=250)


class ImportedTemplate(BaseModel):
    fields: dict[str, str]


@router.post("/import", response_model=ImportedTemplate)
def import_document(user: User, kind: TemplateKind = Form(), file: UploadFile = File()):
    from .ai import import_template_draft, office_inputs
    from .requests import AI_ENABLED

    allow(user, "ADMIN")
    if not AI_ENABLED:
        raise Problem(503, "AI_UNAVAILABLE", "Распознавание шаблонов требует доступной модели")
    ext = Path(file.filename or "").suffix.lower().lstrip(".")
    if ext not in OFFICE_MIME:
        raise invalid("Выберите PDF, DOCX, DOC, ODT или TXT")
    content = file.file.read(MAX_BYTES + 1)
    if not 0 < len(content) <= MAX_BYTES:
        raise invalid("Размер файла должен быть от 1 байта до 10 МиБ")
    try:
        verify_office(content, ext)
        inputs = office_inputs([(content, OFFICE_MIME[ext])])
    except Exception as exc:
        raise invalid("Не удалось прочитать документ. Проверьте формат, размер и количество страниц") from exc
    if not inputs:
        raise invalid("В документе не найден текст для импорта")
    field = IMPORT_FIELDS[kind]
    metrics = {}
    try:
        reply = import_template_draft(IMPORT_LABEL[kind], sorted(ALLOWED[field]), inputs, metrics)
        fields = {field: reply["text"]}
        if kind == "letter" and reply.get("subject"):
            fields["letterSubject"] = reply["subject"]
        validated = TemplateFields.model_validate({**DEFAULTS, **fields})
        return {"fields": {key: getattr(validated, key) for key in fields}}
    except ValidationError as exc:
        raise invalid("Модель вернула некорректный шаблон. Проверьте документ и повторите импорт") from exc
    except Exception as exc:
        raise Problem(503, "AI_UNAVAILABLE", "Модель не смогла распознать шаблон") from exc
    finally:
        if metrics:
            with engine.begin() as conn:
                conn.execute(model_usage.insert().values(
                    id=uid(), org=user.org, kind="template_import", recorded_at=now(), **metrics))
