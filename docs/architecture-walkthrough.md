# Dashboard-Walkthrough — FastAPI + HTMX

> Eine kommentierte Tour durch das Dashboard: wie ein Request vom Browser zur
> SQL-Query und zurück zum HTML wird, warum es HTMX statt einer SPA ist, und
> welche Fallen dabei auftraten. Geschrieben so, dass man jeden Schritt
> nachvollziehen kann, auch ohne vorher eine Web-App gebaut zu haben.

---

## 0 · Mentales Modell — was ist hier eigentlich passiert?

Bevor wir in den Code schauen, das **Big Picture**:

```
   Browser (Chrome)               Dein Mac (Python)              Disk
   ───────────────                ─────────────────              ────
   ┌────────────┐                 ┌────────────────┐         ┌─────────┐
   │  HTML/CSS  │  ─── HTTP ───►  │   uvicorn      │  ────►  │ jobs.db │
   │   + JS     │  ◄── HTML ───   │   FastAPI App  │  ◄────  │  SQLite │
   └────────────┘                 │   + Jinja2     │         └─────────┘
                                  └────────────────┘
```

Drei Schichten:

1. **Browser** zeigt HTML an. Wenn du auf "Bookmark" klickst, schickt er einen `POST`-Request.
2. **Python-Prozess** (uvicorn = Webserver, FastAPI = Routing-Logik) empfängt den Request, macht eine SQL-Query, rendert eine HTML-Antwort mit Jinja2, schickt sie zurück.
3. **SQLite** ist exakt dieselbe `data/jobs.db` die deine Scraper befüllen — nichts Neues.

Das ist **alles**. Keine REST-API, keine GraphQL, kein React, kein Build-Schritt. Nur HTML hin und her.

### Was macht HTMX dann?

Klassisches Web (1995–2010): du klickst irgendwo, Browser lädt **die ganze Seite** neu. Mühsam.

React/Vue/Angular (2013+): JavaScript managt einen Client-State, du schreibst eine zweite App in TypeScript, Build-Tools, npm, Frust.

HTMX (2020+): du schreibst weiter ganz normales HTML, aber kannst auf **jedes Element** Attribute schreiben, die sagen "wenn ich angeklickt werde, mach einen HTTP-Request und ersetze nur **diesen Teil** der Seite mit der Antwort". Dadurch fühlt sich die App fast wie ein SPA an, aber du schreibst nur Server-Code + HTML.

**Beispiel:**
```html
<button hx-post="/job/42/status" hx-vals='{"status": "bookmarked"}'
        hx-target="#row-42" hx-swap="outerHTML">
  Bookmark
</button>
```

Das heisst: "Beim Klick: schicke `POST /job/42/status` mit `status=bookmarked`. Die HTML-Antwort ersetzt das Element mit `id="row-42"`."

Der Server liefert genau eine Tabellenzeile (kein Full-Page-Reload, kein JSON), HTMX swappt sie ein. Fertig.

Das ist der ganze Trick.

---

## 1 · Architektur — was steht wo?

```
Job finder/
├── src/                       ← Phase 1 Code (Scraper, Scoring, DB)
│   ├── database.py            ← SQLite-Wrapper. Wir haben hier nur 2 Sachen ergänzt:
│   │                            (a) Migration: jobs.notes Spalte
│   │                            (b) Dashboard-Queries (query_jobs, count_jobs, ...)
│   ├── models.py              ← Job-Dataclass + Enums (unverändert)
│   └── ...                    ← Scraper, Scoring, etc. (unverändert)
│
└── dashboard/                 ← NEU. Komplettes Web-Frontend.
    ├── app.py                 ← FastAPI-App-Objekt. 12 Zeilen Code.
    ├── deps.py                ← Shared Setup: DB-Handle, Jinja2, Konstanten.
    ├── routes/
    │   ├── browse.py          ← Tab "Browse" — die Tabelle + Filter
    │   ├── pipeline.py        ← Tab "Pipeline" — Kanban
    │   ├── stats.py           ← Tab "Stats" — Charts
    │   └── actions.py         ← POST-Endpoints (Status update, Notes, Scrape)
    ├── templates/
    │   ├── base.html          ← Layout (Tabs, Theme-Toggle)
    │   ├── browse.html        ← Browse-Tab Page
    │   ├── pipeline.html      ← Pipeline-Tab Page
    │   ├── stats.html         ← Stats-Tab Page
    │   └── partials/          ← Kleine Stücke die per HTMX nachgeladen werden
    │       ├── browse_table.html    ← Die Tabelle (wird beim Filtern getauscht)
    │       ├── job_row.html         ← Eine Zeile (wird beim Status-Update getauscht)
    │       ├── job_detail.html      ← Der Slide-in-Drawer rechts
    │       ├── kanban_columns.html  ← Die Kanban-Spalten
    │       └── kanban_card.html     ← Eine Kanban-Karte
    └── static/
        └── style.css          ← CSS mit Dark/Light Tokens
```

**Trennung zwischen `src/` und `dashboard/`:** der ganze Phase-1-Code in `src/` weiss nichts vom Dashboard. Das Dashboard importiert aus `src/` (DB, Models), aber nicht umgekehrt. So bleibt die CLI (`python -m src.main scrape`) unabhängig.

---

## 2 · Request Lifecycle — was passiert wenn du eine URL öffnest?

Du öffnest `http://127.0.0.1:8000/?min_score=0.5`. Schritt für Schritt:

```
 1. Browser:        GET /?min_score=0.5
                    │
 2. uvicorn:        ─► empfängt TCP-Request, parsed HTTP, gibt an FastAPI weiter
                    │
 3. FastAPI:        ─► matched die URL "/" gegen seine Routes
                    │   findet: dashboard/routes/browse.py:browse()
                    │
 4. FastAPI:        ─► parsed Query-Params: min_score="0.5" (string)
                    │   ruft die Helper-Funktion _opt_float("0.5") → 0.5 (float)
                    │
 5. browse():       ─► db.query_jobs(min_score=0.5, sort="score", limit=50)
                    │   ↓
 6. database.py:    ─► baut SQL: SELECT * FROM jobs WHERE relevance_score >= 0.5
                    │             ORDER BY relevance_score DESC LIMIT 50
                    │   ↓
 7. SQLite:         ─► liefert 37 Rows
                    │
 8. browse():       ─► templates.TemplateResponse(request, "browse.html", ctx)
                    │   ↓
 9. Jinja2:         ─► nimmt browse.html, rendert mit ctx-Dict
                    │   {{ job.title }} → "Data Engineer @ Sample Energy"
                    │   {% for job in jobs %} → loop über 37 Rows
                    │
10. uvicorn:        ◄─ schickt fertiges HTML als HTTP-Response zurück
                    │
11. Browser:        rendert HTML, lädt CSS + HTMX-JS, fertig.
```

Das wars. Kein Mysterium. **Das Dashboard in einer Zeile:** URL kommt rein → SQL-Query → HTML rendern → zurückschicken.

---

## 3 · Code-Deep-Dive — die Schlüsselstellen

### 3.1 · Die FastAPI-App ([dashboard/app.py](dashboard/app.py))

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from dashboard.routes import actions, browse, pipeline, stats

app = FastAPI(title="Job Finder Dashboard", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

app.include_router(browse.router)
app.include_router(pipeline.router)
app.include_router(stats.router)
app.include_router(actions.router)
```

Was lernen wir hier?

- **`app = FastAPI(...)`** — das ist das App-Objekt. Wenn `uvicorn` startet, läuft es genau dieses Objekt.
- **`app.mount("/static", ...)`** — alles unter `/static/...` wird als File aus dem Disk-Ordner ausgeliefert (CSS, später Bilder). Kein Code.
- **`app.include_router(browse.router)`** — wir splitten die Routes in 4 Files. Jede definiert einen `APIRouter()`. App registriert sie alle. Sauberer als alles in eine 500-Zeilen-Datei zu packen.
- **`docs_url=None, redoc_url=None`** — FastAPI generiert standardmässig `/docs` (Swagger UI). Brauchen wir nicht, abgeschaltet.

### 3.2 · Eine Route ([dashboard/routes/browse.py:browse](dashboard/routes/browse.py))

Vereinfacht:

```python
@router.get("/", response_class=HTMLResponse)
def browse(
    request: Request,
    q: Optional[str] = None,                            # ?q=python
    sources: list[str] = Query(default=[]),             # ?sources=linkedin&sources=indeed
    min_score: Optional[str] = None,                    # ?min_score=0.5
    sort: str = "score",
    direction: str = "desc",
    page: int = 1,
):
    min_score_f = _opt_float(min_score)        # "0.5" → 0.5,  "" → None

    jobs = db.query_jobs(
        search=q, sources=sources or None, min_score=min_score_f,
        sort=sort, direction=direction,
        limit=50, offset=(page - 1) * 50,
    )
    total = db.count_jobs(...)

    template = "partials/browse_table.html" if request.headers.get("HX-Request") else "browse.html"
    return templates.TemplateResponse(request, template, {"jobs": jobs, "total": total, ...})
```

Drei Konzepte hier:

**(a) Funktion-Signatur = automatisches URL-Parsing.**
FastAPI schaut sich die Type-Hints an (`Optional[str]`, `list[str]`, `int`) und parsed Query-Params automatisch in die richtigen Typen. Du schreibst keinen einzigen `request.GET.get()`-Aufruf wie in Flask oder Django.

**(b) Den HTMX-Trick erkennt der Server am Header.**
Wenn HTMX einen Request schickt, fügt es automatisch `HX-Request: true` als HTTP-Header hinzu. Wir prüfen das:

```python
template = "partials/browse_table.html" if request.headers.get("HX-Request") else "browse.html"
```

→ HTMX-Request → liefere nur den Tabellen-Partial (~50 KB).
→ Direkter Browser-Aufruf (Tab geöffnet, Reload, etc.) → liefere die ganze Seite mit Layout (~70 KB).

Das ist der **Schlüssel** zum HTMX-Architekturmodell: dieselbe URL liefert je nach Aufrufer entweder Vollseite oder Fragment.

**(c) Empty-String-Trap (das war unser Bug 3).**
Wenn ein Number-Input leer bleibt, sendet der Browser `?min_score=` (Wert = leerer String). Würde ich `min_score: Optional[float] = None` deklarieren, würde Pydantic versuchen `""` → `float` zu konvertieren, fail mit 422 Unprocessable Entity. Lösung: nimm es als `Optional[str]` und parse selber:

```python
def _opt_float(value: Optional[str]) -> Optional[float]:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None
```

Robustes Pattern für jedes Form-Feld das optional + numerisch ist.

### 3.3 · Eine Datenbank-Query ([src/database.py:query_jobs](src/database.py))

Die Query-Funktion ist ein **Query-Builder**. Statt 16 verschiedene SQL-Strings hardzucoden für jede Filter-Kombination, bauen wir das WHERE dynamisch:

```python
def query_jobs(self, *, search=None, sources=None, statuses=None,
               location=None, min_score=None, ..., sort="score", direction="desc",
               limit=200, offset=0):
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

    if min_score is not None:
        where.append("relevance_score >= ?")
        params.append(min_score)

    # ... weitere Filter

    sql = f"SELECT * FROM jobs WHERE {' AND '.join(where)} ORDER BY {order_by} LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [dict(row) for row in self.conn.execute(sql, params).fetchall()]
```

Drei Dinge zu beachten:

1. **`where = ["1=1"]`** — Trick: erspart die Sonderbehandlung für "kein Filter aktiv". `WHERE 1=1` ist immer wahr, also wirkt nur was nach `AND` kommt. Saubere Fallunterscheidungs-freie Schleife.

2. **Parameter-Binding mit `?`** — niemals SQL-Strings mit `f"... WHERE title = '{search}'"` bauen. Das wäre **SQL-Injection**: ein Job-Titel `'; DROP TABLE jobs; --` würde deine DB löschen. SQLite macht mit `?`-Platzhaltern alles sicher.

3. **`order_by` kommt nicht aus Userinput direkt rein** — du kannst nicht parameter-binden auf Spaltennamen, deshalb whitelist:
   ```python
   sort_columns = {
       "score": "relevance_score",
       "company": "company COLLATE NOCASE",
       ...
   }
   column = sort_columns.get(sort, "relevance_score")  # default = score
   ```
   Wenn jemand `?sort=foo` schickt, fällt es auf den Default zurück. Sicherheits-Boundary.

### 3.4 · Ein Template ([dashboard/templates/partials/job_row.html](dashboard/templates/partials/job_row.html))

```html
<tr class="job-row" id="row-{{ job.id }}" data-id="{{ job.id }}">
  <td><input type="checkbox" class="bulk-check" value="{{ job.id }}"></td>
  <td>
    <span class="score-pill {{ 'high' if (job.relevance_score or 0) >= 0.6 else 'mid' if (job.relevance_score or 0) >= 0.4 else 'low' }}">
      {{ '%.2f' % (job.relevance_score or 0) }}
    </span>
  </td>
  <td class="title-cell">{{ job.title }}</td>
  ...
  <td>
    <button class="btn"
            hx-post="/job/{{ job.id }}/status"
            hx-vals='{"status": "bookmarked"}'
            hx-target="#row-{{ job.id }}"
            hx-swap="outerHTML">★</button>
  </td>
</tr>
```

**Jinja2-Syntax**:
- `{{ ... }}` → Wert ausgeben
- `{% if ... %} ... {% else %} ... {% endif %}` → Logik
- `{% for x in xs %} ... {% endfor %}` → Schleife
- `{{ '%.2f' % x }}` → Python-Format-String (auf 2 Dezimalstellen formatieren)

**HTMX-Attribute auf dem Button**:
- `hx-post="/job/{{ job.id }}/status"` → POST an diese URL
- `hx-vals='{"status": "bookmarked"}'` → Body-Daten
- `hx-target="#row-{{ job.id }}"` → welches Element wird ersetzt (die ganze Tabellenzeile mit `id="row-42"`)
- `hx-swap="outerHTML"` → das ganze Element wird ersetzt (vs. `innerHTML` = nur der Inhalt)

**Wenn du klickst:**
1. HTMX schickt `POST /job/42/status` mit `status=bookmarked`
2. Server (in `routes/actions.py`) macht `db.update_status(42, "bookmarked")`, fetched den Job neu, rendert `job_row.html` mit dem aktualisierten Job
3. Server liefert das gerenderte `<tr>...</tr>` Element
4. HTMX swappt es gegen das alte `<tr id="row-42">`

Resultat: der Status-Pill ändert sich von "New" zu "Bookmarked" ohne Page-Reload. Dauer: ~50 ms.

---

## 4 · Die drei Bugs — was sie über Web-Dev lehren

### Bug 1: SQLite + Threading

**Symptom:** Erste Test-Requests warfen `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread.`

**Ursache:** Mein DB-Connection-Objekt wurde im Main-Thread erstellt (beim Module-Load). FastAPI führt aber sync Routes (`def browse(...)` ohne `async`) im Threadpool aus. Andere Threads → SQLite-Schutzmechanismus.

**Fix:**
```python
self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
```
SQLite kann mit Multi-Thread-Access umgehen, wenn du im **WAL-Modus** läufst (haben wir mit `PRAGMA journal_mode=WAL`). `check_same_thread=False` schaltet die Schutzcheck ab.

**Lehre:** Web-Frameworks haben einen Thread-Pool. Globale Connections musst du thread-safe machen oder per-Request neu öffnen. Bei SQLite ist `check_same_thread=False` + WAL die Standard-Antwort. Bei Postgres/MySQL nutzt man Connection-Pools.

### Bug 2: Starlette TemplateResponse-Signatur

**Symptom:** `TypeError: unhashable type: 'dict'` deep in the Jinja2 cache lookup.

**Ursache:** Starlette ≥ 0.30 hat die TemplateResponse-Signatur geändert:
```python
# alt (vor 0.30)
return templates.TemplateResponse("page.html", {"request": request, "x": 1})

# neu (ab 0.30)
return templates.TemplateResponse(request, "page.html", {"x": 1})
```

Mein Code war im alten Stil. Starlette interpretierte das Dict `{"request": ..., "x": 1}` als das `request`-Argument, dann den String `"page.html"` als Context-Dict, dann scheiterte irgendwo der Cache-Lookup.

**Lehre:** Library-Versionen sind ein echtes Problem. Wenn der Stack-Trace tief in Library-Code mit kryptischer Fehlermeldung endet, oft ist es eine API-Änderung. Das Pinnen wichtiger Versionen in `pyproject.toml` ist nicht overcautious.

### Bug 3: Empty-String → 422

**Symptom:** Filter-Form kehrte aus dem Browser komplett zurück mit 422 Unprocessable Entity. War im Server-Log sofort sichtbar — aber meine vorherigen Smoke-Tests haben es nicht gefangen weil ich mit gefüllten Werten getestet habe.

**Ursache:** Beschrieben oben in 3.2. Empty Number-Input → Browser sendet `min_score=` → FastAPI versucht `"" → float` → ValidationError.

**Lehre:** **Teste exakt die Pfade, die das echte UI durchläuft.** Mein Smoke-Test war `curl ?min_score=0.5` — gefüllter Wert. Der echte Filter-Form mit allen Feldern leer war ein anderer Pfad. Der Server-Log war der goldene Hinweis: 422 heisst immer "Pydantic-Validierung gefailt", schau in die Request-Daten.

**Generelle Lehre:** der Server-Log ist beim Debugging dein bester Freund. Bevor du irgendwas am Code änderst, **lies den Log**.

---

## 5 · Wie du das jetzt erweiterst

### Beispiel: einen "Salary"-Filter hinzufügen

1. **DB-Methode erweitern** in `src/database.py:query_jobs`:
   ```python
   if salary_min is not None:
       where.append("salary_min >= ?")
       params.append(salary_min)
   ```
   Gleiches in `count_jobs`.

2. **Route-Handler erweitern** in `dashboard/routes/browse.py`:
   ```python
   def browse(..., salary_min: Optional[str] = None):
       salary_min_i = _opt_int(salary_min)
       jobs = db.query_jobs(..., salary_min=salary_min_i)
       ctx["salary_min"] = salary_min_i
   ```

3. **Template erweitern** in `dashboard/templates/browse.html`:
   ```html
   <h3>Salary</h3>
   <input type="number" name="salary_min" placeholder="min CHF/year"
          value="{{ salary_min if salary_min is not none else '' }}">
   ```

Restart uvicorn, fertig. Drei Files, ~10 Zeilen.

### Beispiel: einen neuen Tab hinzufügen ("Companies"-Übersicht)

1. **Neue Route-Datei** `dashboard/routes/companies.py` (existiert nicht — das ist die Übung):
   ```python
   from fastapi import APIRouter, Request
   from dashboard.deps import db, templates

   router = APIRouter()

   @router.get("/companies")
   def companies(request: Request):
       rows = db.conn.execute("""
           SELECT company, COUNT(*) as cnt, AVG(relevance_score) as avg_score
           FROM jobs GROUP BY company ORDER BY avg_score DESC
       """).fetchall()
       return templates.TemplateResponse(
           request, "companies.html",
           {"active_tab": "companies", "companies": [dict(r) for r in rows]}
       )
   ```

2. **In `dashboard/app.py` den Router registrieren:**
   ```python
   from dashboard.routes import actions, browse, companies, pipeline, stats
   app.include_router(companies.router)
   ```

3. **Template `dashboard/templates/companies.html` schreiben** (extends `base.html`).

4. **Tab-Link in `base.html` ergänzen:**
   ```html
   <a href="/companies" class="tab {% if active_tab == 'companies' %}active{% endif %}">Companies</a>
   ```

Fertig. Das ist der Mehrwert der Trennung in `routes/` Files: jeder Tab ist isoliert, du kannst neue dazubauen ohne irgendwas Bestehendes anzufassen.

---

## 6 · Was du mitnehmen solltest

1. **Web-Frameworks sind dünn.** FastAPI selber ist unter 5000 Zeilen Code. Der ganze Stack ist verständlich, kein Mysterium.

2. **HTMX schlägt React für 95% der internen Tools.** Du brauchst kein npm, keinen Build-Step, kein TypeScript-Projekt. Server-rendered HTML + ein paar Attribute reicht.

3. **Type-Hints sind nicht Deko.** FastAPI nutzt sie für Routing, Validierung, Dokumentation. Pydantic macht aus deinem Type-Hint einen Runtime-Validator. Das ist der Grund warum FastAPI so wenig Boilerplate hat.

4. **Trennung zwischen Lese-Pfaden (GET, idempotent) und Schreib-Pfaden (POST/PUT/DELETE)** ist HTTP-Standard und macht Architektur natürlich. `routes/browse.py` macht nur Lesen, `routes/actions.py` macht nur Schreiben.

5. **Bugs sind oft an Schicht-Grenzen.** Threading (Python ↔ uvicorn), Library-Versionen (FastAPI ↔ Starlette), Form-Encoding (Browser ↔ Server). Nicht in der Geschäftslogik. Wenn was nicht geht: schau wo zwei Systeme aneinanderstossen.

---

## 7 · Update 2026-04-30 — Drag-Drop, DB-Explorer, Stale-Tracking

Drei Features dazu nach dem ersten Test:

### 7.1 — SortableJS Drag-and-Drop im Pipeline-Tab

Pure HTMX-Click-Buttons hatten ein subtiles Problem (HTMX `hx-on::after-request` triggerte nicht immer). Statt zu debuggen → SortableJS via CDN dazugenommen, Click-Buttons zusätzlich auf reines `fetch()` umgestellt.

**Wie es jetzt läuft:**
```javascript
// Auf jeder Kanban-Spalte initialisiert:
Sortable.create(el, {
  group: 'pipeline',          // Karten dürfen zwischen allen Spalten wandern
  onAdd: async (evt) => {
    const newStatus = evt.to.dataset.status;       // "applied", "interview", ...
    const jobId = evt.item.dataset.id;
    await fetch(`/job/${jobId}/status`,
                {method: 'POST', body: new FormData(...)});
    htmx.ajax('GET', '/pipeline', {target:'#kanban-wrap', swap:'innerHTML'});
  }
});
```

**Lehre:** wenn HTMX an einer Stelle nicht rund läuft, ist die Mischung von HTMX (für die meisten Routes) und nativem `fetch()` (für drag-drop, das eh JS-getrieben ist) absolut OK. Kein Dogma. Das Schöne an HTMX: es ist additiv, du kannst es teilweise nutzen.

### 7.2 — DB-Explorer-Tab

Ein neuer Tab "DB" im Dashboard für **transparente** SQLite-Inspektion. Zeigt:
- **Schema**: alle Spalten + Typen + NOT NULL + PK pro Tabelle
- **SQL Playground**: read-only Query-Box mit Cmd/Ctrl+Enter zum Ausführen, Cheatsheet-Snippets links
- **Audit-Log**: jede Status-Änderung, jede Notiz-Speicherung, jede Bulk-Operation mit dem **exakten SQL-Statement** das ausgeführt wurde

**Sicherheit:** der Query-Runner whitelistet nur lesende Statements:
```python
def execute_readonly_sql(self, sql, max_rows=200):
    first = sql.strip().lower()
    allowed = (first.startswith("select ") or first.startswith("with ")
               or first.startswith("explain ") or first.startswith("pragma table_info")
               ...)
    if not allowed:
        raise ValueError("Only SELECT / WITH / EXPLAIN / PRAGMA allowed.")
    if ";" in sql.rstrip(";"):
        raise ValueError("Multiple statements not allowed.")  # blocks chained writes
    self.conn.execute("SAVEPOINT readonly_query")
    try:
        cursor = self.conn.execute(sql)
        rows = cursor.fetchmany(max_rows)
        ...
    finally:
        self.conn.execute("ROLLBACK TO SAVEPOINT readonly_query")
```

Drei Verteidigungs-Schichten:
1. **String-Whitelist** — nur Lese-Statements erlaubt
2. **Semicolon-Block** — kein Chained-Statement (`SELECT 1; DROP TABLE jobs;` wird abgelehnt)
3. **SAVEPOINT + ROLLBACK** — selbst wenn was die ersten zwei umgehen würde, wird die Transaktion zurückgerollt → DB-Zustand nie verändert

**Audit-Log = neue Tabelle `audit_log`:**
```sql
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    operation TEXT NOT NULL,           -- "UPDATE_STATUS", "UPDATE_NOTES", ...
    job_id INTEGER,
    sql_statement TEXT,                -- das echte SQL mit ?-Platzhaltern
    parameters TEXT,                   -- JSON der gebundenen Werte
    description TEXT
);
```

Jede Status-Schreib-Methode in `JobDatabase` ruft jetzt `self._audit(...)` direkt nach dem `execute()` auf:
```python
def update_status(self, job_id: int, status: str) -> None:
    sql = "UPDATE jobs SET application_status = ?, date_updated = ? WHERE id = ?"
    params = (status, datetime.now().isoformat(), job_id)
    self.conn.execute(sql, params)
    self._audit("UPDATE_STATUS", job_id=job_id, sql=sql, params=params,
                description=f"Status → {status}")
    self.conn.commit()
```

**Was du im UI siehst** (DB-Tab → "Recent operations"):

| When | Operation | Job | SQL | Parameters |
|------|-----------|-----|-----|------------|
| 2026-04-30 00:23:13 | UPDATE_STATUS | #1243 | `UPDATE jobs SET application_status = ?, date_updated = ? WHERE id = ?` | `["bookmarked", "2026-04-30T00:23:13", 1243]` |

→ Das ist **buchstäblich** das SQL das gegen SQLite läuft. Kein Mysterium, keine Magic. Du kannst die Statements kopieren und in der CLI-Shell mit `sqlite3 data/jobs.db` selbst ausführen.

### 7.3 — Stale-Tracking

**Schema-Änderung:**
```sql
ALTER TABLE jobs ADD COLUMN last_seen_at TEXT;
CREATE INDEX idx_jobs_last_seen ON jobs(last_seen_at);
```

**Migration backfilled** alle bestehenden Zeilen: `UPDATE jobs SET last_seen_at = date_scraped WHERE last_seen_at IS NULL`. Damit war beim ersten Start nichts plötzlich stale.

**`upsert_job()` aktualisiert die Spalte** bei jedem Scrape-Treffer:
```python
self.conn.execute(
    "UPDATE jobs SET ..., last_seen_at=? WHERE id=?",
    (..., now, existing_id)
)
```

→ Job wird neu gefunden = `last_seen_at` springt auf jetzt.
→ Job wird beim Scrape NICHT mehr gefunden = `last_seen_at` bleibt alt.

**Filter in `query_jobs()`:**
```python
if hide_stale and not statuses:
    where.append(
        "(application_status != 'new' "
        " OR last_seen_at IS NULL "
        " OR last_seen_at >= date('now', '-14 days'))"
    )
```

Lese-Logik: ein Job wird **versteckt** wenn ALLE drei zutreffen:
- `status = 'new'` (du hast noch nicht engagiert) UND
- `last_seen_at IS NOT NULL` (wir wissen wann er das letzte Mal gesehen wurde) UND
- letztes Sehen vor mehr als 14 Tagen

Andersherum: jeder Job mit Status ≠ `new` (also Bookmarked/Applied/Interview/Offer/Rejected) bleibt immer sichtbar — auch wenn das Original-Posting offline geht. **Engagement-Schutz.**

**SQL-Date-Funktionen sind nice:** `date('now', '-14 days')` ist eingebauter SQLite-Trick, kein Python-Datum-Berechnen nötig.

---

### 7.4 — Was du jetzt im DB-Tab ausprobieren kannst

```sql
-- Wie viele Jobs sind potenziell stale?
SELECT COUNT(*) FROM jobs
WHERE application_status = 'new'
  AND last_seen_at < date('now', '-14 days');

-- Alters-Verteilung deiner DB
SELECT
  CASE
    WHEN last_seen_at >= date('now', '-7 days')  THEN '<7d'
    WHEN last_seen_at >= date('now', '-14 days') THEN '7–14d'
    WHEN last_seen_at >= date('now', '-30 days') THEN '14–30d'
    ELSE '>30d'
  END AS age_bucket,
  COUNT(*) AS n
FROM jobs
GROUP BY age_bucket;

-- Welche Sources liefern die besten Scores?
SELECT source, ROUND(AVG(relevance_score), 3) AS avg_score, COUNT(*) AS n
FROM jobs
GROUP BY source
ORDER BY avg_score DESC;

-- Deine ganze Bewerbungs-Historie
SELECT * FROM audit_log
ORDER BY timestamp DESC LIMIT 50;
```

Klick die Snippets im Cheatsheet links auf der DB-Seite an — sie laden in die Query-Box.

---

## 9 · Weiterlesen

Wenn dich das Thema gepackt hat:

- **HTMX Essays** — https://htmx.org/essays/ — der Erfinder Carson Gross schreibt sehr meinungsstarke Posts über Web-Architektur. Kurz und unterhaltsam.
- **FastAPI Docs** — https://fastapi.tiangolo.com/ — die offiziellen Tutorials sind exzellent.
- **"How Browsers Work"** (Tali Garsiel, 2011) — https://web.dev/articles/howbrowserswork — Klassiker für das mentale Modell auf der Browser-Seite.
- **SQLite Documentation** — https://www.sqlite.org/lang.html — SQL ist eine Welt für sich. Die offizielle Doku ist (überraschend) sehr lesbar.

---

*Begleitmaterial zum Dashboard. Die Beispiele zeigen das Muster, nicht den
tagesaktuellen Code — im Zweifel gilt die Quelle.*
