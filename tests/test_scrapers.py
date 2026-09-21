"""Scraper parsing, against local fixtures only.

The `no_network` guard in conftest means these can never accidentally hit the
real sites. What is tested is the parsing — the part that breaks when a site is
redesigned — not the fetching.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from src.models import SourcePortal
from src.scraper.barcelonajobs import BarcelonaJobsScraper
from src.scraper.base import BaseScraper
from src.scraper.rate_limiter import RateLimiter


def rss_item(title: str, link: str = "https://example.com/job/1",
             description: str = "A job description.") -> ET.Element:
    xml = f"""<item>
        <title>{title}</title>
        <link>{link}</link>
        <description>{description}</description>
        <pubDate>Mon, 01 Sep 2026 08:00:00 +0000</pubDate>
    </item>"""
    return ET.fromstring(xml)


@pytest.fixture
def scraper() -> BarcelonaJobsScraper:
    return BarcelonaJobsScraper(rate_limiter=RateLimiter(min_delay=0, max_delay=0))


# --- the documented title pattern -----------------------------------------


def test_splits_title_company_location(scraper):
    job = scraper._parse_item(rss_item("Data Engineer - Sample Energy - Barcelona, Spain"))
    assert job is not None
    assert job.title == "Data Engineer"
    assert job.company == "Sample Energy"
    assert "Barcelona" in job.location


def test_extra_hyphens_in_the_title_are_handled(scraper):
    """Splitting from the right is what makes this work."""
    job = scraper._parse_item(
        rss_item("Beca - Business Dev - Avnet - BCN, ES"))
    assert job is not None
    assert job.company == "Avnet"
    assert job.location == "BCN, ES"
    assert "Business Dev" in job.title


def test_source_is_tagged(scraper):
    job = scraper._parse_item(rss_item("Data Engineer - Sample Energy - Barcelona"))
    assert job.source is SourcePortal.BARCELONAJOBS


def test_unparseable_title_yields_no_invented_company(scraper):
    """A title that does not follow the pattern is kept — it still has a title and
    a URL, which is enough to be useful — but the company stays empty rather than
    being guessed at. An invented company name would feed straight into the tier
    scoring and the dedup key."""
    job = scraper._parse_item(rss_item("Just a title with no separators"))
    assert job is not None
    assert job.company == ""
    assert job.title == "Just a title with no separators"


def test_missing_link_is_skipped(scraper):
    item = rss_item("Data Engineer - Sample Energy - Barcelona", link="")
    assert scraper._parse_item(item) is None


# --- the base contract ----------------------------------------------------


def test_every_scraper_implements_the_interface():
    """A scraper missing `scrape`/`name`/`source` fails at runtime, not import."""
    from src.main import SCRAPER_REGISTRY

    for name, (module_path, class_name) in SCRAPER_REGISTRY.items():
        mod = __import__(module_path, fromlist=[class_name])
        cls = getattr(mod, class_name)
        assert issubclass(cls, BaseScraper), name
        instance = cls(rate_limiter=RateLimiter(min_delay=0, max_delay=0))
        assert isinstance(instance.name, str) and instance.name
        assert isinstance(instance.source, SourcePortal)


def test_no_scraper_targets_a_prohibited_portal():
    """LinkedIn, Indeed and Glassdoor prohibit this in their terms. Their absence
    is a deliberate design decision, so it deserves a test rather than a comment
    somebody deletes."""
    from src.main import SCRAPER_REGISTRY

    banned = ("linkedin", "indeed", "glassdoor")
    for name, (module_path, _) in SCRAPER_REGISTRY.items():
        assert not any(b in name.lower() or b in module_path.lower() for b in banned), name

    sources = {s.value.lower() for s in SourcePortal}
    assert not any(b in s for b in banned for s in sources)


# --- rate limiting --------------------------------------------------------


def test_rate_limiter_reads_its_settings_from_the_profile(config):
    limiter = RateLimiter.from_config(config)
    cfg = config["scrapers"]["rate_limiting"]
    assert limiter.min_delay == cfg["min_delay_seconds"]
    assert limiter.max_delay == cfg["max_delay_seconds"]
    assert limiter.respect_robots is cfg["respect_robots_txt"]


def test_robots_checking_is_on_in_the_shipped_profile(config):
    """If this is ever flipped to false, it should be a visible decision."""
    assert config["scrapers"]["rate_limiting"]["respect_robots_txt"] is True


# --- identification and robots.txt --------------------------------------
# The README claims this tool identifies itself and honours robots.txt. These
# tests exist because that claim was once false in both halves: the check was
# dead code and every scraper sent a Chrome User-Agent. A compliance claim
# nobody verifies is worse than no claim.


def test_user_agent_does_not_impersonate_a_browser():
    from src.scraper.rate_limiter import USER_AGENT
    lowered = USER_AGENT.lower()
    for browser in ("mozilla", "chrome", "safari", "applewebkit", "gecko", "edge"):
        assert browser not in lowered, f"UA impersonates a browser: {USER_AGENT}"


def test_user_agent_names_the_tool_and_its_version():
    """The default is a bare name — valid and honest on its own. A contact URL or
    address is a worthwhile addition but belongs in the profile, not in a default
    that ships with a placeholder in it."""
    from src.scraper.rate_limiter import USER_AGENT
    assert USER_AGENT.startswith("JobScoringEngine/")


def test_a_contact_string_can_be_configured(config):
    from src.scraper.rate_limiter import RateLimiter as RL
    cfg = {**config}
    cfg["scrapers"] = {**cfg["scrapers"], "rate_limiting": {
        **cfg["scrapers"]["rate_limiting"],
        "user_agent": "JobScoringEngine/0.1 (+mailto:you@example.com)",
    }}
    limiter = RL.from_config(cfg)
    assert "mailto:you@example.com" in limiter.user_agent
    assert limiter.headers()["User-Agent"] == limiter.user_agent


def test_no_scraper_module_carries_its_own_browser_user_agent():
    """A per-module HEADERS dict is how the honest UA got bypassed before."""
    import pathlib as _p
    for path in _p.Path("src/scraper").glob("*.py"):
        assert "Mozilla/5.0" not in path.read_text(encoding="utf-8"), path.name


def test_every_scraper_fetches_through_the_polite_session(config):
    """Structural, not a convention: a raw requests.Session would skip the check."""
    from src.main import SCRAPER_REGISTRY
    from src.scraper.rate_limiter import PoliteSession

    for name, (module_path, class_name) in SCRAPER_REGISTRY.items():
        mod = __import__(module_path, fromlist=[class_name])
        instance = getattr(mod, class_name)(
            rate_limiter=RateLimiter(min_delay=0, max_delay=0))
        assert isinstance(instance._session, PoliteSession), name


def test_polite_session_refuses_a_disallowed_url():
    from src.scraper.rate_limiter import PoliteSession, RobotsDisallowed

    class Refusing(RateLimiter):
        def can_fetch(self, url):
            return False

    session = PoliteSession(Refusing(min_delay=0, max_delay=0))
    with pytest.raises(RobotsDisallowed):
        session.get("https://example.com/jobs")


def test_a_disallowed_url_is_a_request_exception():
    """Scrapers already wrap fetches; a refusal has to travel that same path so
    it is logged and skipped rather than crashing a run."""
    import requests

    from src.scraper.rate_limiter import RobotsDisallowed
    assert issubclass(RobotsDisallowed, requests.RequestException)


def test_robots_rules_are_evaluated_under_the_ua_that_is_sent():
    """Checking robots as one agent and fetching as another is not compliance."""
    from src.scraper.rate_limiter import PoliteSession

    seen = {}

    class Recording(RateLimiter):
        def can_fetch(self, url):
            seen["ua_checked"] = self.user_agent
            return False

    limiter = Recording(min_delay=0, max_delay=0)
    session = PoliteSession(limiter)
    try:
        session.get("https://example.com/x")
    except Exception:
        pass
    assert seen["ua_checked"] == session.headers["User-Agent"]


def test_robots_check_can_be_turned_off_but_defaults_on():
    limiter = RateLimiter(respect_robots=False)
    assert limiter.can_fetch("https://example.com/anything") is True
    assert RateLimiter().respect_robots is True


def test_unreachable_robots_txt_is_treated_as_permission(monkeypatch):
    """A momentary network error must not read as a prohibition — but it must
    also not be silently ignored, so it is logged at debug."""
    from urllib.robotparser import RobotFileParser

    def boom(self):
        raise OSError("network down")

    monkeypatch.setattr(RobotFileParser, "read", boom)
    assert RateLimiter().can_fetch("https://example.com/jobs") is True


def test_default_user_agent_contains_no_unfilled_placeholder():
    """This string is transmitted on every request. A forgotten "<your-name>"
    would announce a half-configured tool to every site it touches — in exactly
    the header that is supposed to establish good faith."""
    from src.scraper.rate_limiter import USER_AGENT
    for marker in ("<", ">", "TODO", "your-", "example.com"):
        assert marker not in USER_AGENT, f"placeholder in UA: {USER_AGENT}"
