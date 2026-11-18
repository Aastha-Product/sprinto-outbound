"""Offline tests for Phase 0: models, settings, prompt loading, LLM budget, domain parsing."""

import pytest
from pydantic import ValidationError

from outbound import llm
from outbound.config import ConfigError, Settings
from outbound.domains import normalise_domain, parse_domains
from outbound.models import AgentAction, CompanyProfile, EmailDraft, Evidence, QualificationResult


def make_settings(**overrides) -> Settings:
    """Settings that ignore your real .env file."""
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------- models
def test_evidence_requires_all_fields():
    Evidence(claim="Has enterprise plan", source_url="https://x.com/pricing", quote="Enterprise")
    with pytest.raises(ValidationError):
        Evidence(claim="no source")


def test_agent_action_only_allows_three_actions():
    assert AgentAction(action="web_search", argument="acme SOC 2", why="unknown status").action == "web_search"
    with pytest.raises(ValidationError):
        AgentAction(action="send_email", argument="x", why="nope")


def test_qualification_verdict_is_restricted():
    QualificationResult(verdict="insufficient_data", score=0, reasoning="not enough evidence")
    with pytest.raises(ValidationError):
        QualificationResult(verdict="maybe", score=50, reasoning="x")


def test_company_profile_defaults_to_unknown():
    p = CompanyProfile()
    assert p.is_b2b_software is None and p.evidence == [] and p.unknowns == []


def test_company_profile_round_trips_json():
    p = CompanyProfile(name="Acme", is_b2b_software=True)
    assert CompanyProfile.model_validate_json(p.model_dump_json()) == p


def test_email_draft_needs_hook_url():
    with pytest.raises(ValidationError):
        EmailDraft(subject="hi", body="b", hook="h", pain_point_used="p", reasoning="r")


# ---------------------------------------------------------------- settings
def test_secrets_are_masked_in_repr():
    s = make_settings(gemini_api_key="super-secret-123", hunter_api_key="hunter-secret-456")
    text = repr(s) + str(s)
    assert "super-secret-123" not in text and "hunter-secret-456" not in text


def test_require_gives_friendly_error():
    s = make_settings()
    with pytest.raises(ConfigError) as err:
        s.require("gemini")
    assert "GEMINI_API_KEY" in str(err.value) and "SETUP_WINDOWS.md" in str(err.value)


def test_lazy_validation_per_feature():
    s = make_settings(gemini_api_key="k", gemini_model="some-flash")
    s.require("gemini")  # fine without Hunter/Gmail keys
    assert s.missing_for("hunter") == ["HUNTER_API_KEY"]


def test_placeholder_owner_email_counts_as_missing():
    assert "OWNER_EMAIL" in make_settings(owner_email="you@gmail.com").missing_for("gmail")
    assert "OWNER_EMAIL" not in make_settings(owner_email="me@example.com").missing_for("gmail")


# ---------------------------------------------------------------- prompts + budget
def test_render_prompt_fills_variables():
    assert "Is Acme on SOC 2?" in llm.render_prompt("research", {"question": "Is Acme on SOC 2?"})


def test_render_prompt_rejects_missing_variable():
    with pytest.raises(KeyError):
        llm.render_prompt("research", {})


def test_llm_budget_is_enforced():
    llm.start_domain("acme.com")
    for _ in range(3):
        llm._budget.spend(limit=3)
    with pytest.raises(llm.LLMBudgetExceeded):
        llm._budget.spend(limit=3)
    llm.start_domain("next.com")
    assert llm.calls_used() == 0


# ---------------------------------------------------------------- domains file
def test_parse_domains_ignores_comments_and_blanks():
    text = """
    # a comment line
    rocketlane.com
    vanta.com          # inline comment

    https://www.Stripe.com/pricing
    rocketlane.com
    """
    assert parse_domains(text) == ["rocketlane.com", "vanta.com", "stripe.com"]


@pytest.mark.parametrize(
    "raw, expected",
    [("Example.COM", "example.com"), ("http://www.example.com/a?b=1", "example.com"), ("  ", "")],
)
def test_normalise_domain(raw, expected):
    assert normalise_domain(raw) == expected
