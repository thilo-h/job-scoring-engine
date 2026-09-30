"""SQLite database operations and CSV export."""

import csv
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from src.experience_extractor import extract_for_job as extract_min_years_for_job
from src.models import ApplicationStatus, Job
from src.workload_extractor import extract_for_job

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    external_id TEXT,

    location TEXT,
    canton TEXT,
    is_remote INTEGER,

    description TEXT,
    job_type TEXT,
    workload_percent INTEGER,
    min_years_experience INTEGER,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT DEFAULT 'CHF',

    company_size TEXT,
    industry TEXT,
    is_startup INTEGER,

    languages TEXT,

    relevance_score REAL,
    application_status TEXT DEFAULT 'new',
    notes TEXT,

    date_posted TEXT,
    date_scraped TEXT NOT NULL,
    date_updated TEXT,
    last_seen_at TEXT,
    dedup_key TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_date ON jobs(date_posted);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scraper_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    jobs_found INTEGER DEFAULT 0,
    jobs_new INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    location TEXT,
    scraper_name TEXT NOT NULL,
    executed_at TEXT NOT NULL,
    results_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    operation TEXT NOT NULL,
    job_id INTEGER,
    sql_statement TEXT,
    parameters TEXT,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC);

-- Per-job chat history for the Chat-Assistant (Phase E).
-- `mode` partitions independent conversations: 'cover_letter', 'interview_prep',
-- 'application_advisor'. `proposed_edit` is non-NULL when the assistant
-- suggested a full-markdown edit to the cover letter that the user can apply.
CREATE TABLE IF NOT EXISTS job_chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    proposed_edit TEXT,
    created_at TEXT NOT NULL,
    cost_usd REAL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_chat_job_mode ON job_chat_messages(job_id, mode, created_at);
"""


# ---------------------------------------------------------------------------
# Outbound-Tracking: Bewerbungen, Watchlist, Kleinzustand.
# ---------------------------------------------------------------------------
APPLICATION_CHANNELS = ("ats", "email", "direct_contact", "referral")
APPLICATION_STATUSES = (
    "drafted", "sent", "followed_up", "replied", "interview", "offer", "rejected", "stale",
)
FOLLOWUP_BUSINESS_DAYS = 10   # "sent ohne Antwort nach 10 Werktagen → nachfassen"
DRAFT_STALE_DAYS = 3          # "drafted älter als 3 Tage → Warnung"
NEW_QUEUE_LIMIT = 500         # "NEW-Warteschlange nie über 500"

# Fortschritt entlang des Trichters. Ein Sync aus der Pipeline darf eine
# Bewerbung nur vorwärts bewegen — rejected/stale sind Endzustände daneben.
_APPLICATION_RANK = {
    "drafted": 0, "sent": 1, "followed_up": 2, "replied": 3, "interview": 4, "offer": 5,
}
# Pipeline-Status des Jobs ↔ Bewerbungsstatus
_JOB_TO_APPLICATION = {"applied": "sent", "interview": "interview", "offer": "offer", "rejected": "rejected"}
_APPLICATION_TO_JOB = {"sent": "applied", "interview": "interview", "offer": "offer", "rejected": "rejected"}
# Reply-Klassen, die als echte Antwort zählen. 'acknowledgement' ist die
# automatische ATS-Eingangsbestätigung — die ist keine Antwort.
REAL_REPLY_CLASSES = {"interview", "rejected", "offer", "needs_review"}

# Optionales Feld: zählt eine Stelle für ein studienbegleitendes
# Praxismodul? Nur Übersicht, keine Logik hängt daran. Auto-Wert aus dem
# Rollentitel; Wortstämme statt der
# wörtlichen Liste "Data, Analytics, ML, Engineering", sonst fiele "AI
# Engineer" durch. Kein Treffer heisst 'unknown', nicht 'false' — 'false'
# setzt nur der Mensch.
PBL_VALUES = ("true", "false", "unknown")
_PBL_TITLE_RE = re.compile(r"\b(data|analytic\w*|ml|machine learning|engineer\w*)\b", re.IGNORECASE)


def pbl_from_title(role: str) -> str:
    return "true" if _PBL_TITLE_RE.search(role or "") else "unknown"


def add_business_days(start: datetime, days: int) -> datetime:
    """Werktage Mo–Fr. Feiertage bleiben unberücksichtigt (kantonal verschieden)."""
    current, added = start, 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def week_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Montag 00:00 dieser Woche und Montag 00:00 der nächsten."""
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday, monday + timedelta(days=7)


# Retention: Treffer unter diesem Score, die so lange unangetastet blieben,
# wandern nach "archived" — und kommen zurück, wenn ihr Score wieder steigt.
RETENTION_MAX_SCORE = 0.5
RETENTION_DAYS = 60


# Experience-Bands: ranges of min_years_experience values, both ends inclusive.
# Boundaries overlap (1 hits both 0-1 and 1-3) — UI is single-select so no conflict.
# The "unknown" band targets rows where neither regex nor LLM found a signal.
_EXPERIENCE_BANDS: dict[str, tuple[int, int | None]] = {
    "0-1": (0, 1),
    "1-3": (1, 3),
    "3-5": (3, 5),
    "5plus": (5, None),
}


def _experience_band_clause(band: str) -> str:
    """SQL fragment for a single experience-band filter selection.

    Strict semantics — NULL is never silently mixed in. To see NULL rows,
    pick band='unknown' explicitly. To see everything, omit the filter.
    """
    if band == "unknown":
        return "min_years_experience IS NULL"
    bounds = _EXPERIENCE_BANDS.get(band)
    if not bounds:
        return ""
    lo, hi = bounds
    if hi is None:
        return f"min_years_experience >= {lo}"
    return f"min_years_experience BETWEEN {lo} AND {hi}"


class JobDatabase:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            # check_same_thread=False so the connection can be shared across the
            # FastAPI thread pool. WAL mode (set below) handles concurrency safely.
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def init_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        self.conn.executescript(SCHEMA_SQL)
        self._migrate()
        self.conn.commit()
        self._backfill_applications()
        logger.info(f"Database initialized at {self.db_path}")

    def _migrate(self) -> None:
        """Idempotent schema migrations for DBs created before columns existed.

        Die Reihenfolge der vier Abschnitte ist Teil der Logik, nicht Kosmetik:
        erst alle Tabellen, dann alle Spalten, dann die Backfills, zuletzt die
        Indizes. Ein Backfill vor dem ALTER TABLE seiner eigenen Spalte fällt
        nur beim Anlegen einer frischen Datei auf — in einer gewachsenen DB
        existiert die Spalte längst. Genauso liest ein PRAGMA table_info() auf
        eine noch nicht angelegte Tabelle eine leere Spaltenliste und lässt
        jede daran hängende Migration stillschweigend ausfallen.
        """
        # ------------------------------------------------------------------
        # 1. Tabellen — alles, was nicht in SCHEMA_SQL steht
        # ------------------------------------------------------------------
        # Replies-Table + IMAP-State (Phase C).
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                imap_uid INTEGER,
                raw_message_id TEXT,
                in_reply_to TEXT,
                references_header TEXT,
                from_email TEXT,
                from_domain TEXT,
                subject TEXT,
                body_excerpt TEXT,
                received_at TEXT,
                classification TEXT,
                classification_confidence REAL,
                classified_at TEXT,
                match_method TEXT,
                auto_bumped INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_replies_job ON replies(job_id);
            CREATE INDEX IF NOT EXISTS idx_replies_received ON replies(received_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_replies_msgid
                ON replies(raw_message_id)
                WHERE raw_message_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS imap_state (
                mailbox TEXT PRIMARY KEY,
                last_uid INTEGER NOT NULL DEFAULT 0,
                last_polled_at TEXT
            );

            -- Company-type classification cache (startup/scaleup filter).
            -- One row per distinct company name; filled lazily by the
            -- company_classifier agent (CLI: classify-companies).
            CREATE TABLE IF NOT EXISTS company_profiles (
                company TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                industry TEXT,
                confidence REAL,
                reasoning TEXT,
                checked_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_company_profiles_cat
                ON company_profiles(category);
            """
        )

        # Outbound-Umbau (2026-09-14) — Watchlist, Bewerbungen, Kleinzustand.
        self.conn.executescript(
            """
            -- Jede Stelle, die die Watchlist je gesehen hat, genau einmal. Die
            -- Tabelle ist das Gedächtnis gegen Doppel-Alerts: was hier steht,
            -- wird nie wieder als "neu" gemeldet.
            --   alert_status: seeded      Bestand beim ersten Kontakt, nie gemeldet
            --                 excluded    hard_excludes gegriffen
            --                 pending     wartet auf Sofort-Alert (retry bei Mailfehler)
            --                 alerted     Sofort-Alert verschickt
            --                 digest_only neu, aber Tier ohne Sofort-Alert (B, D)
            CREATE TABLE IF NOT EXISTS watchlist_hits (
                job_id INTEGER PRIMARY KEY,
                company_key TEXT,
                company_name TEXT,
                tier TEXT,
                role_class TEXT,
                reasons TEXT NOT NULL,
                alert_status TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                alerted_at TEXT,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_wl_hits_detected
                ON watchlist_hits(detected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_wl_hits_status
                ON watchlist_hits(alert_status);

            -- Ein Eintrag pro Watchlist-Firma, bei jedem Poll neu berechnet.
            -- sources_seen (JSON-Liste): welche Quellen haben für diese Firma
            -- je einen Treffer geliefert. Nur eine heisst Abdeckungslücke.
            CREATE TABLE IF NOT EXISTS watchlist_companies (
                company_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tier TEXT NOT NULL,
                channel TEXT NOT NULL,
                careers_url TEXT,
                sources_seen TEXT NOT NULL DEFAULT '[]',
                job_count INTEGER NOT NULL DEFAULT 0,
                careers_page_jobs INTEGER,
                careers_page_note TEXT,
                last_hit_at TEXT,
                first_polled_at TEXT NOT NULL,
                last_polled_at TEXT NOT NULL
            );

            -- Outbound-Tracking. job_id ist nullable: Direktkontakte
            -- und Referrals haben oft keine gescrapte Stelle dahinter.
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                company TEXT NOT NULL,
                role TEXT NOT NULL,
                channel TEXT NOT NULL
                    CHECK (channel IN ('ats', 'email', 'direct_contact', 'referral')),
                status TEXT NOT NULL DEFAULT 'drafted'
                    CHECK (status IN ('drafted', 'sent', 'followed_up', 'replied',
                                      'interview', 'offer', 'rejected', 'stale')),
                created_at TEXT NOT NULL,
                sent_at TEXT,
                followup_due TEXT,
                followup_sent_at TEXT,
                replied_at TEXT,
                outcome TEXT,
                notes TEXT,
                -- Jünger als die Tabelle: steht hier UND als ALTER weiter
                -- unten, damit eine frische Datei die Spalte direkt bekommt
                -- und eine gewachsene sie nachträgt.
                counts_for_project_based_learning TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (counts_for_project_based_learning IN ('true', 'false', 'unknown')),
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_job
                ON applications(job_id) WHERE job_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_applications_status
                ON applications(status);

            -- Kleinzustand ohne eigene Tabelle: letzter Keyword-Scan,
            -- letzter Digest-Versand usw.
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )

        # ------------------------------------------------------------------
        # 2. Spalten — ALTER TABLE ADD COLUMN, rein additiv
        # ------------------------------------------------------------------
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        added: set[str] = set()
        for col, ddl in [
            ("notes", "ALTER TABLE jobs ADD COLUMN notes TEXT"),
            ("last_seen_at", "ALTER TABLE jobs ADD COLUMN last_seen_at TEXT"),
            ("applied_at", "ALTER TABLE jobs ADD COLUMN applied_at TEXT"),
            # Step 7 — Cover Letter & email submission tracking
            ("cover_letter", "ALTER TABLE jobs ADD COLUMN cover_letter TEXT"),
            ("cover_letter_lang", "ALTER TABLE jobs ADD COLUMN cover_letter_lang TEXT"),
            ("cover_letter_format", "ALTER TABLE jobs ADD COLUMN cover_letter_format TEXT"),
            ("cover_letter_generated_at", "ALTER TABLE jobs ADD COLUMN cover_letter_generated_at TEXT"),
            ("email_subject", "ALTER TABLE jobs ADD COLUMN email_subject TEXT"),
            ("email_body_text", "ALTER TABLE jobs ADD COLUMN email_body_text TEXT"),
            ("contact_email", "ALTER TABLE jobs ADD COLUMN contact_email TEXT"),
            ("application_sent_at", "ALTER TABLE jobs ADD COLUMN application_sent_at TEXT"),
            ("cover_letter_briefing", "ALTER TABLE jobs ADD COLUMN cover_letter_briefing TEXT"),
            # Prüfhinweise zum Anschreiben (JSON-Liste), 2026-09-17
            ("cover_letter_checks", "ALTER TABLE jobs ADD COLUMN cover_letter_checks TEXT"),
            ("match_score", "ALTER TABLE jobs ADD COLUMN match_score REAL"),
            ("match_reason", "ALTER TABLE jobs ADD COLUMN match_reason TEXT"),
            ("match_details", "ALTER TABLE jobs ADD COLUMN match_details TEXT"),
            ("match_checked_at", "ALTER TABLE jobs ADD COLUMN match_checked_at TEXT"),
            ("apply_method", "ALTER TABLE jobs ADD COLUMN apply_method TEXT"),
            ("apply_method_checked_at", "ALTER TABLE jobs ADD COLUMN apply_method_checked_at TEXT"),
            ("min_years_experience", "ALTER TABLE jobs ADD COLUMN min_years_experience INTEGER"),
            # Reply-Tracking (Phase C, 2026-06-21) — Message-ID + recipient
            # are persisted on send so the IMAP poller can correlate incoming
            # replies via In-Reply-To/References headers or sender domain.
            ("application_message_id", "ALTER TABLE jobs ADD COLUMN application_message_id TEXT"),
            ("application_recipient_email", "ALTER TABLE jobs ADD COLUMN application_recipient_email TEXT"),
            ("application_recipient_domain", "ALTER TABLE jobs ADD COLUMN application_recipient_domain TEXT"),
        ]:
            if col not in cols:
                self.conn.execute(ddl)
                added.add(col)
                logger.info("Migration: added jobs.%s column", col)

        app_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(applications)")}
        app_added: set[str] = set()
        if "counts_for_project_based_learning" not in app_cols:
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN counts_for_project_based_learning TEXT "
                "NOT NULL DEFAULT 'unknown' CHECK (counts_for_project_based_learning IN ('true', 'false', 'unknown'))"
            )
            app_added.add("counts_for_project_based_learning")
            logger.info("Migration: added applications.counts_for_project_based_learning")

        cp_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(company_profiles)")}
        if "industry" not in cp_cols:
            self.conn.execute("ALTER TABLE company_profiles ADD COLUMN industry TEXT")
            logger.info("Migration: added company_profiles.industry column")

        wl_cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(watchlist_companies)")}
        if "careers_page_note" not in wl_cols:
            self.conn.execute("ALTER TABLE watchlist_companies ADD COLUMN careers_page_note TEXT")
            logger.info("Migration: added watchlist_companies.careers_page_note column")

        # ------------------------------------------------------------------
        # 3. Backfills — nur beim ersten Lauf nach dem jeweiligen ALTER
        # ------------------------------------------------------------------
        if "last_seen_at" in added:
            # Backfill last_seen_at from date_scraped so existing jobs aren't
            # immediately considered stale on first run after upgrade.
            self.conn.execute(
                "UPDATE jobs SET last_seen_at = date_scraped WHERE last_seen_at IS NULL"
            )
            logger.info("Migration: backfilled jobs.last_seen_at")
        if "applied_at" in added:
            # Backfill from the audit trail: every status change is logged, so
            # the first "Status → applied" entry per job is the date the
            # application actually went out. MIN() because a job can be moved
            # in and out of the column repeatedly — the first time is the one
            # that answers "when did I apply?".
            self.conn.execute(
                """UPDATE jobs SET applied_at = (
                       SELECT MIN(a.timestamp) FROM audit_log a
                       WHERE a.job_id = jobs.id
                         AND a.description = 'Status → applied'
                   )
                   WHERE applied_at IS NULL"""
            )
            # Applications sent through the tool predate the audit trail in
            # some DBs; application_sent_at is authoritative where it exists.
            self.conn.execute(
                "UPDATE jobs SET applied_at = application_sent_at "
                "WHERE applied_at IS NULL AND application_sent_at IS NOT NULL"
            )
            n = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
            ).fetchone()[0]
            logger.info("Migration: backfilled jobs.applied_at (%s rows)", n)
        if "counts_for_project_based_learning" in app_added:
            for row in self.conn.execute("SELECT id, role FROM applications").fetchall():
                self.conn.execute(
                    "UPDATE applications SET counts_for_project_based_learning = ? WHERE id = ?",
                    (pbl_from_title(row["role"]), row["id"]),
                )
            logger.info("Migration: backfilled applications.counts_for_project_based_learning")

        # ------------------------------------------------------------------
        # 4. Indizes — idempotent, daher bei jedem Start erneut versucht
        # ------------------------------------------------------------------
        # Retention prüft pro Job, ob es Audit-Einträge gibt.
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_log(job_id)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at)"
        )
        # Lookups during reply-correlation.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_app_msgid "
            "ON jobs(application_message_id) "
            "WHERE application_message_id IS NOT NULL"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_app_domain "
            "ON jobs(application_recipient_domain) "
            "WHERE application_recipient_domain IS NOT NULL"
        )

    def upsert_job(self, job: Job) -> bool:
        """Insert or update a job. Returns True if the job is new."""
        dedup_key = job.dedup_key()
        now = datetime.now().isoformat()

        # Best-effort enrichment: extract workload_percent from title +
        # description if the scraper didn't already fill it.
        if job.workload_percent is None:
            inferred = extract_for_job(title=job.title, description=job.description)
            if inferred is not None:
                job.workload_percent = inferred

        # Same enrichment for min_years_experience.
        if job.min_years_experience is None:
            inferred_yrs = extract_min_years_for_job(
                title=job.title, description=job.description
            )
            if inferred_yrs is not None:
                job.min_years_experience = inferred_yrs

        # Check if job already exists (by URL or dedup_key)
        existing = self.conn.execute(
            "SELECT id FROM jobs WHERE url = ? OR dedup_key = ?",
            (job.url, dedup_key),
        ).fetchone()

        if existing:
            # Update existing job. last_seen_at = now signals "still alive".
            self.conn.execute(
                """UPDATE jobs SET
                    title=?, company=?, source=?, location=?, canton=?,
                    is_remote=?, description=?, job_type=?, workload_percent=?,
                    min_years_experience=?,
                    salary_min=?, salary_max=?, relevance_score=?,
                    date_updated=?, last_seen_at=?, dedup_key=?
                WHERE id=?""",
                (
                    job.title, job.company, job.source.value,
                    job.location, job.canton,
                    _bool_to_int(job.is_remote), job.description,
                    job.job_type.value, job.workload_percent,
                    job.min_years_experience,
                    job.salary_min, job.salary_max, job.relevance_score,
                    now, now, dedup_key, existing["id"],
                ),
            )
            self.conn.commit()
            return False

        # Insert new job
        self.conn.execute(
            """INSERT INTO jobs (
                title, company, url, source, external_id,
                location, canton, is_remote,
                description, job_type, workload_percent, min_years_experience,
                salary_min, salary_max, salary_currency,
                company_size, industry, is_startup,
                languages, relevance_score, application_status,
                date_posted, date_scraped, last_seen_at, dedup_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.title, job.company, job.url, job.source.value, job.external_id,
                job.location, job.canton, _bool_to_int(job.is_remote),
                job.description, job.job_type.value, job.workload_percent,
                job.min_years_experience,
                job.salary_min, job.salary_max, job.salary_currency,
                job.company_size, job.industry, _bool_to_int(job.is_startup),
                json.dumps(job.languages) if job.languages else None,
                job.relevance_score, job.application_status.value,
                job.date_posted.isoformat() if job.date_posted else None,
                job.date_scraped.isoformat(), now, dedup_key,
            ),
        )
        self.conn.commit()
        return True

    def job_exists(self, url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE url = ?", (url,)
        ).fetchone()
        return row is not None

    def get_jobs(
        self,
        min_score: Optional[float] = None,
        source: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Retrieve jobs from the database, sorted by relevance score."""
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []

        if min_score is not None:
            query += " AND relevance_score >= ?"
            params.append(min_score)
        if source:
            query += " AND source = ?"
            params.append(source)

        query += " ORDER BY relevance_score DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def export_csv(self, path: str | Path, min_score: Optional[float] = None) -> int:
        """Export jobs to CSV. Returns the number of exported rows."""
        jobs = self.get_jobs(min_score=min_score, limit=10000)
        if not jobs:
            logger.warning("No jobs to export")
            return 0

        export_path = Path(path)
        export_path.parent.mkdir(parents=True, exist_ok=True)

        # Spaltenreihenfolge: wichtigste Felder zuerst, damit der Score
        # in Excel/Numbers sofort sichtbar ist ohne horizontal scrollen.
        fieldnames = [
            "relevance_score", "title", "company", "location",
            "workload_percent", "job_type", "is_remote",
            "source", "canton", "date_posted", "date_scraped",
            "url", "description",
        ]

        with open(export_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(jobs)

        logger.info(f"Exported {len(jobs)} jobs to {export_path}")
        return len(jobs)

    def log_scrape_run(
        self,
        scraper_name: str,
        jobs_found: int,
        jobs_new: int,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        now = datetime.now().isoformat()
        self.conn.execute(
            """INSERT INTO scrape_runs
            (scraper_name, started_at, finished_at, jobs_found, jobs_new, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (scraper_name, now, now, jobs_found, jobs_new, status, error),
        )
        self.conn.commit()

    def log_search_queries(self, scraper_name: str, queries: list[dict]) -> None:
        """Persist per-keyword search results from one scraper run.

        Feeds the search_queries table, which answers "which keyword is worth
        its runtime?" — each keyword costs roughly one call per location, so
        the list is the main lever on total scrape duration. Without this the
        question can only be approximated by matching keywords against stored
        job titles, which misses hits whose title differs from the query.
        """
        if not queries:
            return
        now = datetime.now().isoformat()
        self.conn.executemany(
            """INSERT INTO search_queries
            (keyword, location, scraper_name, executed_at, results_count)
            VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    q["keyword"],
                    q.get("location"),
                    scraper_name,
                    now,
                    q.get("results_count", 0),
                )
                for q in queries
            ],
        )
        self.conn.commit()

    def get_stats(self) -> dict:
        """Get summary statistics."""
        total = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_source = self.conn.execute(
            "SELECT source, COUNT(*) as cnt FROM jobs GROUP BY source ORDER BY cnt DESC"
        ).fetchall()
        avg_score = self.conn.execute(
            "SELECT AVG(relevance_score) FROM jobs WHERE relevance_score IS NOT NULL"
        ).fetchone()[0]

        return {
            "total_jobs": total,
            "by_source": {row["source"]: row["cnt"] for row in by_source},
            "avg_relevance_score": round(avg_score, 3) if avg_score else None,
        }

    # ------------------------------------------------------------------
    # Outbound — applications
    # ------------------------------------------------------------------

    def _backfill_applications(self) -> None:
        """Einmalig: Jobs, die schon beworben/weiter sind, als Bewerbung anlegen."""
        if self.get_meta("applications.backfilled"):
            return
        # Nur der aktuelle Status zählt, nicht applied_at: im Audit-Log stehen
        # auch Testläufe, bei denen eine Karte in Minuten durch mehrere Spalten
        # gezogen und am selben Tag zurückgenommen wurde. Solche Einträge sagen
        # nichts über tatsächlich verschickte Bewerbungen.
        rows = self.conn.execute(
            "SELECT id, application_status FROM jobs "
            "WHERE application_status IN ('applied', 'interview', 'offer', 'rejected')"
        ).fetchall()
        for row in rows:
            self.sync_application_from_job(row["id"], row["application_status"])
        self.set_meta("applications.backfilled", str(len(rows)))
        if rows:
            logger.info("Migration: %s Bewerbungen aus der Pipeline übernommen", len(rows))

    def create_application(
        self,
        *,
        company: str,
        role: str,
        channel: str,
        job_id: Optional[int] = None,
        status: str = "drafted",
        notes: Optional[str] = None,
        when: Optional[datetime] = None,
    ) -> int:
        """Neue Bewerbung. Existiert für ``job_id`` schon eine, wird deren id zurückgegeben."""
        if channel not in APPLICATION_CHANNELS:
            raise ValueError(f"channel {channel!r} — erlaubt: {APPLICATION_CHANNELS}")
        if status not in ("drafted", "sent"):
            raise ValueError("Neue Bewerbungen starten als 'drafted' oder 'sent'")
        company, role = (company or "").strip(), (role or "").strip()
        if not company or not role:
            raise ValueError("company und role sind Pflicht")
        if job_id is not None:
            existing = self.application_for_job(job_id)
            if existing:
                return existing["id"]
        when = when or datetime.now()
        sent_at = when.isoformat() if status == "sent" else None
        followup_due = (
            add_business_days(when, FOLLOWUP_BUSINESS_DAYS).date().isoformat()
            if status == "sent" else None
        )
        cur = self.conn.execute(
            "INSERT INTO applications (job_id, company, role, channel, status, created_at, "
            "sent_at, followup_due, notes, counts_for_project_based_learning) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, company, role, channel, status, datetime.now().isoformat(),
             sent_at, followup_due, (notes or "").strip() or None, pbl_from_title(role)),
        )
        self._audit("APPLICATION_CREATE", job_id=job_id,
                    description=f"Bewerbung {company} — {role} ({channel}, {status})")
        self.conn.commit()
        return cur.lastrowid

    def get_application(self, app_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        return dict(row) if row else None

    def application_for_job(self, job_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_applications(self, statuses: Optional[list[str]] = None) -> list[dict]:
        sql = (
            "SELECT a.*, j.url AS job_url, j.title AS job_title "
            "FROM applications a LEFT JOIN jobs j ON j.id = a.job_id"
        )
        params: list = []
        if statuses:
            sql += f" WHERE a.status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY COALESCE(a.sent_at, a.created_at) DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def advance_application(
        self,
        app_id: int,
        action: str,
        *,
        when: Optional[datetime] = None,
        outcome: Optional[str] = None,
        touch_job: bool = True,
    ) -> dict:
        """Status-Übergang einer Bewerbung. Setzt die passenden Zeitstempel.

        ``touch_job`` spiegelt sent/interview/offer/rejected in die Pipeline
        des verknüpften Jobs. Aus dem Pipeline-Sync heraus ist es False,
        sonst liefe jede Änderung im Kreis.
        """
        app = self.get_application(app_id)
        if app is None:
            raise ValueError(f"Bewerbung {app_id} existiert nicht")
        if action not in APPLICATION_STATUSES or action == "drafted":
            raise ValueError(f"Aktion {action!r} unbekannt")
        when = when or datetime.now()
        stamp = when.isoformat()
        fields: dict = {"status": action}
        if action == "sent":
            sent = datetime.fromisoformat(app["sent_at"]) if app["sent_at"] else when
            fields["sent_at"] = sent.isoformat()
            fields["followup_due"] = add_business_days(sent, FOLLOWUP_BUSINESS_DAYS).date().isoformat()
        elif action == "followed_up":
            fields["followup_sent_at"] = stamp
        elif action in ("replied", "interview", "offer", "rejected"):
            fields["replied_at"] = app["replied_at"] or stamp
        if outcome is not None:
            fields["outcome"] = outcome.strip() or None
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE applications SET {assignments} WHERE id = ?", (*fields.values(), app_id)
        )
        self._audit("APPLICATION_STATUS", job_id=app["job_id"],
                    description=f"Bewerbung {app_id}: {app['status']} → {action}")
        job_status = _APPLICATION_TO_JOB.get(action)
        if touch_job and app["job_id"] and job_status:
            self.conn.execute(
                "UPDATE jobs SET application_status = ?, date_updated = ?, "
                "applied_at = COALESCE(applied_at, ?) WHERE id = ?",
                (job_status, stamp, fields.get("sent_at") or app["sent_at"] or stamp, app["job_id"]),
            )
        self.conn.commit()
        return self.get_application(app_id)

    def set_application_pbl(self, app_id: int, value: str) -> None:
        """Manueller Override für counts_for_project_based_learning."""
        if value not in PBL_VALUES:
            raise ValueError(f"{value!r} — erlaubt: {PBL_VALUES}")
        self.conn.execute(
            "UPDATE applications SET counts_for_project_based_learning = ? WHERE id = ?", (value, app_id)
        )
        self.conn.commit()

    def update_application_notes(self, app_id: int, notes: str) -> None:
        self.conn.execute(
            "UPDATE applications SET notes = ? WHERE id = ?", ((notes or "").strip() or None, app_id)
        )
        self.conn.commit()

    def sync_application_from_job(
        self, job_id: int, job_status: str, *, channel: Optional[str] = None
    ) -> None:
        """Pipeline → Outbound. Legt die Bewerbung an oder schiebt sie vorwärts.

        Wer im Kanban eine Karte auf "applied" zieht, hat eine Bewerbung
        verschickt — die soll im Outbound-Zähler auftauchen, ohne dass man sie
        ein zweites Mal erfasst. Rückwärts (applied → bookmarked) passiert
        nichts: eine verschickte Bewerbung bleibt verschickt.
        """
        target = _JOB_TO_APPLICATION.get(job_status)
        if target is None:
            self._undo_misdragged_application(job_id, job_status)
            return
        job = self.conn.execute(
            "SELECT company, title, applied_at, application_sent_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            return
        app = self.application_for_job(job_id)
        if app is None:
            sent = job["application_sent_at"] or job["applied_at"]
            self.create_application(
                company=job["company"], role=job["title"],
                channel=channel or ("email" if job["application_sent_at"] else "ats"),
                job_id=job_id, status="sent",
                when=datetime.fromisoformat(sent) if sent else None,
            )
            app = self.application_for_job(job_id)
        if target == "sent":
            if app["status"] == "drafted":
                self.advance_application(app["id"], "sent", touch_job=False)
            return
        if app["status"] == target:
            return
        forward = _APPLICATION_RANK.get(target, 99) > _APPLICATION_RANK.get(app["status"], -1)
        if forward or target == "rejected":
            self.advance_application(app["id"], target, touch_job=False)

    def _undo_misdragged_application(self, job_id: int, job_status: str) -> None:
        """Karte innerhalb eines Tages von "applied" zurückgezogen → Fehlklick.

        Ohne das stünde nach jedem versehentlichen Drag eine Phantom-Bewerbung
        im Wochenzähler — der Zahl, an der der ganze Umbau gemessen wird. Nur
        wenn seither nichts passiert ist (kein Nachfassen, keine Antwort).
        Nach 24 h bleibt eine verschickte Bewerbung verschickt.
        """
        app = self.application_for_job(job_id)
        if (
            app is None
            or app["status"] != "sent"
            or app["followup_sent_at"]
            or app["replied_at"]
            or app["created_at"] < (datetime.now() - timedelta(hours=24)).isoformat()
        ):
            return
        self.conn.execute("DELETE FROM applications WHERE id = ?", (app["id"],))
        self._audit("APPLICATION_UNDO", job_id=job_id,
                    description=f"Bewerbung {app['id']} zurückgenommen (Karte → {job_status})")

    def remember_draft_recipient(self, job_id: int, address: Optional[str], domain: str) -> None:
        """Empfänger eines Entwurfs vormerken, ohne das Antwort-Tracking zu aktivieren.

        Volle Adresse → contact_email (wie bisher). Reine Domain → vorgemerkt in
        application_recipient_domain, aber nur solange application_sent_at leer
        ist: der Reply-Tracker matcht ausschliesslich Jobs mit Versanddatum, ein
        Entwurf wird also nie zugeordnet. Beim Markieren als versendet füllt der
        Drawer das Feld damit vor, mark_sent übernimmt es dann regulär.

        Eine neue volle Adresse räumt eine vorgemerkte Domain weg, damit das
        Feld die jüngste Eingabe zeigt.
        """
        if address:
            self.conn.execute(
                "UPDATE jobs SET contact_email = ?, application_recipient_domain = "
                "CASE WHEN application_sent_at IS NULL THEN NULL ELSE application_recipient_domain END "
                "WHERE id = ?",
                (address, job_id),
            )
        else:
            self.conn.execute(
                "UPDATE jobs SET application_recipient_domain = ? "
                "WHERE id = ? AND application_sent_at IS NULL",
                (domain.strip().lower(), job_id),
            )
        self.conn.commit()

    def set_application_recipient(
        self,
        job_id: int,
        recipient_email: Optional[str],
        recipient_domain: str,
        message_id: Optional[str] = None,
    ) -> None:
        """Empfänger fürs Antwort-Tracking nachtragen oder ändern, wenn die Bewerbung schon raus ist.

        ``recipient_email`` ist None, wenn nur eine Domain bekannt ist
        (``@firma.example``) — dann bleiben contact_email und
        application_recipient_email leer; eine Domain ist keine Adresse, an die
        „Send…" schreiben könnte. Der Reply-Tracker matcht allein über
        application_recipient_domain.

        Anders als mark_sent bleibt das Versanddatum stehen. application_sent_at
        wird nur gesetzt, falls leer (Kanban-Drag hat keins) — der Reply-Tracker
        matcht per Domain nur Jobs mit application_sent_at.

        Beim Ändern folgt contact_email der neuen Adresse nur, wenn sie bisher
        die alte Empfängeradresse war — eine separat gepflegte Kontaktadresse
        bleibt stehen. (In SET-Ausdrücken sieht SQLite die alten Spaltenwerte.)
        Antworten, die schon zugeordnet sind, bleiben beim Job.
        """
        address = (recipient_email or "").strip() or None
        domain = recipient_domain.strip().lower()
        app = self.application_for_job(job_id)
        fallback_sent = (app or {}).get("sent_at") or datetime.now().isoformat()
        old = self.conn.execute(
            "SELECT application_recipient_email, application_recipient_domain FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        previous = old and (old["application_recipient_email"]
                            or ("@" + old["application_recipient_domain"] if old["application_recipient_domain"] else None))
        self.conn.execute(
            "UPDATE jobs SET contact_email = CASE WHEN :address IS NOT NULL AND "
            "(contact_email IS NULL OR contact_email = application_recipient_email) "
            "THEN :address ELSE contact_email END, "
            "application_recipient_email = :address, application_recipient_domain = :domain, "
            "application_message_id = COALESCE(:message_id, application_message_id), "
            "application_sent_at = COALESCE(application_sent_at, applied_at, :sent) WHERE id = :job_id",
            {"address": address, "domain": domain, "message_id": message_id, "sent": fallback_sent, "job_id": job_id},
        )
        target = address or "@" + domain
        self._audit("APPLICATION_RECIPIENT", job_id=job_id,
                    description=(f"Empfänger fürs Antwort-Tracking: {previous} → {target}"
                                 if previous and previous != target else f"Empfänger fürs Antwort-Tracking: {target}"))
        self.conn.commit()

    def mark_application_replied_for_job(self, job_id: int, received_at: str) -> None:
        app = self.application_for_job(job_id)
        if app is None or app["replied_at"]:
            return
        self.conn.execute(
            "UPDATE applications SET replied_at = ?, status = CASE "
            "WHEN status IN ('sent', 'followed_up') THEN 'replied' ELSE status END "
            "WHERE id = ?",
            (received_at, app["id"]),
        )

    def outbound_overview(self, now: Optional[datetime] = None) -> dict:
        """Die vier Zahlen der Startseite plus die Listen dahinter."""
        now = now or datetime.now()
        monday, next_monday = week_bounds(now)
        week = (monday.isoformat(), next_monday.isoformat())
        today = now.date().isoformat()
        stale_cutoff = (now - timedelta(days=DRAFT_STALE_DAYS)).isoformat()

        def rows(sql: str, params: tuple = ()) -> list[dict]:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

        base = ("SELECT a.*, j.url AS job_url FROM applications a "
                "LEFT JOIN jobs j ON j.id = a.job_id ")
        sent_week = rows(base + "WHERE a.sent_at >= ? AND a.sent_at < ? ORDER BY a.sent_at DESC", week)
        replies_week = rows(base + "WHERE a.replied_at >= ? AND a.replied_at < ? ORDER BY a.replied_at DESC", week)
        followups = rows(
            base + "WHERE a.status = 'sent' AND a.replied_at IS NULL "
            "AND a.followup_due IS NOT NULL AND a.followup_due <= ? ORDER BY a.followup_due",
            (today,),
        )
        drafts = rows(base + "WHERE a.status = 'drafted' ORDER BY a.created_at")
        active = rows(
            base + "WHERE a.status IN ('sent', 'followed_up', 'replied', 'interview', 'offer') "
            "ORDER BY COALESCE(a.sent_at, a.created_at) DESC"
        )
        closed = rows(
            base + "WHERE a.status IN ('rejected', 'stale') "
            "ORDER BY COALESCE(a.replied_at, a.sent_at, a.created_at) DESC LIMIT 20"
        )
        for d in drafts:
            d["is_stale"] = d["created_at"] <= stale_cutoff
            d["age_days"] = (now - datetime.fromisoformat(d["created_at"])).days
        for f in followups:
            f["overdue_days"] = (now.date() - datetime.fromisoformat(f["followup_due"]).date()).days
        new_queue = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE application_status = 'new'"
        ).fetchone()[0]
        return {
            "week_start": monday.date(),
            "week_end": (next_monday - timedelta(days=1)).date(),
            "sent_this_week": len(sent_week),
            "followups_open": len(followups),
            "replies_this_week": len(replies_week),
            "drafts_stale": sum(1 for d in drafts if d["is_stale"]),
            "sent_week": sent_week,
            "replies_week": replies_week,
            "followups": followups,
            "drafts": drafts,
            "active": active,
            "closed": closed,
            "total_sent": self.conn.execute(
                "SELECT COUNT(*) FROM applications WHERE sent_at IS NOT NULL"
            ).fetchone()[0],
            "pbl_counts": dict(self.conn.execute(
                "SELECT counts_for_project_based_learning, COUNT(*) FROM applications "
                "WHERE status NOT IN ('rejected', 'stale') GROUP BY 1"
            ).fetchall()),
            "new_queue": new_queue,
            "new_queue_limit": NEW_QUEUE_LIMIT,
        }

    def run_retention(self, *, dry_run: bool = False, now: Optional[datetime] = None) -> dict:
        """Archiviert alte, schwache, nie angefasste Stellen — und holt sie zurück, wenn ihr Score steigt.

        "Unangetastet" heisst: Status noch 'new', keine Notiz, kein Anschreiben,
        kein Eintrag im Audit-Log (jede Statusänderung landet dort), keine
        Bewerbung und kein gemeldeter Watchlist-Treffer. Die Retention selbst
        schreibt auch ins Audit-Log; diese Einträge zählen nicht als Anfassen.

        Zurückgeholt wird, was die Retention archiviert hat und nach einem
        Rescore wieder bei Score ≥ 0.5 liegt — sonst verschwänden Stellen für
        immer, nur weil sie unter einer älteren Scoring-Config schwach waren.
        """
        now = now or datetime.now()
        cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
        archive_ids = [r[0] for r in self.conn.execute(
            """SELECT j.id FROM jobs j
               WHERE j.application_status = 'new'
                 AND COALESCE(j.relevance_score, 0) < ?
                 AND j.date_scraped < ?
                 AND (j.notes IS NULL OR TRIM(j.notes) = '')
                 AND j.cover_letter IS NULL
                 AND NOT EXISTS (SELECT 1 FROM audit_log a
                                 WHERE a.job_id = j.id AND a.operation NOT LIKE 'RETENTION_%')
                 AND NOT EXISTS (SELECT 1 FROM applications ap WHERE ap.job_id = j.id)
                 AND NOT EXISTS (SELECT 1 FROM watchlist_hits h WHERE h.job_id = j.id
                                 AND h.alert_status IN ('pending', 'alerted', 'digest_only'))""",
            (RETENTION_MAX_SCORE, cutoff),
        )]
        restore_ids = [r[0] for r in self.conn.execute(
            """SELECT j.id FROM jobs j
               WHERE j.application_status = 'archived'
                 AND COALESCE(j.relevance_score, 0) >= ?
                 AND (SELECT a.operation FROM audit_log a WHERE a.job_id = j.id
                      ORDER BY a.timestamp DESC, a.id DESC LIMIT 1) = 'RETENTION_ARCHIVE'""",
            (RETENTION_MAX_SCORE,),
        )]
        result = {"archived": len(archive_ids), "restored": len(restore_ids), "dry_run": dry_run}
        if dry_run:
            return result
        stamp = now.isoformat()
        with self.conn:
            for ids, status, operation, text in (
                (archive_ids, "archived", "RETENTION_ARCHIVE",
                 f"Retention: Score < {RETENTION_MAX_SCORE}, {RETENTION_DAYS} Tage unangetastet → archived"),
                (restore_ids, "new", "RETENTION_RESTORE",
                 f"Retention: Score wieder ≥ {RETENTION_MAX_SCORE} → new"),
            ):
                self.conn.executemany(
                    "UPDATE jobs SET application_status = ?, date_updated = ? WHERE id = ?",
                    [(status, stamp, i) for i in ids],
                )
                self.conn.executemany(
                    "INSERT INTO audit_log (timestamp, operation, job_id, sql_statement, parameters, description) "
                    "VALUES (?, ?, ?, '', '[]', ?)",
                    [(stamp, operation, i, text) for i in ids],
                )
        return result

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: Optional[str]) -> None:
        self.conn.execute(
            "INSERT INTO app_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, datetime.now().isoformat()),
        )
        self.conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Dashboard queries (Phase 2)
    # ------------------------------------------------------------------

    # Location column is free text ("Barcelona, Spain", "Zürich", "Madrid
    # (Híbrido)") — country membership is keyword-based. City lists cover
    # what actually occurs in the scraped data; extend when a new hub shows up.
    _CH_LOCATION_PATTERNS = [
        "Zürich", "Zurich", "Winterthur", "Basel", "Bern", "Genf", "Geneva",
        "Genève", "Zug", "Lausanne", "St. Gallen", "St.Gallen", "Sankt Gallen",
        "Luzern", "Lucerne", "Chur", "Vernier", "Yverdon", "Schlieren",
        "Dübendorf", "Wallisellen", "Fribourg", "Neuchâtel", "Lugano",
        "Switzerland", "Schweiz", "Suisse", "Svizzera",
    ]
    _ES_LOCATION_PATTERNS = [
        "Barcelona", "Madrid", "Valencia", "Sevilla", "Seville", "Bilbao",
        "Girona", "Zaragoza", "Málaga", "Malaga", "Alicante", "Murcia",
        "Mallorca", "Sant Cugat", "Manresa", "Arteixo", "Pontevedra",
        "Coruña", "Vigo", "Granada", "Spain", "España", "Espana", "Spanien",
    ]

    @classmethod
    def _country_location_clause(cls, patterns: list[str]) -> tuple[str, list[str]]:
        likes = " OR ".join(["location LIKE ?"] * len(patterns))
        return f"({likes})", [f"%{p}%" for p in patterns]

    def _presence_ch_es_clause(self) -> tuple[str, list[str]]:
        """Companies that post jobs in both Switzerland AND Spain.

        Presence is inferred from the jobs table itself — a company counts as
        operating in a country when at least one of its postings (any status,
        any age) carries a location there.
        """
        ch_clause, ch_params = self._country_location_clause(self._CH_LOCATION_PATTERNS)
        es_clause, es_params = self._country_location_clause(self._ES_LOCATION_PATTERNS)
        # Guard against '' matching '': jobs without a company name would
        # otherwise link the two country sets to each other.
        clause = (
            "(company IS NOT NULL AND company != '' AND company IN ("
            f"SELECT company FROM jobs WHERE {ch_clause} "
            "INTERSECT "
            f"SELECT company FROM jobs WHERE {es_clause}))"
        )
        return clause, ch_params + es_params

    # ------------------------------------------------------------------
    # Company-type classification (startup/scaleup filter)
    # ------------------------------------------------------------------

    COMPANY_CATEGORIES = ["startup", "scaleup", "sme", "enterprise", "unknown"]

    # Industry verticals — mirrors `preferences.domains` in the profile YAML.
    # Shared between the classifier tool schema and the browse filter.
    COMPANY_INDUSTRIES = [
        "saas_software", "ai_data", "fintech", "climate_energy",
        "health_biotech", "mobility_logistics", "ecommerce_marketplace",
        "beauty_fmcg", "proptech_construction", "industrial_hardware",
        "consulting_services", "finance_vc", "media_creative",
        "public_education", "other",
    ]

    def companies_needing_classification(
        self, *, limit: int = 100, fresh_days: Optional[int] = 30
    ) -> list[dict]:
        """Distinct companies without a (complete) company_profiles row.

        Includes companies classified before the industry column existed
        (industry IS NULL) so a re-run backfills them. Returns dicts with
        company name plus up to 3 sample titles/locations so the classifier
        has something to go on besides the bare name. Prioritizes companies
        with the most (fresh) jobs — those give the filter the most coverage
        per API dollar.
        """
        fresh_filter = ""
        if fresh_days is not None:
            fresh_filter = f"AND j.last_seen_at >= date('now', '-{int(fresh_days)} days')"
        rows = self.conn.execute(
            f"""
            SELECT j.company, COUNT(*) AS n_jobs,
                   GROUP_CONCAT(j.title, ' | ') AS titles,
                   GROUP_CONCAT(DISTINCT j.location) AS locations
            FROM jobs j
            LEFT JOIN company_profiles cp ON cp.company = j.company
            WHERE j.company != ''
              AND (cp.company IS NULL OR cp.industry IS NULL) {fresh_filter}
            GROUP BY j.company
            ORDER BY n_jobs DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            titles = (r["titles"] or "").split(" | ")[:3]
            out.append({
                "company": r["company"],
                "n_jobs": r["n_jobs"],
                "sample_titles": titles,
                "locations": (r["locations"] or "")[:200],
            })
        return out

    def save_company_profile(
        self, company: str, *, category: str, confidence: Optional[float],
        reasoning: Optional[str], industry: Optional[str] = None,
    ) -> None:
        if category not in self.COMPANY_CATEGORIES:
            category = "unknown"
        if industry is not None and industry not in self.COMPANY_INDUSTRIES:
            industry = "other"
        self.conn.execute(
            "INSERT INTO company_profiles (company, category, industry, confidence, reasoning, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(company) DO UPDATE SET category = excluded.category, "
            "industry = excluded.industry, "
            "confidence = excluded.confidence, reasoning = excluded.reasoning, "
            "checked_at = excluded.checked_at",
            (company, category, industry, confidence, reasoning, datetime.now().isoformat()),
        )
        self.conn.commit()

    def company_classification_stats(self) -> dict:
        total = self.conn.execute(
            "SELECT COUNT(DISTINCT company) FROM jobs WHERE company != ''"
        ).fetchone()[0]
        by_cat = {
            row["category"]: row["n"]
            for row in self.conn.execute(
                "SELECT category, COUNT(*) AS n FROM company_profiles GROUP BY category"
            )
        }
        return {"companies_total": total, "classified": sum(by_cat.values()), "by_category": by_cat}

    def query_jobs(
        self,
        *,
        search: Optional[str] = None,
        sources: Optional[list[str]] = None,
        statuses: Optional[list[str]] = None,
        location: Optional[str] = None,
        presence_ch_es: bool = False,
        company_types: Optional[list[str]] = None,
        industries: Optional[list[str]] = None,
        min_score: Optional[float] = None,
        max_score: Optional[float] = None,
        workload_min: Optional[int] = None,
        workload_max: Optional[int] = None,
        experience_band: Optional[str] = None,
        is_remote: Optional[bool] = None,
        hide_ignored: bool = True,
        hide_stale: bool = True,
        stale_days: int = 30,
        sort: str = "score",
        direction: str = "desc",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """Flexible filtered query for the Browse tab."""
        where: list[str] = ["1=1"]
        params: list = []

        if search:
            where.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        if sources:
            placeholders = ",".join("?" * len(sources))
            where.append(f"source IN ({placeholders})")
            params.extend(sources)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            where.append(f"application_status IN ({placeholders})")
            params.extend(statuses)
        elif hide_ignored:
            where.append("application_status NOT IN ('ignored', 'archived')")
        if location:
            # Comma-separated terms are OR-ed: "Zürich, Barcelona" matches either.
            terms = [t.strip() for t in location.split(",") if t.strip()]
            if terms:
                likes = " OR ".join(["location LIKE ?"] * len(terms))
                where.append(f"({likes})")
                params.extend(f"%{t}%" for t in terms)
        if presence_ch_es:
            clause, ch_es_params = self._presence_ch_es_clause()
            where.append(clause)
            params.extend(ch_es_params)
        if company_types:
            placeholders = ",".join("?" * len(company_types))
            where.append(
                "company IN (SELECT company FROM company_profiles "
                f"WHERE category IN ({placeholders}))"
            )
            params.extend(company_types)
        if industries:
            placeholders = ",".join("?" * len(industries))
            where.append(
                "company IN (SELECT company FROM company_profiles "
                f"WHERE industry IN ({placeholders}))"
            )
            params.extend(industries)
        if min_score is not None:
            where.append("relevance_score >= ?")
            params.append(min_score)
        if max_score is not None:
            where.append("relevance_score <= ?")
            params.append(max_score)
        if workload_min is not None:
            # NULL workload = unknown; include rather than hide.
            where.append("(workload_percent IS NULL OR workload_percent >= ?)")
            params.append(workload_min)
        if workload_max is not None:
            where.append("(workload_percent IS NULL OR workload_percent <= ?)")
            params.append(workload_max)
        if experience_band:
            clause = _experience_band_clause(experience_band)
            if clause:
                where.append(clause)
        if is_remote is True:
            where.append("is_remote = 1")
        elif is_remote is False:
            where.append("(is_remote = 0 OR is_remote IS NULL)")
        if hide_stale and not statuses:
            # Only hide stale jobs that the user has never engaged with.
            # Engaged jobs (bookmarked / applied / interview / ...) stay visible.
            where.append(
                "(application_status != 'new' "
                f"OR last_seen_at IS NULL "
                f"OR last_seen_at >= date('now', '-{int(stale_days)} days'))"
            )

        sort_columns = {
            "score": "relevance_score",
            "date": "COALESCE(date_posted, date_scraped)",
            "company": "company COLLATE NOCASE",
            "title": "title COLLATE NOCASE",
            "location": "location COLLATE NOCASE",
            "workload": "workload_percent",
            "source": "source COLLATE NOCASE",
            "last_seen": "last_seen_at",
            "status": "application_status",
        }
        column = sort_columns.get(sort, "relevance_score")
        dir_sql = "DESC" if direction == "desc" else "ASC"
        # Always push NULLs to the end so empty cells don't dominate the top.
        order_by = f"{column} IS NULL, {column} {dir_sql}"

        sql = (
            f"SELECT * FROM jobs WHERE {' AND '.join(where)} "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def count_jobs(
        self,
        *,
        search: Optional[str] = None,
        sources: Optional[list[str]] = None,
        statuses: Optional[list[str]] = None,
        location: Optional[str] = None,
        presence_ch_es: bool = False,
        company_types: Optional[list[str]] = None,
        industries: Optional[list[str]] = None,
        min_score: Optional[float] = None,
        max_score: Optional[float] = None,
        workload_min: Optional[int] = None,
        workload_max: Optional[int] = None,
        experience_band: Optional[str] = None,
        is_remote: Optional[bool] = None,
        hide_ignored: bool = True,
        hide_stale: bool = True,
        stale_days: int = 30,
    ) -> int:
        where = ["1=1"]
        params: list = []
        if search:
            where.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like, like])
        if sources:
            placeholders = ",".join("?" * len(sources))
            where.append(f"source IN ({placeholders})")
            params.extend(sources)
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            where.append(f"application_status IN ({placeholders})")
            params.extend(statuses)
        elif hide_ignored:
            where.append("application_status NOT IN ('ignored', 'archived')")
        if location:
            terms = [t.strip() for t in location.split(",") if t.strip()]
            if terms:
                likes = " OR ".join(["location LIKE ?"] * len(terms))
                where.append(f"({likes})")
                params.extend(f"%{t}%" for t in terms)
        if presence_ch_es:
            clause, ch_es_params = self._presence_ch_es_clause()
            where.append(clause)
            params.extend(ch_es_params)
        if company_types:
            placeholders = ",".join("?" * len(company_types))
            where.append(
                "company IN (SELECT company FROM company_profiles "
                f"WHERE category IN ({placeholders}))"
            )
            params.extend(company_types)
        if industries:
            placeholders = ",".join("?" * len(industries))
            where.append(
                "company IN (SELECT company FROM company_profiles "
                f"WHERE industry IN ({placeholders}))"
            )
            params.extend(industries)
        if min_score is not None:
            where.append("relevance_score >= ?")
            params.append(min_score)
        if max_score is not None:
            where.append("relevance_score <= ?")
            params.append(max_score)
        if workload_min is not None:
            # NULL workload = unknown; include rather than hide.
            where.append("(workload_percent IS NULL OR workload_percent >= ?)")
            params.append(workload_min)
        if workload_max is not None:
            where.append("(workload_percent IS NULL OR workload_percent <= ?)")
            params.append(workload_max)
        if experience_band:
            clause = _experience_band_clause(experience_band)
            if clause:
                where.append(clause)
        if is_remote is True:
            where.append("is_remote = 1")
        elif is_remote is False:
            where.append("(is_remote = 0 OR is_remote IS NULL)")
        if hide_stale and not statuses:
            where.append(
                "(application_status != 'new' "
                f"OR last_seen_at IS NULL "
                f"OR last_seen_at >= date('now', '-{int(stale_days)} days'))"
            )
        sql = f"SELECT COUNT(*) FROM jobs WHERE {' AND '.join(where)}"
        return self.conn.execute(sql, params).fetchone()[0]

    def get_job(self, job_id: int) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def update_status(self, job_id: int, status: str) -> None:
        valid = {s.value for s in ApplicationStatus}
        if status not in valid:
            raise ValueError(f"Invalid status {status!r}; expected one of {valid}")
        now = datetime.now().isoformat()
        # The first move into 'applied' stamps applied_at; later moves onward
        # to interview/offer/rejected leave it alone, so a card can still show
        # when the application actually went out.
        sql = (
            "UPDATE jobs SET application_status = ?, date_updated = ?, "
            "applied_at = CASE WHEN ? = 'applied' AND applied_at IS NULL "
            "THEN ? ELSE applied_at END "
            "WHERE id = ?"
        )
        params = (status, now, status, now, job_id)
        self.conn.execute(sql, params)
        self._audit("UPDATE_STATUS", job_id=job_id, sql=sql, params=params,
                    description=f"Status → {status}")
        self.sync_application_from_job(job_id, status)
        self.conn.commit()

    def _audit(
        self,
        operation: str,
        *,
        job_id: Optional[int] = None,
        sql: str = "",
        params: tuple | list = (),
        description: str = "",
    ) -> None:
        """Append an entry to audit_log so the user can see what SQL ran."""
        self.conn.execute(
            "INSERT INTO audit_log (timestamp, operation, job_id, sql_statement, parameters, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now().isoformat(),
                operation,
                job_id,
                sql,
                json.dumps(list(params), default=str),
                description,
            ),
        )

    def update_status_bulk(self, job_ids: list[int], status: str) -> int:
        if not job_ids:
            return 0
        valid = {s.value for s in ApplicationStatus}
        if status not in valid:
            raise ValueError(f"Invalid status {status!r}")
        placeholders = ",".join("?" * len(job_ids))
        now = datetime.now().isoformat()
        sql = (
            f"UPDATE jobs SET application_status = ?, date_updated = ?, "
            f"applied_at = CASE WHEN ? = 'applied' AND applied_at IS NULL "
            f"THEN ? ELSE applied_at END "
            f"WHERE id IN ({placeholders})"
        )
        params = [status, now, status, now, *job_ids]
        cursor = self.conn.execute(sql, params)
        for job_id in job_ids:
            self.sync_application_from_job(job_id, status)
        self._audit(
            "UPDATE_STATUS_BULK", sql=sql, params=params,
            description=f"{cursor.rowcount} jobs → {status}",
        )
        self.conn.commit()
        return cursor.rowcount

    def update_notes(self, job_id: int, notes: str) -> None:
        sql = "UPDATE jobs SET notes = ?, date_updated = ? WHERE id = ?"
        params = (notes, datetime.now().isoformat(), job_id)
        self.conn.execute(sql, params)
        self._audit(
            "UPDATE_NOTES", job_id=job_id, sql=sql, params=params,
            description=f"Notes ({len(notes)} chars)",
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Step 7 — Cover letter & application submission methods
    # ------------------------------------------------------------------

    def save_cover_letter(
        self,
        job_id: int,
        *,
        markdown: str,
        language: Optional[str] = None,
        format: Optional[str] = None,
        subject: Optional[str] = None,
        body_text: Optional[str] = None,
        briefing: Optional[str] = None,
        checks: Optional[str] = None,
    ) -> None:
        """Persist a generated/edited cover letter and its metadata.

        ``briefing`` is a JSON string holding company/role/profile summaries.
        Like subject/body_text it uses COALESCE — user-edit saves don't wipe
        the briefing, but Generate always passes a fresh one.

        Sprache und Format ebenfalls per COALESCE: das Speichern im Editor
        übergibt beide nicht und hat sie bisher auf NULL gesetzt — der Renderer
        braucht die Sprache aber für Typografie und Betreff. ``checks`` (JSON)
        wird immer ersetzt, weil jede Fassung neu geprüft wird.
        """
        now = datetime.now().isoformat()
        sql = (
            "UPDATE jobs SET cover_letter = ?, cover_letter_lang = COALESCE(?, cover_letter_lang), "
            "cover_letter_format = COALESCE(?, cover_letter_format), cover_letter_generated_at = ?, "
            "email_subject = COALESCE(?, email_subject), "
            "email_body_text = COALESCE(?, email_body_text), "
            "cover_letter_briefing = COALESCE(?, cover_letter_briefing), "
            "cover_letter_checks = ?, "
            "date_updated = ? WHERE id = ?"
        )
        params = (markdown, language, format, now, subject, body_text, briefing, checks, now, job_id)
        self.conn.execute(sql, params)
        self._audit(
            "SAVE_COVER_LETTER", job_id=job_id, sql=sql, params=params,
            description=f"Cover letter ({len(markdown)} chars, lang={language}, fmt={format})",
        )
        self.conn.commit()

    def save_briefing(self, job_id: int, *, briefing_json: str) -> None:
        """Persist a standalone Company/Role/Profile briefing without touching
        the cover letter content. Used by the 'Generate Briefing' button which
        is independent of the cover-letter-generation flow.
        """
        now = datetime.now().isoformat()
        sql = (
            "UPDATE jobs SET cover_letter_briefing = ?, date_updated = ? "
            "WHERE id = ?"
        )
        params = (briefing_json, now, job_id)
        self.conn.execute(sql, params)
        self._audit(
            "SAVE_BRIEFING", job_id=job_id, sql=sql, params=params,
            description=f"Briefing ({len(briefing_json)} chars JSON)",
        )
        self.conn.commit()

    def update_email_fields(
        self,
        job_id: int,
        *,
        subject: Optional[str] = None,
        body_text: Optional[str] = None,
        contact_email: Optional[str] = None,
    ) -> None:
        """Update one or more email-related fields without regenerating the letter."""
        fields = []
        params: list = []
        if subject is not None:
            fields.append("email_subject = ?")
            params.append(subject)
        if body_text is not None:
            fields.append("email_body_text = ?")
            params.append(body_text)
        if contact_email is not None:
            fields.append("contact_email = ?")
            params.append(contact_email)
        if not fields:
            return
        fields.append("date_updated = ?")
        params.append(datetime.now().isoformat())
        params.append(job_id)
        sql = f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?"
        self.conn.execute(sql, params)
        self._audit(
            "UPDATE_EMAIL_FIELDS", job_id=job_id, sql=sql, params=params,
            description=f"Updated {[f.split(' = ')[0] for f in fields[:-1]]}",
        )
        self.conn.commit()

    def save_apply_method(self, job_id: int, *, apply_method_json: str) -> None:
        """Persist the apply-method JSON blob. Always overwrites — re-running
        detection on the same job replaces the previous result."""
        now = datetime.now().isoformat()
        sql = (
            "UPDATE jobs SET apply_method = ?, apply_method_checked_at = ?, "
            "date_updated = ? WHERE id = ?"
        )
        params = (apply_method_json, now, now, job_id)
        self.conn.execute(sql, params)
        self._audit(
            "SAVE_APPLY_METHOD", job_id=job_id, sql=sql, params=params,
            description=f"Apply method ({len(apply_method_json)} chars)",
        )
        self.conn.commit()

    def save_triage(
        self,
        job_id: int,
        *,
        score: float,
        reason: str,
        details: Optional[str] = None,
    ) -> None:
        """Persist a job-quality triage result.

        ``details`` is a JSON string holding strengths/gaps lists.
        Always overwrites — re-running triage on the same job replaces.
        """
        now = datetime.now().isoformat()
        sql = (
            "UPDATE jobs SET match_score = ?, match_reason = ?, "
            "match_details = ?, match_checked_at = ?, "
            "date_updated = ? WHERE id = ?"
        )
        params = (score, reason, details, now, now, job_id)
        self.conn.execute(sql, params)
        self._audit(
            "SAVE_TRIAGE", job_id=job_id, sql=sql, params=params,
            description=f"Match score {score:.1f}/10",
        )
        self.conn.commit()

    def mark_sent(
        self,
        job_id: int,
        *,
        eml_or_smtp: str = "smtp",
        message_id: Optional[str] = None,
        recipient_email: Optional[str] = None,
        recipient_domain: Optional[str] = None,
    ) -> None:
        """Mark application as sent and bump status to `applied`.

        Persists Message-ID + recipient email so the reply-tracker can
        correlate incoming replies later via In-Reply-To headers or
        sender-domain matching.

        ``recipient_domain`` hat Vorrang vor der Ableitung aus der Adresse — für
        den Fall, dass nur die Firmendomain bekannt ist (``recipient_email``
        dann None, application_recipient_email bleibt leer).
        """
        now = datetime.now().isoformat()
        if recipient_domain:
            recipient_domain = recipient_domain.lower().strip()
        elif recipient_email and "@" in recipient_email:
            recipient_domain = recipient_email.split("@", 1)[1].lower().strip()
        sql = (
            "UPDATE jobs SET application_sent_at = ?, application_status = 'applied', "
            "applied_at = COALESCE(applied_at, ?), "
            "application_message_id = COALESCE(?, application_message_id), "
            "application_recipient_email = COALESCE(?, application_recipient_email), "
            "application_recipient_domain = COALESCE(?, application_recipient_domain), "
            "date_updated = ? WHERE id = ?"
        )
        params = (now, now, message_id, recipient_email, recipient_domain, now, job_id)
        self.conn.execute(sql, params)
        self._audit(
            "MARK_SENT", job_id=job_id, sql=sql, params=params,
            description=f"Application sent ({eml_or_smtp})",
        )
        self.sync_application_from_job(job_id, "applied", channel="email")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Reply tracking (Phase C, IMAP poller)
    # ------------------------------------------------------------------
    def find_job_by_message_id(self, message_id: str) -> Optional[dict]:
        """Look up the job whose outbound mail had this Message-ID."""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE application_message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        return dict(row) if row else None

    def find_jobs_by_recipient_domain(
        self,
        domain: str,
        *,
        within_days: int = 90,
    ) -> list[dict]:
        """All applied jobs whose recipient domain matches.

        Fallback-Matching: we only consider replies from a domain we wrote to
        in the last `within_days` days, to avoid stale cross-talk.

        Subdomains zählen mit: eine Antwort von ``hr.firma.example`` passt zu
        gespeichertem ``firma.example``, ``notfirma.example`` nicht — der
        Absender muss auf "." + Domain enden. Bewusst kein LIKE: "_" wäre dort
        ein Platzhalter, und Domains mit "_" sind nicht ausgeschlossen.
        """
        cutoff = (datetime.now() - timedelta(days=within_days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM jobs "
            "WHERE (application_recipient_domain = :d "
            "       OR substr(:d, -length(application_recipient_domain) - 1) "
            "          = '.' || application_recipient_domain) "
            "AND application_sent_at IS NOT NULL "
            "AND application_sent_at >= :cutoff "
            "ORDER BY application_sent_at DESC",
            {"d": domain.lower().strip(), "cutoff": cutoff},
        ).fetchall()
        return [dict(r) for r in rows]

    def reply_exists(self, raw_message_id: str) -> bool:
        """Idempotency: don't double-record the same IMAP message."""
        row = self.conn.execute(
            "SELECT 1 FROM replies WHERE raw_message_id = ? LIMIT 1",
            (raw_message_id,),
        ).fetchone()
        return row is not None

    def record_reply(
        self,
        *,
        job_id: Optional[int],
        imap_uid: Optional[int],
        raw_message_id: Optional[str],
        in_reply_to: Optional[str],
        references_header: Optional[str],
        from_email: Optional[str],
        subject: Optional[str],
        body_excerpt: Optional[str],
        received_at: Optional[str],
        classification: Optional[str] = None,
        classification_confidence: Optional[float] = None,
        match_method: Optional[str] = None,
        auto_bumped: bool = False,
    ) -> int:
        """Persist a detected reply. Returns the row id."""
        from_domain = None
        if from_email and "@" in from_email:
            from_domain = from_email.split("@", 1)[1].lower().strip()
        now = datetime.now().isoformat()
        classified_at = now if classification else None
        cur = self.conn.execute(
            "INSERT INTO replies ("
            "job_id, imap_uid, raw_message_id, in_reply_to, references_header, "
            "from_email, from_domain, subject, body_excerpt, received_at, "
            "classification, classification_confidence, classified_at, "
            "match_method, auto_bumped, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id, imap_uid, raw_message_id, in_reply_to, references_header,
                from_email, from_domain, subject, body_excerpt, received_at,
                classification, classification_confidence, classified_at,
                match_method, 1 if auto_bumped else 0, now,
            ),
        )
        if job_id and classification in REAL_REPLY_CLASSES:
            self.mark_application_replied_for_job(job_id, received_at or now)
        self.conn.commit()
        return cur.lastrowid

    def replies_for_job(self, job_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM replies WHERE job_id = ? ORDER BY received_at DESC, id DESC",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_replies(self, *, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT r.*, j.title as job_title, j.company as job_company "
            "FROM replies r LEFT JOIN jobs j ON j.id = r.job_id "
            "ORDER BY r.received_at DESC, r.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_imap_last_uid(self, mailbox: str) -> int:
        row = self.conn.execute(
            "SELECT last_uid FROM imap_state WHERE mailbox = ?", (mailbox,)
        ).fetchone()
        return int(row["last_uid"]) if row else 0

    def set_imap_last_uid(self, mailbox: str, uid: int) -> None:
        now = datetime.now().isoformat()
        self.conn.execute(
            "INSERT INTO imap_state (mailbox, last_uid, last_polled_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(mailbox) DO UPDATE SET last_uid = excluded.last_uid, "
            "last_polled_at = excluded.last_polled_at",
            (mailbox, uid, now),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Chat-Assistant — per-job conversation history (Phase E)
    # ------------------------------------------------------------------
    def chat_messages(self, job_id: int, mode: str) -> list[dict]:
        """Return chat history for a (job, mode) tuple, oldest first."""
        rows = self.conn.execute(
            "SELECT id, role, content, proposed_edit, created_at, cost_usd "
            "FROM job_chat_messages "
            "WHERE job_id = ? AND mode = ? "
            "ORDER BY created_at ASC, id ASC",
            (job_id, mode),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_chat_message(
        self,
        job_id: int,
        mode: str,
        role: str,
        content: str,
        *,
        proposed_edit: Optional[str] = None,
        cost_usd: Optional[float] = None,
    ) -> int:
        """Append a single chat message; returns the new row id."""
        now = datetime.now().isoformat()
        cur = self.conn.execute(
            "INSERT INTO job_chat_messages "
            "(job_id, mode, role, content, proposed_edit, created_at, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, mode, role, content, proposed_edit, now, cost_usd),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def clear_chat(self, job_id: int, mode: str) -> int:
        """Delete all messages for (job, mode). Returns rows deleted."""
        cur = self.conn.execute(
            "DELETE FROM job_chat_messages WHERE job_id = ? AND mode = ?",
            (job_id, mode),
        )
        self.conn.commit()
        return cur.rowcount

    def get_chat_message(self, message_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM job_chat_messages WHERE id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def jobs_by_status(self, statuses: list[str]) -> dict[str, list[dict]]:
        """For the Pipeline (Kanban) tab — returns {status: [jobs]}."""
        if not statuses:
            return {}
        placeholders = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE application_status IN ({placeholders}) "
            f"ORDER BY relevance_score DESC NULLS LAST",
            statuses,
        ).fetchall()
        result: dict[str, list[dict]] = {s: [] for s in statuses}
        for row in rows:
            d = dict(row)
            result.setdefault(d["application_status"], []).append(d)
        return result

    def distinct_sources(self) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT source FROM jobs ORDER BY source"
            ).fetchall()
        ]

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT application_status, COUNT(*) FROM jobs GROUP BY application_status"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # DB explorer helpers
    # ------------------------------------------------------------------

    def list_tables(self) -> list[dict]:
        """All user-defined tables with row counts."""
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        result = []
        for row in rows:
            name = row["name"]
            count = self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            result.append({"name": name, "row_count": count})
        return result

    def describe_table(self, table: str) -> list[dict]:
        """PRAGMA table_info() — column name, type, nullability, default, pk."""
        # table name controlled (we whitelist) but quote anyway
        if not table.replace("_", "").isalnum():
            raise ValueError(f"Bad table name {table!r}")
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [
            {
                "cid": r["cid"],
                "name": r["name"],
                "type": r["type"],
                "notnull": bool(r["notnull"]),
                "default": r["dflt_value"],
                "pk": bool(r["pk"]),
            }
            for r in rows
        ]

    def recent_audit(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def execute_readonly_sql(self, sql: str, max_rows: int = 200) -> dict:
        """Run a user-supplied SELECT (or pragma/explain). Returns rows + columns.

        Safety:
          - Only allows queries starting with SELECT, WITH, EXPLAIN, or PRAGMA
            table_info/foreign_key_list/index_list (read-only pragmas).
          - Limits rows returned to ``max_rows``.
          - Wraps in a SAVEPOINT and rolls back even if the query somehow
            wrote (defence in depth).
        """
        # Strip leading comments + whitespace so a query like
        #   `-- some note\nSELECT ...`
        # passes the prefix check. Handles both `--` line comments and
        # `/* ... */` block comments.
        head = sql.lstrip()
        while True:
            if head.startswith("--"):
                nl = head.find("\n")
                head = head[nl + 1:].lstrip() if nl >= 0 else ""
            elif head.startswith("/*"):
                end = head.find("*/")
                head = head[end + 2:].lstrip() if end >= 0 else ""
            else:
                break
        first = head.lower()
        allowed = (
            first.startswith("select ")
            or first.startswith("with ")
            or first.startswith("explain ")
            or first.startswith("pragma table_info")
            or first.startswith("pragma index_list")
            or first.startswith("pragma foreign_key_list")
        )
        if not allowed:
            raise ValueError(
                "Only SELECT, WITH, EXPLAIN, and read-only PRAGMA queries are allowed."
            )
        # Forbid sneaky chained writes via semicolon.
        if ";" in sql.rstrip(";"):
            raise ValueError("Multiple statements are not allowed.")

        self.conn.execute("SAVEPOINT readonly_query")
        try:
            cursor = self.conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(max_rows)
            row_dicts = [
                {col: row[i] for i, col in enumerate(columns)} for row in rows
            ]
        finally:
            self.conn.execute("ROLLBACK TO SAVEPOINT readonly_query")
            self.conn.execute("RELEASE SAVEPOINT readonly_query")
        return {
            "columns": columns,
            "rows": row_dicts,
            "row_count": len(row_dicts),
            "truncated": len(row_dicts) >= max_rows,
        }

    def stats_for_dashboard(self) -> dict:
        """Aggregate stats for the Stats tab."""
        total = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        avg_score = self.conn.execute(
            "SELECT AVG(relevance_score) FROM jobs WHERE relevance_score IS NOT NULL"
        ).fetchone()[0]
        by_source = [
            {"source": r["source"], "count": r["cnt"]}
            for r in self.conn.execute(
                "SELECT source, COUNT(*) as cnt FROM jobs GROUP BY source ORDER BY cnt DESC"
            ).fetchall()
        ]
        by_status = [
            {"status": r["application_status"], "count": r["cnt"]}
            for r in self.conn.execute(
                "SELECT application_status, COUNT(*) as cnt FROM jobs "
                "GROUP BY application_status ORDER BY cnt DESC"
            ).fetchall()
        ]
        # Score histogram in 0.1-buckets
        score_buckets = self.conn.execute(
            """SELECT CAST(relevance_score * 10 AS INTEGER) AS bucket, COUNT(*) AS cnt
               FROM jobs WHERE relevance_score IS NOT NULL
               GROUP BY bucket ORDER BY bucket"""
        ).fetchall()
        # Jobs scraped per day (last 30 days, by date_scraped)
        per_day = self.conn.execute(
            """SELECT substr(date_scraped, 1, 10) AS day, COUNT(*) AS cnt
               FROM jobs GROUP BY day ORDER BY day DESC LIMIT 30"""
        ).fetchall()
        stale = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE application_status = 'new' "
            "AND last_seen_at IS NOT NULL "
            "AND last_seen_at < date('now', '-14 days')"
        ).fetchone()[0]
        return {
            "total": total,
            "avg_score": round(avg_score, 3) if avg_score else 0.0,
            "stale_count": stale,
            "by_source": by_source,
            "by_status": by_status,
            "score_buckets": [
                {"bucket": r["bucket"] / 10, "count": r["cnt"]} for r in score_buckets
            ],
            "per_day": [
                {"day": r["day"], "count": r["cnt"]} for r in reversed(per_day)
            ],
        }


def _bool_to_int(value: Optional[bool]) -> Optional[int]:
    if value is None:
        return None
    return 1 if value else 0
