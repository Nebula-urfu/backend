import io
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse, Response
from openpyxl import Workbook
from PIL import Image
from sqlalchemy import select

from .views import FileRef
from .auth import Conn, User, allow
from .db import STORAGE, files, uid
from .errors import invalid, missing, require, version as check_version
from .requests import find

router = APIRouter(tags=["files"])
MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "pdf": "application/pdf", "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_BYTES = 10 * 1024 * 1024


def store(conn, owner, name, media_type, content, request_id=None, purpose=None, version=None):
    ident = uid()
    STORAGE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STORAGE / ident).open("xb") as handle:
        handle.write(content)
    data = dict(fileId=ident, name=name, mediaType=media_type, sizeBytes=len(content))
    conn.execute(files.insert().values(id=ident, owner=owner, request_id=request_id,
                                      purpose=purpose, version=version, data=data))
    return data


def verify(data, ext):
    if ext in ("jpeg", "jpg", "png", "webp"):
        with Image.open(io.BytesIO(data), formats=[{"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}[ext]]) as image:
            if image.width * image.height > 25_000_000:
                raise ValueError()
            image.verify()
    elif ext == "pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError()
    elif ext == "txt":
        if "\x00" in data.decode("utf-8"):
            raise ValueError()
    else:
        with ZipFile(io.BytesIO(data)) as archive:
            required = "word/document.xml" if ext == "docx" else "xl/workbook.xml"
            entries = archive.infolist()
            if len(entries) > 500 or sum(item.file_size for item in entries) > 40_000_000 or required not in archive.namelist():
                raise ValueError()


@router.post("/files", status_code=201, response_model=FileRef)
def upload(user: User, conn: Conn, file: UploadFile = File()):
    allow(user, "RESIDENT", "STAFF")
    name = re.sub(r"[\x00-\x1f\x7f]", "", (file.filename or "").replace("\\", "/").split("/")[-1])
    ext = Path(name).suffix.lower().lstrip(".")
    accepted = {"jpg", "jpeg", "png", "webp"} if user.role == "RESIDENT" else set(MIME)
    if not name or len(name) > 180 or ext not in accepted:
        raise invalid("Неподдерживаемый формат файла")
    data = file.file.read(MAX_BYTES + 1)
    if not 0 < len(data) <= MAX_BYTES:
        raise invalid("Размер файла должен быть от 1 байта до 10 МиБ")
    try:
        verify(data, ext)
    except Exception as exc:
        raise invalid("Содержимое файла не соответствует формату") from exc
    return store(conn, user.id, name, MIME[ext], data)


@router.get("/files/{ident}/download", response_class=FileResponse)
def download(ident: str, user: User, conn: Conn):
    row = conn.execute(select(files).where(files.c.id == ident)).first()
    if not row:
        raise missing()
    if row.request_id:
        req = find(conn, user, row.request_id)
        if user.role == "RESIDENT" and row.purpose != "photo":
            raise missing()
        if row.purpose == "package":
            package = req.data["package"]
            if user.role == "STAFF" and (package["version"] != row.version or package["state"] != "APPROVED"):
                raise missing()
    elif row.owner != user.id:
        raise missing()
    d = row.data
    return FileResponse(STORAGE / row.id, media_type=d["mediaType"], filename=d["name"],
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


def docx(title, paragraphs):
    doc = Document()
    doc.add_heading(title, 0)
    doc.add_paragraph("Шаблон пилота. Требует проверки перед использованием.")
    for paragraph in paragraphs:
        doc.add_paragraph(paragraph)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def xlsx(title, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for values in rows:
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def format_amount(price):
    return f'{price["amount"]} {price["currency"]}'


def generate(conn, user, row, version, winner=None):
    d = row.data
    scope = f'{d["address"]}, подъезд {d["entrance"]}. Заявка {row.id}. ТЗ v{d["tz"]["version"]}.'
    contents = [
        ("TZ", "Техническое задание.docx", docx("Техническое задание", [
            scope, *(f'{s["title"]}: {s["text"]}' for s in d["tz"]["sections"]),
            *(f'{q["text"]}: {q["answer"] or "Без ответа"}' for q in d["tz"]["openQuestions"]),
        ])),
        ("QUOTE_FORM", "Форма КП.xlsx", xlsx("КП", [
            ["Шаблон пилота"], [scope], ["Объём работ по утверждённому ТЗ"],
            *[[s["title"], s["text"]] for s in d["tz"]["sections"]],
            ["Подрядчик", "Цена, RUB", "Срок, дней", "Гарантия"],
        ])),
    ]
    if winner:
        quote = winner["data"]
        nmc = d["nmc"]
        contents += [
            ("CONTRACT", "Проект договора.docx", docx("Проект договора", [
                scope, f'Подрядчик: {quote["contractorName"]}', f'Цена: {format_amount(quote["totalPrice"])}',
                f'Срок, дней: {quote.get("termsDays") or "уточнить"}',
                f'Гарантия: {quote.get("warranty") or "уточнить"}',
                "Работы и приёмка по приложенному техническому заданию. Реквизиты и условия оплаты подлежат заполнению.",
            ])),
            ("EVALUATION_PROTOCOL", "Протокол оценки.xlsx", xlsx("Оценка", [
                ["Шаблон пилота"], [scope], [f'НМЦ v{nmc["version"]}', format_amount(nmc["amount"]), nmc["justification"]],
                ["Подрядчик", "Цена", "Срок", "Гарантия", "Решение"],
                *[[q["data"]["contractorName"], format_amount(q["data"]["totalPrice"]), q["data"].get("termsDays"),
                   q["data"].get("warranty"), "Выбран сотрудником" if q["id"] == winner["id"] else ""]
                  for q in (d["quotes"][i] for i in nmc["quoteIds"])],
                ["Комментарий", d["tender"]["selectedWinner"]["comment"]],
            ])),
        ]
    purpose = "package" if winner else "draft"
    refs = [
        store(conn, user.id, name, MIME[name.rsplit(".", 1)[1]], content, row.id, purpose, version)
        for _, name, content in contents
    ]
    if not winner:
        return refs
    return [{"kind": kind, "fileId": ref["fileId"], "name": name, "version": version}
            for (kind, name, _), ref in zip(contents, refs)]


@router.get("/requests/{ident}/package/zip")
def package_zip(ident: str, user: User, conn: Conn, version: int | None = None):
    allow(user, "STAFF", "REVIEWER")
    package = find(conn, user, ident).data["package"]
    require(package["state"] == "APPROVED")
    if version is not None:
        check_version(package["version"], version)
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        for doc in package["documents"]:
            archive.write(STORAGE / doc["fileId"], doc["name"])
    return Response(out.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="procurement.zip"', "Cache-Control": "private, no-store"})
