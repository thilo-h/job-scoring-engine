"""CLI entry point for the Job Finder."""

# macOS SSL fix: must come before every other import so that third-party
# libraries (requests and friends) use the right CA bundle too.
import os

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from src.config import load_config
from src.database import JobDatabase
from src.models import job_from_row
from src.scoring import JobScorer
from src.scraper.base import BaseScraper
from src.scraper.rate_limiter import RateLimiter

console = Console()

# Registry of available scrapers: name -> (module_path, class_name)
# Reihenfolge = Ausführungsreihenfolge beim scrape-Command
# Only sources whose terms permit automated access are implemented. Portals
# that prohibit scraping in their terms of use (LinkedIn, Indeed, Glassdoor)
# are deliberately absent — see the README.
SCRAPER_REGISTRY = {
    # Switzerland
    "jobs_ch":       ("src.scraper.jobs_ch",          "JobsChScraper"),   # public search API
    "swissdevjobs":  ("src.scraper.swissdevjobs",     "SwissDevJobsScraper"),  # public preload API
    "startupticker": ("src.scraper.startupticker",    "StartuptickerScraper"),  # server-rendered HTML
    # Spain
    "tecnoempleo":   ("src.scraper.tecnoempleo",      "TecnoempleoScraper"),   # server-rendered HTML
    "barcelonajobs": ("src.scraper.barcelonajobs",    "BarcelonaJobsScraper"),  # RSS feed
    # Curated company career pages (cross-country, profile-driven)
    "career_pages":  ("src.scraper.career_pages",     "CareerPagesScraper"),
}


def open_db(config: dict) -> JobDatabase:
    """Open the profile's database, creating the schema if it is not there yet.

    `init_schema()` is idempotent, so every command can call this — including the
    read-only ones. Without it, the first command anyone runs in a fresh clone
    fails with a bare "no such table: jobs" from SQLite instead of just working.
    """
    db = JobDatabase(config["output"]["database_path"])
    db.init_schema()
    return db


def get_scrapers(config: dict, rate_limiter: RateLimiter) -> list[BaseScraper]:
    """Instantiate enabled scrapers from config."""
    enabled = config["scrapers"]["enabled"]
    scrapers: list[BaseScraper] = []

    for name in enabled:
        if name not in SCRAPER_REGISTRY:
            logging.debug(f"Scraper '{name}' not yet implemented, skipping")
            continue

        module_path, class_name = SCRAPER_REGISTRY[name]
        try:
            mod = __import__(module_path, fromlist=[class_name])
            cls = getattr(mod, class_name)
            scrapers.append(cls(rate_limiter=rate_limiter))
        except Exception as e:
            logging.error(f"Failed to load scraper '{name}': {e}")

    return scrapers


def cmd_scrape(config: dict, if_due_days: Optional[float] = None) -> None:
    """Run all enabled scrapers, then auto-export to CSV."""
    db = open_db(config)

    if if_due_days is not None:
        # Breitensuche nur wöchentlich. Das Nightly-Script ruft das
        # jede Nacht auf; gescrapt wird erst, wenn der letzte abgeschlossene
        # Lauf — auch ein manueller aus dem Dashboard — alt genug ist.
        from datetime import datetime, timedelta
        last = db.conn.execute(
            "SELECT MAX(started_at) FROM scrape_runs WHERE status = 'completed'"
        ).fetchone()[0]
        if last and datetime.fromisoformat(last) > datetime.now() - timedelta(days=if_due_days):
            console.print(f"[dim]Breitensuche nicht fällig — letzter Lauf {last[:16]}, Intervall {if_due_days} Tage[/dim]")
            db.close()
            return
    scorer = JobScorer(config)
    rate_limiter = RateLimiter.from_config(config)
    scrapers = get_scrapers(config, rate_limiter)

    if not scrapers:
        console.print("[yellow]No scrapers available. Check your config.[/yellow]")
        return

    total_found = 0
    total_new = 0

    for scraper in scrapers:
        console.print(f"\n[bold blue]Running {scraper.name}...[/bold blue]")
        jobs_found = 0
        jobs_new = 0

        try:
            for job in scraper.scrape(config["search"]):
                job.relevance_score = scorer.score(job)
                is_new = db.upsert_job(job)
                jobs_found += 1
                if is_new:
                    jobs_new += 1

                # Progress indicator every 10 jobs
                if jobs_found % 10 == 0:
                    console.print(f"  ... {jobs_found} jobs processed", style="dim")

            db.log_scrape_run(scraper.name, jobs_found, jobs_new, "completed")
            console.print(
                f"[green]{scraper.name}: {jobs_found} found, {jobs_new} new[/green]"
            )
        except Exception as e:
            db.log_scrape_run(scraper.name, jobs_found, jobs_new, "failed", str(e))
            console.print(f"[red]{scraper.name} failed: {e}[/red]")
        finally:
            # Drain the per-keyword log even on failure — a scraper that died
            # halfway still tells us which keywords ran and what they returned.
            db.log_search_queries(scraper.name, scraper.query_log)

        total_found += jobs_found
        total_new += jobs_new

    console.print(f"\n[bold]Total: {total_found} jobs found, {total_new} new[/bold]")

    # ── Auto-Export ─────────────────────────────────────────────────────
    # Zwei Exporte pro Scrape-Run:
    #   1) Live-CSV  → data/jobs_export.csv (immer überschrieben, für Numbers)
    #   2) Snapshot  → data/exports/jobs_<timestamp>.csv (historische Trail)
    from datetime import datetime

    live_path = config["output"]["csv_export_path"]
    snapshot_dir = Path(live_path).parent / "exports"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"jobs_{datetime.now():%Y-%m-%d_%H-%M}.csv"

    n_live = db.export_csv(live_path)
    n_snapshot = db.export_csv(snapshot_path)
    console.print(f"\n[green]Auto-exported {n_live} jobs[/green]")
    console.print(f"  📄 Live    : {live_path}", style="dim")
    console.print(f"  📦 Snapshot: {snapshot_path} ({n_snapshot} rows)", style="dim")

    db.close()


def cmd_top(config: dict, limit: int) -> None:
    """Show top-scored jobs."""
    db = open_db(config)
    jobs = db.get_jobs(limit=limit)

    if not jobs:
        console.print("[yellow]No jobs in database. Run scrape first.[/yellow]")
        return

    table = Table(title=f"Top {limit} Jobs by Relevance")
    table.add_column("Score", style="bold cyan", width=6)
    table.add_column("Title", style="white", max_width=40)
    table.add_column("Company", style="green", max_width=25)
    table.add_column("Location", max_width=15)
    table.add_column("Workload", width=8)
    table.add_column("Source", style="dim", width=12)

    for job in jobs:
        score = job.get("relevance_score")
        score_str = f"{score:.2f}" if score is not None else "—"
        workload = job.get("workload_percent")
        workload_str = f"{workload}%" if workload else "—"

        table.add_row(
            score_str,
            job.get("title", ""),
            job.get("company", ""),
            job.get("location", ""),
            workload_str,
            job.get("source", ""),
        )

    console.print(table)
    db.close()


def cmd_export(config: dict) -> None:
    """Export jobs to CSV."""
    db = open_db(config)
    path = config["output"]["csv_export_path"]
    count = db.export_csv(path)
    console.print(f"[green]Exported {count} jobs to {path}[/green]")
    db.close()


def cmd_rescore(config: dict) -> None:
    """Re-score alle Jobs in der DB mit dem aktuellen Scoring — kein neues Scraping."""
    db = open_db(config)
    scorer = JobScorer(config)

    rows = db.conn.execute(
        "SELECT id, title, company, location, description, min_years_experience, "
        "workload_percent, is_remote FROM jobs"
    ).fetchall()

    if not rows:
        console.print("[yellow]Keine Jobs in der DB. Erst scrapen![/yellow]")
        return

    updated = 0
    score_changes: list[tuple[str, float, float]] = []

    for row in rows:
        job = job_from_row(row)
        new_score = scorer.score(job)

        # Alten Score holen für Diff
        old_row = db.conn.execute(
            "SELECT relevance_score FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        old_score = old_row["relevance_score"] if old_row else None

        db.conn.execute(
            "UPDATE jobs SET relevance_score = ? WHERE id = ?",
            (new_score, row["id"]),
        )
        updated += 1

        if old_score is not None and abs(new_score - old_score) > 0.05:
            score_changes.append((row["title"], old_score, new_score))

    db.conn.commit()
    console.print(f"[green]Re-scored {updated} jobs[/green]")

    # Top-10 grösste Score-Änderungen zeigen
    if score_changes:
        score_changes.sort(key=lambda x: abs(x[2] - x[1]), reverse=True)
        console.print("\n[bold]Grösste Score-Änderungen:[/bold]")
        for title, old, new in score_changes[:10]:
            arrow = "↑" if new > old else "↓"
            color = "green" if new > old else "red"
            console.print(
                f"  [{color}]{arrow}[/{color}] {old:.3f} → {new:.3f}  ({title[:60]})"
            )

    # CSV neu exportieren mit neuen Scores
    path = config["output"]["csv_export_path"]
    n = db.export_csv(path)
    console.print(f"\n[green]CSV neu exportiert: {n} Jobs → {path}[/green]")
    db.close()


def cmd_stats(config: dict) -> None:
    """Show database statistics."""
    db = open_db(config)
    stats = db.get_stats()
    console.print("\n[bold]Database Statistics[/bold]")
    console.print(f"  Total jobs: {stats['total_jobs']}")
    if stats["avg_relevance_score"]:
        console.print(f"  Avg relevance score: {stats['avg_relevance_score']:.3f}")
    if stats["by_source"]:
        console.print("  By source:")
        for source, count in stats["by_source"].items():
            console.print(f"    {source}: {count}")
    db.close()


def cmd_poll_replies(config: dict, dry_run: bool) -> None:
    """Poll IMAP for replies to sent applications and update statuses."""
    from src.agent.reply_tracker import poll_replies

    db = open_db(config)
    try:
        summary = poll_replies(db, dry_run=dry_run)
    except RuntimeError as exc:
        console.print(f"[red]Reply-tracker error:[/red] {exc}")
        sys.exit(1)

    console.print("\n[bold]Reply-poll summary[/bold]")
    for k, v in summary.as_dict().items():
        console.print(f"  {k}: {v}")
    if summary.bumped:
        console.print(
            f"\n[green]Auto-bumped {summary.bumped} job status(es).[/green] "
            "Open the dashboard to review."
        )
    db.close()


def cmd_classify_companies(config: dict, limit: int, all_ages: bool) -> None:
    """Classify unclassified companies (startup/scaleup/sme/enterprise) via Haiku."""
    from src.agent.company_classifier import classify_companies

    db = open_db(config)
    fresh_days = None if all_ages else 30
    summary = classify_companies(db, limit=limit, fresh_days=fresh_days)

    console.print("\n[bold]Company classification summary[/bold]")
    console.print(f"  classified: {summary.classified}  (in {summary.api_calls} API calls, ${summary.cost_usd:.4f})")
    for cat, n in sorted(summary.by_category.items(), key=lambda kv: -kv[1]):
        console.print(f"    {cat}: {n}")
    stats = db.company_classification_stats()
    console.print(
        f"  total coverage: {stats['classified']}/{stats['companies_total']} companies in DB"
    )
    db.close()


def cmd_mark_sent(config: dict, job_id: int, recipient: str, message_id: Optional[str]) -> None:
    """Manually record a sent application (for mails sent outside the tool)."""
    db = open_db(config)
    job = db.get_job(job_id)
    if not job:
        console.print(f"[red]Job {job_id} nicht gefunden.[/red]")
        sys.exit(1)
    if "@" not in recipient:
        console.print(f"[red]'{recipient}' ist keine gültige E-Mail-Adresse.[/red]")
        sys.exit(1)
    db.mark_sent(
        job_id, eml_or_smtp="manual",
        message_id=message_id, recipient_email=recipient,
    )
    domain = recipient.split("@", 1)[1].lower()
    console.print(
        f"[green]✓[/green] Job {job_id} ({job['title']} @ {job['company']}) als beworben markiert.\n"
        f"  Empfänger: {recipient} (Domain-Matching: {domain})\n"
        f"  Message-ID: {message_id or '(keine — Reply-Tracker nutzt Domain-Fallback)'}"
    )
    db.close()


def cmd_watchlist_poll(config: dict, *, scrape: bool, notify: bool) -> None:
    """Täglicher Watchlist-Poll: Karriereseiten, Breitensuche-Abgleich, Keywords."""
    from src.watchlist import TIER_ORDER, load_watchlist, poll

    profile = config["_meta"]["profile"]
    watchlist = load_watchlist(profile)
    if watchlist is None:
        console.print(f"[yellow]Keine config/watchlist_{profile}.yaml — nichts zu tun.[/yellow]")
        return

    db = open_db(config)
    result = poll(config, db, watchlist, scrape=scrape, notify=notify)

    counts = {t: sum(1 for e in watchlist.entries if e.tier == t) for t in TIER_ORDER}
    console.print(
        f"\n[bold]Watchlist[/bold] {len(watchlist.entries)} Firmen "
        + " · ".join(f"{t}:{n}" for t, n in counts.items())
    )
    if scrape:
        table = Table(title="Karriereseiten", show_lines=False)
        table.add_column("Firma")
        table.add_column("Tier")
        table.add_column("Stellen", justify="right")
        table.add_column("neu in DB", justify="right")
        table.add_column("Hinweis")
        for o in result.scraped:
            hint = o.note or (f"{o.rejected} Nicht-Stellen-Links verworfen" if o.rejected else "")
            table.add_row(o.entry.name, o.entry.tier, str(o.jobs), str(o.new_jobs), hint)
        console.print(table)
    console.print(
        f"Übersprungen: {len(result.skipped_no_url)} ohne careers_url, "
        f"{len(result.skipped_direct_email)} direct_email"
    )
    console.print(
        f"Watchlist-Firmen zugeordnete Stellen in der DB: {result.matched_jobs}"
    )
    labels = {"seeded": "Bestand übernommen", "excluded": "hard_exclude", "pending": "neu → Sofort-Alert",
              "digest_only": "neu → nur Digest"}
    for status, n in sorted(result.status_counts.items()):
        console.print(f"  {labels.get(status, status)}: {n}")
    console.print(f"Keyword-Treffer: {result.keyword_hits}")

    if result.alert_error:
        console.print(f"[red]Alert-Versand fehlgeschlagen: {result.alert_error} — bleibt für den nächsten Poll offen[/red]")
    elif result.alert:
        where = result.alert.path if result.alert.mode == "dry_run" else result.alert.recipient
        console.print(f"[green]Alert mit {result.alerted} Stellen → {where}[/green] ({result.alert.mode})")
    elif notify:
        console.print("[dim]Nichts Neues zu melden.[/dim]")
    else:
        console.print("[dim]--no-notify: offene Alerts bleiben für den nächsten Poll liegen.[/dim]")
    db.close()


def cmd_digest(config: dict, *, if_due: bool, print_only: bool) -> None:
    """Wochendigest bauen und an sich selbst schicken (NOTIFY_DRY_RUN beachtet)."""
    from src.digest import run_digest
    from src.watchlist import load_watchlist

    db = open_db(config)
    result = run_digest(
        config, db, load_watchlist(config["_meta"]["profile"]),
        if_due=if_due, send=not print_only,
    )
    if result.skipped_reason:
        console.print(f"[dim]Digest übersprungen: {result.skipped_reason}[/dim]")
    else:
        console.print(f"[bold]{result.subject}[/bold]\n")
        console.print(result.body, markup=False, highlight=False)
        if result.sent:
            where = result.sent.path if result.sent.mode == "dry_run" else result.sent.recipient
            console.print(f"\n[green]Digest → {where}[/green] ({result.sent.mode})")
    db.close()


def main():
    parser = argparse.ArgumentParser(
        description="Job Scoring Engine — scrape, score and review job postings")
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to config YAML (overrides --profile)",
    )
    parser.add_argument(
        "--profile", "-p",
        type=str,
        default=None,
        help="Profile name (loads config/profile_<name>.yaml). Defaults to "
             "$JOBFINDER_PROFILE or 'example'.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # scrape
    scrape_parser = subparsers.add_parser("scrape", help="Run all enabled scrapers")
    scrape_parser.add_argument(
        "--if-due-days", type=float, default=None,
        help="Nur scrapen, wenn der letzte abgeschlossene Lauf älter als N Tage ist (Nightly: 6.5)",
    )

    # retention
    retention_parser = subparsers.add_parser(
        "retention", help="Score < 0.5 und 60 Tage unangetastet → archived (und zurück, wenn der Score steigt)",
    )
    retention_parser.add_argument("--dry-run", action="store_true", help="Nur zählen")

    # top
    top_parser = subparsers.add_parser("top", help="Show top-scored jobs")
    top_parser.add_argument("-n", "--limit", type=int, default=20, help="Number of jobs to show")

    # export
    subparsers.add_parser("export", help="Export jobs to CSV")

    # rescore
    subparsers.add_parser(
        "rescore",
        help="Re-score alle Jobs in der DB mit dem aktuellen Scoring (kein neues Scraping)",
    )

    # stats
    subparsers.add_parser("stats", help="Show database statistics")

    # poll-replies
    poll_parser = subparsers.add_parser(
        "poll-replies",
        help="Connect to IMAP and correlate inbound mails with sent applications",
    )
    poll_parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch + match but skip Haiku classification and status bump",
    )

    # mark-sent
    marksent_parser = subparsers.add_parser(
        "mark-sent",
        help="Manuell als beworben markieren (für außerhalb des Tools versendete Mails) — füttert das Reply-Tracking",
    )
    marksent_parser.add_argument("job_id", type=int, help="Job-ID (siehe Dashboard-Drawer oder DB)")
    marksent_parser.add_argument(
        "-r", "--recipient", required=True,
        help="Empfänger-Adresse der Bewerbung (z.B. jobs@firma.ch)",
    )
    marksent_parser.add_argument(
        "-m", "--message-id", default=None,
        help="Optional: Message-ID aus dem Sent-Ordner (Header 'Message-ID: <...>') für exaktes Threading",
    )

    # classify-companies
    classify_parser = subparsers.add_parser(
        "classify-companies",
        help="Classify companies as startup/scaleup/sme/enterprise (drives the company-type browse filter)",
    )
    classify_parser.add_argument(
        "-n", "--limit", type=int, default=200,
        help="Max companies to classify per run (default: 200)",
    )
    classify_parser.add_argument(
        "--all-ages", action="store_true",
        help="Also classify companies whose jobs are stale (default: only fresh jobs from the last 30 days)",
    )

    # watchlist-poll
    wl_parser = subparsers.add_parser(
        "watchlist-poll",
        help="Täglicher Watchlist-Poll (config/watchlist_<profil>.yaml): Karriereseiten, "
             "Breitensuche-Abgleich, Keyword-Treffer, Sofort-Alert",
    )
    wl_parser.add_argument("--no-scrape", action="store_true",
                           help="Karriereseiten nicht abrufen, nur die DB abgleichen")
    wl_parser.add_argument("--no-notify", action="store_true",
                           help="Keine Mail; offene Alerts bleiben für den nächsten Poll liegen")

    # digest
    digest_parser = subparsers.add_parser(
        "digest", help="Wochendigest (Outbound, Nachfassen, Watchlist, Abdeckung) an die eigene Adresse",
    )
    digest_parser.add_argument("--if-due", action="store_true",
                               help="Nur verschicken, wenn für diese Kalenderwoche noch keiner rausging")
    digest_parser.add_argument("--print-only", action="store_true",
                               help="Nur anzeigen, nichts verschicken")

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_time=False)],
    )

    # Load config
    try:
        config = load_config(args.config, profile=args.profile)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Config error: {e}[/red]")
        sys.exit(1)
    console.print(f"[dim]Active profile: {config['_meta']['profile']}  →  {config['_meta']['config_path']}[/dim]")

    # Default to scrape if no command given
    command = args.command or "scrape"

    if command == "scrape":
        cmd_scrape(config, getattr(args, "if_due_days", None))
    elif command == "retention":
        db = open_db(config)
        result = db.run_retention(dry_run=args.dry_run)
        queue = db.conn.execute("SELECT COUNT(*) FROM jobs WHERE application_status = 'new'").fetchone()[0]
        verb = "würden" if args.dry_run else ""
        console.print(f"Retention: {result['archived']} {verb} archiviert, {result['restored']} {verb} zurückgeholt".replace("  ", " "))
        console.print(f"NEW-Warteschlange {'jetzt' if not args.dry_run else 'aktuell'}: {queue}" + (" (Ziel ≤ 500)" if queue > 500 else ""))
        db.close()
    elif command == "top":
        cmd_top(config, args.limit)
    elif command == "export":
        cmd_export(config)
    elif command == "rescore":
        cmd_rescore(config)
    elif command == "stats":
        cmd_stats(config)
    elif command == "poll-replies":
        cmd_poll_replies(config, args.dry_run)
    elif command == "classify-companies":
        cmd_classify_companies(config, args.limit, args.all_ages)
    elif command == "mark-sent":
        cmd_mark_sent(config, args.job_id, args.recipient, args.message_id)
    elif command == "digest":
        cmd_digest(config, if_due=args.if_due, print_only=args.print_only)
    elif command == "watchlist-poll":
        cmd_watchlist_poll(config, scrape=not args.no_scrape, notify=not args.no_notify)


if __name__ == "__main__":
    main()
