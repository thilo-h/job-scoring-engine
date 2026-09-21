"""URL → structured Job extractor for the manual-add UI.

User pastes the URL of a job posting. The backend fetches the HTML, sends a
stripped-down version to Haiku, and extracts title/company/location/description
as structured JSON. The user reviews and edits it in the form before saving —
this fills in a form, it does not decide anything.

Failure modes handled:
  - The site declines the request, or its robots.txt asks tools not to fetch
    that path, or it is a SPA that returns nav-only HTML
    → return ``{"ok": False, "error": "..."}`` so the UI falls back to manual entry.
  - Page is genuinely empty / not a job posting → Haiku returns nulls and
    the UI just shows the form blank.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import requests

from src.llm import get_client
from src.scraper.rate_limiter import PoliteSession, RateLimiter, RobotsDisallowed

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 1200

PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00

ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


@dataclass
class ExtractedJob:
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    is_remote: Optional[bool] = None
    workload_percent: Optional[int] = None
    job_type: Optional[str] = None  # full_time / part_time / internship / contract
    languages: list[str] = None  # type: ignore[assignment]
    cost_usd: float = 0.0


def _fetch_html(url: str, timeout: int = 12) -> tuple[str, Optional[str]]:
    """Fetch one user-pasted URL as HTML. Returns (text, error_or_None).

    Goes through :class:`PoliteSession` like the scrapers do: the honest
    User-Agent, and robots.txt honoured. Some sites will refuse this — that is
    their decision to make, and the caller already has the right answer for it.
    Every failure here is a normal outcome, not an exception: the manual-add form
    falls back to letting the user type the fields in, which is what they would
    have done anyway.
    """
    session = PoliteSession(RateLimiter(min_delay=0, max_delay=0), ACCEPT)
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except RobotsDisallowed:
        return "", "This site's robots.txt asks tools not to fetch that path. Fill the form manually."
    except requests.RequestException as exc:
        return "", f"Fetch failed: {exc}"
    if resp.status_code in (403, 999):
        return "", f"The site declined the request (status {resp.status_code}). Fill the form manually."
    if resp.status_code >= 400:
        return "", f"HTTP {resp.status_code} from {url}"
    return resp.text, None


def _strip_html(html: str, max_chars: int = 18000) -> str:
    """Cheap HTML → text: drop scripts/styles/tags, collapse whitespace.
    Truncated to keep token budget bounded."""
    html = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


SYSTEM_PROMPT = """You extract structured job-posting data from messy HTML
content. The user pastes a URL of any job listing — company career page,
LinkedIn, Indeed, etc. — and you pick out the fields a job tracker needs.

Rules:
- Be conservative. If a field isn't clearly stated, leave it empty / null.
- ``description`` should be the substantive job description / requirements
  text — NOT page navigation, cookie banners, or company boilerplate.
  Trim to roughly 2000–4000 characters of the most relevant content.
- ``is_remote``: true if the posting explicitly says "remote", "fully
  remote", "100% remote". false if it says "on-site" / "office-based".
  null if unclear.
- ``workload_percent``: 100, 80, 60, etc. — Swiss-style "Pensum". Only set
  if explicitly mentioned. null otherwise.
- ``job_type``: one of full_time / part_time / internship / working_student /
  contract — null if unclear.
- ``languages``: list of language names mentioned as required (e.g.
  ["German", "English"]).

Reply ONLY via the ``submit_extraction`` tool — no chitchat.
"""


def extract_from_url(url: str, *, api_key: Optional[str] = None) -> dict:
    """Top-level entry. Returns a dict ready to fill the manual-add form.

    Shape: ``{"ok": bool, "url": str, "error": str|None, "extracted": ExtractedJob|None}``
    """
    html, fetch_err = _fetch_html(url)
    if fetch_err:
        return {"ok": False, "url": url, "error": fetch_err, "extracted": None}

    text = _strip_html(html)
    if len(text) < 200:
        return {
            "ok": False, "url": url,
            "error": "Page returned almost no text (likely JS-rendered SPA). Fill the form manually.",
            "extracted": None,
        }

    # A missing key is a normal outcome here, not an exception: the manual-add
    # form falls back to letting the user type the fields in.
    try:
        client = get_client(api_key, purpose="URL extraction")
    except RuntimeError as exc:
        return {"ok": False, "url": url, "error": str(exc), "extracted": None}

    tool = {
        "name": "submit_extraction",
        "description": "Submit the structured job-posting fields extracted from the page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Job title (e.g. 'Junior Brand Manager')"},
                "company": {"type": "string"},
                "location": {"type": "string", "description": "City, region or 'Remote'"},
                "description": {"type": "string", "description": "Substantive job description, 2000-4000 chars"},
                "is_remote": {"type": ["boolean", "null"]},
                "workload_percent": {"type": ["integer", "null"], "minimum": 10, "maximum": 100},
                "job_type": {
                    "type": ["string", "null"],
                    "enum": ["full_time", "part_time", "internship", "working_student", "contract", None],
                },
                "languages": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "company"],
        },
    }

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "submit_extraction"},
            messages=[{"role": "user", "content": (
                f"URL: {url}\n\n"
                f"Page content (HTML stripped to text):\n\n{text}"
            )}],
        )
    except Exception as exc:
        logger.exception("URL extraction LLM call failed")
        return {"ok": False, "url": url, "error": f"Claude call failed: {exc}", "extracted": None}

    tool_block = next(
        (b for b in response.content
         if b.type == "tool_use" and getattr(b, "name", "") == "submit_extraction"),
        None,
    )
    if tool_block is None:
        return {"ok": False, "url": url, "error": "Claude did not return structured extraction", "extracted": None}

    data = tool_block.input
    usage = response.usage
    cost = (
        getattr(usage, "input_tokens", 0) * PRICE_INPUT_PER_MTOK / 1_000_000
        + getattr(usage, "output_tokens", 0) * PRICE_OUTPUT_PER_MTOK / 1_000_000
    )

    extracted = ExtractedJob(
        title=data.get("title", "") or "",
        company=data.get("company", "") or "",
        location=data.get("location", "") or "",
        description=data.get("description", "") or "",
        is_remote=data.get("is_remote"),
        workload_percent=data.get("workload_percent"),
        job_type=data.get("job_type"),
        languages=data.get("languages") or [],
        cost_usd=cost,
    )
    logger.info(
        "url_extract %s @ %s (location=%s, $%.4f)",
        extracted.title[:40], extracted.company, extracted.location, cost,
    )
    return {
        "ok": True, "url": url, "error": None,
        "extracted": asdict(extracted),
    }
