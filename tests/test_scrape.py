"""Scraper tests with saved HTML fixtures and a fake HTTP transport (no network)."""

from pathlib import Path

import httpx

from outbound.config import Settings
from outbound.scrape import Fetcher, clean_url, detect_signals, html_to_text, rank_links, same_site, scrape_domain, truncate
from outbound.storage import MemoryStore

FIX = Path(__file__).parent / "fixtures"
HOME = (FIX / "acme_home.html").read_text(encoding="utf-8")
PRICING = (FIX / "acme_pricing.html").read_text(encoding="utf-8")


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def fake_site(routes: dict[str, tuple[int, str, str]]) -> httpx.Client:
    """routes: path -> (status, content_type, body). Unknown paths return 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        status, ctype, body = routes.get(request.url.path, (404, "text/html", "not found"))
        return httpx.Response(status, headers={"content-type": ctype}, text=body)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def make_fetcher(routes, **kw) -> Fetcher:
    return Fetcher(settings(**kw), client=fake_site(routes), sleep=lambda s: None)


# ---------------------------------------------------------------- helpers
def test_same_site_allows_subdomains_only():
    assert same_site("https://trust.acme.io/x", "acme.io")
    assert same_site("acme.io", "acme.io")
    assert not same_site("https://notacme.io", "acme.io")
    assert not same_site("https://acme.io.evil.com", "acme.io")


def test_clean_url_drops_query_and_fragment():
    assert clean_url("https://Acme.io/careers?utm=1#jobs") == "https://acme.io/careers"


def test_html_to_text_removes_scripts_and_styles():
    text = html_to_text(HOME, "https://acme.io/")
    assert "approval workflows" in text
    assert "tracking" not in text and "color:red" not in text


def test_truncate_marks_cut_text():
    text, cut = truncate("word " * 100, 50)
    assert cut and len(text) <= 52


def test_rank_links_keeps_same_site_keyword_links_one_per_keyword():
    links = rank_links(HOME, "https://acme.io/", "acme.io", limit=5)
    assert links[0] == "https://acme.io/pricing"  # pricing first; shortest path beats /fr/pricing
    assert "https://acme.io/security" in links
    assert "https://www.acme.io/customers" in links
    assert "https://acme.io/careers" in links
    assert not any("other.com" in u or u.endswith(".pdf") for u in links)


def test_candidate_links_keeps_unfetched_useful_pages():
    from outbound.scrape import candidate_links

    html = HOME + '<a href="/press/series-b">We raised our Series B</a><a href="/team">Team</a>'
    links = candidate_links(html, "https://acme.io/", "acme.io", exclude={"https://acme.io/pricing"})
    assert "https://acme.io/pricing" not in links
    assert "https://acme.io/press/series-b" in links and "https://acme.io/team" in links
    assert not any("other.com" in u for u in links)


def test_detect_signals_finds_competitor_and_mentions():
    signals = {s.signal: s for s in detect_signals(HOME, "https://acme.io/", "acme.io")}
    assert "competitor_link:vanta.com" in signals
    assert "mentions:SOC 2" in signals and "mentions:GDPR" in signals and "mentions:SAML/SSO" in signals
    assert "mentions:Compliance in progress" in signals
    assert "in progress" in signals["mentions:Compliance in progress"].snippet


def test_sprinto_trust_center_is_its_own_signal():
    html = '<html><body><a href="https://app.sprinto.com/trust-center/view/x">Trust center</a></body></html>'
    signals = [s.signal for s in detect_signals(html, "https://acme.io/", "acme.io")]
    assert "sprinto_link" in signals
    assert not any(s.startswith("competitor") for s in signals)  # Sprinto = existing customer, not a competitor penalty


# ---------------------------------------------------------------- fetching
def test_fetcher_respects_robots_txt():
    f = make_fetcher({"/robots.txt": (200, "text/plain", (FIX / "robots_disallow_security.txt").read_text())})
    page, html = f.fetch("https://acme.io/security", "acme.io")
    assert page.error == "skipped: disallowed by robots.txt" and html == ""


def test_fetcher_refuses_other_domains():
    f = make_fetcher({})
    page, _ = f.fetch("https://evil.com/", "acme.io")
    assert page.error.startswith("refused")


def test_fetcher_records_http_errors_without_crashing():
    f = make_fetcher({"/": (500, "text/html", "boom")})
    page, _ = f.fetch("https://acme.io/", "acme.io")
    assert page.error == "HTTP 500" and page.status_code == 500


def test_scrape_domain_end_to_end_and_cache():
    routes = {
        "/robots.txt": (200, "text/plain", (FIX / "robots_disallow_security.txt").read_text()),
        "/": (200, "text/html", HOME),
        "/pricing": (200, "text/html", PRICING),
    }
    store = MemoryStore()
    fetcher = make_fetcher(routes, max_pages_per_domain=4)
    result = scrape_domain("acme.io", settings(max_pages_per_domain=4), store, fetcher)
    ok = [p for p in result.pages if not p.error]
    assert [clean_url(p.url) for p in ok][:2] == ["https://acme.io/", "https://acme.io/pricing"]
    assert "https://acme.io/security" in result.skipped_by_robots
    assert len(result.pages) <= 4
    assert any(s.signal == "competitor_link:vanta.com" for s in result.detected_signals)
    # second call comes from the cache: a fetcher that would fail proves no request is made
    cached = scrape_domain("acme.io", settings(), store, make_fetcher({}))
    assert cached.pages[0].text == result.pages[0].text
