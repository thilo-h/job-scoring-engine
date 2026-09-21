#!/usr/bin/env python3
"""Job-Radar — FIFA-Karten mit Nonagon-Spiderweb für mehrere Stellen auf einer Seite.

Die Radar-Logik selbst liegt in ``src/radar.py`` und ist dieselbe wie im
Job-Drawer des Dashboards. Dieses Script rendert nur eine Übersichtsseite,
z.B. für die ganze Shortlist:

    python scripts/job_radar.py [--profile example] [--status bookmarked applied]
                               [--limit 30] [--out data/exports/job_radar.html]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.models import job_from_row  # noqa: E402
from src.radar import AXES, TEAM_NOTE, build_radar  # noqa: E402
from src.scoring import JobScorer  # noqa: E402


def load_jobs(db_path: Path, statuses: list[str], limit: int) -> list[sqlite3.Row]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in statuses)
    rows = con.execute(
        f"SELECT * FROM jobs WHERE application_status IN ({placeholders}) "
        f"ORDER BY relevance_score DESC LIMIT ?",
        (*statuses, limit),
    ).fetchall()
    con.close()
    return rows


def card_html(row: sqlite3.Row, scorer: JobScorer) -> str:
    radar = build_radar(scorer, job_from_row(row))
    stats = "".join(
        f'<div class="stat"><span class="stat-val">{a["value"]}</span>'
        f'<span class="stat-key">{a["label"]}</span></div>'
        for a in radar.axes
    )
    badges = [f'<span class="badge">REL {radar.final_score:.2f}</span>']
    if row["match_score"] is not None:
        badges.append(f'<span class="badge badge-llm">LLM {row["match_score"]:.1f}/10</span>')
    if radar.penalty or radar.experience_penalty:
        badges.append(f'<span class="badge badge-pen">−{radar.penalty + radar.experience_penalty:.2f} Penalty</span>')
    if radar.boost:
        badges.append(f'<span class="badge badge-boost">+{radar.boost:.2f} Boost</span>')

    return f"""
<article class="card card-{radar.tier}">
  <header class="card-head">
    <div class="ovr"><span class="ovr-num">{radar.ovr}</span><span class="ovr-pos">{radar.position}</span></div>
    <div class="ident">
      <h2>{escape(row["title"] or "")}</h2>
      <p class="club">{escape(row["company"] or "—")}</p>
      <p class="nation">{escape(row["location"] or "—")}</p>
    </div>
  </header>
  {radar.svg(260)}
  <div class="stats">{stats}</div>
  <div class="badges">{"".join(badges)}</div>
</article>"""


CSS = """
:root{--bg:#0b0d12;--fg:#f2f4f8;--muted:#8b93a7;--line:#232838;
      --gold:#d9b45b;--silver:#a8b0c0;--bronze:#b07a4e;--area:#4ea3ff;}
*{box-sizing:border-box}
body{margin:0;padding:32px;background:var(--bg);color:var(--fg);
     font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif}
h1{font-size:22px;margin:0 0 6px}
.lede{color:var(--muted);max-width:70ch;margin:0 0 28px}
.grid{display:grid;gap:20px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{border:1px solid var(--line);border-radius:14px;padding:18px;
      background:linear-gradient(170deg,#141824,#0d1017);display:flex;flex-direction:column}
.card-gold{border-color:var(--gold)}
.card-silver{border-color:var(--silver)}
.card-bronze{border-color:var(--bronze)}
.card-head{display:flex;gap:14px;align-items:flex-start;margin-bottom:8px}
.ovr{text-align:center;flex:0 0 auto}
.ovr-num{display:block;font-size:38px;font-weight:750;line-height:1;letter-spacing:-1px}
.card-gold .ovr-num{color:var(--gold)}
.card-silver .ovr-num{color:var(--silver)}
.card-bronze .ovr-num{color:var(--bronze)}
.ovr-pos{display:block;font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.1em}
.ident{min-width:0}
.ident h2{font-size:14px;font-weight:650;margin:0 0 3px;line-height:1.3}
.club{margin:0;font-size:13px;color:var(--fg);opacity:.85}
.nation{margin:0;font-size:12px;color:var(--muted)}
.radar{width:100%;height:auto;margin:2px 0 10px}
.radar-ring{fill:none;stroke:var(--line);stroke-width:1}
.radar-ring-outer{stroke:#2f3648}
.radar-spoke{stroke:var(--line);stroke-width:1}
.radar-label{fill:var(--muted);font-size:9px;font-weight:700;letter-spacing:.06em}
.radar-area{fill:rgba(78,163,255,.26);stroke:var(--area);stroke-width:1.8;stroke-linejoin:round}
.radar-vertex{fill:var(--area)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:5px 10px;
       padding-top:12px;border-top:1px solid var(--line)}
.stat{display:flex;align-items:baseline;gap:6px}
.stat-val{font-weight:700;font-size:13px;min-width:22px;font-variant-numeric:tabular-nums}
.stat-key{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.badge{font-size:10px;padding:3px 7px;border-radius:20px;border:1px solid var(--line);color:var(--muted)}
.badge-llm{border-color:#2c4a63;color:#7db9e8}
.badge-pen{border-color:#5b2b2b;color:#e08585}
.badge-boost{border-color:#2b5b3a;color:#7fd39a}
footer{margin-top:34px;padding-top:18px;border-top:1px solid var(--line);
       color:var(--muted);font-size:12px;max-width:80ch}
code{background:#161a26;padding:1px 5px;border-radius:4px;font-size:11px}
"""


def build_page(cards: str, count: int, profile: str) -> str:
    legend = " · ".join(f"<b>{abbr}</b> {label}" for _key, abbr, label in AXES)
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job-Radar — {escape(profile)}</title><style>{CSS}</style></head>
<body>
<h1>Job-Radar — {count} Stellen</h1>
<p class="lede">Jede Achse ist ein Sub-Score aus <code>src/scoring.py</code>, auf 0–100
skaliert. OVR = mit den Profil-Gewichten gemitteltes Web (Boosts und Penalties
liegen ausserhalb und stehen als Badge darunter).</p>
<div class="grid">{cards}</div>
<footer>
<p>{legend}</p>
<p>{escape(TEAM_NOTE)}</p>
</footer></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="example")
    ap.add_argument("--status", nargs="+", default=["bookmarked", "applied"])
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--out", default="data/exports/job_radar.html")
    args = ap.parse_args()

    cfg = load_config(profile=args.profile)
    scorer = JobScorer(cfg)
    rows = load_jobs(ROOT / cfg["output"]["database_path"], args.status, args.limit)
    cards = "".join(card_html(r, scorer) for r in rows)

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_page(cards, len(rows), args.profile), encoding="utf-8")
    print(f"{len(rows)} Karten → {out}")


if __name__ == "__main__":
    main()
