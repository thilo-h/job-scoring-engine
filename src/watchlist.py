"""Watchlist-Modus — zweiter, paralleler Suchpfad neben der Breitensuche.

Die Breitensuche filtert die Welt über den Relevance-Score. Die Watchlist
filtert nicht, sie beobachtet: jede neue Stelle bei einer der Firmen aus
``config/watchlist_<profil>.yaml`` wird gemeldet, auch bei Score 0.3.

Ein Poll hat drei Phasen:

1. **Karriereseiten scrapen** — nur Einträge mit ``careers_url`` und ohne
   ``channel: direct_email``. Nutzt den bestehenden ``CareerPagesScraper``.
2. **Breitensuche durchkämmen** — jede Stelle in der DB, deren Firmenname
   auf einen Watchlist-Eintrag passt, egal aus welcher Quelle. Das ist nötig,
   weil in der Praxis ein grosser Teil der Einträge keine ``careers_url``
   trägt — die Breitensuche findet diese Firmen aber oft trotzdem.
3. **Keyword-Scan** — frische Stellen aller Firmen auf ``alerting.keyword_hits``
   (z.B. "Masterarbeit"), unabhängig von Firma und Score.

Gegen Alert-Fluten: eine Stelle wird nur dann sofort gemeldet, wenn die Firma
schon vor diesem Poll beobachtet wurde, die Quelle für diese Firma schon
bekannt war und die Stelle frisch ist. Alles andere ist Bestand ("seeded").
Wer also später eine ``careers_url`` nachträgt, bekommt nicht den gesamten
Bestand dieser Firma auf einmal, sondern ab dem nächsten Tag nur die wirklich neuen.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from src.config import CONFIG_DIR
from src.notify import NotifyResult, send_to_self
from src.scoring import JobScorer, _compile_patterns

logger = logging.getLogger(__name__)

TIER_ORDER = ["A", "A2", "B", "C", "D"]
TIER_LABELS = {
    "A": "Gebäude & Energie",
    "A2": "Mobilität & Transport",
    "B": "Data-/AI-Beratungen",
    "C": "Forschung & Institute",
    "D": "Wildcards",
}

# Nur Stellen, die jünger sind, gelten als "neu". Schützt vor Alerts für alte
# Stellen, die erst jetzt einer Firma zugeordnet werden (z.B. neuer Alias).
ALERT_FRESH_DAYS = 14
# Erster Keyword-Scan überhaupt: so weit zurückschauen statt die ganze DB.
KEYWORD_FIRST_LOOKBACK_DAYS = 7

_LEGAL_SUFFIX_RE = re.compile(r"\s+(ag|gmbh|sa|ltd|inc|group|holding)\.?$", re.IGNORECASE)
_ROLE_RANK = {"strong": 0, "weak": 1, None: 2}


# Der generische HTML-Parser in career_pages.py sammelt alle Links unter
# /career/, /jobs/ … ein. Auf einer SPA-Karriereseite sind davon regelmässig
# dreissig Treffer und kein einziger eine Stelle: Länderseiten, Themenseiten,
# ein Podcast. Andere Seiten liefern saubere Titel. Echte Stellentitel enthalten
# fast immer ein Berufs-Substantiv oder ein Pensum — nur solche Links übernimmt
# die Watchlist, wenn kein ATS-Adapter die Daten liefert.
_JOB_TITLE_RE = re.compile(
    r"\b(engineer|ingenieur\w*|manager\w*|consultant|berater\w*|scientist|analyst\w*|"
    r"developer|entwickler\w*|architect|architekt\w*|specialist|spezialist\w*|expert\w*|"
    r"mitarbeiter\w*|praktik\w*|intern|internship|trainee|werkstudent\w*|lead|leiter\w*|"
    r"owner|designer\w*|researcher|forscher\w*|techniker\w*|technician|assistent\w*|"
    r"assistant|sachbearbeiter\w*|officer|coordinator|koordinator\w*|planer\w*|"
    r"associate|director|head|doktorand\w*|postdoc|phd|masterarbeit|thesis|lehrstelle|"
    r"lernende\w*|apprentice|operator|monteur\w*|elektriker\w*|controller|accountant|"
    r"buchhalter\w*|recruiter|administrator|support)\b"
    r"|\d{2,3}\s*%",
    re.IGNORECASE,
)


def looks_like_job_title(title: str) -> bool:
    return bool(_JOB_TITLE_RE.search(title or ""))


def _normalize_company(name: str) -> str:
    text = (name or "").lower().replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _normalize_company(name)).strip("-")


@dataclass
class WatchEntry:
    name: str
    tier: str
    city: str = ""
    vertical: str = ""
    careers_url: Optional[str] = None
    ats: Optional[str] = None
    ats_slug: Optional[str] = None
    channel: str = "scrape"
    aliases: list[str] = field(default_factory=list)
    why: str = ""
    note: str = ""
    _patterns: list[re.Pattern] = field(default_factory=list, repr=False)

    @property
    def key(self) -> str:
        return _slug(self.name)

    @property
    def is_direct_email(self) -> bool:
        return self.channel == "direct_email"

    @property
    def scrape_entry(self) -> Optional[dict]:
        """Eintrag im Format von ``search.career_pages`` — None, wenn nichts zu scrapen ist."""
        if self.ats and self.ats_slug:
            return {"name": self.name, "ats": self.ats, "ats_slug": self.ats_slug, "location": self.city}
        if self.careers_url:
            return {"name": self.name, "url": self.careers_url, "location": self.city}
        return None

    def matches_company(self, normalized_company: str) -> bool:
        return any(p.search(normalized_company) for p in self._patterns)


@dataclass
class Watchlist:
    path: Path
    entries: list[WatchEntry]
    instant_tiers: set[str]
    digest_tiers: set[str]
    keyword_hits: list[str]
    _kw_patterns: list[re.Pattern] = field(repr=False)
    _role_strong: list[re.Pattern] = field(repr=False)
    _role_weak: list[re.Pattern] = field(repr=False)
    _boost: list[re.Pattern] = field(repr=False)
    _exclude_title: list[tuple[str, re.Pattern]] = field(repr=False)
    _exclude_text: list[tuple[str, re.Pattern]] = field(repr=False)
    _company_cache: dict[str, Optional[WatchEntry]] = field(default_factory=dict, repr=False)

    def entry_for(self, company: str) -> Optional[WatchEntry]:
        """Watchlist-Eintrag zu einem Firmennamen aus der DB, sonst None."""
        normalized = _normalize_company(company)
        if normalized not in self._company_cache:
            self._company_cache[normalized] = next(
                (e for e in self.entries if e.matches_company(normalized)), None
            )
        return self._company_cache[normalized]

    def keywords_in(self, text: str) -> list[str]:
        lowered = (text or "").lower()
        return [kw for kw, p in zip(self.keyword_hits, self._kw_patterns, strict=True) if p.search(lowered)]

    def excluded_by(self, title: str, description: Optional[str]) -> Optional[str]:
        """Welches hard_exclude greift — Titel-Liste nur auf den Titel."""
        t = (title or "").lower()
        for raw, p in self._exclude_title:
            if p.search(t):
                return raw
        d = f"{t}\n{(description or '').lower()}"
        for raw, p in self._exclude_text:
            if p.search(d):
                return raw
        return None

    def role_class(self, title: str) -> Optional[str]:
        t = (title or "").lower()
        if any(p.search(t) for p in self._role_strong):
            return "strong"
        if any(p.search(t) for p in self._role_weak):
            return "weak"
        return None

    def boost_count(self, text: str) -> int:
        lowered = (text or "").lower()
        return sum(1 for p in self._boost if p.search(lowered))


def watchlist_path(profile: str) -> Path:
    return CONFIG_DIR / f"watchlist_{profile}.yaml"


def load_watchlist(profile: str) -> Optional[Watchlist]:
    """Lädt ``config/watchlist_<profil>.yaml``. None, wenn es keine gibt."""
    path = watchlist_path(profile)
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    entries: list[WatchEntry] = []
    for item in raw.get("companies") or []:
        name = (item.get("name") or "").strip()
        tier = str(item.get("tier") or "").strip()
        if not name:
            continue
        if tier not in TIER_LABELS:
            # Kommentar-Überschriften gehen beim YAML-Parsen verloren, deshalb
            # muss jeder Eintrag sein Tier selbst tragen. Lieber laut scheitern
            # als eine Firma still ohne Alert laufen lassen.
            raise ValueError(f"{path.name}: '{name}' hat kein gültiges tier ({tier!r})")
        aliases = [a.strip().lower() for a in item.get("aliases") or [] if a and a.strip()]
        if not aliases:
            aliases = [_LEGAL_SUFFIX_RE.sub("", _normalize_company(name))]
        entries.append(WatchEntry(
            name=name,
            tier=tier,
            city=item.get("city") or "",
            vertical=item.get("vertical") or "",
            careers_url=(item.get("careers_url") or None),
            ats=(item.get("ats") or None),
            ats_slug=(item.get("ats_slug") or None),
            channel=item.get("channel") or "scrape",
            aliases=aliases,
            why=item.get("why") or "",
            note=item.get("note") or "",
            _patterns=_compile_patterns(aliases),
        ))

    alerting = raw.get("alerting") or {}
    keyword_hits = [str(k) for k in alerting.get("keyword_hits") or []]
    roles = raw.get("role_patterns") or {}
    excludes = raw.get("hard_excludes") or {}
    if isinstance(excludes, list):  # altes, flaches Format → alles nur im Titel
        excludes = {"title": excludes, "text": []}

    def paired(words: list[str]) -> list[tuple[str, re.Pattern]]:
        return list(zip(words, _compile_patterns(words), strict=True))

    return Watchlist(
        path=path,
        entries=entries,
        instant_tiers=set(alerting.get("instant_tiers") or ["A", "A2", "C"]),
        digest_tiers=set(alerting.get("digest_tiers") or TIER_ORDER),
        keyword_hits=keyword_hits,
        _kw_patterns=_compile_patterns(keyword_hits),
        _role_strong=_compile_patterns(roles.get("strong") or []),
        _role_weak=_compile_patterns(roles.get("weak") or []),
        _boost=_compile_patterns(raw.get("boost_keywords") or []),
        _exclude_title=paired(excludes.get("title") or []),
        _exclude_text=paired(excludes.get("text") or []),
    )


# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------

@dataclass
class ScrapeOutcome:
    entry: WatchEntry
    jobs: int = 0
    new_jobs: int = 0
    rejected: int = 0              # HTML-Links ohne plausiblen Stellentitel
    note: Optional[str] = None     # z.B. "HTTP 404" — landet im Digest
    error: Optional[str] = None


@dataclass
class PollResult:
    started_at: datetime
    scraped: list[ScrapeOutcome] = field(default_factory=list)
    skipped_direct_email: list[WatchEntry] = field(default_factory=list)
    skipped_no_url: list[WatchEntry] = field(default_factory=list)
    matched_jobs: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    keyword_hits: int = 0
    alerted: int = 0
    alert: Optional[NotifyResult] = None
    alert_error: Optional[str] = None


def poll(
    config: dict,
    db,
    watchlist: Watchlist,
    *,
    scrape: bool = True,
    notify: bool = True,
    now: Optional[datetime] = None,
) -> PollResult:
    """Ein kompletter Watchlist-Durchlauf. Idempotent — mehrfach am Tag ist ok."""
    now = now or datetime.now()
    result = PollResult(started_at=now)

    state = {
        row["company_key"]: dict(row)
        for row in db.conn.execute("SELECT * FROM watchlist_companies")
    }
    known_sources = {k: set(json.loads(v["sources_seen"] or "[]")) for k, v in state.items()}

    # --- Phase 1: Karriereseiten -----------------------------------------
    careers_counts: dict[str, int] = {}
    careers_notes: dict[str, Optional[str]] = {}
    scraper = scorer = None
    for entry in watchlist.entries:
        if entry.is_direct_email:
            result.skipped_direct_email.append(entry)
            continue
        target = entry.scrape_entry
        if target is None:
            result.skipped_no_url.append(entry)
            continue
        if not scrape:
            continue
        if scraper is None:
            from src.scraper.career_pages import CareerPagesScraper
            from src.scraper.rate_limiter import RateLimiter
            scraper = CareerPagesScraper(rate_limiter=RateLimiter.from_config(config))
            scorer = JobScorer(config)
        outcome = ScrapeOutcome(entry=entry)
        via_html = "ats" not in target
        try:
            for job in scraper.scrape_company(target):
                if via_html and not looks_like_job_title(job.title):
                    outcome.rejected += 1
                    continue
                job.relevance_score = scorer.score(job)
                outcome.new_jobs += int(db.upsert_job(job))
                outcome.jobs += 1
            if outcome.jobs == 0 and via_html:
                outcome.note = _diagnose_empty_page(scraper, entry.careers_url, outcome.rejected)
        except Exception as exc:  # eine kaputte Seite darf den Poll nicht stoppen
            outcome.error = str(exc)
            outcome.note = f"Fehler: {exc}"[:200]
            logger.warning("[watchlist] %s: %s", entry.name, exc)
        careers_counts[entry.key] = outcome.jobs
        careers_notes[entry.key] = outcome.note
        result.scraped.append(outcome)
        scraper._rate_limiter.wait()

    # --- Phase 2: alle Stellen den Watchlist-Firmen zuordnen ---------------
    hits = {
        row["job_id"]: dict(row)
        for row in db.conn.execute("SELECT job_id, reasons, alert_status FROM watchlist_hits")
    }
    fresh_cutoff = (now - timedelta(days=ALERT_FRESH_DAYS)).isoformat()
    aggregates: dict[str, dict] = {}
    new_rows: list[tuple] = []

    jobs = db.conn.execute(
        "SELECT id, title, company, source, date_scraped, application_status FROM jobs"
    ).fetchall()
    for job in jobs:
        entry = watchlist.entry_for(job["company"])
        if entry is None:
            continue
        result.matched_jobs += 1
        agg = aggregates.setdefault(entry.key, {"sources": set(), "count": 0, "last": None})
        agg["sources"].add(job["source"])
        agg["count"] += 1
        if not agg["last"] or (job["date_scraped"] or "") > agg["last"]:
            agg["last"] = job["date_scraped"]

        if job["id"] in hits:
            continue
        description = db.conn.execute(
            "SELECT description FROM jobs WHERE id = ?", (job["id"],)
        ).fetchone()["description"]
        reasons = [f"tier:{entry.tier}"]
        excluded = watchlist.excluded_by(job["title"], description)
        if excluded:
            status = "excluded"
            reasons.append(f"exclude:{excluded}")
        elif (
            entry.key not in state                                  # Firma zum ersten Mal beobachtet
            or job["source"] not in known_sources.get(entry.key, set())  # Quelle neu angebunden
            or (job["date_scraped"] or "") < fresh_cutoff           # alte Stelle, neu zugeordnet
            or job["application_status"] != "new"                   # schon angefasst
        ):
            status = "seeded"
        elif entry.tier in watchlist.instant_tiers:
            status = "pending"
        else:
            status = "digest_only"
        row = (job["id"], entry.key, entry.name, entry.tier,
               watchlist.role_class(job["title"]), ";".join(reasons), status, now.isoformat())
        new_rows.append(row)
        hits[job["id"]] = {"job_id": job["id"], "reasons": row[5], "alert_status": status}
        result.status_counts[status] = result.status_counts.get(status, 0) + 1

    db.conn.executemany(
        "INSERT INTO watchlist_hits (job_id, company_key, company_name, tier, role_class, "
        "reasons, alert_status, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        new_rows,
    )

    # --- Phase 3: Keyword-Scan über frische Stellen aller Firmen -----------
    last_scan = db.get_meta("watchlist.keyword_scan_at")
    since = last_scan or (now - timedelta(days=KEYWORD_FIRST_LOOKBACK_DAYS)).isoformat()
    for job in db.conn.execute(
        "SELECT id, title, company, description FROM jobs "
        "WHERE date_scraped >= ? AND application_status NOT IN ('ignored', 'archived')",
        (since,),
    ):
        found = watchlist.keywords_in(f"{job['title']}\n{job['description'] or ''}")
        if not found:
            continue
        kw_reasons = [f"kw:{k}" for k in found]
        existing = hits.get(job["id"])
        if existing is None:
            entry = watchlist.entry_for(job["company"])
            db.conn.execute(
                "INSERT INTO watchlist_hits (job_id, company_key, company_name, tier, role_class, "
                "reasons, alert_status, detected_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (job["id"], entry.key if entry else None, entry.name if entry else None,
                 entry.tier if entry else None, watchlist.role_class(job["title"]),
                 ";".join(kw_reasons), now.isoformat()),
            )
            hits[job["id"]] = {"reasons": ";".join(kw_reasons), "alert_status": "pending"}
            result.keyword_hits += 1
            continue
        old = existing["reasons"].split(";")
        added = [r for r in kw_reasons if r not in old]
        if not added:
            continue
        # Keyword-Treffer werden gemeldet, egal wie die Firmen-Regel entschied —
        # ausser die Stelle ist schon raus.
        status = existing["alert_status"]
        if status in ("seeded", "excluded", "digest_only"):
            status = "pending"
        db.conn.execute(
            "UPDATE watchlist_hits SET reasons = ?, alert_status = ? WHERE job_id = ?",
            (";".join(old + added), status, job["id"]),
        )
        existing.update(reasons=";".join(old + added), alert_status=status)
        result.keyword_hits += 1
    db.conn.commit()
    db.set_meta("watchlist.keyword_scan_at", now.isoformat())

    # --- Firmen-Zustand (inkl. sources_seen) -------------------------------
    for entry in watchlist.entries:
        agg = aggregates.get(entry.key) or {"sources": set(), "count": 0, "last": None}
        db.conn.execute(
            """INSERT INTO watchlist_companies (
                   company_key, name, tier, channel, careers_url, sources_seen,
                   job_count, careers_page_jobs, careers_page_note, last_hit_at,
                   first_polled_at, last_polled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_key) DO UPDATE SET
                   name = excluded.name, tier = excluded.tier, channel = excluded.channel,
                   careers_url = excluded.careers_url, sources_seen = excluded.sources_seen,
                   job_count = excluded.job_count,
                   careers_page_jobs = COALESCE(excluded.careers_page_jobs,
                                                watchlist_companies.careers_page_jobs),
                   careers_page_note = CASE WHEN excluded.careers_page_jobs IS NULL
                                            THEN watchlist_companies.careers_page_note
                                            ELSE excluded.careers_page_note END,
                   last_hit_at = excluded.last_hit_at,
                   last_polled_at = excluded.last_polled_at""",
            (entry.key, entry.name, entry.tier, entry.channel, entry.careers_url,
             json.dumps(sorted(agg["sources"])), agg["count"],
             careers_counts.get(entry.key), careers_notes.get(entry.key), agg["last"],
             now.isoformat(), now.isoformat()),
        )
    db.conn.commit()

    # --- Sofort-Alert ------------------------------------------------------
    if notify:
        pending = pending_alerts(db)
        if pending:
            subject, body = render_alert(pending, watchlist, now=now)
            try:
                result.alert = send_to_self(config, subject=subject, body=body, slug="watchlist-alert")
                db.conn.executemany(
                    "UPDATE watchlist_hits SET alert_status = 'alerted', alerted_at = ? WHERE job_id = ?",
                    [(now.isoformat(), h["job_id"]) for h in pending],
                )
                db.conn.commit()
                result.alerted = len(pending)
            except Exception as exc:
                # Bleibt 'pending' → nächster Poll versucht es erneut.
                result.alert_error = str(exc)
                logger.error("[watchlist] Alert-Versand fehlgeschlagen: %s", exc)
    return result


def _diagnose_empty_page(scraper, url: str, rejected: int) -> str:
    """Warum liefert eine Karriereseite nichts? Eine Zeile für den Digest.

    ``_scrape_company`` schluckt HTTP-Fehler (loggt nur auf debug). Für die
    Watchlist ist der Unterschied aber wichtig: 404 heisst "URL falsch,
    nachtragen", 200 ohne Treffer heisst "SPA, ATS-Angabe nachtragen".
    """
    try:
        status = scraper._session.get(url, timeout=15).status_code
    except Exception as exc:
        return f"nicht erreichbar ({type(exc).__name__})"
    if status != 200:
        return f"HTTP {status} — URL prüfen"
    if rejected:
        return f"{rejected} Links, keiner sieht nach Stelle aus — ats/ats_slug nachtragen"
    return "Seite lädt, aber keine Stellen im HTML (SPA) — ats/ats_slug nachtragen"


def pending_alerts(db) -> list[dict]:
    rows = db.conn.execute(
        """SELECT h.*, j.title, j.company, j.location, j.url, j.source,
                  j.relevance_score, j.description
           FROM watchlist_hits h JOIN jobs j ON j.id = h.job_id
           WHERE h.alert_status = 'pending'"""
    ).fetchall()
    return [dict(r) for r in rows]


def recent_hits(db, *, since: datetime) -> list[dict]:
    """Neue Treffer (ohne Bestand/Ausschlüsse) seit ``since`` — für Digest und Dashboard."""
    rows = db.conn.execute(
        """SELECT h.*, j.title, j.company, j.location, j.url, j.source,
                  j.relevance_score, j.application_status
           FROM watchlist_hits h JOIN jobs j ON j.id = h.job_id
           WHERE h.detected_at >= ?
             AND h.alert_status IN ('pending', 'alerted', 'digest_only')
           ORDER BY h.detected_at DESC""",
        (since.isoformat(),),
    ).fetchall()
    return [dict(r) for r in rows]


def company_states(db) -> list[dict]:
    rows = db.conn.execute("SELECT * FROM watchlist_companies").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sources"] = json.loads(d.get("sources_seen") or "[]")
        out.append(d)
    rank = {t: i for i, t in enumerate(TIER_ORDER)}
    return sorted(out, key=lambda d: (rank.get(d["tier"], 99), d["name"].lower()))


# ---------------------------------------------------------------------------
# Darstellung
# ---------------------------------------------------------------------------

def _sort_key(hit: dict, watchlist: Watchlist) -> tuple:
    text = f"{hit.get('title') or ''}\n{hit.get('description') or ''}"
    return (_ROLE_RANK.get(hit.get("role_class"), 2),
            -watchlist.boost_count(text),
            -(hit.get("relevance_score") or 0))


def _dedupe(hits: list[dict]) -> list[dict]:
    """Dieselbe Stelle über Karriereseite und jobs.ch nur einmal zeigen."""
    seen: set[tuple] = set()
    out = []
    for h in hits:
        key = (h.get("company_key") or h.get("company"), re.sub(r"\W+", " ", (h.get("title") or "").lower()).strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def format_hit(hit: dict) -> str:
    role = {"strong": "Rolle: stark", "weak": "Rolle: schwach"}.get(hit.get("role_class"), "")
    meta = " · ".join(x for x in [
        hit.get("location") or "",
        hit.get("source") or "",
        f"Score {hit['relevance_score']:.2f}" if hit.get("relevance_score") is not None else "",
        role,
    ] if x)
    return f"{display_company(hit)} — {hit.get('title')}\n  {meta}\n  {hit.get('url')}"


def display_company(hit: dict) -> str:
    """Firmenname so, wie er in der Stelle steht — nicht der Watchlist-Eintrag.

    Ein Watchlist-Eintrag kann enger benannt sein als seine Aliase treffen —
    etwa ein einzelnes Institut einer Hochschule, dessen Aliase jede Stelle
    dieser Hochschule erfassen. Mit dem Eintragsnamen sähe dann eine fachfremde
    Stelle aus, als käme sie aus diesem Institut. Dass es ein Watchlist-Treffer
    ist, sagt bereits die Tier-Überschrift darüber.
    """
    return (hit.get("company") or hit.get("company_name") or "?").strip()


def render_alert(pending: list[dict], watchlist: Watchlist, *, now: datetime) -> tuple[str, str]:
    by_tier: dict[str, list[dict]] = {}
    keyword_only: list[dict] = []
    for h in pending:
        if h.get("tier") in watchlist.instant_tiers:
            by_tier.setdefault(h["tier"], []).append(h)
        else:
            keyword_only.append(h)

    lines = [f"Watchlist — {len(pending)} neue Stelle{'n' if len(pending) != 1 else ''} ({now:%d.%m.%Y})", ""]
    for tier in TIER_ORDER:
        tier_hits = _dedupe(sorted(by_tier.get(tier, []), key=lambda h: _sort_key(h, watchlist)))
        if not tier_hits:
            continue
        lines += [f"━━ Tier {tier} · {TIER_LABELS[tier]} ━━", ""]
        for h in tier_hits:
            kws = [r[3:] for r in h["reasons"].split(";") if r.startswith("kw:")]
            lines.append(format_hit(h) + (f"\n  Keyword: {', '.join(kws)}" if kws else ""))
            lines.append("")
    if keyword_only:
        lines += ["━━ Keyword-Treffer ━━", ""]
        for h in _dedupe(sorted(keyword_only, key=lambda h: _sort_key(h, watchlist))):
            kws = [r[3:] for r in h["reasons"].split(";") if r.startswith("kw:")]
            lines.append(f"«{', '.join(kws)}» · " + format_hit(h))
            lines.append("")
    lines += [
        "—",
        "Warum diese Mail: neue Stelle bei einer Watchlist-Firma "
        f"(Tier {', '.join(t for t in TIER_ORDER if t in watchlist.instant_tiers)}) "
        "oder ein Keyword-Treffer — unabhängig vom Relevance-Score.",
        "Tier B und D stehen gesammelt im Montags-Digest.",
    ]

    companies = []
    for h in pending:
        name = (h.get("company") or h.get("company_name") or "").strip()
        name = name if len(name) <= 28 else name[:26].rstrip() + "…"
        if name and name not in companies:
            companies.append(name)
    tail = ", ".join(companies[:3]) + (" …" if len(companies) > 3 else "")
    subject = f"Watchlist: {len(pending)} neu — {tail}"
    return subject, "\n".join(lines)
