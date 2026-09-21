"""Shared dependencies: per-profile DB + Jinja2 environment.

Multi-profile model
-------------------
The dashboard resolves the active profile per request via the
``jobfinder_profile`` cookie (set by the header dropdown) or, as a fallback,
the ``JOBFINDER_PROFILE`` env var. Each profile has its own config YAML
(``config/profile_<name>.yaml``), its own SQLite database (path from
``output.database_path``), and its own attachment catalog (from
``assets.attachments``).

``db`` and ``templates`` stay importable as module-level names for routes;
they're proxies that look up the per-profile resources at attribute access.
"""

import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from src.config import DEFAULT_PROFILE, load_config, resolve_profile
from src.database import JobDatabase
from src.models import ApplicationStatus, SourcePortal

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
PROJECT_ROOT = ROOT.parent

# Attachment paths in the profile YAML are full project-relative paths, so
# `ASSETS_DIR` is just an alias to the project root for any lingering
# references.
ASSETS_DIR = PROJECT_ROOT

# ContextVar holds the active profile name for the current request. The
# middleware in app.py sets/resets it around each request.
# The default is a str and therefore immutable; it is only a fallback for code
# paths that run outside a request (CLI, background tasks).
current_profile: ContextVar[str] = ContextVar(
    "current_profile", default=resolve_profile()  # noqa: B039
)

# Caches keyed by profile name. JobDatabase keeps a single sqlite connection
# per profile; with WAL mode + single-worker uvicorn that's safe.
_config_cache: dict[str, dict] = {}
_db_cache: dict[str, JobDatabase] = {}


def get_config() -> dict:
    """Return the active profile's config dict, loading lazily."""
    profile = current_profile.get()
    if profile not in _config_cache:
        _config_cache[profile] = load_config(profile=profile)
    return _config_cache[profile]


def get_db() -> JobDatabase:
    """Return the active profile's JobDatabase, loading lazily."""
    profile = current_profile.get()
    if profile not in _db_cache:
        cfg = get_config()
        db = JobDatabase(cfg["output"]["database_path"])
        db.init_schema()
        _db_cache[profile] = db
    return _db_cache[profile]


class _DBProxy:
    """Attribute-forwarding proxy so existing ``from .deps import db`` keeps
    working while ``db.<method>`` resolves to the active profile's instance.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_db(), name)


db = _DBProxy()


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _from_json(s):
    """Jinja filter: parse a JSON string, returning None on failure/empty."""
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


# Regex für defensives Parsen von XML-style "<item>foo</item>"-Strings
# (kommt von Haiku-Triage-Outputs die das Array-Schema ignoriert haben).
_AS_LIST_XML_RE = __import__("re").compile(
    r"<item[^>]*>(.*?)</item>", __import__("re").IGNORECASE | __import__("re").DOTALL
)


def _as_list(value, max_items: int = 5):
    """Jinja filter: normalisiert ein Feld zu einer Liste[str], robust gegen
    historische Daten-Quirks (z.B. alte Triage-Records wo strengths/gaps als
    XML-String statt Array gespeichert sind).

    Verwendung im Template:
        {% for s in match_details.strengths | as_list %}<li>{{ s }}</li>{% endfor %}
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()][:max_items]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        # XML-style <item>...</item> Patterns
        items = _AS_LIST_XML_RE.findall(s)
        if items:
            return [i.strip() for i in items if i.strip()][:max_items]
        # Bullet/Zeilen-Liste
        import re as _re
        lines = [_re.sub(r"^[-•*\d+\.\s]+", "", line).strip()
                 for line in s.split("\n") if line.strip()]
        lines = [line for line in lines if line]
        if len(lines) > 1:
            return lines[:max_items]
        # Kommagetrennte einzelne Zeile (als letzte Fallback-Heuristik)
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if len(parts) > 1:
            return parts[:max_items]
        return [s]
    return []


def _days_ago(iso_string):
    """Jinja filter: ISO date/datetime → integer days since.

    Returns None for empty/invalid input so the template can skip rendering.
    """
    if not iso_string:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso_string)
        return max(0, (datetime.now() - dt).days)
    except (ValueError, TypeError):
        return None


templates.env.filters["from_json"] = _from_json
templates.env.filters["days_ago"] = _days_ago
templates.env.filters["as_list"] = _as_list

# Pipeline tab columns (in order). `new` & `ignored` deliberately excluded:
# they live on the Browse tab.
PIPELINE_STATUSES = ["bookmarked", "applied", "interview", "offer", "rejected"]

# Friendly labels for status pills/buttons.
STATUS_LABELS = {
    "new": "New",
    "bookmarked": "Bookmarked",
    "applied": "Applied",
    "interview": "Interview",
    "offer": "Offer",
    "rejected": "Rejected",
    "ignored": "Ignored",
    "archived": "Archived",   # Retention: Score < 0.5, 60 Tage unangetastet
}

ALL_STATUSES = [s.value for s in ApplicationStatus]
ALL_SOURCES = [s.value for s in SourcePortal]

templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["PIPELINE_STATUSES"] = PIPELINE_STATUSES
templates.env.globals["ALL_STATUSES"] = ALL_STATUSES


# ----------------------------------------------------------------------
# Application attachments — pulled from active profile's `assets.attachments`
# ----------------------------------------------------------------------


def attachment_catalog() -> list[dict]:
    """Return the attachment catalog for the active profile."""
    return list((get_config().get("assets") or {}).get("attachments") or [])


def attachment_catalog_with_existence() -> list[dict]:
    """Return the catalog enriched with `exists` and `size_kb`. Missing files
    are kept in the list so the user sees what's referenced but unavailable.

    Each ``entry["file"]`` is a project-root-relative path (e.g.
    ``"assets/example/cv_example.md"``)."""
    out = []
    for entry in attachment_catalog():
        path = PROJECT_ROOT / entry["file"]
        item = dict(entry)
        item["exists"] = path.exists()
        item["size_kb"] = round(path.stat().st_size / 1024) if path.exists() else 0
        out.append(item)
    return out


# Exposed so templates can show "switch active profile" UI.
def list_profiles() -> list[str]:
    """Discover all configured profiles by scanning config/profile_*.yaml."""
    config_dir = Path(__file__).resolve().parent.parent / "config"
    profiles = []
    for p in sorted(config_dir.glob("profile_*.yaml")):
        name = p.stem.removeprefix("profile_")
        profiles.append(name)
    return profiles or [DEFAULT_PROFILE]


# Cache-buster for /static/style.css — uses the file's mtime so the URL
# changes whenever the stylesheet is edited. base.html appends this as a
# `?v=` query string, which forces the browser to re-fetch instead of
# serving the cached version.
def _asset_version() -> str:
    css_path = ROOT / "static" / "style.css"
    try:
        return str(int(css_path.stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_version"] = _asset_version


templates.env.globals["LANGUAGE_OPTIONS"] = [
    {"value": "", "label": "Auto"},
    {"value": "de", "label": "Deutsch"},
    {"value": "en", "label": "English"},
    {"value": "es", "label": "Español"},
]
templates.env.globals["FORMAT_OPTIONS"] = [
    {"value": "", "label": "Auto"},
    {"value": "flowing", "label": "Flowing (Brief)"},
    {"value": "structured", "label": "Structured (Bullets)"},
]
