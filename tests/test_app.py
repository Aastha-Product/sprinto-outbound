"""The web API end-to-end with a fake LLM, fake Gmail and an in-memory store (no network)."""

import pytest
from fastapi.testclient import TestClient

import app as webapp
from outbound import send as send_mod
from outbound.config import Settings
from tests.test_pipeline import setup  # noqa: F401  (re-use the pipeline fixture: fake LLM + cached data)


@pytest.fixture
def client(setup, monkeypatch):  # noqa: F811
    store, settings, sent = setup
    monkeypatch.setattr(webapp, "get_store", lambda *a: store)
    monkeypatch.setattr(webapp, "get_settings", lambda: settings)
    return TestClient(webapp.app), store, settings, sent


def test_health_never_shows_secrets(client):
    c, _, settings, _ = client
    body = c.get("/api/health").text
    assert "me@example.com" not in body and "test-api-key" not in body


def test_full_web_flow_masks_emails_and_sends_only_to_owner(client):
    c, store, settings, sent = client
    run = c.post("/api/runs", json={"domains": "acme.io\n# comment\n"}).json()
    assert run["domains"] == ["acme.io"]
    for step in range(1, 7):
        out = c.post(f"/api/runs/{run['run_id']}/step", json={"domain": "acme.io", "step": step}).json()
        assert out["status"] == "done", out
        assert "corpus" not in (out["artifact"] or {})

    full = c.get(f"/api/runs/{run['run_id']}").text
    assert "alex@acme.io" not in full and "a***@acme.io" in full  # contact emails are masked

    sent_out = c.post(f"/api/runs/{run['run_id']}/send", json={"domain": "acme.io"}).json()
    assert sent_out["artifact"]["output"]["status"] == "sent"
    assert sent[0]["To"] == "me@example.com"
    assert "me@example.com" not in str(sent_out)  # owner email masked too

    report = c.get(f"/api/runs/{run['run_id']}/report").text
    assert "Decision report" in report and "alex@acme.io" not in report


def test_step_7_cannot_be_called_through_step_endpoint(client):
    c, *_ = client
    run = c.post("/api/runs", json={"domains": "acme.io"}).json()
    assert c.post(f"/api/runs/{run['run_id']}/step", json={"domain": "acme.io", "step": 7}).status_code == 400


def test_domain_must_belong_to_run(client):
    c, *_ = client
    run = c.post("/api/runs", json={"domains": "acme.io"}).json()
    assert c.post(f"/api/runs/{run['run_id']}/step", json={"domain": "evil.com", "step": 1}).status_code == 400


def test_limits(client, monkeypatch):
    c, store, settings, _ = client
    assert c.post("/api/runs", json={"domains": "\n".join(f"d{i}.com" for i in range(11))}).status_code == 400
    monkeypatch.setattr(webapp, "get_settings", lambda: settings.model_copy(update={"max_runs_per_day": 1}))
    assert c.post("/api/runs", json={"domains": "a.com"}).status_code == 200
    assert c.post("/api/runs", json={"domains": "b.com"}).status_code == 429


def test_runs_disabled_without_gemini_key(client, monkeypatch):
    c, *_ = client
    monkeypatch.setattr(webapp, "get_settings", lambda: Settings(_env_file=None))
    assert c.post("/api/runs", json={"domains": "a.com"}).status_code == 503
