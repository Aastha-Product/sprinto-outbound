"""Step 1: fetch a company's homepage + its most useful subpages, politely, and find cheap signals.

Plain-English flow for one domain:
1. Turn `rocketlane.com` into `https://rocketlane.com` (falls back to `https://www.` if needed).
2. Read the site's robots.txt and skip any page it disallows for our User-Agent.
3. Fetch the homepage, collect its internal links, and rank them by useful keywords
   (pricing, security, trust, customers, careers, ...). Fetch the best ones, up to MAX_PAGES_PER_DOMAIN.
4. Turn each page's HTML into clean text (trafilatura; BeautifulSoup as a fallback) and truncate it.
5. Without any LLM, look for cheap signals: links to competitor trust centers (trust.vanta.com ...),
   and mentions of SOC 2, ISO 27001, GDPR, HIPAA, SSO/SAML, "trust center", "in progress" etc.
6. Never crash on a bad page: the error is recorded on that page and we move on.
Results are cached for a few days so re-runs don't re-download everything.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from bs4 import BeautifulSoup

from outbound.config import Settings
from outbound.icp import domain_in_list, load_icp
from outbound.models import DetectedSignal, PageContent, ScrapeResult
from outbound.storage import Store

log = logging.getLogger(__name__)

# Which subpages are worth fetching, in priority order (from BUILD_PROMPT.md, not ICP rules).
LINK_KEYWORDS = [
    "pricing", "security", "trust", "compliance", "customers", "about",
    "careers", "jobs", "enterprise", "integrations", "privacy",
]
# Extra keywords for pages the agent loop may fetch later: these often state headcount or funding.
CANDIDATE_KEYWORDS = LINK_KEYWORDS + ["company", "team", "press", "news", "newsroom", "media", "funding", "investors", "announc", "leadership"]
# Common paths to try when a site's links weren't saved (older cached scrapes) or don't match keywords.
FALLBACK_PATHS = ["/about", "/about-us", "/company", "/careers", "/press", "/newsroom", "/news"]
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".mp4", ".mp3", ".xml", ".json", ".css", ".js")
SCRAPE_CACHE_DAYS = 7
DELAY_SECONDS = 1.0

# Silent-failure detection (lecture L4: "six of eight failure modes produce no error").
# A 200 page with fewer readable words than this is treated as NOT read: usually the content is drawn by
# JavaScript after load (View Source != Inspect). Real example: trust.vanta.com returns 3 words.
THIN_WORDS = 50
BOT_MARKERS = ("just a moment...", "cf-chl", "challenge-platform", "attention required! | cloudflare", "verifying you are human",
               "checking your browser", "_incapsula_resource", "px-captcha")
CAPTCHA_MARKERS = ("g-recaptcha", "h-captcha", "hcaptcha.com", "recaptcha/api", "cf-turnstile")
QUALITY_NOTES = {
    "thin": "only {words} words of readable text; the content is probably drawn by JavaScript after load "
            "(needs a real browser or another source)",
    "bot_blocked": "bot protection (e.g. Cloudflare) answered instead of the site; not retried, we respect the barrier",
    "rate_limited": "the site asked us to slow down (HTTP 429); not retried in this run",
    "login_wall": "the page asks for a login; we never log in",
    "captcha": "the page shows a CAPTCHA; we never solve CAPTCHAs",
}
EMBEDDED_MAX_CHARS = 1500
EMBEDDED_HEADER = "[data embedded in the page's HTML]"

# Cheap text signals (regex, case-insensitive). These are hints for the LLM, not scores.
MENTION_PATTERNS: dict[str, str] = {
    "SOC 2": r"\bsoc[\s-]?2\b|\bsoc2\b",
    "ISO 27001": r"\biso[\s/]*(?:iec[\s]*)?27001\b",
    "GDPR": r"\bgdpr\b",
    "HIPAA": r"\bhipaa\b",
    "SAML/SSO": r"\bsaml\b|\bsso\b|single sign[\s-]?on",
    "Trust center": r"\btrust\s+(?:center|centre|portal|hub)\b",
    "Compliance in progress": (
        r"(?:soc[\s-]?2|iso[\s/]*27001)[^.\n]{0,80}(?:in progress|coming soon|underway|pursuing|working towards|in the process)"
        r"|(?:in progress|coming soon|underway|pursuing|working towards|in the process)[^.\n]{0,80}(?:soc[\s-]?2|iso[\s/]*27001)"
    ),
    "Security questionnaire": r"security\s+(?:questionnaires?|reviews?)|vendor\s+(?:assessments?|risk)",
}


# ---------------------------------------------------------------- small helpers
def same_site(url_or_host: str, domain: str) -> bool:
    """True if the URL/host is `domain` itself or one of its subdomains (e.g. trust.acme.com for acme.com)."""
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower().strip(".")
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def clean_url(url: str) -> str:
    """Drop #fragments and ?query strings so the same page isn't fetched twice."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path or "/", "", "", ""))


def html_to_text(html: str, url: str = "") -> str:
    """Main readable text of a page. trafilatura first; BeautifulSoup if that finds too little."""
    text = ""
    try:
        text = trafilatura.extract(html, url=url or None, include_comments=False, include_tables=True, favor_recall=True) or ""
    except Exception as exc:  # trafilatura can choke on odd HTML; fall back quietly
        log.debug("trafilatura failed on %s: %s", url, exc)
    if len(text) >= 500:
        return text.strip()
    fallback = _visible_text(html, drop_chrome=True)
    return (fallback if len(fallback) > len(text) else text).strip()


def _visible_text(html: str, drop_chrome: bool) -> str:
    """All visible text. With drop_chrome, also removes nav/header/footer menus."""
    soup = BeautifulSoup(html, "html.parser")
    junk = ["script", "style", "noscript", "svg", "template", "iframe"]
    if drop_chrome:
        junk += ["nav", "header", "footer", "form", "aside"]
    for tag in soup(junk):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def page_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    return soup.title.get_text(strip=True)[:200] if soup.title else None


def embedded_data_text(html: str) -> str:
    """Facts a site ships inside its HTML for search engines and its own app, readable without a browser.

    - meta description / og:description (a one-line "what we do")
    - JSON-LD (`<script type="application/ld+json">`): Organization name, description, numberOfEmployees,
      foundingDate, address country
    - Next.js `__NEXT_DATA__`: the page's own data feed; we keep its sentence-like strings
    Pages that look empty to a plain request often still carry these.
    """
    soup = BeautifulSoup(html, "html.parser")
    lines: list[str] = []
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            lines.append(tag["content"].strip())
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _jsonld_nodes(data):
            lines.extend(_jsonld_facts(node))
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            lines.extend(s for s in _string_leaves(json.loads(next_data.string)) if len(s.split()) >= 6)
        except json.JSONDecodeError:
            pass
    unique = list(dict.fromkeys(" ".join(line.split()) for line in lines if line and line.strip()))
    return truncate("\n".join(unique), EMBEDDED_MAX_CHARS)[0] if unique else ""


def _jsonld_nodes(data) -> list[dict]:
    if isinstance(data, list):
        return [n for item in data for n in _jsonld_nodes(item)]
    if isinstance(data, dict):
        graph = data.get("@graph")
        return [data] + ([n for item in graph for n in _jsonld_nodes(item)] if isinstance(graph, list) else [])
    return []


def _jsonld_facts(node: dict) -> list[str]:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if not any(k in ("Organization", "Corporation", "SoftwareApplication", "WebApplication", "Product", "LocalBusiness") for k in kinds):
        return []
    facts = []
    if isinstance(node.get("name"), str):
        facts.append(f"Name: {node['name']}")
    if isinstance(node.get("description"), str):
        facts.append(node["description"])
    employees = node.get("numberOfEmployees")
    if isinstance(employees, dict):
        employees = employees.get("value") or "-".join(str(employees[k]) for k in ("minValue", "maxValue") if k in employees)
    if employees:
        facts.append(f"Number of employees: {employees}")
    if node.get("foundingDate"):
        facts.append(f"Founded: {node['foundingDate']}")
    address = node.get("address")
    if isinstance(address, dict) and address.get("addressCountry"):
        country = address["addressCountry"]
        facts.append(f"Country: {country.get('name') if isinstance(country, dict) else country}")
    return [str(f) for f in facts]


def _string_leaves(data, limit: int = 200) -> list[str]:
    out: list[str] = []
    stack = [data]
    while stack and len(out) < limit:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and "<" not in item and "http" not in item:
            out.append(item)
    return out


def assess_page(html: str, text: str, status: int | None = None) -> str:
    """Classify a page that came back without an HTTP error. Returns a PageQuality value.

    This is the code-level check from the lecture: "a field is either populated or it isn't".
    We don't ask the model whether the page was useful; we count.
    """
    lower = html[:200_000].lower()
    words = len(text.split())
    if status == 429:
        return "rate_limited"
    if any(m in lower for m in BOT_MARKERS) and words < 200:
        return "bot_blocked"
    if any(m in lower for m in CAPTCHA_MARKERS) and words < 200:
        return "captcha"
    if 'type="password"' in lower and words < 150:
        return "login_wall"
    if words < THIN_WORDS:
        return "thin"
    return "ok"


def quality_note(quality: str, words: int) -> str | None:
    return QUALITY_NOTES[quality].format(words=words) if quality in QUALITY_NOTES else None


def http_error_quality(resp: httpx.Response) -> tuple[str, str]:
    """Name the failure for an HTTP error, so the report says WHY (bot wall vs rate limit vs missing page)."""
    body = resp.text[:20_000].lower() if resp.content else ""
    if resp.status_code == 429:
        return "rate_limited", f"HTTP 429: {QUALITY_NOTES['rate_limited']}"
    if resp.status_code in (403, 503) and (any(m in body for m in BOT_MARKERS) or resp.headers.get("cf-mitigated")):
        return "bot_blocked", f"HTTP {resp.status_code}: {QUALITY_NOTES['bot_blocked']}"
    return "error", f"HTTP {resp.status_code}"


def recheck_cached_page(page: PageContent) -> PageContent:
    """Scrapes cached before quality checks existed: classify them from their saved text."""
    if page.error:
        page.quality = "error"
    elif page.word_count == 0 and page.quality == "ok":
        page.word_count = len(page.text.split())
        if page.word_count < THIN_WORDS:
            page.quality, page.quality_note = "thin", quality_note("thin", page.word_count)
    return page


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rsplit(" ", 1)[0] + " …", True


# ---------------------------------------------------------------- link discovery
def rank_links(html: str, page_url: str, domain: str, limit: int) -> list[str]:
    """Pick up to `limit` internal links, one per keyword in LINK_KEYWORDS order, shortest path first."""
    soup = BeautifulSoup(html, "html.parser")
    home = clean_url(page_url)
    candidates: dict[str, set[str]] = {}  # url -> keywords it matches
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = clean_url(urljoin(page_url, href))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not same_site(url, domain) or url == home:
            continue
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):
            continue
        haystack = parsed.path.lower() + " " + (parsed.hostname or "") + " " + a.get_text(" ", strip=True).lower()
        matched = {kw for kw in LINK_KEYWORDS if kw in haystack}
        if matched:
            candidates.setdefault(url, set()).update(matched)

    def path_cost(url: str) -> tuple[int, int]:
        path = urlparse(url).path.rstrip("/")
        return path.count("/"), len(path)

    chosen: list[str] = []
    for kw in LINK_KEYWORDS:
        if len(chosen) >= limit:
            break
        options = sorted((u for u, kws in candidates.items() if kw in kws and u not in chosen), key=path_cost)
        if options:
            chosen.append(options[0])
    return chosen


def candidate_links(html: str, page_url: str, domain: str, exclude: set[str], limit: int = 20) -> list[str]:
    """All same-site links matching CANDIDATE_KEYWORDS (not already fetched), most useful first."""
    soup = BeautifulSoup(html, "html.parser")
    scored: dict[str, tuple[int, int, int]] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = clean_url(urljoin(page_url, href))
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not same_site(url, domain) or url in exclude:
            continue
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):
            continue
        haystack = parsed.path.lower() + " " + a.get_text(" ", strip=True).lower()
        ranks = [i for i, kw in enumerate(CANDIDATE_KEYWORDS) if kw in haystack]
        if ranks:
            path = parsed.path.rstrip("/")
            scored[url] = min(scored.get(url, (99, 0, 0)), (min(ranks), path.count("/"), len(path)))
    return [u for u, _ in sorted(scored.items(), key=lambda kv: kv[1])][:limit]


# ---------------------------------------------------------------- cheap signals
def detect_signals(html: str, url: str, domain: str) -> list[DetectedSignal]:
    """Rule-based signals from one page's raw HTML (links + all visible text, including footers)."""
    icp = load_icp()
    found: list[DetectedSignal] = []
    soup = BeautifulSoup(html, "html.parser")

    # 1) Links or images pointing at Sprinto (=> existing customer) or a competitor (trust.vanta.com, Drata badge).
    for tag in soup.find_all(["a", "img", "iframe", "script"]):
        ref = tag.get("href") or tag.get("src") or ""
        host = urlparse(urljoin(url, ref)).hostname or ""
        if not host or same_site(host, domain):
            continue
        label = tag.get_text(" ", strip=True)[:80] or (tag.get("alt") or "")[:80]
        snippet = f"link to {host} {label}".strip()
        if domain_in_list(host, icp.sprinto_domains):
            found.append(DetectedSignal(signal="sprinto_link", source_url=url, snippet=snippet))
        elif comp := domain_in_list(host, icp.competitors):
            found.append(DetectedSignal(signal=f"competitor_link:{comp}", source_url=url, snippet=snippet))

    # 2) Text mentions.
    text = _visible_text(html, drop_chrome=False)
    for name, pattern in MENTION_PATTERNS.items():
        m = re.search(pattern, text, flags=re.I)
        if m:
            start, end = max(0, m.start() - 80), min(len(text), m.end() + 80)
            snippet = " ".join(text[start:end].split())
            found.append(DetectedSignal(signal=f"mentions:{name}", source_url=url, snippet=snippet))

    # 3) "Powered by Vanta" style text, using competitor brand names from ICP.md.
    brands = [c.split(".")[0] for c in icp.competitors if not same_site(c, domain) and c not in icp.sprinto_domains]
    if brands:
        m = re.search(r"(?:powered by|trust center by|verified by)\s+(" + "|".join(map(re.escape, brands)) + r")\b", text, re.I)
        if m:
            snippet = " ".join(text[max(0, m.start() - 40) : m.end() + 40].split())
            found.append(DetectedSignal(signal=f"competitor_text:{m.group(1).lower()}", source_url=url, snippet=snippet))

    unique: dict[tuple[str, str], DetectedSignal] = {}
    for s in found:
        unique.setdefault((s.signal, s.source_url), s)
    return list(unique.values())


# ---------------------------------------------------------------- polite fetcher
class Fetcher:
    """httpx client that respects robots.txt, waits between requests and never raises on HTTP errors."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None, sleep=time.sleep, delay: float = DELAY_SECONDS):
        self.settings = settings
        self.client = client or httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept": "text/html,application/xhtml+xml"},
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
        )
        self.sleep = sleep
        self.delay = delay
        self._last_request = 0.0
        self._robots: dict[str, RobotFileParser | None] = {}

    def _wait(self) -> None:
        if self._last_request:
            gap = time.monotonic() - self._last_request
            if gap < self.delay:
                self.sleep(self.delay - gap)
        self._last_request = time.monotonic()

    def _raw_get(self, url: str) -> httpx.Response:
        """GET with one retry on a network hiccup (connection reset, timeout)."""
        self._wait()
        try:
            return self.client.get(url)
        except httpx.TransportError:
            self.sleep(2.0)
            self._last_request = time.monotonic()
            return self.client.get(url)

    def allowed(self, url: str) -> bool:
        """Check robots.txt for this URL's host (fetched once per host)."""
        p = urlparse(url)
        host_root = f"{p.scheme}://{p.netloc}"
        if host_root not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                resp = self._raw_get(host_root + "/robots.txt")
                if resp.status_code in (401, 403):
                    parser.disallow_all = True
                elif resp.status_code >= 400:
                    parser = None  # no robots.txt = everything allowed
                else:
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError as exc:
                log.debug("robots.txt unreachable for %s (%s); treating as allowed", host_root, exc)
                parser = None
            self._robots[host_root] = parser
        parser = self._robots[host_root]
        return True if parser is None else parser.can_fetch(self.settings.user_agent, url)

    def fetch(self, url: str, domain: str) -> tuple[PageContent, str]:
        """Fetch one page. Returns (PageContent, raw_html). Errors are recorded, never raised."""
        if not same_site(url, domain):
            return PageContent(url=url, error="refused: different domain", quality="error"), ""
        if not self.allowed(url):
            return PageContent(url=url, error="skipped: disallowed by robots.txt", quality="error"), ""
        try:
            resp = self._raw_get(url)
        except httpx.HTTPError as exc:
            return PageContent(url=url, error=f"request failed: {type(exc).__name__}", quality="error"), ""
        final = str(resp.url)
        if not same_site(final, domain):
            return PageContent(url=url, status_code=resp.status_code, error=f"redirected off-site to {urlparse(final).hostname}", quality="error"), ""
        if resp.status_code >= 400:
            quality, message = http_error_quality(resp)
            return PageContent(url=final, status_code=resp.status_code, error=message, quality=quality), ""
        if "html" not in resp.headers.get("content-type", "html"):
            return PageContent(url=final, status_code=resp.status_code, error="not an HTML page", quality="error"), ""
        html = resp.text
        main = html_to_text(html, final)
        words = len(main.split())
        quality = assess_page(html, main, resp.status_code)
        # Add what the HTML carries for machines (meta description, JSON-LD, __NEXT_DATA__) if it's new.
        embedded = "\n".join(line for line in embedded_data_text(html).splitlines() if line not in main)
        budget = self.settings.max_chars_per_page - (len(embedded) + len(EMBEDDED_HEADER) + 4 if embedded else 0)
        text, cut = truncate(main, max(budget, 500))
        if embedded:
            text = f"{text}\n\n{EMBEDDED_HEADER}\n{embedded}".strip()
        return PageContent(url=final, status_code=resp.status_code, title=page_title(html), text=text, truncated=cut,
                           word_count=words, quality=quality, quality_note=quality_note(quality, words)), html

    def close(self) -> None:
        self.client.close()


# ---------------------------------------------------------------- main entry points
def scrape_domain(domain: str, settings: Settings, store: Store | None = None, fetcher: Fetcher | None = None, use_cache: bool = True) -> ScrapeResult:
    """Step 1 for one domain. Uses the cache when possible."""
    if store and use_cache:
        cached = store.get_cache("scrape", domain, max_age_days=SCRAPE_CACHE_DAYS)
        if cached:
            log.info("[%s] scrape: using cached result", domain)
            result = ScrapeResult.model_validate(cached)
            result.pages = [recheck_cached_page(p) for p in result.pages]
            return result

    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(settings)
    try:
        result = ScrapeResult(domain=domain, base_url=f"https://{domain}")
        home, html = fetcher.fetch(result.base_url, domain)
        if home.error and not domain.startswith("www."):
            alt, alt_html = fetcher.fetch(f"https://www.{domain}", domain)
            if not alt.error:
                home, html = alt, alt_html
        _record(result, home, html, domain)
        if html:
            for url in rank_links(html, home.url, domain, limit=settings.max_pages_per_domain - 1):
                page, page_html = fetcher.fetch(url, domain)
                _record(result, page, page_html, domain)
            fetched = {clean_url(p.url) for p in result.pages}
            result.candidate_links = candidate_links(html, home.url, domain, exclude=fetched)
        ok = sum(1 for p in result.pages if p.readable)
        log.info("[%s] scrape: %d page(s) readable, %d not, %d signal(s)", domain, ok, len(result.pages) - ok, len(result.detected_signals))
        for p in result.pages:
            if not p.error and not p.readable:
                log.warning("[%s] %s looks like a silent failure: %s", domain, p.url, p.quality_note)
    finally:
        if own_fetcher:
            fetcher.close()

    if store and any(not p.error for p in result.pages):
        store.set_cache("scrape", domain, result.model_dump(mode="json"))
    return result


def _record(result: ScrapeResult, page: PageContent, html: str, domain: str) -> None:
    if not page.error and any(clean_url(p.url) == clean_url(page.url) for p in result.pages if not p.error):
        return  # a link that redirected to a page we already have
    result.pages.append(page)
    if page.error and "robots" in page.error:
        result.skipped_by_robots.append(page.url)
    if html:
        result.detected_signals.extend(detect_signals(html, page.url, domain))


def fetch_extra_page(url: str, domain: str, settings: Settings, fetcher: Fetcher | None = None) -> tuple[PageContent, list[DetectedSignal]]:
    """Used by the agent loop's `fetch_page` action: one more page, same rules (same site, robots.txt)."""
    own = fetcher is None
    fetcher = fetcher or Fetcher(settings)
    try:
        target = url if "://" in url else f"https://{domain}/{url.lstrip('/')}"
        page, html = fetcher.fetch(clean_url(target), domain)
        return page, (detect_signals(html, page.url, domain) if html else [])
    finally:
        if own:
            fetcher.close()
