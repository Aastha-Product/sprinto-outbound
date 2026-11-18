"""The ONE place the code talks to Google Gemini.

Two functions do all the work:
- `generate_json(prompt_name, variables, schema)` -> a pydantic object in the shape of `schema`.
- `research_with_search(question)` -> (answer_text, citations) using Google Search grounding.

Safety built in:
- Prompts are loaded from `prompts/<name>.md` (never written inside the code).
- Retries with exponential backoff on rate limits (429) and server errors (5xx).
- A per-domain call budget (`MAX_LLM_CALLS_PER_DOMAIN`). Call `start_domain(domain)` before
  working on a new domain; once the budget is used up, `LLMBudgetExceeded` is raised.
- A per-run ceiling (`MAX_LLM_CALLS_PER_RUN`, `MAX_SEARCHES_PER_RUN`), switched on by `start_run()` in the CLI.
  Reaching it stops the whole run cleanly (L4: "a loop without a limit is a cost without a ceiling").
- An answer cache (L4: "a company researched last week should cost nothing this week"): the same prompt to the
  same model returns the saved answer for free. Switched on by `use_store(store)`; cached answers count against
  no budget.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from outbound import PROJECT_ROOT
from outbound.config import get_settings
from outbound.models import Citation

log = logging.getLogger(__name__)

PROMPTS_DIR = PROJECT_ROOT / "prompts"
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model answered, but not in a usable shape."""


class LLMBudgetExceeded(RuntimeError):
    """Too many LLM calls for one domain (guardrail: MAX_LLM_CALLS_PER_DOMAIN)."""


class LLMFatalError(RuntimeError):
    """Gemini refuses every call (no credits, bad key, no access). The whole run should stop."""


class RunBudgetExceeded(LLMFatalError):
    """The per-run ceiling was reached. Stops the run like any fatal error; saved results are kept."""


FATAL_HINTS = {
    400: "Gemini rejected the request. If it mentions the API key, check GEMINI_API_KEY in .env.",
    401: "Gemini says the API key is not valid. Check GEMINI_API_KEY in .env.",
    402: "Your Gemini credits are used up. Add credit or switch the project to the free tier at https://aistudio.google.com (Billing).",
    403: "This Gemini API key has no access to the model. Check GEMINI_API_KEY / GEMINI_MODEL in .env.",
    404: "GEMINI_MODEL was not found. Run: python scripts\\list_models.py and pick a listed name.",
}


def _fatal(exc: errors.APIError) -> LLMFatalError | None:
    """Turn account-level errors into one clear message; other errors stay per-domain."""
    code = getattr(exc, "code", None)
    text = str(exc).lower()
    if code == 400 and "api key" not in text and "api_key" not in text:
        return None  # a bad prompt/schema: only this call failed
    if code in FATAL_HINTS:
        return LLMFatalError(f"Gemini error {code}: {FATAL_HINTS[code]}")
    return None


# ---------------------------------------------------------------- call budget
class _Budget:
    """Counts LLM calls for the current domain."""

    def __init__(self) -> None:
        self.domain: str | None = None
        self.used = 0

    def start(self, domain: str | None) -> None:
        self.domain = domain
        self.used = 0

    def spend(self, limit: int) -> None:
        if self.used >= limit:
            raise LLMBudgetExceeded(
                f"LLM call limit reached for {self.domain or 'this run'} "
                f"({self.used}/{limit}, MAX_LLM_CALLS_PER_DOMAIN)."
            )
        self.used += 1


_budget = _Budget()


def start_domain(domain: str | None, already_used: int = 0) -> None:
    """Reset the LLM call counter at the start of a domain.

    `already_used` lets the web app (one step per request) carry the count over from earlier steps,
    so the per-domain limit holds across requests.
    """
    _budget.start(domain)
    _budget.used = already_used


def calls_used() -> int:
    return _budget.used


# ---------------------------------------------------------------- per-run ceiling + usage stats
class _RunMeter:
    """Counts paid calls and searches for one CLI run. Inactive (no ceiling) until `start_run()`."""

    def __init__(self) -> None:
        self.active = False
        self.max_calls = self.max_searches = 0
        self.calls = self.searches = self.cached_calls = self.cached_searches = 0

    def charge(self, kind: str) -> None:
        if kind == "search":
            if self.active and self.searches >= self.max_searches:
                raise RunBudgetExceeded(f"Run budget reached: {self.searches} web searches (MAX_SEARCHES_PER_RUN). "
                                        "Everything finished so far is saved; raise the limit in .env to continue.")
            self.searches += 1
        else:
            if self.active and self.calls >= self.max_calls:
                raise RunBudgetExceeded(f"Run budget reached: {self.calls} Gemini calls (MAX_LLM_CALLS_PER_RUN). "
                                        "Everything finished so far is saved; raise the limit in .env to continue.")
            self.calls += 1


_run = _RunMeter()


def start_run(max_calls: int | None = None, max_searches: int | None = None) -> None:
    """Switch on the per-run ceiling and reset the counters (the CLI calls this once per run)."""
    s = get_settings()
    _run.__init__()
    _run.active = True
    _run.max_calls = max_calls if max_calls is not None else s.max_llm_calls_per_run
    _run.max_searches = max_searches if max_searches is not None else s.max_searches_per_run


def run_usage() -> dict[str, int]:
    """Paid vs cached calls and searches so far in this run (for the end-of-run summary)."""
    return {"calls": _run.calls, "cached_calls": _run.cached_calls, "searches": _run.searches,
            "cached_searches": _run.cached_searches, "max_calls": _run.max_calls, "max_searches": _run.max_searches}


# ---------------------------------------------------------------- answer cache
LLM_CACHE_DAYS = 14
SEARCH_CACHE_DAYS = 7
_store = None  # set by use_store(); None = no caching (e.g. in tests)


def use_store(store) -> None:
    """Give the LLM layer a Store so identical prompts are answered from the cache (Neon or files)."""
    global _store
    _store = store


def _cache_key(*parts: object) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:40]


def _cache_get(kind: str, key: str, days: int) -> dict | None:
    if _store is None:
        return None
    try:
        return _store.get_cache(kind, key, max_age_days=days)
    except Exception as exc:  # a cache problem must never break a run; just pay for the call
        log.debug("cache read failed (%s); calling Gemini", exc)
        return None


def _cache_set(kind: str, key: str, data: dict) -> None:
    if _store is None:
        return
    try:
        _store.set_cache(kind, key, data)
    except Exception as exc:
        log.debug("cache write failed (%s)", exc)


def calls_left() -> int:
    return max(get_settings().max_llm_calls_per_domain - _budget.used, 0)


# ---------------------------------------------------------------- prompts
def render_prompt(prompt_name: str, variables: dict[str, object]) -> str:
    """Load `prompts/<name>.md` and replace every `{{variable}}` with its value.

    We use `{{name}}` (not Python's `{name}`) so prompts can contain JSON examples safely.
    A missing variable is an error, so a half-filled prompt never reaches the model.
    """
    path = PROMPTS_DIR / f"{prompt_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    template = path.read_text(encoding="utf-8")
    needed = set(PLACEHOLDER_RE.findall(template))
    missing = needed - set(variables)
    if missing:
        raise KeyError(f"Prompt '{prompt_name}' needs variables: {sorted(missing)}")
    return PLACEHOLDER_RE.sub(lambda m: str(variables[m.group(1)]), template)


# ---------------------------------------------------------------- client + retries
@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    """Create the Gemini client once (needs GEMINI_API_KEY)."""
    settings = get_settings()
    settings.require("gemini_key")
    # A hard per-call time limit: without it a stuck request can freeze the whole run.
    return genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_SECONDS * 1000),
    )


GEMINI_TIMEOUT_SECONDS = 120


def _is_retryable(exc: BaseException) -> bool:
    """Retry on rate limits (429) and server-side errors (5xx); not on bad requests."""
    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.ClientError) and getattr(exc, "code", None) == 429:
        return True
    return False


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    # Free tier = a few calls per minute, so back off up to a minute (4s, 8s, 16s, 32s, 60s).
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(6),
    reraise=True,
    before_sleep=lambda rs: log.warning(
        "Gemini busy (%s). Waiting, then retrying (attempt %d)...", type(rs.outcome.exception()).__name__, rs.attempt_number
    ),
)


def _call(contents: str, config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    settings = get_settings()
    settings.require("gemini")
    return get_client().models.generate_content(model=settings.gemini_model, contents=contents, config=config)


_call_with_retry = _retry(_call)


def _generate(contents: str, config: types.GenerateContentConfig, retries: bool = True) -> types.GenerateContentResponse:
    """One Gemini call (with retries unless `retries=False`); account-level failures become `LLMFatalError`."""
    try:
        return (_call_with_retry if retries else _call)(contents, config)
    except errors.ClientError as exc:
        fatal = _fatal(exc)
        if fatal:
            raise fatal from None
        raise


def model_name() -> str:
    return get_settings().gemini_model


# ---------------------------------------------------------------- public API
def generate_json(
    prompt_name: str,
    variables: dict[str, object],
    schema: type[T],
    temperature: float = 0.0,
) -> T:
    """Ask Gemini for JSON in the exact shape of `schema` and return it as a pydantic object.

    Temperature 0 by default: the same input should give (nearly) the same answer.
    """
    settings = get_settings()
    prompt = render_prompt(prompt_name, variables)  # fail on a bad prompt BEFORE spending budget
    key = _cache_key("json", settings.gemini_model, prompt_name, prompt, schema.__name__, temperature)
    cached = _cache_get("llm", key, LLM_CACHE_DAYS)
    if cached and cached.get("text"):
        try:
            result = schema.model_validate_json(cached["text"])
            _run.cached_calls += 1
            log.debug("'%s': answered from cache (free)", prompt_name)
            return result
        except ValidationError:
            pass  # schema changed since it was cached: ask again
    _budget.spend(settings.max_llm_calls_per_domain)
    _run.charge("call")
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=llm_schema(schema),
        temperature=temperature,
    )
    response = _generate(prompt, config)
    text = response.text or ""
    if not text.strip():
        raise LLMError(f"Gemini returned an empty answer for '{prompt_name}' (possibly blocked by safety filters).")
    try:
        result = schema.model_validate_json(text)
    except ValidationError as exc:
        raise LLMError(f"Gemini's answer for '{prompt_name}' did not match {schema.__name__}: {exc}") from exc
    _cache_set("llm", key, {"text": text, "prompt": prompt_name, "model": settings.gemini_model})
    return result


def llm_schema(schema: type[BaseModel]) -> dict:
    """The JSON schema we send to Gemini: pydantic's schema minus `default`s (not useful to the model)."""

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "default"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(schema.model_json_schema())


def research_with_search(question: str) -> tuple[str, list[Citation]]:
    """Answer a question using Google Search grounding. Returns (text, citations).

    Grounding can fail (quota, model doesn't support it, turned off in .env). In that case we
    log a warning and return ("", []), so the agent loop simply continues with website data only.
    """
    if not search_available():
        log.warning("Web search is unavailable (%s); continuing with website data only.", _search_off_reason or "USE_SEARCH_GROUNDING=false")
        return "", []
    prompt = render_prompt("research", {"question": question})
    key = _cache_key("search", get_settings().gemini_model, prompt)
    cached = _cache_get("search", key, SEARCH_CACHE_DAYS)
    if cached and cached.get("text") and cached.get("citations"):
        _run.cached_searches += 1
        log.info("Web search '%s': answered from cache (free)", question)
        return cached["text"], [Citation.model_validate(c) for c in cached["citations"]]
    _budget.spend(get_settings().max_llm_calls_per_domain)
    _run.charge("search")
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.0,
    )
    try:
        response = _generate(prompt, config, retries=False)  # a quota error won't fix itself by retrying
    except LLMFatalError:
        raise
    except errors.APIError as exc:
        if getattr(exc, "code", None) in (403, 429):  # no grounding quota on this key: stop trying this run
            disable_search(f"Google Search quota not available for this key (HTTP {exc.code})")
        log.warning("Web search failed (HTTP %s). Continuing with website data only.", getattr(exc, "code", "?"))
        return "", []
    text, citations = response.text or "", _extract_citations(response)
    if text and citations:  # only cache answers that can be used as evidence
        _cache_set("search", key, {"text": text, "citations": [c.model_dump() for c in citations], "question": question})
    return text, citations


_search_off_reason = ""


def search_available() -> bool:
    """False if USE_SEARCH_GROUNDING=false or a search already hit a quota error in this process."""
    return get_settings().use_search_grounding and not _search_off_reason


def disable_search(reason: str) -> None:
    global _search_off_reason
    if not _search_off_reason:
        log.warning("Turning web search off for the rest of this run: %s", reason)
    _search_off_reason = reason


def _extract_citations(response: types.GenerateContentResponse) -> list[Citation]:
    """Pull the source web pages out of Gemini's grounding metadata (de-duplicated)."""
    citations: list[Citation] = []
    seen: set[str] = set()
    for candidate in response.candidates or []:
        meta = candidate.grounding_metadata
        if not meta or not meta.grounding_chunks:
            continue
        for chunk in meta.grounding_chunks:
            web = chunk.web
            if web and web.uri and web.uri not in seen:
                seen.add(web.uri)
                citations.append(Citation(url=web.uri, title=web.title or ""))
    if not citations:
        log.warning("Web search returned no citations; treat the answer as unverified.")
    return citations


def ping() -> str:
    """Tiny test call used by scripts/check_setup.py. Does not count towards any domain budget."""
    response = _generate(render_prompt("ping", {}), types.GenerateContentConfig(temperature=0.0))
    return (response.text or "").strip()
