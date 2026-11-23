"""Run the manual scenario over HTTP with seeded accounts."""
import io
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from zipfile import ZipFile

import httpx
from docx import Document
from openpyxl import load_workbook
from PIL import Image

BASE = os.getenv("SMOKE_BASE", "http://127.0.0.1:8000") + "/api/v1"
PASSWORD = os.getenv("SMOKE_PASSWORD", "local-scenario-password")


def call(client, method, path, expected=200, **kwargs):
    r = client.request(method, path, **kwargs)
    assert r.status_code == expected, (method, path, expected, r.status_code, r.text[:500])
    return r


def login(name):
    client = httpx.Client(base_url=BASE, timeout=20)
    data = call(client, "POST", "/session/login", json={"username": name, "password": PASSWORD},
                headers={"X-Requested-With": "Nebula"}).json()
    client.headers["X-CSRF-Token"] = data["csrfToken"]
    return client


def run():
    resident, staff, reviewer, outsider, neighbor = [login(name) for name in ("resident", "staff", "reviewer", "outsider", "neighbor")]
    picture = io.BytesIO()
    Image.new("RGB", (12, 12), "green").save(picture, format="PNG")
    fid = call(resident, "POST", "/files", 201, files={"file": ("roof.png", picture.getvalue())}).json()["fileId"]
    file_path = f"/files/{fid}/download"
    call(staff, "GET", file_path, 404)
    body = dict(description="Протечка крыши", entrance="2", photoFileIds=[fid])
    headers = {"Idempotency-Key": str(uuid4())}
    ident = call(resident, "POST", "/requests", 201, json=body, headers=headers).json()["id"]
    assert call(resident, "POST", "/requests", 201, json=body, headers=headers).json()["id"] == ident
    call(resident, "POST", "/requests", 409, json={**body, "entrance": "3"}, headers=headers)
    p = f"/requests/{ident}"
    assert call(staff, "GET", file_path).content == picture.getvalue()
    for client in (outsider, neighbor):
        call(client, "GET", p, 404)
        call(client, "GET", file_path, 404)
        assert ident not in [r["id"] for r in call(client, "GET", "/requests").json()["items"]]
    triage = dict(urgency="EMERGENCY", workType="Ремонт крыши", route="EMERGENCY_CONTRACTOR_CALL")
    call(resident, "POST", p + "/triage/confirm", 403, json=triage)
    saved = staff.headers.pop("X-CSRF-Token")
    call(staff, "POST", p + "/triage/confirm", 403, json=triage)
    staff.headers["X-CSRF-Token"] = saved
    call(staff, "POST", p + "/triage/confirm", json=triage)
    assert call(staff, "GET", "/requests").json()["items"][0]["urgency"] == "EMERGENCY"
    tz = call(staff, "GET", p + "/tz").json()
    sections = [{"id": s["id"], "text": "Ремонт крыши, 30 м², за 10 дней с актом приёмки."} for s in tz["sections"]]
    tz = call(staff, "PUT", p + "/tz", json=dict(baseVersion=tz["version"], sections=sections)).json()

    def submit(kind, v):
        return "/approvals/" + call(staff, "POST", f"{p}/{kind}/submit", 201, json={"version": v}).json()["id"]

    approval = submit("tz", tz["version"])
    call(reviewer, "POST", approval + "/reject", 400, json={"subjectVersion": tz["version"], "comment": " "})
    call(reviewer, "POST", approval + "/reject", json={"subjectVersion": tz["version"], "comment": "Уточнить приёмку",
        "corrections": {"sections": [{"id": "acceptance", "text": "Проверить отсутствие протечек."}]}})
    tz = call(staff, "GET", p + "/tz").json()
    assert next(s for s in tz["sections"] if s["id"] == "acceptance")["text"] == "Проверить отсутствие протечек."
    tz = call(staff, "PUT", p + "/tz", json=dict(baseVersion=tz["version"], sections=sections)).json()
    call(reviewer, "POST", approval + "/approve", 409, json={"subjectVersion": tz["version"] - 1})
    approval = submit("tz", tz["version"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reviewer.post(approval + "/approve", json={"subjectVersion": tz["version"]}).status_code, range(2)))
    assert sorted(outcomes) == [200, 409], outcomes

    companies = call(staff, "GET", "/contractors").json()["items"]
    draft = call(staff, "POST", p + "/quote-requests", 201, json={"contractorIds": [c["id"] for c in companies]}).json()
    assert len(draft["attachments"]) == 2
    quotes = []
    for i, company in enumerate(companies):
        fid = call(staff, "POST", "/files", 201, files={"file": ("quote.txt", b"Offer")}).json()["fileId"]
        q = call(staff, "POST", p + "/quotes", 202, json={"fileIds": [fid], "contractorId": company["id"]}).json()
        quotes.append(q["id"])
        call(staff, "PUT", f"{p}/quotes/{q['id']}/data", json=dict(baseVersion=q["version"], data={
            "contractorName": company["name"], "totalPrice": {"amount": str(100 + i * 10), "currency": "RUB"},
            "termsDays": 10, "warranty": "1 год",
        }))
    assert len(call(staff, "GET", p + "/quotes/comparison").json()["quotes"]) == 3
    nmc = call(staff, "POST", p + "/nmc/calculate", json={}).json()
    assert nmc["amount"]["amount"] == "110.00"
    call(reviewer, "POST", submit("nmc", nmc["version"]) + "/approve", json={"subjectVersion": nmc["version"]})

    def package_for(quote_id):
        call(staff, "PUT", p + "/tender/winner", json={"quoteId": quote_id, "comment": "Решение сотрудника"})
        package = call(staff, "GET", p + "/package").json()
        call(staff, "GET", p + "/package/zip", 409)
        call(staff, "GET", "/files/" + package["documents"][0]["fileId"] + "/download", 404)
        return package, submit("package", package["version"])

    package, ap = package_for(quotes[0])
    call(reviewer, "POST", ap + "/reject", json={"subjectVersion": package["version"], "comment": "Пересмотреть выбор"})
    package, ap = package_for(quotes[1])
    call(reviewer, "POST", ap + "/approve", json={"subjectVersion": package["version"]})
    completed = call(resident, "GET", p).json()
    assert completed["status"] == "COMPLETED" and "steps" not in completed
    call(resident, "GET", p + "/package/zip", 403)
    call(outsider, "GET", "/files/" + package["documents"][0]["fileId"] + "/download", 404)
    bundle = call(staff, "GET", p + "/package/zip").content
    with ZipFile(io.BytesIO(bundle)) as archive:
        assert len(archive.namelist()) == 4
        for name in archive.namelist():
            content = io.BytesIO(archive.read(name))
            if name.endswith(".docx"):
                assert any("Ремонт крыши" in line.text or "Подрядчик:" in line.text for line in Document(content).paragraphs)
            else:
                assert load_workbook(content).active.max_row >= 5
    assert len(call(reviewer, "GET", "/approval-journal", params={"requestId": ident}).json()["items"]) == 5
    call(staff, "DELETE", "/session", 204)
    call(staff, "GET", p, 401)
    print(ident, flush=True)
    return ident


if __name__ == "__main__":
    run()
