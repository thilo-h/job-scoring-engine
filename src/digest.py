"""Wochendigest — montags per Mail an sich selbst.

Eine Mail, die sagt, was diese Woche zu tun ist, statt was gefunden wurde:
Outbound-Zahlen der Vorwoche, Nachfassliste, Direktkontakte ohne Antwort,
neue Watchlist-Stellen, Karriereseiten ohne URL oder ohne Ertrag,
Abdeckungslücken und der Zustand der NEW-Warteschlange.

Verschickt wird per eigenem launchd-Job montags 11:00 (com.jobfinder.digest →
scripts/weekly_digest.sh). ``--if-due`` sorgt dafür, dass höchstens einer pro
Kalenderwoche rausgeht, auch wenn der Job oder jemand von Hand mehrfach läuft.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.database import DRAFT_STALE_DAYS, NEW_QUEUE_LIMIT, week_bounds
from src.notify import NotifyResult, send_to_self
from src.watchlist import TIER_LABELS, TIER_ORDER, Watchlist, company_states, format_hit, recent_hits

# Pro Tier im Digest höchstens so viele Stellen ausschreiben. Ein einzelner
# grosser Arbeitgeber kann dreistellig viele offene Stellen führen; ohne Deckel
# sprengt eine neue Kohorte die Mail.
MAX_HITS_PER_TIER = 8


@dataclass
class DigestResult:
    subject: str
    body: str
    sent: Optional[NotifyResult] = None
    skipped_reason: Optional[str] = None


def _iso_week(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", "'")


def build_digest(config: dict, db, watchlist: Optional[Watchlist], *, now: Optional[datetime] = None) -> tuple[str, str]:
    now = now or datetime.now()
    this_monday, _ = week_bounds(now)
    last_monday = this_monday - timedelta(days=7)
    week_range = (last_monday.isoformat(), this_monday.isoformat())
    _, week, _ = now.isocalendar()

    def count(sql: str, params: tuple = ()) -> int:
        return db.conn.execute(sql, params).fetchone()[0]

    overview = db.outbound_overview(now)
    sent_last_week = count("SELECT COUNT(*) FROM applications WHERE sent_at >= ? AND sent_at < ?", week_range)
    replies_last_week = count("SELECT COUNT(*) FROM applications WHERE replied_at >= ? AND replied_at < ?", week_range)

    lines: list[str] = [
        f"Wochendigest KW {week} · {now:%d.%m.%Y}",
        "",
        f"━━ Outbound — Vorwoche {last_monday:%d.%m.}–{this_monday - timedelta(days=1):%d.%m.} ━━",
        f"  Versendet: {sent_last_week}    Antworten: {replies_last_week}",
        f"  Offen zum Nachfassen: {overview['followups_open']}    "
        f"Entwürfe älter als {DRAFT_STALE_DAYS} Tage: {overview['drafts_stale']}",
        f"  Insgesamt versendet: {overview['total_sent']}",
    ]
    if sent_last_week == 0:
        lines.append("  → Letzte Woche ging keine Bewerbung raus.")
    lines.append("")

    if overview["followups"]:
        lines.append("━━ Nachfassen ━━")
        for a in overview["followups"]:
            lines.append(f"  {a['company']} — {a['role']}")
            lines.append(f"    {a['channel']} · versendet {a['sent_at'][:10]} · fällig seit {a['overdue_days']} Tagen")
        lines.append("")

    stale = [d for d in overview["drafts"] if d["is_stale"]]
    if stale:
        lines.append(f"━━ Entwürfe älter als {DRAFT_STALE_DAYS} Tage ━━")
        for d in stale:
            lines.append(f"  {d['company']} — {d['role']} · seit {d['age_days']} Tagen")
        lines.append("")

    if watchlist is not None:
        lines += _watchlist_sections(db, watchlist, now=now)

    # Intake — bewusst ans Ende: der Engpass ist das Absenden, nicht das Finden.
    last_scrape = db.conn.execute(
        "SELECT MAX(started_at) FROM scrape_runs WHERE status = 'completed'"
    ).fetchone()[0]
    archived_last_week = count(
        "SELECT COUNT(*) FROM audit_log WHERE operation = 'RETENTION_ARCHIVE' "
        "AND timestamp >= ? AND timestamp < ?", week_range,
    )
    lines.append("━━ Intake ━━")
    queue = overview["new_queue"]
    flag = f"  ⚠ Ziel ≤ {NEW_QUEUE_LIMIT}: der Filter ist zu weit, nicht du zu langsam." if queue > NEW_QUEUE_LIMIT else ""
    lines.append(f"  NEW-Warteschlange: {_thousands(queue)}{flag}")
    lines.append(f"  Letzte Breitensuche: {last_scrape[:10] if last_scrape else '—'}"
                 f" · archiviert in der Vorwoche: {_thousands(archived_last_week)}")
    lines += ["", "—", "Dashboard: http://127.0.0.1:8000/"]

    subject = (
        f"Digest KW {week}: {sent_last_week} versendet, "
        f"{overview['followups_open']} nachfassen"
    )
    return subject, "\n".join(lines)


def _watchlist_sections(db, watchlist: Watchlist, *, now: datetime) -> list[str]:
    lines: list[str] = []
    hits = recent_hits(db, since=now - timedelta(days=7))

    if hits:
        lines.append(f"━━ Watchlist — {len(hits)} neue Stellen (7 Tage) ━━")
        by_tier: dict[Optional[str], list[dict]] = {}
        for h in hits:
            by_tier.setdefault(h.get("tier") if h.get("tier") in watchlist.digest_tiers else None, []).append(h)
        for tier in TIER_ORDER + [None]:
            tier_hits = by_tier.get(tier) or []
            if not tier_hits:
                continue
            label = f"Tier {tier} · {TIER_LABELS[tier]}" if tier else "Keyword-Treffer"
            lines.append(f"  {label} ({len(tier_hits)})")
            for h in tier_hits[:MAX_HITS_PER_TIER]:
                lines.append("    " + format_hit(h).replace("\n", "\n    "))
            if len(tier_hits) > MAX_HITS_PER_TIER:
                lines.append(f"    + {len(tier_hits) - MAX_HITS_PER_TIER} weitere im Dashboard unter /watchlist")
        lines.append("")

    direct = [e for e in watchlist.entries if e.is_direct_email]
    if direct:
        lines.append("━━ Direktkontakte (Tier C — E-Mail statt Bewerbung) ━━")
        applications = db.list_applications()
        for entry in direct:
            mine = [a for a in applications if watchlist.entry_for(a["company"]) is entry]
            lines.append(f"  {entry.name}: {_contact_state(mine, now)}")
        lines.append("")

    states = company_states(db)
    entries = {e.key: e for e in watchlist.entries}
    missing = [e for e in watchlist.entries if not e.is_direct_email and e.scrape_entry is None]
    if missing:
        lines.append(f"━━ Karriereseite: URL fehlt ({len(missing)}) — bitte nachtragen, nicht raten ━━")
        for tier in TIER_ORDER:
            names = [e.name for e in missing if e.tier == tier]
            if names:
                lines.append(f"  Tier {tier}: {', '.join(names)}")
        lines.append("")

    def scraped(state: dict) -> bool:
        entry = entries.get(state["company_key"])
        return entry is not None and not entry.is_direct_email

    broken = [s for s in states if s.get("careers_page_note") and scraped(s)]
    if broken:
        lines.append(f"━━ Karriereseite liefert nichts ({len(broken)}) ━━")
        for s in broken:
            lines.append(f"  {s['name']}: {s['careers_page_note']}")
        lines.append("")

    single = [s for s in states if len(s["sources"]) == 1]
    if single:
        lines.append(f"━━ Abdeckungslücken — nur eine Quelle ({len(single)}) ━━")
        for s in single:
            lines.append(f"  {s['name']} (Tier {s['tier']}): nur {s['sources'][0]}, {s['job_count']} Stelle{'n' if s['job_count'] != 1 else ''}")
        lines.append("")
    none = [s for s in states if not s["sources"] and scraped(s)]
    if none:
        lines.append(f"  Ohne jeden Treffer in allen Quellen ({len(none)}): " + ", ".join(s["name"] for s in none))
        lines.append("")
    return lines


def _contact_state(applications: list[dict], now: datetime) -> str:
    if not applications:
        return "noch kein Kontakt erfasst"
    latest = max(applications, key=lambda a: a.get("sent_at") or a["created_at"])
    if latest.get("replied_at"):
        return f"Antwort am {latest['replied_at'][:10]} ({latest['status']})"
    if latest.get("sent_at"):
        days = (now - datetime.fromisoformat(latest["sent_at"])).days
        return f"Kontakt seit {days} Tagen ohne Antwort"
    days = (now - datetime.fromisoformat(latest["created_at"])).days
    return f"Entwurf seit {days} Tagen, noch nicht verschickt"


def run_digest(
    config: dict, db, watchlist: Optional[Watchlist], *,
    if_due: bool = False, send: bool = True, now: Optional[datetime] = None,
) -> DigestResult:
    now = now or datetime.now()
    week = _iso_week(now)
    if if_due and db.get_meta("digest.last_week") == week:
        return DigestResult(subject="", body="", skipped_reason=f"für {week} schon verschickt")
    subject, body = build_digest(config, db, watchlist, now=now)
    result = DigestResult(subject=subject, body=body)
    if send:
        result.sent = send_to_self(config, subject=subject, body=body, slug="wochendigest")
        db.set_meta("digest.last_week", week)
    return result
