"""End-to-end: steps 1-7 with a fake LLM, cached scrape + Hunter data, a fake Gmail and an in-memory store."""

import json
from pathlib import Path

import pytest

from outbound import llm, send as send_mod
from outbound.config import Settings
from outbound.models import (
    CompanyProfile, EmailDraft, Evidence, FieldEvidence, PageContent, PainPoint, ScrapeResult, Signal, SignalReport,
)
from outbound.pain import PainPointList
from outbound.pipeline import DomainRunner, RunOptions, parse_steps, run_domain
from outbound.report import summary_rows, write_reports
from outbound.storage import FileStore, MemoryStore

FIX = Path(__file__).parent / "fixtures"
HOME = "https://acme.io/"
PRICING = "https://acme.io/pricing"
HOME_TEXT = ("Acme Cloud automates approval workflows for mid-market and enterprise finance teams. Trusted by 400 companies "
             "across the US and Europe. Book a demo with our sales team today. We are SOC 2 Type II certified. "
             "ISO 27001 certification is in progress and expected next quarter. Team of 120 people.")
PRICING_TEXT = "Enterprise: custom pricing with SSO, SCIM and a security review package. Talk to sales."


def E(quote, url=HOME):
    return Evidence(claim="fact", source_url=url, quote=quote)


def fake_llm(prompt_name, variables, schema, temperature=0.0):
    llm._budget.spend(12)
    if prompt_name == "extract":
        return CompanyProfile(
            name="Acme Cloud", is_b2b_software=True, employee_range="120 people", funding_stage="Series B, 2025",
            evidence=[FieldEvidence(field="name", claim="name", source_url=HOME, quote="Acme Cloud automates approval workflows"),
                      FieldEvidence(field="is_b2b_software", claim="B2B", source_url=HOME, quote="for mid-market and enterprise finance teams"),
                      FieldEvidence(field="employee_range", claim="size", source_url=HOME, quote="Team of 120 people"),
                      FieldEvidence(field="funding_stage", claim="invented", source_url=HOME, quote="raised a Series B in 2025")])
    if prompt_name == "qualify":
        y = lambda rid, q, url=HOME: Signal(rule_id=rid, value="yes", evidence=[E(q, url)])
        return SignalReport(signals=[
            y("ENT_PLAN", "Enterprise: custom pricing with SSO", PRICING), y("ENT_DEMO", "Book a demo with our sales team today"),
            y("ENT_SSO", "custom pricing with SSO, SCIM", PRICING), y("ENT_LOGOS", "Trusted by 400 companies across the US"),
            y("DATA_SENSITIVE", "automates approval workflows for mid-market and enterprise finance teams"),
            y("GAP_IN_PROGRESS", "ISO 27001 certification is in progress and expected next quarter"),
            y("GAP_ONE_FRAMEWORK", "We are SOC 2 Type II certified"), y("STAGE_SIZE", "Team of 120 people"),
            y("GEO_US", "across the US and Europe"), y("GEO_EU_UK", "across the US and Europe"),
            Signal(rule_id="DQ_SPRINTO_CUSTOMER", value="no"),
        ], summary="Strong fit.")
    if prompt_name == "pain_points":
        return PainPointList(pain_points=[
            PainPoint(title="Second framework doubles evidence work", why_this_person="CTO owns the engineers doing it",
                      evidence=[E("ISO 27001 certification is in progress and expected next quarter")]),
            PainPoint(title="Security reviews slow enterprise deals", why_this_person="CTO answers questionnaires",
                      evidence=[E("a security review package", PRICING)]),
        ])
    if prompt_name == "email":
        return EmailDraft(subject="ISO 27001 next to SOC 2", hook="ISO 27001 in progress", hook_evidence_url=HOME,
                          pain_point_used="Second framework doubles evidence work", reasoning="specific + verifiable",
                          body="Hi Alex,\n\nSaw that ISO 27001 is in progress at Acme, on top of SOC 2 Type II. Running two frameworks "
                               "usually means engineers collect the same evidence twice. Is that pulling people off the roadmap? "
                               "Sprinto maps both frameworks to one set of automated controls. Worth a 15-minute chat next week?")
    raise AssertionError(f"unexpected prompt {prompt_name}")


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(llm, "generate_json", fake_llm)
    monkeypatch.setattr(llm, "research_with_search", lambda q: ("", []))
    sent = []
    monkeypatch.setattr(send_mod, "gmail_send", lambda msg, s: sent.append(msg) or "gmail-123")
    store = MemoryStore()
    scrape = ScrapeResult(domain="acme.io", base_url="https://acme.io",
                          pages=[PageContent(url=HOME, text=HOME_TEXT), PageContent(url=PRICING, text=PRICING_TEXT)])
    store.set_cache("scrape", "acme.io", scrape.model_dump(mode="json"))
    store.set_cache("hunter", "acme.io", json.loads((FIX / "hunter_domain_search.json").read_text()))
    settings = Settings(_env_file=None, gemini_api_key="x", gemini_model="fake", owner_email="me@example.com",
                        send_enabled=True, hunter_api_key="test-api-key")
    return store, settings, sent


def test_full_pipeline_dry_run(setup):
    store, settings, sent = setup
    outcomes = run_domain("r1", "acme.io", parse_steps("1-7"), settings, store, RunOptions())
    assert [o.status for o in outcomes] == ["done"] * 7, [o.message for o in outcomes]
    run = store.load_run("r1")["acme.io"]

    profile = run["02_extract"]["output"]
    assert profile["funding_stage"] is None and "funding_stage" in profile["unknowns"]  # invented quote was dropped

    q = run["03_qualify"]["output"]
    assert q["verdict"] == "qualified" and q["score"] >= 60
    assert q["iterations"][0]["action"]["argument"] == "Acme Cloud Sprinto customer"
    assert run["04_poc"]["output"]["chosen"]["first_name"] == "Alex"
    assert len(run["05_pain"]["output"]) == 2
    assert run["06_email"]["output"]["flagged"] is False
    assert run["06_email"]["output"]["full_body"].endswith("Anshull Mishra\nGrowth at Sprinto")
    assert run["07_send"]["output"]["status"] == "dry_run" and sent == []
    assert run["07_send"]["llm_calls_used"] <= settings.max_llm_calls_per_domain
    assert "Would go to: Alex" in store.load_run_file("r1", "acme.io/06_email.txt")

    csv_text, md = write_reports("r1", store)
    assert "acme.io" in csv_text and "## acme.io" in md and "Why this person" in md
    assert summary_rows(store.load_run("r1"))[0]["verdict"] == "qualified"


def test_decision_brief_answers_the_five_questions(setup):
    from outbound.report import decision_brief

    store, settings, _ = setup
    run_domain("rb", "acme.io", parse_steps("1-6"), settings, store, RunOptions())
    b = decision_brief(store.load_run("rb")["acme.io"])
    assert b["verdict"] == "qualified" and b["who"].startswith("Alex Kim")
    assert b["why_now"] and b["evidence"] and b["what_to_say"] == "ISO 27001 next to SOC 2"
    assert any("inferred" in u for u in b["uncertain"])  # the fake pain points default to "inferred"
    _csv, md = write_reports("rb", store)
    assert "**Decision brief**" in md


def test_send_step_reaches_only_owner(setup):
    store, settings, sent = setup
    run_domain("r2", "acme.io", parse_steps("1-6"), settings, store, RunOptions())
    out = DomainRunner("r2", "acme.io", settings, store, RunOptions(send=True)).run_step(7)
    assert out.status == "done" and out.artifact["output"]["status"] == "sent"
    assert sent[0]["To"] == "me@example.com"
    again = DomainRunner("r2", "acme.io", settings, store, RunOptions(send=True)).run_step(7)
    assert again.artifact["output"]["status"] == "skipped" and len(sent) == 1


def test_later_step_without_earlier_ones_stops_cleanly(setup):
    store, settings, _ = setup
    out = DomainRunner("empty", "acme.io", settings, store).run_step(4)
    assert out.status == "stopped" and "has not run yet" in out.message


def test_errors_are_saved_not_raised(setup, monkeypatch):
    store, settings, _ = setup
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gemini down")))
    outcomes = run_domain("r3", "acme.io", parse_steps("1-3"), settings, store, RunOptions())
    assert outcomes[-1].status == "error" and "gemini down" in outcomes[-1].message
    assert store.load_artifact("r3", "acme.io", "02_extract")["status"] == "error"


def test_out_of_credits_stops_the_whole_run(setup, monkeypatch):
    store, settings, _ = setup

    def broke(*a, **k):
        raise llm.LLMFatalError("Gemini error 402: credits used up")

    monkeypatch.setattr(llm, "generate_json", broke)
    with pytest.raises(llm.LLMFatalError):
        run_domain("r4", "acme.io", parse_steps("1-3"), settings, store, RunOptions())
    assert store.load_artifact("r4", "acme.io", "02_extract")["status"] == "error"


def test_continuing_a_run_reuses_finished_steps(setup, monkeypatch):
    store, settings, _ = setup
    run_domain("r5", "acme.io", parse_steps("1-2"), settings, store, RunOptions())
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: pytest.fail("step 2 should have been reused"))
    outcomes = run_domain("r5", "acme.io", parse_steps("1-2"), settings, store, RunOptions(skip_done=True))
    assert all("reused" in o.message for o in outcomes)


def test_fatal_error_classification():
    from google.genai import errors

    assert llm._fatal(errors.ClientError(402, {"error": {"message": "credits depleted"}})) is not None
    assert llm._fatal(errors.ClientError(400, {"error": {"message": "API key not valid"}})) is not None
    assert llm._fatal(errors.ClientError(400, {"error": {"message": "bad schema"}})) is None
    assert llm._fatal(errors.ClientError(429, {"error": {"message": "slow down"}})) is None


def test_file_store_round_trip(tmp_path):
    fs = FileStore(tmp_path / "runs", tmp_path / "cache")
    fs.save_artifact("run1", "acme.io", "01_scrape", {"status": "done", "x": 1})
    fs.save_run_file("run1", "acme.io/06_email.txt", "hello")
    fs.set_cache("hunter", "acme.io", {"data": {}})
    fs.log_event("send", run_id="run1", domain="acme.io", status="sent")
    assert fs.load_run("run1") == {"acme.io": {"01_scrape": {"status": "done", "x": 1}}}
    assert fs.load_run_file("run1", "acme.io/06_email.txt") == "hello"
    assert fs.get_cache("hunter", "acme.io") == {"data": {}}
    assert fs.sent_in_run("run1") == ["acme.io"] and fs.sent_today() == 1
    assert fs.list_runs() == ["run1"]


def test_parse_steps():
    assert parse_steps("1-2") == [1, 2] and parse_steps("3") == [3]
    with pytest.raises(ValueError):
        parse_steps("0-9")
