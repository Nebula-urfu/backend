"""Seed a disposable smoke database. Never run against production."""
import os
from app.auth import PASSWORDS
from app.db import contractors, engine, orgs, uid, users

with engine.begin() as conn:
    conn.execute(orgs.insert(), [dict(id=i, name=i) for i in ("pilot", "outside")])
    secret = PASSWORDS.hash(os.getenv("SMOKE_PASSWORD", "local-scenario-password"))
    for name, org, role in [
        ("resident", "pilot", "RESIDENT"), ("staff", "pilot", "STAFF"), ("reviewer", "pilot", "REVIEWER"),
        ("outsider", "outside", "STAFF"), ("neighbor", "pilot", "RESIDENT"),
    ]:
        conn.execute(users.insert().values(id=name, org=org, name=name, username=name, role=role,
                                           address="Пилотная улица, 1", password=secret))
    for i in range(3):
        identifier = uid()
        conn.execute(contractors.insert().values(id=identifier, org="pilot", data={
            "id": identifier, "name": f"Подрядчик {i + 1}", "email": f"offer{i}@example.org", "nonstandardContract": False,
        }))
