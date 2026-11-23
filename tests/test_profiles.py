import io
from uuid import uuid4

from PIL import Image

from test_admin import login


def test_profile_autofill_and_request_snapshot():
    resident = login("neighbor")
    path = "/api/v1"
    details = {
        "displayName": "Анна Иванова",
        "address": "Екатеринбург, улица Примерная, дом 10",
        "entrance": "2", "floor": "5", "apartment": "21", "phone": "+7 900 111-22-33",
    }
    response = resident.put(path + "/session/profile", json=details)
    assert response.status_code == 200, response.text
    profile = response.json()
    assert all(profile[key] == details[key] for key in ("displayName", "address", "entrance", "floor", "apartment"))
    assert profile["phone"] == ""
    image = io.BytesIO()
    Image.new("RGB", (12, 12)).save(image, "PNG")
    uploaded = resident.post(path + "/files", files={"file": ("test.png", image.getvalue())})
    result = resident.post(path + "/requests", headers={"Idempotency-Key": str(uuid4())}, json={
        "description": "Повреждение двери", "photoFileIds": [uploaded.json()["fileId"]], "floor": "4",
    })
    assert result.status_code == 201, result.text
    request = result.json()
    assert request["residentName"] == details["displayName"]
    assert request["address"] == details["address"]
    assert request["entrance"] == "2" and request["floor"] == "4" and request["apartment"] == "21"
    resident.put(path + "/session/profile", json={**details, "apartment": "22"}).raise_for_status()
    assert resident.get(path + "/requests/" + request["id"]).json()["apartment"] == "21"
    assert all("apartment" not in row for row in resident.get(path + "/requests").json()["items"])
