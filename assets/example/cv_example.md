# Alex Muster — CV

**Address:** Beispielstrasse 1, 8000 Musterstadt, Switzerland
**Email:** alex.muster@example.com
**Phone:** +41 00 000 00 00
**Last updated:** 2026-01-15

> Fictional sample CV. It exists so the scoring, briefing and letter-review
> code paths can run without real personal data. The structure matters more
> than the content: `letter_quality.parse_cv_stations()` reads the `##`
> section headings and the `### Organisation — Location` entries, and takes
> each station's date span from the `· <start> – <end>` marker on the line
> below. Keep that shape when you swap in your own CV.

---

## Profile Summary

Junior data and AI engineer with roughly two years of full-time experience
building applied LLM systems in regulated domains. Comfortable across the
stack: data pipelines, retrieval, evaluation harnesses, and the small web
frontends that make a model's output reviewable by a human.

**Available from:** immediately
**Preferred location:** Musterstadt (primary), Remote (secondary)
**Preferred workload:** 80–100%
**Salary expectation:** — (fill in)
**Visa:** Swiss citizen — no work permit needed in Switzerland or EU

---

## Education

### Musterstadt University of Applied Sciences (MUAS) — Musterstadt, Switzerland
**M.Sc. in Applied Data Science** · Sep 2024 – ongoing
- Coursework: Applied Machine Learning, Data Engineering, MLOps
- Part-time alongside employment

### Example Institute of Technology — Beispielstadt, Germany
**B.Sc. in Computer Science** · Sep 2019 – Sep 2022
- Thesis on retrieval quality metrics for document search
- Exchange semester at Sample University

---

## Professional Experience

### Muster Analytics AG — Musterstadt, Switzerland *(data consultancy)*
**Data Engineer** · Nov 2024 – present
- Build and operate ingestion pipelines for client document archives
- Own the evaluation harness for a retrieval service: labelled set, weekly
  regression run, precision and recall reported per document class
- Stack: Python, SQL, Postgres, Airflow, LLM APIs

### Beispiel Energie GmbH — Beispielstadt, Germany *(utility)*
**Working Student, Data Team** · Aug 2022 – Sep 2024
- Built a classifier that routed incoming service mails to the right queue
- Wrote the deterministic validation layer that gated the model's output
  before it reached the ticketing system

### Sample Software SL — Ejemplo, Spain *(B2B SaaS)*
**Intern, Backend** · Jul 2021 – Dec 2021
- Instrumented the API with structured logging and request tracing

---

## Projects

### Job Scoring Engine — personal project · 2026 – present
- Deterministic and LLM-based scoring pipeline for job postings, with an
  evaluation harness and a review UI that keeps a human in the loop
- Stack: Python, FastAPI, SQLite, HTMX, Anthropic API (tool use, prompt
  caching, batching)

---

## Languages

| Language | Level |
|----------|-------|
| German | Native |
| English | Fluent |
| Spanish | Intermediate (B1–B2) |
| French | Basic (A1–A2) |

---

## Technical Skills

**Data & AI:** Python, SQL, LLM APIs, retrieval and embeddings, evaluation harnesses
**Engineering:** Git, FastAPI, Docker, Postgres, SQLite, pytest
**Business Systems:** MS Office

---

*This file is the source of truth for the letter-review and briefing code
paths. The tooling reads markdown far more reliably than text parsed out of a
PDF, so keep the markdown version current.*
