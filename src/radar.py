"""Job-Radar — die neun Sub-Scores einer Stelle als Nonagon, im Stil einer FIFA-Karte.

Gemeinsame Grundlage für den Job-Drawer im Dashboard (``/job/{id}/radar``)
und ``scripts/job_radar.py``. Das SVG wird hier gerechnet statt mit einer
JS-Bibliothek, damit beide Stellen exakt dieselbe Grafik zeigen.

Jede Achse ist ein Roh-Score aus ``JobScorer.score_breakdown()`` (0–1, als
0–100 angezeigt). OVR ist das mit den Profil-Gewichten gemittelte Web — bewusst
nicht der ``final_score``: Boosts und Penalties liegen ausserhalb des Webs,
sonst passte die grosse Zahl nicht zur Fläche darunter. Damit die Abweichung
nachvollziehbar bleibt, liegen Web und Score auf derselben Skala (0–100 bzw.
0–1) und ``bridge`` listet, was dazwischen passiert:

    Web 75 + Boost 15 − Penalty 5 → Score 85

Früher war das Web auf 0–99 gestreckt (FIFA-Maximum) — dann wich die Zahl
auch ohne jeden Boost um einen Punkt vom Score ab.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.models import Job
from src.scoring import JobScorer

# Reihenfolge = im Uhrzeigersinn ab 12 Uhr. Die Achsen mit dem meisten Gewicht vorne.
AXES = [
    ("profile_match",      "ROL", "Rolle"),
    ("domain_match",       "IND", "Industrie"),
    ("company_tier",       "COM", "Company"),
    ("location_match",     "LOC", "Location"),
    ("mentoring",          "TEA", "Team*"),
    ("skill_match",        "SKI", "Skills"),
    ("workload_match",     "PEN", "Pensum"),
    ("remote_option",      "REM", "Remote"),
    ("thesis_opportunity", "THE", "Thesis"),
]
TEAM_NOTE = (
    "Team* ist ein Platzhalter: dahinter steht der mentoring-Score "
    "(Keyword-Treffer auf mentoring, career development, Weiterbildung …), "
    "keine echten Team-Daten."
)

# Positions-Kürzel wie auf der FIFA-Karte (ST/CM/CB), aus den Rollenfamilien.
POSITION_CODES = {
    "ai_engineer": "AI",
    "forward_deployed_solutions": "FDE",
    "data_ml_engineer": "DE",
    "technology_consultant": "TC",
    "research": "RES",
    "value_engineer": "VE",
}


@dataclass
class Radar:
    raw: dict[str, float]
    weights: dict[str, float]
    ovr: int                   # "Web" in Punkten, siehe build_radar
    tier: str                  # gold | silver | bronze
    position: str
    final_score: float
    boost: float
    penalty: float
    experience_penalty: float
    capped: bool = False       # Score an 0 oder 1 gedeckelt → Rechnung geht nicht auf

    @property
    def axes(self) -> list[dict]:
        return [
            {"key": key, "abbr": abbr, "label": label,
             "value": round(self.raw.get(key, 0.0) * 100),
             "weight": self.weights.get(key, 0.0)}
            for key, abbr, label in AXES
        ]

    @property
    def score_points(self) -> int:
        return round(self.final_score * 100)

    @property
    def bridge(self) -> list[tuple[str, int]]:
        """Was zwischen Web und Score liegt, in Punkten — nur Posten ungleich 0."""
        items = [("Boost", round(self.boost * 100)),
                 ("Penalty", -round(self.penalty * 100)),
                 ("Erfahrung", -round(self.experience_penalty * 100))]
        return [(label, pts) for label, pts in items if pts]

    def svg(self, size: int = 240) -> str:
        return radar_svg(self.raw, size=size)


def build_radar(scorer: JobScorer, job: Job) -> Radar:
    bd = scorer.score_breakdown(job)
    raw, weights = bd["raw_scores"], scorer.weights
    radar = Radar(
        raw=raw,
        weights=weights,
        ovr=0,
        tier="bronze",
        position=POSITION_CODES.get(scorer.role_family(job) or "", "—"),
        final_score=bd["final_score"],
        boost=round((bd.get("boost") or 0) + (bd.get("language_boost") or 0), 3),
        penalty=bd.get("penalty") or 0.0,
        experience_penalty=bd.get("experience_penalty") or 0.0,
    )
    # Web = gewichtete Summe der Achsen, genau die Grösse, auf die der Scorer
    # Boosts und Penalties addiert. Einzeln gerundet ginge "Web + Posten = Score"
    # bei ~5 % der Stellen um einen Punkt nicht auf — deshalb das Web als Rest
    # aus Score minus Posten. Weicht höchstens 1 Punkt vom ungerundeten Wert ab.
    weighted = sum(raw[k] * weights.get(k, 0.0) for k in raw)
    unclamped = weighted + radar.boost - radar.penalty - radar.experience_penalty
    radar.capped = unclamped < 0 or unclamped > 1
    if radar.capped:
        radar.ovr = round(weighted * 100)
    else:
        radar.ovr = radar.score_points - sum(pts for _, pts in radar.bridge)
    radar.tier = "gold" if radar.ovr >= 75 else "silver" if radar.ovr >= 60 else "bronze"
    return radar


def _polygon(values: list[float], radius: float, cx: float, cy: float) -> str:
    n = len(values)
    points = []
    for i, v in enumerate(values):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        r = radius * max(0.0, min(1.0, v))
        points.append(f"{cx + r * math.cos(angle):.1f},{cy + r * math.sin(angle):.1f}")
    return " ".join(points)


def radar_svg(raw: dict[str, float], size: int = 240) -> str:
    """Nonagon als eigenständiges SVG. Farben kommen per CSS-Klasse (radar-*)."""
    cx = cy = size / 2
    radius = size / 2 - 42          # Platz für Achsenbeschriftung rundherum
    n = len(AXES)
    values = [raw.get(key, 0.0) for key, _, _ in AXES]

    parts = [f'<svg viewBox="0 0 {size} {size}" class="radar" role="img" aria-label="Score-Radar">']
    for step in (0.25, 0.5, 0.75, 1.0):
        cls = "radar-ring radar-ring-outer" if step == 1.0 else "radar-ring"
        parts.append(f'<polygon points="{_polygon([step] * n, radius, cx, cy)}" class="{cls}"/>')
    for i, (_key, abbr, label) in enumerate(AXES):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        x2, y2 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x2:.1f}" y2="{y2:.1f}" class="radar-spoke"/>')
        lx, ly = cx + (radius + 17) * math.cos(angle), cy + (radius + 17) * math.sin(angle)
        anchor = "start" if math.cos(angle) > 0.35 else "end" if math.cos(angle) < -0.35 else "middle"
        parts.append(
            f'<text x="{lx:.1f}" y="{ly + 3.5:.1f}" text-anchor="{anchor}" class="radar-label">'
            f'<title>{label}</title>{abbr}</text>'
        )
    parts.append(f'<polygon points="{_polygon(values, radius, cx, cy)}" class="radar-area"/>')
    for i, v in enumerate(values):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        r = radius * max(0.0, min(1.0, v))
        parts.append(
            f'<circle cx="{cx + r * math.cos(angle):.1f}" cy="{cy + r * math.sin(angle):.1f}" '
            f'r="2.6" class="radar-vertex"/>'
        )
    parts.append("</svg>")
    return "".join(parts)
