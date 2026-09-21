"""The briefing: sourced company claims, and the repair loop that enforces them.

Two mechanisms are under test here, and they are independent. The schema makes a
citation impossible to omit. The deterministic check makes it impossible to fake.
Either one alone leaves a hole, so both get their own tests — including the holes
they were written to close.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent.briefing import (
    MAX_REPAIR_ROUNDS,
    BriefingGenerator,
    check_citations,
    search_result_urls,
)
from src.llm import MockAnthropic

FIXTURES = Path("fixtures/llm")


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- the check, in isolation -----------------------------------------------


def test_a_citation_matching_a_search_result_passes():
    data = {"company_summary": "Eighty people in Musterstadt.",
            "company_facts": [{"fact": "eighty people", "source_url": "https://example.com/about"}]}
    assert check_citations(data, searched={"https://example.com/about"}, did_search=True) == []


def test_a_citation_no_search_returned_is_reported():
    """The failure the whole mechanism exists for."""
    data = {"company_summary": "A two-billion unicorn.",
            "company_facts": [{"fact": "unicorn", "source_url": "https://crunchbase.example.com/x"}]}
    findings = check_citations(data, searched={"https://example.com/about"}, did_search=True)
    assert [f.code for f in findings] == ["unsourced_fact"]
    assert "crunchbase.example.com" in findings[0].message


def test_url_comparison_ignores_trailing_slash_and_case():
    """A citation must not be rejected over cosmetics."""
    data = {"company_summary": "x", "company_facts": [
        {"fact": "f", "source_url": "https://Example.com/About/"}]}
    assert check_citations(data, searched={"https://example.com/about"}, did_search=True) == []


def test_a_substantive_summary_with_no_citations_is_reported():
    """An empty array satisfies the schema. It must not satisfy the check."""
    data = {"company_summary": " ".join(["word"] * 40), "company_facts": []}
    findings = check_citations(data, searched={"https://example.com/a"}, did_search=True)
    assert [f.code for f in findings] == ["uncited_summary"]


def test_a_short_summary_with_no_citations_is_fine():
    """Working from the posting alone is a legitimate outcome, not a failure."""
    data = {"company_summary": "The posting says they work on grid analytics.",
            "company_facts": []}
    assert check_citations(data, searched={"https://example.com/a"}, did_search=True) == []


def test_empty_fact_or_url_is_reported():
    for facts in ([{"fact": "", "source_url": "https://example.com/a"}],
                  [{"fact": "something", "source_url": ""}]):
        findings = check_citations({"company_summary": "x", "company_facts": facts},
                                   searched={"https://example.com/a"}, did_search=True)
        assert [f.code for f in findings] == ["empty_citation"]


def test_without_a_search_citations_are_not_checked_against_results():
    """With research off there are no results to check against, so a URL from the
    posting itself must not be treated as invented."""
    data = {"company_summary": "x", "company_facts": [
        {"fact": "f", "source_url": "https://example.com/from-the-posting"}]}
    assert check_citations(data, searched=set(), did_search=False) == []


def test_search_urls_are_collected_from_the_result_blocks():
    response = MockAnthropic(scripted=[load("submit_briefing.json")]).messages.create(
        model="m", max_tokens=10, messages=[])
    urls = search_result_urls(response.content)
    assert "https://example.com/about" in urls
    assert len(urls) == 3


# --- end to end ------------------------------------------------------------


def test_clean_briefing_needs_no_repair(config):
    agent = BriefingGenerator(config=config)
    result = agent.generate(job_title="Data Engineer", company="Sample Energy",
                            location="Musterstadt", description="Grid analytics.")
    assert result.repair_rounds == 0
    assert result.findings == []
    assert result.web_searches == 2
    assert len(result.company_facts) == 3
    assert result.company_summary and result.role_summary and result.profile_summary


def test_every_fact_in_a_clean_briefing_is_traceable(config):
    """The property that matters: no claim without a URL a search returned."""
    agent = BriefingGenerator(config=config)
    result = agent.generate(job_title="T", company="Sample Energy",
                            location="L", description="D")
    searched = search_result_urls(
        MockAnthropic(scripted=[load("submit_briefing.json")]).messages.create(
            model="m", max_tokens=10, messages=[]).content)
    for fact in result.company_facts:
        assert fact["source_url"].rstrip("/").lower() in searched


def test_an_invented_source_triggers_a_repair_round(config):
    agent = BriefingGenerator(config=config)
    agent.client = MockAnthropic(scripted=load("submit_briefing_bad_citation.json"))
    result = agent.generate(job_title="Data Engineer", company="Sample Energy",
                            location="Musterstadt", description="Grid analytics.")
    assert result.repair_rounds == 1
    assert result.findings == []
    assert "unicorn" not in result.company_summary.lower()
    assert [f["source_url"] for f in result.company_facts] == ["https://example.com/about"]
    assert len(agent.client.calls) == 2


def test_the_objection_goes_back_as_a_tool_result(config):
    """A plain text complaint would read as conversation. A tool_result says the
    submission itself was not accepted."""
    agent = BriefingGenerator(config=config)
    agent.client = MockAnthropic(scripted=load("submit_briefing_bad_citation.json"))
    agent.generate(job_title="T", company="C", location="L", description="D")

    second_call = agent.client.calls[1]
    last = second_call["messages"][-1]["content"]
    kinds = [b["type"] for b in last]
    assert "tool_result" in kinds
    tool_result = next(b for b in last if b["type"] == "tool_result")
    assert tool_result["tool_use_id"] == "toolu_briefing_bad"
    objection = next(b["text"] for b in last if b["type"] == "text")
    assert "crunchbase.example.com" in objection

    # The rejected turn is replayed as the assistant's own, so the model sees
    # what it submitted rather than a paraphrase of it.
    assert second_call["messages"][-2]["role"] == "assistant"


def test_the_repair_loop_is_bounded(config):
    """A model that never fixes the citation must not loop forever."""
    stubborn = load("submit_briefing_bad_citation.json")[0]
    agent = BriefingGenerator(config=config)
    agent.client = MockAnthropic(scripted=[stubborn])
    result = agent.generate(job_title="T", company="C", location="L", description="D")
    assert result.repair_rounds == MAX_REPAIR_ROUNDS
    assert len(agent.client.calls) == MAX_REPAIR_ROUNDS + 1


def test_unresolved_findings_are_surfaced_not_swallowed(config):
    """What survives the rounds has to reach the applicant, or the guard is
    decoration."""
    stubborn = load("submit_briefing_bad_citation.json")[0]
    agent = BriefingGenerator(config=config)
    agent.client = MockAnthropic(scripted=[stubborn])
    result = agent.generate(job_title="T", company="C", location="L", description="D")
    assert result.findings
    assert result.findings[0]["code"] == "unsourced_fact"
    assert result.findings == result.to_dict()["findings"]


def test_cost_accumulates_across_repair_rounds(config):
    """Each round is a full call; reporting only the last one would understate it."""
    agent = BriefingGenerator(config=config)
    agent.client = MockAnthropic(scripted=load("submit_briefing_bad_citation.json"))
    two_rounds = agent.generate(job_title="T", company="C", location="L", description="D")

    agent.client = MockAnthropic(scripted=[load("submit_briefing_bad_citation.json")[1]])
    one_round = agent.generate(job_title="T", company="C", location="L", description="D")
    assert two_rounds.cost_usd > one_round.cost_usd


# --- request shape ---------------------------------------------------------


def test_research_off_forces_the_tool_and_declares_no_search(config):
    agent = BriefingGenerator(config=config)
    agent.client = MockAnthropic(scripted=[load("submit_briefing_bad_citation.json")[1]])
    agent.generate(job_title="T", company="C", location="L", description="D",
                   with_research=False)
    call = agent.client.last_call
    assert call["tool_choice"] == {"type": "tool", "name": "submit_briefing"}
    assert not any("web_search" in str(t.get("type", "")) for t in call["tools"])


def test_research_on_leaves_the_tool_choice_open(config):
    """Pinning submit_briefing would skip the search entirely."""
    agent = BriefingGenerator(config=config)
    agent.generate(job_title="T", company="C", location="L", description="D")
    assert agent.client.calls[0]["tool_choice"] == {"type": "any"}


def test_citation_field_is_required_by_the_schema(config):
    """Sourcing is a schema obligation, not a request in the prompt."""
    agent = BriefingGenerator(config=config)
    agent.generate(job_title="T", company="C", location="L", description="D")
    tool = next(t for t in agent.client.last_call["tools"] if "input_schema" in t)
    schema = tool["input_schema"]
    assert "company_facts" in schema["required"]
    item = schema["properties"]["company_facts"]["items"]
    assert item["required"] == ["fact", "source_url"]


def test_the_cv_and_rules_are_cached_and_the_job_is_not(config):
    agent = BriefingGenerator(config=config)
    agent.generate(job_title="Zzyzx Quantum Beekeeper", company="Qqqq",
                   location="Nowhere", description="unique-desc-77")
    call = agent.client.calls[0]
    assert len(agent.client.cached_system_blocks(call)) == 2
    system_text = " ".join(b["text"] for b in call["system"])
    assert "Zzyzx" not in system_text
    assert "unique-desc-77" not in system_text


def test_result_dict_carries_the_evidence_for_the_ui(config):
    """The template reads this back out of the DB, so it has to be in to_dict."""
    agent = BriefingGenerator(config=config)
    result = agent.generate(job_title="T", company="C", location="L", description="D")
    d = result.to_dict()
    assert set(d) >= {"company_summary", "role_summary", "profile_summary",
                      "company_facts", "findings", "web_searches"}
    assert json.loads(json.dumps(d))    # persisted as JSON


def test_a_missing_tool_call_raises_rather_than_returning_blanks(config):
    agent = BriefingGenerator(config=config)
    agent.client = MockAnthropic(scripted=[{
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Here is your briefing in prose."}],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }])
    with pytest.raises(ValueError, match="submit_briefing"):
        agent.generate(job_title="T", company="C", location="L", description="D")
