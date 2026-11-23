"""Derive and version the procurement draft from the current request inputs."""
import os
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

from sqlalchemy import select, update

from .db import approvals, contractors, stamp, uid
from .errors import invalid
from .logging_config import event
from .requests import working_doc, state

MIN_QUOTES = int(os.getenv("MIN_QUOTES", "3"))
if not 1 <= MIN_QUOTES <= 50:
    raise ValueError("MIN_QUOTES must be between 1 and 50")

SYSTEM_ACTOR = {"userId": "automation", "displayName": "Автоматическая подготовка"}


def complete_tz(doc):
    return bool(doc.get("sections")) and all(s["text"] != "Уточнить сотруднику УК" for s in doc["sections"])


def usable(quote, revision):
    fields = quote.get("data") or {}
    return (quote.get("scopeRevision", 0) == revision
            and quote["extraction"] == "EXTRACTED" and fields.get("contractorName") and fields.get("totalPrice"))


def calculate_amount(doc, quotes, ids, revision=None):
    values = [Decimal(quotes[i]["data"]["totalPrice"]["amount"]) for i in ids]
    amount = (sum(values) / len(values)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    doc.update(version=doc["version"] + 1, amount=dict(amount=str(amount), currency="RUB"),
               method="average_of_quotes", quoteIds=ids, quoteVersions={i: quotes[i]["version"] for i in ids},
               justification=f"Среднее арифметическое {len(values)} полных цен в RUB по одному ТЗ. "
                             "Сопоставимость объёма, условий и налогов проверяет сотрудник. "
                             "Округление до копейки, половина вверх. Цены: " + ", ".join(map(str, values)),
               calculatedAt=stamp())
    if revision is not None:
        doc["scopeRevision"] = revision
    state(doc, "DRAFT_READY")
    return doc


def invalidate_review(conn, row, comment):
    for approval in conn.execute(select(approvals).where(
        approvals.c.request_id == row.id, approvals.c.data["state"].astext == "PENDING"
    )).all():
        approval.data.update(state="SUPERSEDED", decidedBy=SYSTEM_ACTOR, decidedAt=stamp(), comment=comment)
        conn.execute(update(approvals).where(approvals.c.id == approval.id).values(data=approval.data))


def reset_document(doc, **fields):
    version = doc.get("version", 0)
    comment = doc.get("lastReviewComment")
    doc.clear()
    doc.update(working_doc(), version=version, **fields)
    if comment:
        doc["lastReviewComment"] = comment
    state(doc, "NOT_STARTED")


def advance(conn, row, preferred=None, selected_by=None, comment=""):
    """Rebuild the candidate documents and put the new version in the review queue."""
    from .documents import generate

    d = row.data
    if not d.get("automatic"):
        return
    invalidate_review(conn, row, "Исходные данные изменились. Нужна проверка актуальной версии.")
    d["status"] = "IN_PROGRESS" if d["triage"].get("confirmed") else "ACCEPTED"
    nmc, tender, package = (d[k] for k in ("nmc", "tender", "package"))
    reset_document(nmc)
    reset_document(tender, documents=[], winnerProposal=None, selectedWinner=None, contract=None)
    reset_document(package, documents=[])

    tz = d["tz"]
    if d["triage"]["state"] != "APPROVED" or not complete_tz(tz) or tz["state"] == "PROCESSING":
        d["waitingFor"] = "TZ"
        event("workflow.waiting", procurement_id=row.id, stage="TZ", triage_state=d["triage"]["state"], tz_state=tz["state"])
        return
    tz["triageSummary"] = d["triage"]["confirmed"].copy()
    state(tz, "APPROVED")
    revision = d.get("scopeRevision", 0)
    quotes = d["quotes"]
    current = [q for q in quotes.values() if q.get("scopeRevision", 0) == revision]
    if any(q["extraction"] == "EXTRACTING" for q in current):
        d["waitingFor"] = "EXTRACTION"
        event("workflow.waiting", procurement_id=row.id, stage="EXTRACTION", quote_count=len(current), scope_revision=revision)
        state(nmc, "PROCESSING")
        return

    ids = []
    companies = set()
    known_contractors = set()
    for quote in sorted(current, key=lambda q: (q.get("createdAt", ""), q["id"]), reverse=True):
        if not usable(quote, revision):
            continue
        name = " ".join(quote["data"]["contractorName"].casefold().split())
        contractor_id = quote["contractorId"]
        if name in companies or contractor_id in known_contractors:
            continue
        companies.add(name)
        if contractor_id:
            known_contractors.add(contractor_id)
        ids.append(quote["id"])
    if len(ids) < MIN_QUOTES:
        d["waitingFor"] = "QUOTES"
        event("workflow.waiting", procurement_id=row.id, stage="QUOTES", usable_quotes=len(ids), required_quotes=MIN_QUOTES,
              stale_quotes=len(quotes) - len(current), scope_revision=revision)
        return
    if preferred is not None and preferred not in ids:
        raise invalid("Выберите подрядчика из актуального расчёта")

    calculate_amount(nmc, quotes, ids, revision)
    state(nmc, "APPROVED")
    ranked = sorted(ids, key=lambda i: (
        Decimal(quotes[i]["data"]["totalPrice"]["amount"]),
        quotes[i]["data"].get("termsDays") if quotes[i]["data"].get("termsDays") is not None else float("inf"),
        quotes[i]["data"]["contractorName"].casefold(), i,
    ))
    rationale = "Рекомендация по самой низкой цене. При равной цене приоритет у предложения с меньшим указанным сроком."
    winner = quotes[preferred or ranked[0]]
    tender["winnerProposal"] = {"quoteId": ranked[0], "rationale": rationale}
    tender["selectedWinner"] = dict(quoteId=winner["id"], selectedBy=selected_by or SYSTEM_ACTOR,
                                    selectedAt=stamp(), comment=comment or rationale)
    contractor = conn.execute(select(contractors.c.data).where(
        contractors.c.id == winner["contractorId"], contractors.c.org == row.org
    )).scalar_one_or_none() or {}
    tender["contract"] = dict(type="NON_STANDARD" if contractor.get("nonstandardContract") else "STANDARD")
    state(tender, "APPROVED")
    package["version"] += 1
    package["documents"] = generate(conn, SimpleNamespace(id=row.author), row, package["version"], winner)
    package.update(tzSnapshot=tz.copy(), nmcSnapshot=nmc.copy(), tenderSnapshot=tender.copy())
    state(package, "IN_REVIEW")
    d["waitingFor"] = "FINAL_REVIEW"
    approval = dict(id=uid(), requestId=row.id, subjectType="PACKAGE", subjectVersion=package["version"],
                    state="PENDING", urgency=d["triage"]["confirmed"]["urgency"], address=d["address"],
                    submittedBy=SYSTEM_ACTOR, submittedAt=stamp(), subject=package.copy())
    conn.execute(approvals.insert().values(id=approval["id"], request_id=row.id, org=row.org,
                                          kind="package", version=package["version"], data=approval))
    event("workflow.package_ready", procurement_id=row.id, approval_id=approval["id"], package_version=package["version"],
          tz_version=tz["version"], nmc_version=nmc["version"], quote_count=len(ids), winner_quote_id=winner["id"])


def scope_changed(row, refresh_letter=True):
    d = row.data
    d["scopeRevision"] = d.get("scopeRevision", 0) + 1
    event("workflow.scope_changed", procurement_id=row.id, scope_revision=d["scopeRevision"])
    tz = d["tz"]
    if refresh_letter and tz.get("letter") and complete_tz(tz):
        draft = tz["letter"]
        issue = tz["sections"][0]["text"]
        draft["body"] = (f"Просим подготовить коммерческое предложение по приложенному ТЗ.\n\n"
                         f"Предмет работ: {issue}\n\nУкажите полную стоимость, сроки и условия гарантии.")[:6000]
