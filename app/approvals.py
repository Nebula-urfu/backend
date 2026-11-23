from copy import deepcopy
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import select, update

from .auth import Conn, User, allow
from .db import approvals, stamp, uid
from .errors import invalid, missing, require, version
from .requests import actor, find, page, save, state
from .schemas import DecisionInput, VersionInput
from . import views

router = APIRouter(tags=["approvals"])
KINDS = {"tz": "TZ", "nmc": "NMC", "package": "PACKAGE"}


@router.post("/requests/{ident}/{kind}/submit", status_code=201, response_model=views.Approval)
def submit(ident: str, kind: Literal["tz", "nmc", "package"], body: VersionInput, user: User, conn: Conn):
    allow(user, "STAFF")
    row = find(conn, user, ident, True)
    doc = row.data[kind]
    version(doc["version"], body.version)
    require(doc["state"] == "DRAFT_READY")
    if kind == "nmc":
        require("amount" in doc)
    if kind == "tz":
        if any(s["text"] == "Уточнить сотруднику УК" for s in doc["sections"]):
            raise invalid("Заполните все разделы ТЗ")
    state(doc, "IN_REVIEW")
    triage = row.data["triage"]["confirmed"]
    approval = dict(id=uid(), requestId=ident, subjectType=KINDS[kind], subjectVersion=body.version,
                    state="PENDING", urgency=triage["urgency"], address=row.data["address"],
                    submittedBy=actor(user), submittedAt=stamp(), subject=deepcopy(doc))
    conn.execute(approvals.insert().values(id=approval["id"], request_id=ident, org=user.org,
                                          kind=kind, version=body.version, data=approval))
    save(conn, row)
    return without_subject(approval)


def without_subject(doc):
    return {k: v for k, v in doc.items() if k != "subject"}


def query(user, conn, request_id=None):
    q = select(approvals).where(approvals.c.org == user.org)
    if request_id:
        q = q.where(approvals.c.request_id == request_id)
    return [row.data for row in conn.execute(q.order_by(approvals.c.data["submittedAt"].astext.desc()))]


def journal(items):
    return [dict(id=a["id"], requestId=a["requestId"], subjectType=a["subjectType"],
                 subjectVersion=a["subjectVersion"], decision=a["state"],
                 decidedBy=a["decidedBy"], decidedAt=a["decidedAt"], comment=a.get("comment"))
            for a in items if a["state"] != "PENDING"]


@router.get("/approvals", response_model=views.Page[views.Approval])
def queue(user: User, conn: Conn, state: str = "PENDING", limit: int = Query(100, ge=1, le=100)):
    allow(user, "REVIEWER")
    return page([without_subject(a) for a in query(user, conn) if a["state"] == state][:limit])


def approval_row(ident, user, conn, lock=False):
    q = select(approvals).where(approvals.c.id == ident, approvals.c.org == user.org)
    row = conn.execute(q.with_for_update() if lock else q).first()
    if not row:
        raise missing()
    return row


@router.get("/approvals/{ident}", response_model=views.ApprovalDetail, response_model_exclude_unset=True)
def detail(ident: str, user: User, conn: Conn):
    allow(user, "REVIEWER")
    row = approval_row(ident, user, conn)
    return dict(row.data, previousDecisions=journal([
        a for a in query(user, conn, row.request_id) if a["id"] != ident
    ]))


@router.post("/approvals/{ident}/{action}", response_model=views.Approval)
def decide(ident: str, action: Literal["approve", "reject"], body: DecisionInput, user: User, conn: Conn):
    allow(user, "REVIEWER")
    approval = approval_row(ident, user, conn, True)
    row = find(conn, user, approval.request_id, True)
    version(approval.version, body.subjectVersion)
    doc = row.data[approval.kind]
    version(doc["version"], body.subjectVersion)
    require(approval.data["state"] == "PENDING" and doc["state"] == "IN_REVIEW")
    comment = body.comment.strip()
    if action == "reject" and not comment:
        raise invalid("Укажите причину возврата")
    passed = action == "approve"
    state(doc, "APPROVED" if passed else "NEEDS_REWORK")
    if not passed:
        doc["lastReviewComment"] = comment
    if approval.kind == "nmc" and passed:
        state(row.data["tender"], "DRAFT_READY")
    if approval.kind == "package":
        state(row.data["tender"], "APPROVED" if passed else "NEEDS_REWORK")
        if passed:
            doc["approvedAt"] = stamp()
            row.data["status"] = "COMPLETED"
    approval.data.update(state="APPROVED" if passed else "REJECTED", decidedBy=actor(user), decidedAt=stamp(), comment=comment or None)
    conn.execute(update(approvals).where(approvals.c.id == ident).values(data=approval.data))
    save(conn, row)
    return without_subject(approval.data)


@router.get("/approval-journal", response_model=views.Page[views.JournalEntry])
def list_journal(user: User, conn: Conn, requestId: str | None = None,
                 subjectType: str | None = None, limit: int = Query(100, ge=1, le=100)):
    allow(user, "STAFF", "REVIEWER")
    return page([a for a in journal(query(user, conn, requestId)) if not subjectType or a["subjectType"] == subjectType][:limit])
