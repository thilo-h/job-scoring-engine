"""Company / role / fit briefing, with every company claim tied to a source.

The briefing answers three questions before the applicant spends time on a
posting: who is this company, what does the role actually do, and where is the
honest gap. That is assistance — it informs a decision the human then makes.

Two patterns in here are the point of the module, beyond the summaries:

**Citations as a schema requirement.** A model asked about a company will
happily produce fluent, plausible, unsourced claims. So the tool schema does not
accept a company summary on its own: every company fact used in it has to arrive
in ``company_facts`` alongside the ``source_url`` of the search result it came
from. Sourcing is not a request in the prompt, which a model can drift away
from; it is a field it cannot omit.

**Deterministic verification, fed back as a repair round.** A required field is
only half the guard — the model can still cite a URL it never saw. So the code
collects the URLs the search tool actually returned and checks every cited URL
against that set. A citation that does not match is a finding, and findings go
back to the model as a ``tool_result`` saying the submission was not accepted,
together with what was wrong. The model revises; the check runs again; at most
``MAX_REPAIR_ROUNDS`` times. What survives is either sourced or gone.

The loop is worth more than the citation rule it enforces here: it is the shape
for any case where a deterministic rule can judge a model's output. The rule
stays in code, where it is reproducible and cheap; the model does the rewriting,
which is the part rules cannot do.

Research can be switched off with ``with_research=False``, which drops the cost
to a single cheap call and leaves the company summary working from the posting
alone — in which case it says so, rather than inventing depth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.llm import get_client

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CV_PATH = ROOT / "assets" / "example" / "cv_example.md"

# Haiku: three short summaries plus a handful of searches. The briefing is the
# cheap look at a posting, and it has to stay cheap to get used.
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 1500

# The newer search variant needs the Sonnet tier; this is the Haiku one.
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"

# Two rounds, then ship what is left. A third round has not fixed what two
# could not, and every round is a full call.
MAX_REPAIR_ROUNDS = 2

PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00
PRICE_CACHE_READ_PER_MTOK = 0.10
PRICE_CACHE_WRITE_PER_MTOK = 1.25


@dataclass
class Finding:
    """One deterministic objection to a submission."""
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class BriefingResult:
    """The three summaries the UI shows, plus what backs them up."""
    company_summary: str
    role_summary: str
    profile_summary: str
    # Every company fact with the search result it came from. Empty means the
    # briefing makes no claim about the company beyond the posting itself.
    company_facts: list[dict] = field(default_factory=list)
    # Objections that survived the repair rounds, surfaced to the applicant so
    # an unsourced claim is visible rather than silently trusted.
    findings: list[dict] = field(default_factory=list)
    web_searches: int = 0
    repair_rounds: int = 0
    # Telemetry
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        """What gets persisted as JSON and read back by the template."""
        return {
            "company_summary": self.company_summary,
            "role_summary": self.role_summary,
            "profile_summary": self.profile_summary,
            "company_facts": self.company_facts,
            "findings": self.findings,
            "web_searches": self.web_searches,
        }


_TOOL = {
    "name": "submit_briefing",
    "description": (
        "Submit the three-part briefing. Every claim about the company that is "
        "not in the job posting must appear in company_facts with the URL of the "
        "search result it came from."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company_summary": {
                "type": "string",
                "description": (
                    "Who the company is and what they do; size, stage, location "
                    "relevance. 2-3 sentences. Anything here that is not in the "
                    "posting needs an entry in company_facts."
                ),
            },
            "role_summary": {
                "type": "string",
                "description": (
                    "What the role does day to day and what the key "
                    "responsibility is. 2-3 sentences, from the posting."
                ),
            },
            "profile_summary": {
                "type": "string",
                "description": (
                    "Honest fit assessment against the CV: where it matches and "
                    "where the gap is. Direct, not flattering. 2-3 sentences."
                ),
            },
            "company_facts": {
                "type": "array",
                "description": (
                    "One entry per company claim in company_summary that came "
                    "from a search result. Empty array when the summary works "
                    "from the posting alone."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "The claim, as it is used in the summary.",
                        },
                        "source_url": {
                            "type": "string",
                            "description": (
                                "The URL of the search result this came from, "
                                "exactly as returned — not a guess, and not the "
                                "company's homepage by assumption."
                            ),
                        },
                    },
                    "required": ["fact", "source_url"],
                },
            },
        },
        "required": ["company_summary", "role_summary", "profile_summary", "company_facts"],
    },
}


class _Usage:
    """Accumulates usage across the repair rounds."""

    def __init__(self):
        self.input = self.output = self.cache_read = self.cache_write = 0

    def add(self, usage) -> None:
        self.input += getattr(usage, "input_tokens", 0) or 0
        self.output += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost(self) -> float:
        return (
            self.input * PRICE_INPUT_PER_MTOK
            + self.output * PRICE_OUTPUT_PER_MTOK
            + self.cache_read * PRICE_CACHE_READ_PER_MTOK
            + self.cache_write * PRICE_CACHE_WRITE_PER_MTOK
        ) / 1_000_000


def _normalise(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def search_result_urls(content) -> set[str]:
    """URLs the search tool actually returned.

    The result contents are opaque, but the URLs are not, and they are all the
    verification needs: a citation is either in this set or invented.
    """
    urls: set[str] = set()
    for block in content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        for item in getattr(block, "content", None) or []:
            url = getattr(item, "url", None) or (
                item.get("url") if isinstance(item, dict) else None
            )
            if url:
                urls.add(_normalise(url))
    return urls


def check_citations(data: dict, *, searched: set[str], did_search: bool) -> list[Finding]:
    """Deterministic objections to a submission. No model involved.

    Three of them, in the order they matter:

    * a cited URL that was never returned by a search — the failure this whole
      mechanism exists for
    * a company summary with substance but no citations at all, once searches
      have run, which is how an unsourced claim slips past a required field that
      was satisfied with an empty array
    * an empty fact or URL, which satisfies the schema and says nothing
    """
    findings: list[Finding] = []
    facts = data.get("company_facts") or []

    for i, entry in enumerate(facts, start=1):
        fact = (entry.get("fact") or "").strip()
        url = (entry.get("source_url") or "").strip()
        if not fact or not url:
            findings.append(Finding(
                "empty_citation",
                f"Company fact {i} is missing its text or its source URL.",
            ))
            continue
        if did_search and _normalise(url) not in searched:
            findings.append(Finding(
                "unsourced_fact",
                f"Company fact {i} cites {url}, which is not among the URLs the "
                f"search returned. Drop the claim, or replace it with one a "
                f"search result actually supports.",
            ))

    summary = (data.get("company_summary") or "").strip()
    if did_search and not facts and len(summary.split()) > 25:
        findings.append(Finding(
            "uncited_summary",
            "The company summary makes substantive claims but company_facts is "
            "empty. Either cite the searches for each claim, or cut the summary "
            "back to what the posting itself says.",
        ))
    return findings


def _submit_block(content):
    block = next(
        (b for b in content
         if getattr(b, "type", None) == "tool_use"
         and getattr(b, "name", "") == "submit_briefing"),
        None,
    )
    if block is None:
        seen = [(getattr(b, "type", "?"), getattr(b, "name", "")) for b in content]
        raise ValueError(f"Expected a submit_briefing tool call, got: {seen}")
    return block


class BriefingGenerator:
    """Three-point briefing for one posting, with sourced company claims."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        config: Optional[dict] = None,
    ):
        self.client = get_client(api_key, purpose="briefing generation")

        assets = (config or {}).get("assets") or {}
        cv_path = ROOT / assets.get("cv_md", DEFAULT_CV_PATH.relative_to(ROOT))
        self._cv_text = cv_path.read_text(encoding="utf-8")

        profile = (config or {}).get("profile") or {}
        self._profile_name = profile.get("name", "Alex Muster")
        self._background = (profile.get("background_summary") or "").strip()

    # -- prompts -----------------------------------------------------------

    def _system_blocks(self) -> list[dict]:
        """Stable first, so the cached prefix survives from posting to posting."""
        rules = (
            "You write short, factual briefings about job postings. Reply in the "
            "language of the posting: a German posting gets a German briefing, "
            "English gets English, Spanish gets Spanish. Two to three sentences "
            "per summary. No marketing language, no emoji.\n\n"
            "The rule that matters: you may not state anything about the company "
            "that is not either in the posting or in a search result you actually "
            "received. For every company claim that came from a search, record it "
            "in company_facts with that result's URL. If the searches gave you "
            "nothing usable, say what the posting says and leave company_facts "
            "empty — a short honest summary is worth more than a padded one.\n\n"
            "The fit assessment is for the applicant's own use. Name the gap "
            "plainly; flattery wastes their time."
        )
        return [
            {"type": "text", "text": rules, "cache_control": {"type": "ephemeral"}},
            {
                "type": "text",
                "text": (
                    f"## Applicant\nName: {self._profile_name}\n"
                    f"Background: {self._background}\n\n"
                    f"## CV\n{self._cv_text[:4000]}"
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ]

    @staticmethod
    def _user_prompt(*, job_title, company, location, description, url,
                     research_max_uses, with_research) -> str:
        research = ""
        if with_research:
            research = (
                f"\nBefore submitting, run 1–{research_max_uses} web searches about "
                f"'{company}' — what they do, recent news, how big they are. Then "
                f"write the briefing, and cite each company claim you take from a "
                f"result. Searching and then not citing is the one thing that gets "
                f"a submission rejected.\n"
            )
        return (
            f"Brief this posting for {company}.\n{research}\n"
            f"Title: {job_title}\n"
            f"Company: {company}\n"
            f"Location: {location or '(unknown)'}\n"
            f"URL: {url or '(none)'}\n\n"
            f"Posting text (truncated):\n{(description or '')[:3000] or '(none scraped)'}"
        )

    # -- the call ----------------------------------------------------------

    def generate(
        self,
        *,
        job_title: str,
        company: str,
        location: str,
        description: str,
        url: str = "",
        with_research: bool = True,
        research_max_uses: int = 3,
    ) -> BriefingResult:
        """Produce the briefing, verify its citations, repair once or twice.

        Never writes to the database — the caller persists the result.
        """
        tools: list[dict] = []
        if with_research:
            tools.append({
                "type": WEB_SEARCH_TOOL_TYPE,
                "name": "web_search",
                "max_uses": research_max_uses,
            })
        tools.append(_TOOL)

        # With search available the model has to orchestrate search-then-submit,
        # so the tool cannot be pinned to submit_briefing; "any" lets it do both.
        tool_choice = ({"type": "any"} if with_research
                       else {"type": "tool", "name": "submit_briefing"})

        system_blocks = self._system_blocks()
        messages: list[dict] = [{
            "role": "user",
            "content": self._user_prompt(
                job_title=job_title, company=company, location=location,
                description=description, url=url,
                research_max_uses=research_max_uses, with_research=with_research,
            ),
        }]

        response = self.client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=system_blocks,
            tools=tools, tool_choice=tool_choice, messages=messages,
        )

        usage = _Usage()
        usage.add(response.usage)

        searched = search_result_urls(response.content)
        n_searches = sum(
            1 for b in response.content
            if getattr(b, "type", None) == "server_tool_use"
            and getattr(b, "name", "") == "web_search"
        )
        tool_block = _submit_block(response.content)
        data = dict(tool_block.input)
        findings = check_citations(data, searched=searched, did_search=bool(n_searches))

        rounds = 0
        while findings and rounds < MAX_REPAIR_ROUNDS:
            rounds += 1
            objections = "\n".join(f"- {f.message}" for f in findings)
            logger.info("Briefing repair round %d: %s", rounds,
                        objections.replace("\n", " | "))
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_block.id,
                     "content": "Not accepted — the citation check found problems."},
                    {"type": "text", "text": (
                        "Revise and call submit_briefing again with the complete "
                        "briefing. Do not invent a different source for the same "
                        "claim: either it is supported by a result you received, "
                        "or it comes out.\n\n" + objections
                    )},
                ]},
            ]
            response = self.client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, system=system_blocks,
                tools=tools, tool_choice={"type": "tool", "name": "submit_briefing"},
                messages=messages,
            )
            usage.add(response.usage)
            # Searches from earlier rounds still count as sources.
            searched |= search_result_urls(response.content)
            tool_block = _submit_block(response.content)
            data = dict(tool_block.input)
            findings = check_citations(data, searched=searched,
                                       did_search=bool(n_searches))

        if findings:
            logger.info("Briefing kept %d unresolved finding(s)", len(findings))

        cost = usage.cost()
        logger.info(
            "Briefing for %s: %d search(es), %d repair round(s), $%.4f",
            company, n_searches, rounds, cost,
        )

        return BriefingResult(
            company_summary=(data.get("company_summary") or "").strip(),
            role_summary=(data.get("role_summary") or "").strip(),
            profile_summary=(data.get("profile_summary") or "").strip(),
            company_facts=data.get("company_facts") or [],
            findings=[f.to_dict() for f in findings],
            web_searches=n_searches,
            repair_rounds=rounds,
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read_tokens=usage.cache_read,
            cache_write_tokens=usage.cache_write,
            cost_usd=cost,
        )
