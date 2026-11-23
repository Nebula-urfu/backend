
from fastapi import APIRouter
from sqlalchemy import select, update

from .auth import Conn, User, allow
from .db import contractors, files, stamp, uid
from .documents import generate
from .errors import invalid, missing, require, version
from .requests import AI_ENABLED, actor, attach, enqueue, find, page, public, read, save, state
from .schemas import CalculationInput, LetterInput, QuoteEdit, QuoteInput, Recipients, WinnerInput
from . import views
from .workflow import MIN_QUOTES, advance, calculate_amount, complete_tz

router = APIRouter(tags=["procurement"])


def directory(conn, user):
    return {row.id: row.data for row in conn.execute(select(contractors).where(contractors.c.org == user.org))}


@router.get("/contractors", response_model=views.Page[views.Contractor])
def list_contractors(user: User, conn: Conn):
    allow(user, "STAFF", "REVIEWER")
    return page(sorted(directory(conn, user).values(), key=lambda c: c["name"]))


def staff_request(ident, user, conn):
    allow(user, "STAFF")
    return find(conn, user, ident, True)


@router.put("/requests/{ident}/quote-letter", response_model=views.Tz, response_model_exclude_unset=True)
def edit_letter(ident: str, body: LetterInput, user: User, conn: Conn):
    row = staff_request(ident, user, conn)
    tz = row.data["tz"]
    version(tz["version"], body.baseVersion)
    require(tz["state"] in ("DRAFT_READY", "NEEDS_REWORK") or (row.data.get("automatic") and tz["state"] == "APPROVED"))
    if any(i not in directory(conn, user) for i in body.contractorIds):
        raise missing()
    letter = body.model_dump(exclude={"baseVersion"})
    tz["letter"] = letter
    if row.data.get("automatic") and row.data.get("intake"):
        row.data["intake"]["pending"] = False
    tz["version"] += 1
    state(tz, "DRAFT_READY")
    advance(conn, row)
    save(conn, row)
    return tz

@router.post("/requests/{ident}/quote-requests", status_code=201, response_model=views.DraftRequest)
def quote_request(ident: str, body: Recipients, user: User, conn: Conn):
    row = staff_request(ident, user, conn)
    require(row.data["tz"]["state"] == "APPROVED")
    records = directory(conn, user)
    if any(i not in records for i in body.contractorIds):
        raise missing()
    letter = row.data["tz"].get("letter") or {}
    return dict(id=uid(), recipients=[
        {"contractorId": i, "name": records[i]["name"], "email": records[i]["email"]} for i in dict.fromkeys(body.contractorIds)
    ], subject=letter.get("subject", "Запрос коммерческого предложения"),
        body=letter.get("body", "Просим указать полную стоимость работ по приложенному ТЗ, сроки и гарантию."),
        attachments=generate(conn, user, row, row.data["tz"]["version"]))


def editable(row):
    if row.data.get("automatic"):
        require(row.data["tz"]["state"] == "APPROVED" and complete_tz(row.data["tz"]))
        return row.data["quotes"]
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
    d = find(conn, user, ident).data
    return page([dict(public(q), staleScope=bool(d.get("automatic") and
                      q.get("scopeRevision", 0) != d.get("scopeRevision", 0))) for q in d["quotes"].values()])


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
    quote["scopeRevision"] = row.data.get("scopeRevision", 0)
    quotes[quote["id"]] = quote
    if row.data.get("automatic") and row.data.get("intake"):
        row.data["intake"]["pending"] = False
    if AI_ENABLED:
        enqueue(conn, row.id, "quote", quote["id"], quote)
    if row.data.get("automatic"):
        advance(conn, row)
    else:
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
    if row.data.get("automatic"):
        advance(conn, row)
    else:
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
    quote["scopeRevision"] = row.data.get("scopeRevision", 0)
    if row.data.get("automatic"):
        advance(conn, row)
    else:
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
    if row.data.get("automatic"):
        raise invalid("Для этой заявки НМЦ рассчитывается автоматически")
    ids = body.quoteIds if body.quoteIds is not None else [i for i, q in quotes.items() if q.get("editedByStaff")]
    if len(set(ids)) != len(ids) or any(i not in quotes or not quotes[i].get("editedByStaff") for i in ids):
        raise invalid("Проверьте поля выбранных КП")
    companies = {quotes[i]["data"]["contractorName"].casefold() for i in ids}
    known_contractors = [quotes[i]["contractorId"] for i in ids if quotes[i]["contractorId"]]
    if len(companies) != len(ids) or len(set(known_contractors)) != len(known_contractors) or len(ids) < MIN_QUOTES:
        raise invalid(f"Нужно не менее {MIN_QUOTES} проверенных КП от разных подрядчиков")
    doc = calculate_amount(row.data["nmc"], quotes, ids)
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
    if d.get("automatic"):
        require(package["state"] in ("IN_REVIEW", "APPROVED", "NEEDS_REWORK"))
        advance(conn, row, preferred=body.quoteId, selected_by=actor(user), comment=body.comment)
        save(conn, row)
        return d["tender"]
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
