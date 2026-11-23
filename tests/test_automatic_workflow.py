import io
from uuid import uuid4
from zipfile import ZipFile

from PIL import Image

from app import ai, procurement, requests
from test_admin import login


def test_automatic_package_and_dependency_recalculation(monkeypatch):
    monkeypatch.setattr(requests, "AUTO_WORKFLOW", True)
    monkeypatch.setattr(requests, "AI_ENABLED", True)
    monkeypatch.setattr(procurement, "AI_ENABLED", True)
    monkeypatch.setattr(ai, "AI_ENABLED", True)
    prices = iter((120, 90, 150))

    def provider(job, data):
        if job.kind == "quote":
            price = next(prices)
            return dict(contractorName=f"Подрядчик {price}", totalPrice={"amount": str(price), "currency": "RUB"},
                        termsDays=7, warranty="Год", items=[], discrepancies=[])
        return dict(triage={"urgency": "PLANNED", "workType": "Ремонт кровли", "route": "PLANNED_PROCUREMENT"},
                    scope="Восстановить покрытие кровли у места протечки.", volume="Уточнить площадь повреждения.",
                    schedule="Согласовать дату после осмотра.", acceptance="Проверить герметичность кровли.",
                    questions=[], contractorNumbers=[1, 2, 3], letterSubject="Запрос КП", letterBody="Просим оценить ремонт кровли.")

    monkeypatch.setattr(ai, "inference", provider)
    resident, staff, reviewer = login("resident"), login("staff"), login("reviewer")
    api = "/api/v1"

    def call(client, method, path, status=200, **kwargs):
        response = client.request(method, api + path, **kwargs)
        assert response.status_code == status, (path, response.text)
        return response

    image = io.BytesIO()
    Image.new("RGB", (12, 12)).save(image, format="PNG")
    fid = call(resident, "POST", "/files", 201, files={"file": ("test.png", image.getvalue())}).json()["fileId"]
    req = call(resident, "POST", "/requests", 201, headers={"Idempotency-Key": str(uuid4())}, json={
        "description": "Протекает крыша", "entrance": "1", "photoFileIds": [fid],
    }).json()
    path = "/requests/" + req["id"]
    assert ai.process_one()
    assert call(staff, "GET", path + "/triage").json()["state"] == "APPROVED"
    tz = call(staff, "GET", path + "/tz").json()
    assert tz["state"] == "APPROVED"
    assert call(staff, "GET", path).json()["waitingFor"] == "QUOTES"
    ids = []
    for i in range(3):
        fid = call(staff, "POST", "/files", 201, files={"file": ("quote.txt", b"Commercial quote")}).json()["fileId"]
        ids.append(call(staff, "POST", path + "/quotes", 202, json={"fileIds": [fid]}).json()["id"])
        assert ai.process_one()
        if i < 2:
            assert call(staff, "GET", path + "/package").json()["documents"] == []
    assert call(staff, "GET", path + "/nmc").json()["amount"]["amount"] == "120.00"
    assert call(staff, "GET", path + "/tender").json()["selectedWinner"]["quoteId"] == ids[1]

    def pending():
        queue = call(reviewer, "GET", "/approvals").json()["items"]
        return next(item for item in queue if item["requestId"] == req["id"])

    old = pending()
    package = call(reviewer, "GET", "/approvals/" + old["id"]).json()["subject"]
    assert package["nmcSnapshot"]["amount"]["amount"] == "120.00"
    assert len(package["documents"]) == 4
    assert len(ZipFile(io.BytesIO(call(reviewer, "GET", path + "/package/zip").content)).namelist()) == 4
    call(staff, "GET", path + "/package/zip")

    quotes = call(staff, "GET", path + "/quotes").json()["items"]
    q = next(q for q in quotes if q["id"] == ids[0])
    q["data"]["totalPrice"]["amount"] = "60"
    call(staff, "PUT", path + f"/quotes/{q['id']}/data", json={"baseVersion": q["version"], "data": q["data"]})
    new = pending()
    assert new["subjectVersion"] > old["subjectVersion"]
    assert call(reviewer, "GET", "/approvals/" + old["id"]).json()["state"] == "SUPERSEDED"
    call(reviewer, "POST", "/approvals/" + old["id"] + "/approve", 409, json={"subjectVersion": old["subjectVersion"]})
    assert call(staff, "GET", path + "/nmc").json()["amount"]["amount"] == "100.00"
    assert call(staff, "GET", path + "/tender").json()["selectedWinner"]["quoteId"] == ids[0]
    call(reviewer, "POST", "/approvals/" + new["id"] + "/approve", json={"subjectVersion": new["subjectVersion"]})
    assert call(staff, "GET", path + "/package").json()["approvedAt"]
    call(staff, "GET", path + "/package/zip")

    call(staff, "PUT", path + "/tz", json={"baseVersion": tz["version"], "sections": [
        {"id": "scope", "text": "Восстановить кровельное покрытие и водосток."},
    ]})
    assert call(staff, "GET", path).json()["waitingFor"] == "QUOTES"
    call(staff, "GET", path + "/package/zip", 409)
    for quote in call(staff, "GET", path + "/quotes").json()["items"]:
        assert quote["staleScope"]
        call(staff, "PUT", path + f"/quotes/{quote['id']}/data",
             json={"baseVersion": quote["version"], "data": quote["data"]})
    assert pending()["subjectVersion"] > new["subjectVersion"]
