"""The ONE place the code talks to Google Gemini.

Two functions do all the work:
- `generate_json(prompt_name, variables, schema)` -> a pydantic object in the shape of `schema`.
- `research_with_search(question)` -> (answer_text, citations) using Google Search grounding.

Safety built in:
- Prompts are loaded from `prompts/<name>.md` (never written inside the code).
- Retries with exponential backoff on rate limits (429) and server errors (5xx).
- A per-domain call budget (`MAX_LLM_CALLS_PER_DOMAIN`). Call `start_domain(domain)` before
  working on a new domain; once the budget is used up, `LLMBudgetExceeded` is raised.
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


def start_domain(domain: str | None) -> None:
    """Reset the LLM call counter. Call this once at the start of each domain."""
    _budget.start(domain)


def calls_used() -> int:
    return _budget.used


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
    return genai.Client(api_key=settings.gemini_api_key.get_secret_value())


def _is_retryable(exc: BaseException) -> bool:
    """Retry on rate limits (429) and server-side errors (5xx); not on bad requests."""
    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.ClientError) and getattr(exc, "code", None) == 429:
        return True
    return False


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
    before_sleep=lambda rs: log.warning(
        "Gemini busy (%s). Retrying (attempt %d)...", type(rs.outcome.exception()).__name__, rs.attempt_number
    ),
)


@_retry
def _generate(contents: str, config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    settings = get_settings()
    settings.require("gemini")
    return get_client().models.generate_content(model=settings.gemini_model, contents=contents, config=config)


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
    _budget.spend(settings.max_llm_calls_per_domain)
    prompt = render_prompt(prompt_name, variables)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=temperature,
    )
    response = _generate(prompt, config)
    text = response.text or ""
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise LLMError(f"Gemini's answer for '{prompt_name}' did not match {schema.__name__}: {exc}") from exc


def research_with_search(question: str) -> tuple[str, list[Citation]]:
    """Answer a question using Google Search grounding. Returns (text, citations).

    Grounding can fail (quota, model doesn't support it, turned off in .env). In that case we
    log a warning and return ("", []), so the agent loop simply continues with website data only.
    """
    settings = get_settings()
    if not settings.use_search_grounding:
        log.warning("Search grounding is off (USE_SEARCH_GROUNDING=false); skipping web search.")
        return "", []
    _budget.spend(settings.max_llm_calls_per_domain)
    prompt = render_prompt("research", {"question": question})
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.0,
    )
    try:
        response = _generate(prompt, config)
    except errors.APIError as exc:
        log.warning("Web search (grounding) failed: %s. Continuing with website data only.", exc)
        return "", []
    return response.text or "", _extract_citations(response)


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
