"""Anti-hallucination core: keeps every source we collected and checks the LLM's evidence against it.

A piece of evidence `{claim, source_url, quote}` is accepted only if:
1. `source_url` is a page we fetched, or a citation returned by a web search we ran, AND
2. `quote` really appears in that source's text (small differences in spacing/quotes/case are ignored).
For *absence* rules ("no trust page found") the quote can't appear anywhere, so only (1) is checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from outbound.models import Citation, DetectedSignal, Evidence, PageContent

_QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def normalise_text(text: str) -> str:
    """Lowercase, straighten quotes/dashes, collapse whitespace; for forgiving quote matching."""
    text = text.translate(_QUOTE_CHARS).lower()
    text = re.sub(r"[^\w%$€£.,'&/+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .")


def quote_in_text(quote: str, text: str, min_shingle_ratio: float = 0.8) -> bool:
    """True if `quote` appears in `text`: exactly (after normalising) or nearly (80% of 4-word chunks match)."""
    q, t = normalise_text(quote), normalise_text(text)
    if len(q) < 3:
        return False
    if q in t:
        return True
    words = q.split()
    if len(words) < 6:
        return False
    shingles = [" ".join(words[i : i + 4]) for i in range(len(words) - 3)]
    hits = sum(1 for s in shingles if s in t)
    return hits / len(shingles) >= min_shingle_ratio


def canonical_url(url: str) -> str:
    """Compare URLs loosely: no scheme, no 'www.', no trailing slash, no #fragment."""
    url = url.strip().split("#", 1)[0]
    url = re.sub(r"^https?://", "", url, flags=re.I)
    url = re.sub(r"^www\.", "", url, flags=re.I)
    return url.rstrip("/").lower()


@dataclass
class Source:
    url: str
    kind: str  # "page" | "search"
    text: str
    title: str = ""
    query: str = ""  # for search results


@dataclass
class EvidenceCorpus:
    """Everything the pipeline has seen for one company. Feeds prompts and verifies evidence."""

    sources: dict[str, Source] = field(default_factory=dict)  # canonical url -> Source

    # ---------- adding
    def add_page(self, page: PageContent, signals: list[DetectedSignal] | None = None) -> None:
        if page.error or not page.text:
            return
        extra = " ".join(s.snippet for s in (signals or []) if canonical_url(s.source_url) == canonical_url(page.url))
        self.sources[canonical_url(page.url)] = Source(page.url, "page", page.text + ("\n" + extra if extra else ""), page.title or "")

    def add_search(self, query: str, text: str, citations: list[Citation]) -> None:
        """A grounded search answer: every citation URL may be used as a source for quotes from `text`."""
        for c in citations:
            key = canonical_url(c.url)
            if key in self.sources and self.sources[key].kind == "search":
                self.sources[key].text += "\n" + text
            else:
                self.sources[key] = Source(c.url, "search", text, c.title, query)

    # ---------- checking
    def known_url(self, url: str) -> bool:
        return canonical_url(url) in self.sources

    def verify(self, ev: Evidence, absence: bool = False) -> bool:
        """Is this evidence backed by a real source (and, unless `absence`, a real quote)?"""
        source = self.sources.get(canonical_url(ev.source_url))
        if source is None:
            return False
        return absence or quote_in_text(ev.quote, source.text)

    def filter(self, evidence: list[Evidence], absence: bool = False) -> list[Evidence]:
        """Keep only verified evidence, de-duplicated."""
        kept, seen = [], set()
        for ev in evidence:
            key = (canonical_url(ev.source_url), normalise_text(ev.quote))
            if key not in seen and self.verify(ev, absence):
                seen.add(key)
                kept.append(ev)
        return kept

    # ---------- prompt rendering
    def as_prompt_block(self, max_chars_per_source: int = 8000) -> str:
        """All sources wrapped in <website_content> / <search_results> tags (treated as untrusted data)."""
        pages = [s for s in self.sources.values() if s.kind == "page"]
        searches: dict[str, list[Source]] = {}
        for s in self.sources.values():
            if s.kind == "search":
                searches.setdefault(s.query, []).append(s)
        parts = ["<website_content>"]
        seen: set[str] = set()
        for p in pages:
            text = drop_repeated_lines(p.text[:max_chars_per_source], seen)
            parts.append(f'<page url="{p.url}" title="{_attr(p.title)}">\n{text}\n</page>')
        parts.append("</website_content>")
        if searches:
            parts.append("<search_results>")
            for query, items in searches.items():
                urls = "\n".join(f"- {s.url} ({_attr(s.title)})" for s in items)
                parts.append(f'<search query="{_attr(query)}">\nCitable sources:\n{urls}\nAnswer text:\n{items[0].text[:max_chars_per_source]}\n</search>')
            parts.append("</search_results>")
        return "\n".join(parts)

    def urls(self) -> list[str]:
        return [s.url for s in self.sources.values()]

    # ---------- saving / loading (so later steps can verify against the same sources)
    def to_list(self) -> list[dict]:
        return [vars(s).copy() for s in self.sources.values()]

    @classmethod
    def from_list(cls, items: list[dict]) -> "EvidenceCorpus":
        corpus = cls()
        for item in items:
            src = Source(**item)
            corpus.sources[canonical_url(src.url)] = src
        return corpus


def drop_repeated_lines(text: str, seen: set[str]) -> str:
    """Remove lines already shown on an earlier page (menus, footers, cookie banners, repeated CTAs).

    Only the PROMPT is shortened: evidence is still verified against each page's full text, so a quote
    from a dropped line still counts. `seen` is shared across pages and updated in place.
    """
    kept = []
    for line in text.splitlines():
        key = normalise_text(line)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        kept.append(line)
    return "\n".join(kept)


def _attr(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ")[:120]
