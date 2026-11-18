"""PoC ranking and Hunter credit safety, using saved Hunter JSON (no real searches)."""

import json
from pathlib import Path

import httpx
import pytest

from outbound.config import Settings
from outbound.icp import load_icp
from outbound.models import CompanyProfile
from outbound.poc import find_poc, parse_contacts, rank_contacts
from outbound.storage import MemoryStore

FIX = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIX / "hunter_domain_search.json").read_text())
ACCOUNT = json.loads((FIX / "hunter_account.json").read_text())


def settings(**kw):
    return Settings(_env_file=None, hunter_api_key="test-api-key", **kw)


def fake_hunter(account=ACCOUNT, search=SEARCH):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        assert request.headers["X-API-KEY"] == "test-api-key"
        assert "api_key" not in str(request.url)  # key never in the URL
        if request.url.path.endswith("/account"):
            return httpx.Response(200, json=account)
        assert request.url.params["limit"] == "10" and request.url.params["type"] == "personal"
        return httpx.Response(200, json=search)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_small_company_prefers_cto_with_good_confidence():
    ranked = rank_contacts(parse_contacts(SEARCH), CompanyProfile(employee_range="51-200"), load_icp())
    names = [c.first_name for c, _ in ranked]
    assert names[0] == "Alex"  # CTO, confidence 92
    assert names.index("Sam") > names.index("Alex")  # CTO but confidence 60 < 70
    assert names[-1] == "Pat"  # Head of Marketing only matches the last-resort "any executive" tier
    assert None not in names  # never a contact without a first name


def test_marketing_exec_only_matches_last_resort_tier():
    ranked = rank_contacts(parse_contacts(SEARCH), CompanyProfile(employee_range="51-200"), load_icp())
    tiers = {c.first_name: t.priority for c, t in ranked}
    assert tiers["Maria"] == 2 and tiers["Dev"] == 3


def test_bigger_company_prefers_vp_eng_over_ceo():
    ranked = rank_contacts(parse_contacts(SEARCH), CompanyProfile(employee_range="200-500"), load_icp())
    assert ranked[0][0].first_name in ("Dev", "Alex")
    assert [c.first_name for c, _ in ranked].index("Maria") > 1


def test_find_poc_spends_one_credit_then_uses_cache():
    store = MemoryStore()
    client, calls = fake_hunter()
    result, _ = find_poc("acme.io", CompanyProfile(employee_range="51-200"), settings(), store, client=client)
    assert result.status == "found" and result.chosen.first_name == "Alex" and result.hunter_credits_used == 1
    assert len(result.alternatives) <= 3 and "Ranked below" in result.reasoning
    again, _ = find_poc("acme.io", CompanyProfile(employee_range="51-200"), settings(), store, client=client)
    assert again.hunter_credits_used == 0 and calls.count("/v2/domain-search") == 1


def test_low_quota_stops_before_searching():
    low = {"data": {"requests": {"searches": {"used": 23, "available": 25}}}}
    client, calls = fake_hunter(account=low)
    result, _ = find_poc("acme.io", CompanyProfile(), settings(hunter_min_remaining=3), MemoryStore(), client=client)
    assert result.status == "skipped" and "/v2/domain-search" not in calls


def test_no_hunter_flag_never_calls_hunter():
    client, calls = fake_hunter()
    result, _ = find_poc("acme.io", CompanyProfile(), settings(), MemoryStore(), no_hunter=True, client=client)
    assert result.status == "skipped" and calls == []


def test_no_contacts_means_no_poc():
    client, _ = fake_hunter(search={"data": {"emails": []}})
    result, _ = find_poc("acme.io", CompanyProfile(), settings(), MemoryStore(), client=client)
    assert result.status == "no_poc"
