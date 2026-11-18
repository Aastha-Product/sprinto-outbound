"""Lecture L4 ("Data Acquisition & Agent Loops") checks: pages that fail WITHOUT an error.

- A 200 page can still be empty (JavaScript shell), a bot wall, a CAPTCHA or a login form.
- Such a page must count as "unknown", never as "nothing there". The scoring rules that claim something is
  missing (no SOC 2, GDPR only, ...) must not earn points from it.
- Data that sites embed for machines (meta description, JSON-LD) is used when the visible text is thin.
- Repeated menu/footer lines are dropped from the prompt (cost), while quotes from them still verify.
All offline: fake HTTP transport, fake LLM.
"""

import json

import httpx
import pytest

from outbound import qualify as q
from outbound.config import Settings
from outbound.evidence import EvidenceCorpus, drop_repeated_lines
from outbound.icp import load_icp, parse_icp
from outbound.models import CompanyProfile, Evidence, PageContent, ScrapeResult, Signal
from outbound.report import decision_report, review_queue
from outbound.scrape import Fetcher, assess_page, embedded_data_text, recheck_cached_page

ICP = load_icp()
LONG_TEXT = " ".join(["Acme automates approval workflows for enterprise finance teams."] * 20)

# Shaped like the real trust.vanta.com response: a 200 page whose body is a script that draws everything later.
JS_SHELL = ('<html><head><title>Trust Center</title><meta name="description" content="Acme trust center: security '
            'and compliance documents for customers."></head><body><div id="root"></div>'
            '<script src="/app.js"></script></body></html>')
CLOUDFLARE = "<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'>Checking your browser</div></body></html>"
LOGIN = "<html><body><form><input type='text' name='u'><input type=\"password\" name='p'><button>Sign in</button></form></body></html>"
JSONLD = {"@context": "https://schema.org", "@type": "Organization", "name": "Acme",
          "numberOfEmployees": {"@type": "QuantitativeValue", "value": 180}, "foundingDate": "2019"}
RICH = f"<html><head><script type='application/ld+json'>{json.dumps(JSONLD)}</script></head><body><main><p>{LONG_TEXT}</p></main></body></html>"


def fetcher(routes: dict[str, tuple[int, str]]) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = routes.get(request.url.path, (404, "not found"))
        return httpx.Response(status, headers={"content-type": "text/html"}, text=body)
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher(Settings(_env_file=None), client=client, sleep=lambda s: None)


# ---------------------------------------------------------------- page quality
@pytest.mark.parametrize("html,text,expected", [
    (JS_SHELL, "Trust Center", "thin"),
    (CLOUDFLARE, "Checking your browser", "bot_blocked"),
    (LOGIN, "Sign in", "login_wall"),
    ("<div class='g-recaptcha'></div>", "Please verify", "captcha"),
    (RICH, LONG_TEXT, "ok"),
])
def test_assess_page_names_the_silent_failure(html, text, expected):
    assert assess_page(html, text) == expected


def test_js_shell_is_fetched_but_marked_unreadable_and_keeps_meta_description():
    page, _ = fetcher({"/robots.txt": (404, ""), "/trust": (200, JS_SHELL)}).fetch("https://acme.io/trust", "acme.io")
    assert page.error is None and page.status_code == 200
    assert page.quality == "thin" and not page.readable
    assert "JavaScript" in page.quality_note
    assert "security and compliance documents" in page.text  # rescued from <meta name="description">


def test_http_errors_say_why():
    f = fetcher({"/robots.txt": (404, ""), "/a": (429, "slow down"), "/b": (403, CLOUDFLARE), "/c": (404, "nope")})
    assert f.fetch("https://acme.io/a", "acme.io")[0].quality == "rate_limited"
    blocked = f.fetch("https://acme.io/b", "acme.io")[0]
    assert blocked.quality == "bot_blocked" and "respect the barrier" in blocked.error
    assert f.fetch("https://acme.io/c", "acme.io")[0].quality == "error"


def test_json_ld_gives_headcount_without_a_browser():
    text = embedded_data_text(RICH)
    assert "Number of employees: 180" in text and "Founded: 2019" in text


def test_embedded_data_ignores_broken_json():
    assert embedded_data_text("<script type='application/ld+json'>{not json</script>") == ""


def test_old_cached_pages_are_rechecked():
    assert recheck_cached_page(PageContent(url="https://trust.acme.io/", text="Loading")).quality == "thin"
    assert recheck_cached_page(PageContent(url="https://acme.io/", text=LONG_TEXT)).readable


# ---------------------------------------------------------------- absence rules need a readable trust page
def ctx_with(pages: list[PageContent]) -> q.QualifyContext:
    sc = ScrapeResult(domain="acme.io", base_url="https://acme.io", pages=pages)
    corpus = EvidenceCorpus()
    for p in pages:
        corpus.add_page(p)
    return q.QualifyContext(domain="acme.io", scrape=sc, profile=CompanyProfile(name="Acme"), corpus=corpus,
                            settings=Settings(_env_file=None), icp=ICP)


HOME = PageContent(url="https://acme.io/", text=LONG_TEXT, word_count=200)
NO_TRUST = Signal(rule_id="GAP_NO_TRUST_PAGE", value="yes",
                  evidence=[Evidence(claim="no trust page", source_url="https://acme.io/", quote="no trust or security page found")])


def test_icp_lists_the_rules_that_need_a_readable_trust_page():
    assert set(ICP.needs_readable_trust_page) == {"GAP_NO_TRUST_PAGE", "GAP_ONE_FRAMEWORK", "GAP_GDPR_ONLY"}


def test_unreadable_trust_page_blocks_the_no_trust_page_points():
    js_trust = PageContent(url="https://trust.acme.io/", status_code=200, text="Trust Center", word_count=2, quality="thin",
                           quality_note="only 2 words of readable text")
    signals = {s.rule_id: s for s in q.clean_signals([NO_TRUST], ctx_with([HOME, js_trust]))}
    assert signals["GAP_NO_TRUST_PAGE"].value == "unknown"
    assert "trust.acme.io" in signals["GAP_NO_TRUST_PAGE"].detail
    assert q.score_signals(list(signals.values()), ICP)[0] == 0


def test_robots_blocked_trust_page_also_blocks_absence():
    skipped = PageContent(url="https://trustcenter.acme.io/", error="skipped: disallowed by robots.txt", quality="error")
    signals = {s.rule_id: s for s in q.clean_signals([NO_TRUST], ctx_with([HOME, skipped]))}
    assert signals["GAP_NO_TRUST_PAGE"].value == "unknown"


def test_trust_page_404_is_real_absence_and_keeps_the_points():
    missing = PageContent(url="https://acme.io/trust", status_code=404, error="HTTP 404", quality="error")
    signals = {s.rule_id: s for s in q.clean_signals([NO_TRUST], ctx_with([HOME, missing]))}
    assert signals["GAP_NO_TRUST_PAGE"].value == "yes"


def test_unreadable_non_trust_page_does_not_block():
    thin_blog = PageContent(url="https://acme.io/blog", status_code=200, text="Loading", quality="thin")
    signals = {s.rule_id: s for s in q.clean_signals([NO_TRUST], ctx_with([HOME, thin_blog]))}
    assert signals["GAP_NO_TRUST_PAGE"].value == "yes"


def test_unknown_rule_id_in_trust_section_is_a_clear_error():
    text = ICP.text.replace("- GAP_GDPR_ONLY\n", "- GAP_TYPO\n")
    with pytest.raises(Exception, match="GAP_TYPO"):
        parse_icp(text)


# ---------------------------------------------------------------- prompt cleaning (cost)
def test_repeated_lines_are_dropped_from_prompt_but_quotes_still_verify():
    footer = "Home | Product | Pricing | Login\nSOC 2 Type II certified"
    a = PageContent(url="https://acme.io/", text="Acme automates approvals.\n" + footer)
    b = PageContent(url="https://acme.io/pricing", text="Enterprise plan with SSO.\n" + footer)
    corpus = EvidenceCorpus()
    corpus.add_page(a)
    corpus.add_page(b)
    block = corpus.as_prompt_block()
    assert block.count("Home | Product | Pricing | Login") == 1
    assert corpus.verify(Evidence(claim="c", source_url="https://acme.io/pricing", quote="SOC 2 Type II certified"))


def test_drop_repeated_lines_keeps_first_occurrence():
    seen: set[str] = set()
    assert drop_repeated_lines("a line\nb line", seen) == "a line\nb line"
    assert drop_repeated_lines("b line\nc line", seen) == "c line"


# ---------------------------------------------------------------- hand-off to a human
def test_review_queue_lists_insufficient_data_and_unconfirmed_gaps():
    run = {
        "acme.io": {"01_scrape": {"status": "done", "output": {"pages": [{"url": "u", "quality": "thin"}]}},
                    "03_qualify": {"output": {"verdict": "insufficient_data", "score": 45, "critical_unknowns": ["Stage fit: Series A–C"], "signals": [
                        {"rule_id": "GAP_NO_TRUST_PAGE", "detail": "can't confirm: https://trust.acme.io/ exists but could not be read"}]}}},
        "ok.io": {"01_scrape": {"status": "done", "output": {"pages": []}}, "03_qualify": {"output": {"verdict": "rejected", "score": 20}}},
    }
    items = review_queue(run)
    assert any("insufficient data" in i and "acme.io" in i for i in items)
    assert any("trust.acme.io" in i for i in items)
    assert not any("ok.io" in i for i in items)
    assert "## Needs a human" in decision_report("r1", run)
