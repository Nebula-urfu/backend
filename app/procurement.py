import os
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter
from sqlalchemy import select, update

from .auth import Conn, User, allow
from .db import contractors, files, stamp, uid
from .documents import generate
from .errors import invalid, missing, require, version
from .requests import AI_ENABLED, actor, attach, enqueue, find, page, public, read, save, state
from .schemas import CalculationInput, QuoteEdit, QuoteInput, Recipients, WinnerInput
from . import views

router = APIRouter(tags=["procurement"])
MIN_QUOTES = int(os.getenv("MIN_QUOTES", "3"))
if not 1 <= MIN_QUOTES <= 50:
    raise ValueError("MIN_QUOTES must be between 1 and 50")


def directory(conn, user):
    return {row.id: row.data for row in conn.execute(select(contractors).where(contractors.c.org == user.org))}


@router.get("/contractors", response_model=views.Page[views.Contractor])
def list_contractors(user: User, conn: Conn):
    allow(user, "STAFF")
    return page(sorted(directory(conn, user).values(), key=lambda c: c["name"]))


def staff_request(ident, user, conn):
    allow(user, "STAFF")
    return find(conn, user, ident, True)


@router.post("/requests/{ident}/quote-requests", status_code=201, response_model=views.DraftRequest)
def quote_request(ident: str, body: Recipients, user: User, conn: Conn):
    row = staff_request(ident, user, conn)
    require(row.data["tz"]["state"] == "APPROVED")
    records = directory(conn, user)
    if any(i not in records for i in body.contractorIds):
        raise missing()
    return dict(id=uid(), recipients=[
        {"contractorId": i, "name": records[i]["name"], "email": records[i]["email"]} for i in dict.fromkeys(body.contractorIds)
    ], subject="Запрос коммерческого предложения",
        body="Просим указать полную стоимость работ по приложенному ТЗ, сроки и гарантию.",
        attachments=generate(conn, user, row, row.data["tz"]["version"]))


def editable(row):
    require(row.data["tz"]["state"] == "APPROVED" and row.data["nmc"]["state"] not in ("IN_REVIEW", "APPROVED"))
    return row.data["quotes"]


def invalidate(row):
    nmc = row.data["nmc"]
    for key in ("amount", "method", "justification", "quoteIds", "quoteVersions"):
        nmc.pop(key, None)
    state(nmc, "DRAFT_READY")


@router.get("/requests/{ident}/quotes", response_model=views.Page[views.Quote])
def list_quotes(ident: str, user: User, conn: Conn):
    allow(user, "STAFF", "REVIEWER")
    return page([public(q) for q in find(conn, user, ident).data["quotes"].values()])


@router.post("/requests/{ident}/quotes", status_code=202, response_model=views.Quote)
def add_quote(ident: str, body: QuoteInput, user: User, conn: Conn):
    row = staff_request(ident, user, conn)
    quotes = editable(row)
    require(len(quotes) < 50)
    if body.contractorId and body.contractorId not in directory(conn, user):
        raise missing()
    attachments = attach(conn, user, row.id, body.fileIds, "quote")
    quote = dict(id=uid(), contractorId=body.contractorId, extraction="EXTRACTING" if AI_ENABLED else "AI_FAILED",
                 files=attachments, version=1, data=None, createdAt=stamp())
    quotes[quote["id"]] = quote
    if AI_ENABLED:
        enqueue(conn, row.id, "quote", quote["id"], quote)
    invalidate(row)
    save(conn, row)
    return public(quote)


@router.delete("/requests/{ident}/quotes/{quote_id}", status_code=204)
def delete_quote(ident: str, quote_id: str, user: User, conn: Conn):
    row = staff_request(ident, user, conn)
    quote = editable(row).pop(quote_id, None)
    if not quote:
        raise missing()
    conn.execute(update(files).where(files.c.id.in_([f["fileId"] for f in quote["files"]])).values(purpose="archived_quote"))
    invalidate(row)
    save(conn, row)


@router.put("/requests/{ident}/quotes/{quote_id}/data", response_model=views.Quote)
def edit_quote(ident: str, quote_id: str, body: QuoteEdit, user: User, conn: Conn):
    row = staff_request(ident, user, conn)
    quote = editable(row).get(quote_id)
    if not quote:
        raise missing()
    version(quote["version"], body.baseVersion)
    if not body.data.contractorName or not body.data.totalPrice:
        raise invalid("Укажите подрядчика и полную сопоставимую цену")
    quote.update(data=body.data.model_dump(mode="json"), version=quote["version"] + 1,
                 extraction="EXTRACTED", editedByStaff=True)
    invalidate(row)
    save(conn, row)
    return public(quote)


@router.get("/requests/{ident}/quotes/comparison", response_model=views.Comparison)
def comparison(ident: str, user: User, conn: Conn):
    allow(user, "STAFF", "REVIEWER")
    quotes = [q for q in find(conn, user, ident).data["quotes"].values() if q["data"]]
    items = {q["id"]: {item["name"]: item for item in q["data"].get("items", [])} for q in quotes}
    rows = []
    discrepancies = []
    for name in sorted(set().union(*(mapping for mapping in items.values()))):
        base = next(mapping[name] for mapping in items.values() if name in mapping)
        cells = []
        for q in quotes:
            item = items[q["id"]].get(name)
            flag = "MISSING" if not item else "MISMATCH" if (item["unit"], item["quantity"]) != (base["unit"], base["quantity"]) else "NONE"
            if flag != "NONE":
                discrepancies.append(f'{flag}: {name} — {q["data"]["contractorName"]}')
            cells.append(dict(quoteId=q["id"], flag=flag, **{k: item[k] for k in ("unitPrice", "price") if item and item[k]}))
        rows.append(dict(name=name, unit=base["unit"], quantity=base["quantity"], cells=cells))
    return dict(quotes=[dict(quoteId=q["id"], **{k: q["data"][k] for k in
        ("contractorName", "totalPrice", "termsDays", "warranty")}) for q in quotes], rows=rows, discrepancies=discrepancies)


@router.get("/requests/{ident}/nmc", response_model=views.Nmc, response_model_exclude_unset=True)
def nmc(ident: str, user: User, conn: Conn):
    return read(ident, user, conn, "nmc")


@router.post("/requests/{ident}/nmc/calculate", response_model=views.Nmc, response_model_exclude_unset=True)
def calculate(ident: str, body: CalculationInput, user: User, conn: Conn):
    row = staff_request(ident, user, conn)
    quotes = editable(row)
    ids = body.quoteIds if body.quoteIds is not None else [i for i, q in quotes.items() if q.get("editedByStaff")]
    if len(set(ids)) != len(ids) or any(i not in quotes or not quotes[i].get("editedByStaff") for i in ids):
        raise invalid("Проверьте поля выбранных КП")
    companies = {quotes[i]["data"]["contractorName"].casefold() for i in ids}
    known_contractors = [quotes[i]["contractorId"] for i in ids if quotes[i]["contractorId"]]
    if len(companies) != len(ids) or len(set(known_contractors)) != len(known_contractors) or len(ids) < MIN_QUOTES:
        raise invalid(f"Нужно не менее {MIN_QUOTES} проверенных КП от разных подрядчиков")
    values = [Decimal(quotes[i]["data"]["totalPrice"]["amount"]) for i in ids]
    amount = (sum(values) / len(values)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    doc = row.data["nmc"]
    doc.update(version=doc["version"] + 1, amount=dict(amount=str(amount), currency="RUB"),
               method="average_of_quotes", quoteIds=ids, quoteVersions={i: quotes[i]["version"] for i in ids},
               justification=f"Среднее арифметическое {len(values)} проверенных полных цен в RUB по одному ТЗ. "
                             "Сопоставимость объёма, условий и налогов проверяет сотрудник. "
                             "Округление до копейки, половина вверх. Цены: " + ", ".join(map(str, values)),
               calculatedAt=stamp())
    state(doc, "DRAFT_READY")
    save(conn, row)
    return doc


@router.get("/requests/{ident}/tender", response_model=views.Tender)
def tender(ident: str, user: User, conn: Conn):
    return read(ident, user, conn, "tender")


@router.put("/requests/{ident}/tender/winner", response_model=views.Tender)
def choose_winner(ident: str, body: WinnerInput, user: User, conn: Conn):
    row = staff_request(ident, user, conn)
    d = row.data
    package = d["package"]
    require(d["nmc"]["state"] == "APPROVED" and package["state"] not in ("IN_REVIEW", "APPROVED"))
    if body.quoteId not in d["nmc"]["quoteIds"]:
        raise invalid("Выберите предложение из утверждённого расчёта")
    winner = d["quotes"][body.quoteId]
    tender_doc = d["tender"]
    tender_doc["selectedWinner"] = dict(quoteId=body.quoteId, selectedBy=actor(user), selectedAt=stamp(), comment=body.comment)
    package["version"] += 1
    package["documents"] = generate(conn, user, row, package["version"], winner)
    contractor = directory(conn, user).get(winner["contractorId"], {})
    tender_doc["contract"] = dict(type="NON_STANDARD" if contractor.get("nonstandardContract") else "STANDARD")
    state(tender_doc, "DRAFT_READY")
    state(package, "DRAFT_READY")
    save(conn, row)
    return tender_doc


@router.get("/requests/{ident}/package", response_model=views.Package, response_model_exclude_unset=True)
def package(ident: str, user: User, conn: Conn):
    return read(ident, user, conn, "package")
