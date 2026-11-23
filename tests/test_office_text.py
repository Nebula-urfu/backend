import io
import shutil
from zipfile import ZipFile

import pytest
from docx import Document
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import ai, requests
from app.office_text import OFFICE_MIME, extract, verify_office
from test_admin import login


def docx_bytes():
    document = Document()
    document.add_paragraph("Условия закупки")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Цена"
    table.rows[0].cells[1].text = "12345 RUB"
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def odt_bytes():
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("mimetype", OFFICE_MIME["odt"])
        archive.writestr("content.xml", """<office:document-content
          xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
          xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
          <office:body><office:text><text:p>Цена 12345 RUB</text:p></office:text></office:body>
        </office:document-content>""")
    return output.getvalue()


@pytest.mark.parametrize("ext, content", [
    ("docx", docx_bytes()), ("odt", odt_bytes()), ("txt", "Цена 12345 RUB".encode("cp1251")),
])
def test_extract_office_text(ext, content):
    verify_office(content, ext)
    text, images = extract(content, OFFICE_MIME[ext])
    assert "Цена" in text and "12345 RUB" in text
    assert not images


def test_scanned_pdf_renders_when_text_is_absent():
    assert shutil.which("pdftoppm"), "Install poppler-utils through nix develop"
    output = io.BytesIO()
    Image.new("RGB", (150, 200), "white").save(output, "PDF")
    content = output.getvalue()
    verify_office(content, "pdf")
    text, images = extract(content, OFFICE_MIME["pdf"])
    assert not text
    assert len(images) == 1 and images[0].startswith("data:image/jpeg;base64,")


def test_template_import_returns_draft_without_persisting_it(monkeypatch):
    monkeypatch.setattr(requests, "AI_ENABLED", True)

    def draft(label, variables, inputs, metrics):
        assert "условия" in label
        assert "price" in variables
        assert "12345 RUB" in inputs[0]["text"]
        return {"text": "Сумма договора: {{price}}.", "subject": None}

    monkeypatch.setattr(ai, "import_template_draft", draft)
    admin, staff = login("admin"), login("staff")
    url = "/api/v1/admin/templates/import"

    def post(client):
        return client.post(url, data={"kind": "contract"},
                           files={"file": ("template.odt", odt_bytes(), OFFICE_MIME["odt"])})

    initial = admin.get("/api/v1/admin/templates").json()
    assert post(staff).status_code == 403
    response = post(admin)
    assert response.status_code == 200, response.text
    assert response.json() == {"fields": {"contractTerms": "Сумма договора: {{price}}."}}
    assert admin.get("/api/v1/admin/templates").json() == initial
    uploaded = staff.post("/api/v1/files", files={"file": ("offer.odt", odt_bytes(), OFFICE_MIME["odt"])})
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["mediaType"] == OFFICE_MIME["odt"]


def test_pdf_text_layer_does_not_require_ocr():
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 13 Tf 45 790 Td (Commercial offer. Vendor: Test Company. Total price: 12345 RUB.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    text, images = extract(output.getvalue(), OFFICE_MIME["pdf"], image_budget=0)
    assert "12345 RUB" in text and images == []
