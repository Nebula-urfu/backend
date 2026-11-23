import hashlib
import json
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .auth import Conn, User, allow
from .db import files, jobs, requests, stamp, uid
from .errors import invalid, missing, require, version
from .schemas import RequestInput, ResidentAnswers, TriageInput, TzInput
from . import views

router = APIRouter(tags=["requests"])
AI_ENABLED = bool(os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_BASE_URL"))
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


def actor(user):
    return {"userId": user.id, "displayName": user.name}


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
        if resident and d.get("intake", {}).get("pending"):
            result["clarificationQuestions"] = d["intake"]["questions"]
    if not resident:
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
    d = dict(body.model_dump(exclude={"photoFileIds"}), address=user.address, createdAt=created_at,
             updatedAt=created_at, status="ACCEPTED", author=actor(user), photos=[], quotes={},
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
    return summary(find(conn, user, ident), True, True)


@router.get("/requests/{ident}", response_model=views.RequestDetail, response_model_exclude_unset=True)
def request_detail(ident: str, user: User, conn: Conn):
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
    allow(user, "STAFF")
    row = find(conn, user, ident, True)
    d = row.data
    require(d["triage"]["state"] != "APPROVED")
    confirmed = body.model_dump()
    if d.get("intake"):
        d["intake"]["pending"] = False
    d["triage"].update(confirmed=confirmed, confirmedBy=actor(user), confirmedAt=stamp())
    state(d["triage"], "APPROVED")
    tz = d["tz"]
    if not tz["version"]:
        tz.update(version=1, origin="STAFF", sections=default_tz(d["description"]), openQuestions=[])
    state(tz, "DRAFT_READY")
    d["status"] = "IN_PROGRESS"
    save(conn, row)
    return public(d["triage"])


@router.get("/requests/{ident}/tz", response_model=views.Tz, response_model_exclude_unset=True)
def tz(ident: str, user: User, conn: Conn):
    return read(ident, user, conn, "tz")


@router.put("/requests/{ident}/tz", response_model=views.Tz, response_model_exclude_unset=True)
def edit_tz(ident: str, body: TzInput, user: User, conn: Conn):
    allow(user, "STAFF")
    row = find(conn, user, ident, True)
    doc = row.data["tz"]
    version(doc["version"], body.baseVersion)
    require(doc["state"] in ("DRAFT_READY", "NEEDS_REWORK"))
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
    state(doc, "DRAFT_READY")
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
        require(row.data["nmc"]["state"] not in ("IN_REVIEW", "APPROVED"))
        failed = [q for q in row.data["quotes"].values() if q["extraction"] == "AI_FAILED" and not q.get("editedByStaff")]
        require(bool(failed))
        for quote in failed:
            quote["extraction"] = "EXTRACTING"
            enqueue(conn, row.id, "quote", quote["id"], quote)
    save(conn, row)
    return {"status": "queued"}


@router.post("/requests/{ident}/clarifications", response_model=views.RequestDetail, response_model_exclude_unset=True)
def answer_clarifications(ident: str, body: ResidentAnswers, user: User, conn: Conn):
    allow(user, "RESIDENT")
    row = find(conn, user, ident, True)
    d = row.data
    intake = d.get("intake", {})
    require(intake.get("pending", False) and d["triage"]["state"] != "APPROVED")
    if len(body.answers) != len(intake["questions"]):
        raise invalid("Ответьте на все вопросы или укажите, что ответ неизвестен")
    intake.update(pending=False, answers=body.answers)
    state(d["triage"], "PROCESSING")
    enqueue(conn, row.id, "triage", "", d["triage"])
    save(conn, row)
    return summary(row, True, True)
