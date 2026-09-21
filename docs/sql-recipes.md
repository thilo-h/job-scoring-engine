# SQL recipes

> Queries für den **DB-Tab** im Dashboard (read-only Playground, Cmd/Ctrl+Enter
> führt aus) oder für `sqlite3 data/jobs_<profil>.db`. Der Playground erlaubt
> nur lesende Statements — die Whitelist steht in `dashboard/routes/db_explorer.py`.

---

## 🆕 Neueste Einträge

```sql
-- Die 20 neuesten Jobs (zuletzt erstmals gescraped) — Quick-Check
SELECT id, ROUND(relevance_score, 2) AS score, title, company, location, source,
       substr(date_scraped, 1, 16) AS scraped
FROM jobs
ORDER BY date_scraped DESC
LIMIT 20;

-- Letzter Job nach Insertion (Auto-Increment ID)
SELECT * FROM jobs ORDER BY id DESC LIMIT 1;

-- Letzter Job nach Scrape-Zeit (was du meistens willst)
SELECT * FROM jobs ORDER BY date_scraped DESC LIMIT 1;

-- Top 10 zuletzt gesehen (Updates aus Re-Scrapes inklusive)
SELECT id, title, company, last_seen_at
FROM jobs
ORDER BY last_seen_at DESC
LIMIT 10;

-- Heute gescrapte Jobs
SELECT COUNT(*) FROM jobs
WHERE substr(date_scraped, 1, 10) = date('now');
```

---

## 📝 Briefe und Prüfbefunde

```sql
-- Entwürfe mit offenen Prüfhinweisen: wo lohnt sich ein Durchgang?
SELECT id, title, company,
       json_array_length(cover_letter_checks) AS findings,
       cover_letter_lang AS lang
FROM jobs
WHERE cover_letter IS NOT NULL
  AND cover_letter_checks IS NOT NULL
  AND json_array_length(cover_letter_checks) > 0
ORDER BY findings DESC;

-- Welche Regeln greifen am häufigsten? Zeigt, wo der Stil-Leitfaden
-- unklar ist oder eine Gewohnheit gegen ihn arbeitet.
SELECT json_extract(f.value, '$.code') AS rule,
       COUNT(*) AS hits
FROM jobs j, json_each(j.cover_letter_checks) f
WHERE j.cover_letter_checks IS NOT NULL
GROUP BY rule
ORDER BY hits DESC;
```

---

## 📤 Bewerbungs-Pipeline

```sql
-- Jobs nach Status
SELECT application_status, COUNT(*) AS n FROM jobs
GROUP BY application_status
ORDER BY n DESC;

-- Vorgemerkt, aber noch nicht weiterverfolgt
SELECT id, ROUND(relevance_score, 2) AS score, title, company, location
FROM jobs
WHERE application_status = 'bookmarked'
ORDER BY relevance_score DESC;

-- Liegezeit: wie lange vom Fund bis zum Weiterverfolgen? Ein hoher Wert
-- heisst meist, dass der Score etwas hoch bewertet, was in der Praxis liegen
-- bleibt — ein Signal fürs Retuning der Gewichte.
SELECT id, title, company,
       ROUND((julianday(applied_at) - julianday(date_scraped))) AS days_idle
FROM jobs
WHERE applied_at IS NOT NULL
ORDER BY days_idle DESC;
```

---

## 📊 Scoring & Targeting

```sql
-- Top-20 noch ungesehene (status='new') Jobs nach Score
SELECT id, ROUND(relevance_score, 2) AS score, title, company, location, source
FROM jobs
WHERE application_status = 'new'
  AND (last_seen_at IS NULL OR last_seen_at >= date('now', '-14 days'))
ORDER BY relevance_score DESC
LIMIT 20;

-- Score-Verteilung in Buckets von 0.1
SELECT
  CAST(relevance_score * 10 AS INTEGER) / 10.0 AS bucket,
  COUNT(*) AS n
FROM jobs
WHERE relevance_score IS NOT NULL
GROUP BY bucket
ORDER BY bucket DESC;

-- Welche Sources liefern die besten Jobs?
SELECT source,
       ROUND(AVG(relevance_score), 3) AS avg_score,
       COUNT(*) AS total,
       SUM(CASE WHEN relevance_score >= 0.55 THEN 1 ELSE 0 END) AS strong_matches
FROM jobs
GROUP BY source
ORDER BY avg_score DESC;
```

---

## 🗓️ Stale-Tracking

```sql
-- Wie viele Jobs sind potentiell stale (>14d ungesehen, status=new)?
SELECT COUNT(*) FROM jobs
WHERE application_status = 'new'
  AND last_seen_at < date('now', '-14 days');

-- Alters-Verteilung
SELECT
  CASE
    WHEN last_seen_at >= date('now', '-7 days')  THEN '< 7 days'
    WHEN last_seen_at >= date('now', '-14 days') THEN '7–14 days'
    WHEN last_seen_at >= date('now', '-30 days') THEN '14–30 days'
    ELSE '> 30 days'
  END AS age_bucket,
  COUNT(*) AS n
FROM jobs
GROUP BY age_bucket
ORDER BY MIN(last_seen_at) DESC;
```

---

## 🔍 Workload, Location, Remote

```sql
-- Jobs mit erkanntem Workload <= 80% (NULL ausgeschlossen, also "garantiert OK")
SELECT id, ROUND(relevance_score, 2) AS score, workload_percent, title, company
FROM jobs
WHERE workload_percent <= 80
  AND application_status = 'new'
ORDER BY relevance_score DESC LIMIT 30;

-- Workload-Verteilung
SELECT workload_percent, COUNT(*) FROM jobs
GROUP BY workload_percent
ORDER BY workload_percent IS NULL, workload_percent;

-- Remote-Jobs in BCN/Madrid
SELECT id, title, company, location, is_remote
FROM jobs
WHERE (is_remote = 1 OR location LIKE '%Barcelona%' OR location LIKE '%Madrid%')
  AND application_status = 'new'
  AND relevance_score >= 0.5
ORDER BY relevance_score DESC LIMIT 20;

-- Top 10 Locations
SELECT location, COUNT(*) AS n FROM jobs
WHERE location IS NOT NULL AND location != ''
GROUP BY location
ORDER BY n DESC LIMIT 10;
```

---

## 🔬 Audit-Log

```sql
-- Letzte 50 Operationen
SELECT timestamp, operation, job_id, description
FROM audit_log
ORDER BY timestamp DESC LIMIT 50;

-- Was hast du heute gemacht?
SELECT operation, COUNT(*) AS n
FROM audit_log
WHERE substr(timestamp, 1, 10) = date('now')
GROUP BY operation
ORDER BY n DESC;

-- Status-Wechsel-Historie für einen bestimmten Job
SELECT timestamp, operation, description
FROM audit_log
WHERE job_id = 1243
ORDER BY timestamp DESC;
```

---

## 🛠️ Schema-Inspektion

```sql
-- Alle Spalten der jobs-Tabelle
PRAGMA table_info(jobs);

-- Indexes
PRAGMA index_list(jobs);

-- DB-Grösse + Row-Counts
SELECT name, (SELECT COUNT(*) FROM jobs) FROM sqlite_master WHERE type='table';
```

---

## 💡 Tricks & Notes

- **Datums-Arithmetik**: `date('now', '-14 days')`, `date('now', '+1 month')`, `julianday(x)` für Differenz in Tagen.
- **Substring**: `substr(date_scraped, 1, 10)` schneidet ISO-Date auf `YYYY-MM-DD`.
- **NULL-Handling**: `IS NULL` statt `= NULL`. `COALESCE(x, default)` für Fallback.
- **LIKE**: case-sensitive! Für unsensitiv: `LOWER(title) LIKE '%product%'`.
- **CTE für Lesbarkeit**:
  ```sql
  WITH active AS (
    SELECT * FROM jobs WHERE application_status = 'new' AND relevance_score >= 0.55
  )
  SELECT source, COUNT(*) FROM active GROUP BY source;
  ```
- **JSON-Felder** (Spalte `languages`): `json_extract(languages, '$[0]')` für erstes Element, `json_array_length(languages)` für Anzahl.

**Sicherheits-Hinweis (DB-Tab):** der Playground im Dashboard erlaubt nur
`SELECT`, `WITH`, `EXPLAIN`, `PRAGMA table_info/index_list/foreign_key_list`.
Schreib-Statements (`UPDATE`/`DELETE`/`INSERT`) sind blockiert. Wenn du wirklich
schreiben musst: `sqlite3 data/jobs_<profil>.db` in der Shell.
