import io

from PIL import Image
from sqlalchemy import select

from app import ai, requests
from app.db import engine, files, model_usage
from test_admin import login


def test_description_uses_only_residents_unattached_photos(monkeypatch):
    monkeypatch.setattr(requests, "AI_ENABLED", True)
    resident, neighbor, staff = (login(name) for name in ("resident", "neighbor", "staff"))
    photo = io.BytesIO()
    Image.new("RGB", (12, 12)).save(photo, "PNG")
    uploaded = resident.post("/api/v1/files", files={"file": ("damage.png", photo.getvalue())})
    assert uploaded.status_code == 201, uploaded.text
    fid = uploaded.json()["fileId"]

    def infer(job, data):
        assert job.kind == "description"
        assert [item["fileId"] for item in data["photos"]] == [fid]
        data["_metrics"].update(model="description-test", outcome="ok", latency_ms=10, total_tokens=100)
        return {"description": "На потолке видны следы протечки."}

    monkeypatch.setattr(ai, "inference", infer)
    path = "/api/v1/requests/description-draft"
    body = {"photoFileIds": [fid]}
    assert neighbor.post(path, json=body).status_code == 404
    assert staff.post(path, json=body).status_code == 403
    result = resident.post(path, json=body)
    assert result.json() == {"description": "На потолке видны следы протечки."}
    with engine.begin() as conn:
        assert conn.execute(select(files.c.request_id).where(files.c.id == fid)).scalar_one() is None
        usage = conn.execute(select(model_usage).where(model_usage.c.model == "description-test")).one()
    assert usage.kind == "description" and usage.total_tokens == 100
