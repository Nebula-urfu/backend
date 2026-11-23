"""Bounded extraction of text and scanned pages from uploaded documents."""

import base64
import io
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import ZipFile

from docx import Document
from docx.table import Table
from pypdf import PdfReader

OFFICE_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "odt": "application/vnd.oasis.opendocument.text",
    "txt": "text/plain",
}
MAX_BYTES = 10 * 1024 * 1024
MAX_TEXT = 32000
MAX_PDF_PAGES = 20
MAX_SCAN_PAGES = 6
OLE_HEADER = bytes.fromhex("D0CF11E0A1B11AE1")
ODT_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


def decode_text(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def check_zip(data, required):
    with ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 500 or sum(item.file_size for item in entries) > 40_000_000 or required not in archive.namelist():
            raise ValueError("Invalid document container")
        if required == "content.xml":
            return archive.read(required)


def verify_office(data, ext):
    if ext == "pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF header")
    if ext == "doc" and not data.startswith(OLE_HEADER):
        raise ValueError("Invalid Word DOC header")
    if ext == "docx":
        check_zip(data, "word/document.xml")
    if ext == "odt":
        check_zip(data, "content.xml")
        with ZipFile(io.BytesIO(data)) as archive:
            if "mimetype" not in archive.namelist() or archive.read("mimetype") != OFFICE_MIME["odt"].encode():
                raise ValueError("Invalid OpenDocument media type")
    if ext == "txt" and any(ord(c) < 32 and c not in "\t\n\r\f" for c in decode_text(data)):
        raise ValueError("Unexpected control characters in text")


def read_docx(data):
    doc = Document(io.BytesIO(data))
    for part in doc.iter_inner_content():
        if isinstance(part, Table):
            for row in part.rows:
                yield " | ".join(cell.text for cell in row.cells)
        else:
            yield part.text


def read_odt(data):
    xml = check_zip(data, "content.xml")
    if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("Unsupported OpenDocument XML declaration")
    for node in ElementTree.fromstring(xml).iter():
        if node.tag in (ODT_TEXT + "p", ODT_TEXT + "h"):
            yield "".join(node.itertext())


def read_doc(data):
    with TemporaryDirectory() as directory:
        source = Path(directory) / "document.doc"
        source.write_bytes(data)
        result = subprocess.run(["catdoc", "-d", "utf-8", str(source)], stdin=subprocess.DEVNULL,
                                capture_output=True, check=True, timeout=8)
        return result.stdout[:MAX_TEXT * 4].decode("utf-8", errors="replace")


def render_page(source, target, page):
    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-scale-to", "1600",
                    "-singlefile", "-jpeg", str(source), str(target)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=8)
    return target.with_suffix(".jpg")


def scan_text(path):
    try:
        process = subprocess.run(["tesseract", str(path), "stdout", "-l", "rus+eng", "--psm", "6"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={**os.environ, "OMP_THREAD_LIMIT": "1"}, check=True, timeout=10)
        return process.stdout[:MAX_TEXT * 4].decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def image_url(path):
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def read_pdf(data, image_budget):
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted or len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError("Encrypted or too many PDF pages")
    text, scans = [], []
    for number, page in enumerate(reader.pages, start=1):
        content = page.get_contents()
        extracted = (page.extract_text() or "").strip() if content is not None and len(content.get_data()) <= 2_000_000 else ""
        if extracted:
            text.append(f"[{number}]\n{extracted[:8000]}")
        if len(extracted) < 40:
            scans.append(number)
    if len(scans) > image_budget:
        raise ValueError("Too many scanned pages for this import")
    images = []
    if scans:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "document.pdf"
            source.write_bytes(data)
            for number in scans:
                image = render_page(source, Path(directory) / f"page-{number}", number)
                recognized = scan_text(image)
                if recognized:
                    text.append(f"[{number}, OCR]\n{recognized[:8000]}")
                if len(recognized) < 40:
                    images.append(image_url(image))
    return "\n\n".join(text)[:MAX_TEXT], images


def extract(data, media_type, image_budget=MAX_SCAN_PAGES):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Unsupported document size")
    if media_type == OFFICE_MIME["pdf"]:
        return read_pdf(data, image_budget)
    if media_type == OFFICE_MIME["docx"]:
        return "\n".join(read_docx(data))[:MAX_TEXT], []
    if media_type == OFFICE_MIME["odt"]:
        return "\n".join(read_odt(data))[:MAX_TEXT], []
    if media_type == OFFICE_MIME["doc"]:
        return read_doc(data)[:MAX_TEXT], []
    if media_type == OFFICE_MIME["txt"]:
        return decode_text(data)[:MAX_TEXT], []
    return "", []
