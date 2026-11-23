import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError
from PIL import Image
from sqlalchemy import select, update

from app import ai
from app.requests import answer_clarifications
from app.schemas import ResidentAnswers
from app.db import engine, jobs, model_usage, requests, stamp, uid, users


def test_llamacpp_recovery_and_image(monkeypatch, tmp_path):
    received = []
    answer = {
        "triage": {"urgency": "PLANNED", "workType": "Ремонт", "route": "PLANNED_PROCUREMENT"},
        "scope": "Обследовать протекающий участок кровли.", "volume": "Уточнить площадь повреждения при осмотре.",
        "schedule": "Срок согласовать после обследования.", "acceptance": "Проверить герметичность отремонтированной кровли.",
        "questions": ["Когда появляется вода?"], "contractorNumbers": [1, 2, 3],
        "letterSubject": "Запрос КП на ремонт кровли", "letterBody": "Просим оценить ремонт кровли и указать срок, цену и гарантию.",
    }

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = json.dumps(dict(id="test", object="chat.completion", created=0, model="qwen3.5-9b", usage=dict(prompt_tokens=80, completion_tokens=20, total_tokens=100), choices=[
                dict(index=0, finish_reason="stop", message=dict(role="assistant", content=json.dumps(answer))),
            ])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{provider.server_port}/v1")
    monkeypatch.setenv("OPENAI_MODEL", "qwen3.5-9b")
    monkeypatch.setattr(ai, "AI_ENABLED", True)
    monkeypatch.setattr(ai, "STORAGE", tmp_path)
    Image.new("RGB", (12, 12)).save(tmp_path / "photo", format="PNG")
    rid, jid = uid(), uid()
    with engine.begin() as conn:
        conn.execute(requests.insert().values(id=rid, org="pilot", author="resident", key=rid,
            fingerprint=rid, data={"description": "Течь", "photos": [{"fileId": "photo"}],
            "address": "Учебный адрес", "entrance": "1", "createdAt": stamp(), "updatedAt": stamp(), "status": "ACCEPTED",
            "nmc": {"state": "NOT_STARTED"}, "tender": {"state": "NOT_STARTED"}, "package": {"state": "NOT_STARTED"},
            "triage": {"state": "PROCESSING", "generation": 1}, "tz": {"version": 0, "state": "NOT_STARTED"}}))
        conn.execute(jobs.insert().values(id=jid, request_id=rid, kind="triage", target="", generation=1,
                                         state="running", attempt=1))
    try:
        ai.recover()
        assert ai.process_one()
        with engine.begin() as conn:
            data = conn.execute(select(requests.c.data).where(requests.c.id == rid)).scalar_one()
            assert data["triage"]["state"] == "DRAFT_READY"
            assert data["tz"]["origin"] == "AI"
            assert data["intake"] == {"pending": True, "questions": ["Когда появляется вода?"]}
            assert len(data["tz"]["letter"]["contractorIds"]) == 3
            assert data["tz"]["letter"]["subject"] == "Запрос КП на ремонт кровли"
            assert conn.execute(select(jobs.c.attempt).where(jobs.c.id == jid)).scalar_one() == 2
            usage = conn.execute(select(model_usage).where(model_usage.c.model == "qwen3.5-9b")).one()
            assert usage.total_tokens == 100 and usage.outcome == "ok"
        path, payload = received[0]
        assert path == "/v1/chat/completions"
        assert payload["model"] == "qwen3.5-9b"
        assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert payload["response_format"]["type"] == "json_schema"
        with engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == jid).values(state="queued"))
        assert ai.process_one()
        assert len(received) == 1
        with engine.begin() as conn:
            resident = conn.execute(select(users).where(users.c.id == "resident")).one()
            response = answer_clarifications(rid, ResidentAnswers(answers=["После сильного дождя"]), resident, conn)
            assert "clarificationQuestions" not in response
        assert ai.process_one()
        assert "После сильного дождя" in received[-1][1]["messages"][1]["content"][-1]["text"]
        with engine.begin() as conn:
            data = conn.execute(select(requests.c.data).where(requests.c.id == rid)).scalar_one()
            assert not data["intake"]["pending"]
            assert data["tz"]["version"] == 2
            assert data["tz"]["openQuestions"][0]["answer"] == "После сильного дождя"
    finally:
        provider.shutdown()
        thread.join()
        provider.server_close()


def test_provider_outage_stops_retrying_and_keeps_manual_path(monkeypatch):
    calls = []

    def unavailable(job, data):
        calls.append(job.id)
        raise TimeoutError()

    monkeypatch.setattr(ai, "AI_ENABLED", True)
    monkeypatch.setattr(ai, "inference", unavailable)
    rid, jid = uid(), uid()
    with engine.begin() as conn:
        conn.execute(requests.insert().values(id=rid, org="pilot", author="resident", key=rid,
            fingerprint=rid, data={"description": "Течь", "photos": [],
            "triage": {"state": "PROCESSING", "generation": 1}, "tz": {"version": 0}}))
        conn.execute(jobs.insert().values(id=jid, request_id=rid, kind="triage", target="", generation=1,
                                         state="queued", attempt=0))
    for _ in range(ai.MAX_ATTEMPTS):
        assert ai.process_one()
    assert not ai.process_one()
    assert calls == [jid] * ai.MAX_ATTEMPTS
    with engine.begin() as conn:
        data = conn.execute(select(requests.c.data).where(requests.c.id == rid)).scalar_one()
        assert data["triage"]["state"] == "AI_FAILED"
        assert data["tz"]["version"] == 0
        assert conn.execute(select(jobs.c.state).where(jobs.c.id == jid)).scalar_one() == "failed"


def test_manual_quote_edit_wins_over_inflight_result(monkeypatch):
    rid, jid, qid = uid(), uid(), uid()
    with engine.begin() as conn:
        conn.execute(requests.insert().values(id=rid, org="pilot", author="resident", key=rid,
            fingerprint=rid, data={"quotes": {qid: {"extraction": "EXTRACTING", "version": 1, "generation": 1}},
                                   "nmc": {"state": "DRAFT_READY"}}))
        conn.execute(jobs.insert().values(id=jid, request_id=rid, kind="quote", target=qid, generation=1,
                                         state="queued", attempt=0))

    def finish_after_manual_edit(job, data):
        with engine.begin() as conn:
            record = conn.execute(select(requests.c.data).where(requests.c.id == rid).with_for_update()).scalar_one()
            record["quotes"][qid].update(extraction="EXTRACTED", version=2, editedByStaff=True, data={"contractorName": "Manual"})
            conn.execute(update(requests).where(requests.c.id == rid).values(data=record))
        return {"contractorName": "Late"}

    monkeypatch.setattr(ai, "AI_ENABLED", True)
    monkeypatch.setattr(ai, "inference", finish_after_manual_edit)
    assert ai.process_one()
    with engine.begin() as conn:
        data = conn.execute(select(requests.c.data).where(requests.c.id == rid)).scalar_one()
        assert data["quotes"][qid]["data"] == {"contractorName": "Manual"}
        assert conn.execute(select(jobs.c.state).where(jobs.c.id == jid)).scalar_one() == "obsolete"


def test_rejects_placeholder_draft():
    with pytest.raises(ValidationError) as error:
        ai.Draft.model_validate({
            "triage": {"urgency": "PLANNED", "workType": "DIAGNOSTIC", "route": "PLANNED_PROCUREMENT"},
            "scope": "DIAGNOSTIC", "volume": "UNKNOWN", "schedule": "UNKNOWN", "acceptance": "UNKNOWN",
        })
    assert len(error.value.errors()) == 5
