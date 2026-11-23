from fastapi.testclient import TestClient

from app.db import engine, model_usage, now, uid
from app.main import app


def login(name, password="local-scenario-password"):
    client = TestClient(app)
    response = client.post("/api/v1/session/login", json={"username": name, "password": password},
                           headers={"X-Requested-With": "Nebula"})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrfToken"]
    return client


def test_account_roles_are_scoped_and_role_changes_revoke_sessions():
    admin, resident, other = login("admin"), login("resident"), login("other-admin")
    root = "/api/v1"
    assert resident.get(root + "/admin/users").status_code == 403
    assert resident.put(root + "/session/role", json={"role": "ADMIN"}).status_code == 403
    new = {"username": "multi", "name": "Три роли", "address": "Пилотная улица, 1",
           "roles": ["RESIDENT", "STAFF", "REVIEWER"], "password": "multi-role-test-secret"}
    response = admin.post(root + "/admin/users", json=new)
    assert response.status_code == 201, response.text
    account = response.json()
    assert "password" not in account
    path = root + "/admin/users/" + account["userId"]
    assert other.put(path, json=new).status_code == 404
    multi = login("multi", new["password"])
    assert multi.get(root + "/approvals").status_code == 403
    for role, endpoint in [("STAFF", "/contractors"), ("REVIEWER", "/approvals"), ("RESIDENT", "/requests")]:
        response = multi.put(root + "/session/role", json={"role": role})
        assert response.status_code == 200, response.text
        assert response.json()["role"] == role
        assert multi.get(root + "/session").json()["role"] == role
        assert multi.get(root + endpoint).status_code == 200
    assert admin.put(path, json={**new, "roles": ["RESIDENT"], "enabled": False}).status_code == 200
    assert multi.get(root + "/session").status_code == 401
    assert multi.post(root + "/session/login", json={"username": "multi", "password": new["password"]},
                      headers={"X-Requested-With": "Nebula"}).status_code == 401


def test_analytics_counts_only_own_organization_and_reported_usage():
    with engine.begin() as conn:
        for org in ("pilot", "outside"):
            conn.execute(model_usage.insert().values(id=uid(), org=org, kind="triage", model="pilot-model",
                outcome="ok", recorded_at=now(), latency_ms=100, prompt_tokens=8, completion_tokens=2, total_tokens=10))
        conn.execute(model_usage.insert().values(id=uid(), org="pilot", kind="quote", model="pilot-model",
            outcome="error", recorded_at=now(), latency_ms=300))
    admin = login("admin")
    response = admin.get("/api/v1/admin/stats?days=7")
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["daily"]) == 7
    model = next(item for item in data["model"] if item["model"] == "pilot-model")
    assert model == dict(model="pilot-model", calls=2, successful=1, reported=1, promptTokens=8,
                         completionTokens=2, totalTokens=10, averageLatencyMs=200)
