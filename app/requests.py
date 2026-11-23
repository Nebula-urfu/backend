import hashlib
import json
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .auth import Conn, User, allow
from .db import files, jobs, requests, stamp, uid
from .errors import Problem, invalid, missing, require, version
from .logging_config import event
from .schemas import DescriptionDraftInput, DescriptionDraftResponse, RequestInput, ResidentAnswers, TriageInput, TzInput
from . import views

router = APIRouter(tags=["requests"])
AI_ENABLED = bool(os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_BASE_URL"))
AUTO_WORKFLOW = os.getenv("AUTOMATIC_WORKFLOW", "true").lower() == "true"
STEPS = ("TRIAGE", "TZ", "QUOTES_NMC", "TENDER", "PACKAGE")
SECTIONS = {
    "scope": "Предмет работ", "volume": "Объём и материалы",
    "schedule": "Сроки", "acceptance": "Критерии приёмки",
}


def page(items):
    return {"items": items, "nextCursor": None}


def find(conn, user, ident, lock=False):
    query = select(requests).where(requests.c.id == ident, requests.c.org == user.org)
    if user.role == "RESIDENT":
        query = query.where(requests.c.author == user.id)
    row = conn.execute(query.with_for_update() if lock else query).first()
    if row is None:
        raise missing()
    return row


def actor(user, include_phone=False):
    result = {"userId": user.id, "displayName": user.name}
    if include_phone and user.phone:
        result["phone"] = user.phone
    return result


def save(conn, row):
    row.data["updatedAt"] = stamp()
    conn.execute(update(requests).where(requests.c.id == row.id).values(data=row.data))


def working_doc(state="NOT_STARTED"):
    return {"state": state, "version": 0}


def state(doc, value):
    doc.update(state=value, updatedAt=stamp())


def summary(row, resident, detail=False):
    d = row.data
    docs = [d["triage"], d["tz"], d["nmc"], d["tender"], d["package"]]
    step = next((i for i, doc in enumerate(docs) if doc["state"] != "APPROVED"), 4)
    result = {k: d[k] for k in ("address", "entrance", "description", "createdAt", "updatedAt")}
    result.update(id=row.id, status=d["status"], photoId=d["photos"][0]["fileId"])
    if detail:
        result["photos"] = d["photos"]
        for field in ("residentName", "floor", "apartment"):
            if field in d:
                result[field] = d[field]
        if resident and d.get("intake", {}).get("pending"):
            result["clarificationQuestions"] = d["intake"]["questions"]
    if not resident:
        if d.get("automatic"):
            result.update(automatic=True, waitingFor=d.get("waitingFor"))
        triage = d["triage"]["confirmed"] or d["triage"]["proposal"]
        result.update(urgency=triage and triage["urgency"], currentStep=STEPS[step], currentStepState=docs[step]["state"])
        if detail:
            result.update(author=d["author"], steps=[
                {"step": name, "state": doc["state"], "updatedAt": doc.get("updatedAt", d["updatedAt"])}
                for name, doc in zip(STEPS, docs)
            ])
    return result


def attach(conn, user, row_id, ids, purpose, types=None):
    if len(set(ids)) != len(ids):
        raise invalid("Файлы должны быть уникальны")
    records = conn.execute(select(files).where(files.c.id.in_(ids)).order_by(files.c.id).with_for_update()).all()
    if len(records) != len(ids) or any(r.owner != user.id or r.request_id for r in records):
        raise missing()
    if types and any(r.data["mediaType"] not in types for r in records):
        raise invalid("Неподдерживаемый формат фотографии")
    conn.execute(update(files).where(files.c.id.in_(ids)).values(request_id=row_id, purpose=purpose))
    indexed = {r.id: r.data for r in records}
    return [indexed[i] for i in ids]


def enqueue(conn, row_id, kind, target, doc):
    generation = doc.get("generation", 0) + 1
    doc["generation"] = generation
    conn.execute(jobs.insert().values(id=uid(), request_id=row_id, kind=kind, target=target,
                                     generation=generation, attempt=0, state="queued"))


def public(doc):
    return {key: value for key, value in doc.items() if key != "generation"}


@router.get("/requests", response_model=views.Page[views.RequestSummary], response_model_exclude_unset=True)
def list_requests(user: User, conn: Conn, limit: int = Query(100, ge=1, le=100)):
    allow(user, "RESIDENT", "STAFF", "REVIEWER")
    query = select(requests).where(requests.c.org == user.org)
    if user.role == "RESIDENT":
        query = query.where(requests.c.author == user.id)
    docs = [summary(r, user.role == "RESIDENT") for r in conn.execute(query)]
    docs.sort(key=lambda r: (r.get("urgency") == "EMERGENCY", r["createdAt"]), reverse=True)
    return page(docs[:limit])


@router.post("/requests", status_code=201, response_model=views.RequestDetail, response_model_exclude_unset=True)
def create_request(body: RequestInput, user: User, conn: Conn,
                   key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=200)] = None):
    allow(user, "RESIDENT")
    if not key or not key.strip():
        raise invalid("Требуется Idempotency-Key")
    fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    ident = uid()
    created_at = stamp()
    entrance = user.entrance if body.entrance is None else body.entrance
    if not entrance or not user.address:
        raise invalid("Укажите адрес и подъезд")
    d = dict(description=body.description, address=user.address, entrance=entrance, createdAt=created_at,
             residentName=user.name, floor=user.floor if body.floor is None else body.floor,
             apartment=user.apartment if body.apartment is None else body.apartment,
             updatedAt=created_at, status="ACCEPTED", author=actor(user), photos=[], quotes={},
             automatic=AUTO_WORKFLOW, scopeRevision=0, waitingFor="TZ",
             triage=dict(state="PROCESSING" if AI_ENABLED else "DRAFT_READY", proposal=None, confirmed=None),
             tz=working_doc(), nmc=working_doc(), tender=dict(state="NOT_STARTED", documents=[], winnerProposal=None,
             selectedWinner=None, contract=None), package=dict(working_doc(), documents=[]))
    inserted = conn.execute(insert(requests).values(
        id=ident, org=user.org, author=user.id, key=key, fingerprint=fingerprint, data=d
    ).on_conflict_do_nothing(index_elements=["author", "key"]).returning(requests.c.id)).scalar()
    if not inserted:
        previous = conn.execute(select(requests).where(requests.c.author == user.id, requests.c.key == key)).one()
        version(previous.fingerprint, fingerprint)
        return summary(previous, True, True)
    d["photos"] = attach(conn, user, ident, body.photoFileIds, "photo", {"image/jpeg", "image/png", "image/webp"})
    if AI_ENABLED:
        enqueue(conn, ident, "triage", "", d["triage"])
    conn.execute(update(requests).where(requests.c.id == ident).values(data=d))
    event("procurement.created", procurement_id=ident)
    return summary(find(conn, user, ident), True, True)


@router.get("/requests/{ident}", response_model=views.RequestDetail, response_model_exclude_unset=True)
def request_detail(ident: str, user: User, conn: Conn):
    allow(user, "RESIDENT", "STAFF", "REVIEWER")
    return summary(find(conn, user, ident), user.role == "RESIDENT", True)


def read(ident, user, conn, key):
    allow(user, "STAFF", "REVIEWER")
    return public(find(conn, user, ident).data[key])


@router.get("/requests/{ident}/triage", response_model=views.Triage, response_model_exclude_unset=True)
def triage(ident: str, user: User, conn: Conn):
    return read(ident, user, conn, "triage")


def default_tz(description):
    return [
        {"id": key, "title": title, "text": description if key == "scope" else "Уточнить сотруднику УК"}
        for key, title in SECTIONS.items()
    ]


@router.post("/requests/{ident}/triage/confirm", response_model=views.Triage)
def confirm(ident: str, body: TriageInput, user: User, conn: Conn):
    from .workflow import advance, scope_changed
    allow(user, "STAFF")
    row = find(conn, user, ident, True)
    d = row.data
    previous = d["triage"].get("confirmed")
    require(d.get("automatic") or d["triage"]["state"] != "APPROVED")
    confirmed = body.model_dump()
    if d.get("intake"):
        d["intake"]["pending"] = False
    d["triage"].update(confirmed=confirmed, confirmedBy=actor(user), confirmedAt=stamp())
    state(d["triage"], "APPROVED")
    tz = d["tz"]
    if not tz["version"]:
        tz.update(version=1, origin="STAFF", sections=default_tz(d["description"]), openQuestions=[])
    if d.get("automatic") and previous and previous["workType"] != confirmed["workType"]:
        tz.update(version=tz["version"] + 1, origin="STAFF", sections=default_tz(d["description"]), openQuestions=[])
        scope_changed(row)
        if AI_ENABLED:
            enqueue(conn, row.id, "redraft", "", tz)
            state(tz, "PROCESSING")
        else:
            state(tz, "DRAFT_READY")
    else:
        state(tz, "DRAFT_READY")
    d["status"] = "IN_PROGRESS"
    advance(conn, row)
    save(conn, row)
    return public(d["triage"])


@router.get("/requests/{ident}/tz", response_model=views.Tz, response_model_exclude_unset=True)
def tz(ident: str, user: User, conn: Conn):
    return read(ident, user, conn, "tz")


@router.put("/requests/{ident}/tz", response_model=views.Tz, response_model_exclude_unset=True)
def edit_tz(ident: str, body: TzInput, user: User, conn: Conn):
    from .workflow import advance, scope_changed
    allow(user, "STAFF")
    row = find(conn, user, ident, True)
    doc = row.data["tz"]
    version(doc["version"], body.baseVersion)
    automatic = row.data.get("automatic")
    require(doc["state"] in ("DRAFT_READY", "NEEDS_REWORK") or (automatic and doc["state"] == "APPROVED"))
    sections = {s["id"]: s for s in doc["sections"]}
    questions = {q["id"]: q for q in doc["openQuestions"]}
    for patch in body.sections:
        if patch.id not in sections:
            raise invalid("Неизвестный раздел")
        sections[patch.id].update(text=patch.text, editedByStaff=True)
    for patch in body.answers:
        if patch.questionId not in questions:
            raise invalid("Неизвестный вопрос")
        questions[patch.questionId]["answer"] = patch.answer
    doc.update(version=doc["version"] + 1, origin="STAFF")
    if automatic and (body.sections or body.answers):
        if row.data.get("intake"):
            row.data["intake"]["pending"] = False
        scope_changed(row)
    state(doc, "DRAFT_READY")
    advance(conn, row)
    save(conn, row)
    return public(doc)


@router.post("/requests/{ident}/steps/{step}/restart", status_code=202)
def restart(ident: str, step: Literal["TRIAGE", "TZ", "QUOTES_NMC", "TENDER", "PACKAGE"], user: User, conn: Conn):
    allow(user, "STAFF")
    row = find(conn, user, ident, True)
    require(AI_ENABLED and step in ("TRIAGE", "QUOTES_NMC"))
    if step == "TRIAGE":
        doc = row.data["triage"]
        require(doc["state"] == "AI_FAILED")
        state(doc, "PROCESSING")
        enqueue(conn, row.id, "triage", "", doc)
    else:
        require(row.data.get("automatic") or row.data["nmc"]["state"] not in ("IN_REVIEW", "APPROVED"))
        failed = [q for q in row.data["quotes"].values() if q["extraction"] == "AI_FAILED" and not q.get("editedByStaff")]
        if row.data.get("automatic"):
            failed = [q for q in failed if q.get("scopeRevision", 0) == row.data.get("scopeRevision", 0)]
        require(bool(failed))
        for quote in failed:
            quote["extraction"] = "EXTRACTING"
            enqueue(conn, row.id, "quote", quote["id"], quote)
        if row.data.get("automatic"):
            from .workflow import advance
            advance(conn, row)
    save(conn, row)
    return {"status": "queued"}


@router.post("/requests/{ident}/clarifications", response_model=views.RequestDetail, response_model_exclude_unset=True)
def answer_clarifications(ident: str, body: ResidentAnswers, user: User, conn: Conn):
    allow(user, "RESIDENT")
    row = find(conn, user, ident, True)
    d = row.data
    intake = d.get("intake", {})
    require(intake.get("pending", False) and (d["triage"]["state"] != "APPROVED" or d.get("automatic")))
    if len(body.answers) != len(intake["questions"]):
        raise invalid("Ответьте на все вопросы или укажите, что ответ неизвестен")
    intake.update(pending=False, answers=body.answers)
    if d.get("automatic"):
        from .workflow import advance, scope_changed
        scope_changed(row, refresh_letter=False)
        state(d["triage"], "PROCESSING")
        advance(conn, row)
    state(d["triage"], "PROCESSING")
    enqueue(conn, row.id, "triage", "", d["triage"])
    save(conn, row)
    return summary(row, True, True)


@router.post("/requests/{ident}/automation", response_model=views.RequestDetail, response_model_exclude_unset=True)
def enable_automation(ident: str, user: User, conn: Conn):
    from .workflow import SYSTEM_ACTOR, advance, complete_tz

    allow(user, "STAFF")
    row = find(conn, user, ident, True)
    d = row.data
    if not d.get("automatic"):
        require(d["status"] != "COMPLETED")
        d["automatic"] = True
        triage = d["triage"]
        if (triage.get("proposal") and triage["state"] == "DRAFT_READY" and complete_tz(d["tz"])
                and not d.get("intake", {}).get("pending")):
            triage.update(confirmed=triage["proposal"], confirmedBy=SYSTEM_ACTOR, confirmedAt=stamp())
            state(triage, "APPROVED")
        advance(conn, row)
        save(conn, row)
    return summary(row, False, True)


@router.post("/requests/description-draft", response_model=DescriptionDraftResponse, responses={503: {"description": "AI unavailable"}})
def describe_photos(body: DescriptionDraftInput, user: User):
    from datetime import timedelta
    from types import SimpleNamespace

    from sqlalchemy import case

    from . import ai
    from .db import attempts, engine, model_usage, now

    allow(user, "RESIDENT")
    if not AI_ENABLED:
        raise Problem(503, "AI_UNAVAILABLE", "Описание можно заполнить вручную")
    ids = body.photoFileIds
    if len(set(ids)) != len(ids):
        raise invalid("Фотографии должны быть уникальны")
    with engine.begin() as conn:
        rows = conn.execute(select(files).where(
            files.c.id.in_(ids), files.c.owner == user.id, files.c.request_id.is_(None)
        )).all()
        if len(rows) != len(ids):
            raise missing()
        if any(r.data["mediaType"] not in ("image/jpeg", "image/png", "image/webp") for r in rows):
            raise invalid("Неподдерживаемый формат фотографии")
        current = now()
        until = current + timedelta(minutes=1)
        expired = attempts.c.expires <= current
        key = "description:" + user.id
        count = conn.execute(insert(attempts).values(id=key, count=1, expires=until).on_conflict_do_update(
            index_elements=["id"], set_={
                "count": case((expired, 1), else_=attempts.c.count + 1),
                "expires": case((expired, until), else_=attempts.c.expires),
            },
        ).returning(attempts.c.count)).scalar_one()
        if count > 6:
            raise Problem(429, "RATE_LIMIT", "Подождите минуту или заполните описание вручную")
        photos = {row.id: row.data for row in rows}
    metrics = {}
    try:
        return ai.inference(SimpleNamespace(kind="description"), {
            "photos": [photos[i] for i in ids], "_metrics": metrics,
        })
    except Exception as exc:
        raise Problem(503, "AI_UNAVAILABLE", "Не удалось составить описание. Попробуйте ещё раз или заполните поле вручную") from exc
    finally:
        event("ai.description_result", **{key: metrics.get(key) for key in ("model", "latency_ms", "total_tokens", "outcome")})
        if metrics:
            with engine.begin() as conn:
                conn.execute(model_usage.insert().values(
                    id=uid(), org=user.org, kind="description", recorded_at=now(), **metrics))
