"""Rate limiting, honest identification and robots.txt compliance.

Three things a scraper owes the site it reads, all of them enforced here rather
than left to each scraper to remember:

* **Say who you are.** :data:`USER_AGENT` identifies the tool and points at its
  source. A browser User-Agent would get through more often — that is exactly
  why it is not used: a site that wants to refuse this tool has to be able to
  recognise it, and `robots.txt` rules only mean something if the name you
  evaluate them under is the name you send.
* **Ask before fetching.** :meth:`can_fetch` reads and caches `robots.txt` per
  host and evaluates the path under the same User-Agent that goes on the wire.
* **Do not hammer.** :meth:`wait` sleeps a randomised delay between requests.

Set ``scrapers.rate_limiting.user_agent`` in the profile to override the
identification string — e.g. to add a contact address, which is good etiquette
for anything that runs unattended.
"""

import logging
import random
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import certifi
import requests

logger = logging.getLogger(__name__)

# Honest by default. Deliberately not a browser string: see the module docstring.
#
# No placeholder in here on purpose. This string goes out in an HTTP header on
# every request, so an unreplaced "<your-name>" would be transmitted verbatim to
# every site — announcing a half-configured tool in the one place that is
# supposed to establish good faith. A bare name is valid and honest on its own;
# a contact URL or address is a worthwhile addition, so set it in the profile
# under `scrapers.rate_limiting.user_agent`.
USER_AGENT = "JobScoringEngine/0.1"


class RateLimiter:
    def __init__(self, min_delay: float = 2.0, max_delay: float = 5.0,
                 respect_robots: bool = True, user_agent: str = USER_AGENT):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self._robot_parsers: dict[str, RobotFileParser] = {}

    # -- identification ----------------------------------------------------

    def headers(self, accept: str = "*/*") -> dict:
        """Request headers every scraper should use, so the UA is consistent."""
        return {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": "en;q=0.9,de;q=0.8",
        }

    # -- politeness --------------------------------------------------------

    def wait(self) -> None:
        """Sleep a random duration between min and max delay."""
        delay = random.uniform(self.min_delay, self.max_delay)
        time.sleep(delay)

    def can_fetch(self, url: str) -> bool:
        """Whether robots.txt allows this URL for our User-Agent.

        An unreachable or malformed robots.txt is treated as permission — that is
        the conventional reading, and the alternative would make a momentary
        network error look like a prohibition. A rule that does apply is obeyed
        and logged, so a source that silently yields nothing can be explained.
        """
        if not self.respect_robots:
            return True

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return True
        base = f"{parsed.scheme}://{parsed.netloc}"

        if base not in self._robot_parsers:
            rp = RobotFileParser()
            rp.set_url(f"{base}/robots.txt")
            try:
                rp.read()
            except Exception as e:
                logger.debug("Could not read robots.txt for %s: %s", base, e)
                return True
            self._robot_parsers[base] = rp

        allowed = self._robot_parsers[base].can_fetch(self.user_agent, url)
        if not allowed:
            logger.warning("robots.txt disallows, skipping: %s", url)
        return allowed

    @classmethod
    def from_config(cls, config: dict) -> "RateLimiter":
        """Create a RateLimiter from the scrapers.rate_limiting config section."""
        rate_config = config.get("scrapers", {}).get("rate_limiting", {})
        return cls(
            min_delay=rate_config.get("min_delay_seconds", 2.0),
            max_delay=rate_config.get("max_delay_seconds", 5.0),
            respect_robots=rate_config.get("respect_robots_txt", True),
            user_agent=rate_config.get("user_agent") or USER_AGENT,
        )


class RobotsDisallowed(requests.RequestException):
    """robots.txt forbids this URL for our User-Agent.

    A subclass of ``RequestException`` on purpose: every scraper already wraps
    its fetches, so a disallowed URL is logged and skipped through the same path
    as a timeout, without any scraper needing to know this rule exists.
    """


class PoliteSession(requests.Session):
    """A session that cannot skip the robots.txt check.

    The check used to live in :class:`RateLimiter` and was called by nobody — the
    machinery existed, the scrapers fetched around it, and the README claimed
    compliance the code did not deliver. Putting it in the transport makes it
    structural: a new scraper gets it by construction, and forgetting is not an
    available mistake.

    The honest User-Agent and the CA bundle are set here for the same reason.
    """

    def __init__(self, limiter: "RateLimiter", accept: str = "*/*"):
        super().__init__()
        self._limiter = limiter
        self.verify = certifi.where()          # macOS ships without system roots
        self.headers.update(limiter.headers(accept))

    def request(self, method, url, *args, **kwargs):
        if not self._limiter.can_fetch(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        return super().request(method, url, *args, **kwargs)
